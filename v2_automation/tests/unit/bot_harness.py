"""Harness for scenario-testing the user bot as a person would use it.

REAL router, REAL SQLite, REAL parsers.  The source website is replaced by `World`: search pages are the REAL captured pages of
the site (source_audit/output/raw/search_*.html) when they exist, otherwise generated with the same markup; anime pages are
generated with the real markup.  Telegram is a recording outbox.

Every `say()` / `tap()` runs the GENERIC INVARIANTS below — a scenario cannot forget them:
  I1  every input gets at least one reply
  I2  callback_data <= 64 bytes and of a known shape
  I3  no title repeated inside one message ("X — X (VF)")
  I4  a result list only proposes titles that contain every word of the query (unless it says "proches")
  I5  a bare number / "ep N" / noise never triggers a source search
  I6  at most ONE active request per user, and the persisted conversation is valid JSON
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from v2_automation import db
from v2_automation.catalog import SourceCatalog
from v2_automation.membership import MembershipService
from v2_automation.parser import normalize
from v2_automation.requests_mgr import RequestManager
from v2_automation.search import OPTIONAL_WORDS, SourceSearch, slugify_query, tokens
from v2_automation.timeutil import now_utc
from v2_automation.user_bot import Incoming, UserBotRouter, classify_text
from v2support import BASE, Clock, asp_response, cfg, make_page

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "asp"           # REAL answers recorded from the site (read-only)
EPISODES = {"one-piece-kai": 127, "one-piece-log-fish-man-island-saga": 21, "bleach-sennen-kessen-hen": 48,
            "bleach-sennen-kessen-hen-vf": 48, "bleach-kai": 64, "bleach-kai-vf": 64, "bleach": 366, "black-torch": 24,
            "black-torch-vf": 24, "mob-psycho-100": 12, "solo-leveling": 12, "naruto": 220, "naruto-shippuden": 500}
KNOWN_CB = re.compile(r"^(chk|reset|xs|ep|last|all|num|list:\d+|e:\d+|wait:\d+|s:\d+|sp:(main|other):\d+|sv:(main|other)|"
                      r"n:\d+|v:(VF|VOSTFR)|xr:\d+)$")


class World:
    """The fake site: the REAL recorded home page / search answers / anime pages when they exist, generated ones otherwise.
    Every request is logged (`calls` for GET, `posts` for the search engines)."""

    def __init__(self):
        self.calls: list[str] = []
        self.posts: list[dict] = []
        self.synth: dict[tuple[str, str], list[tuple]] = {}
        self.pages: dict[str, str] = {}
        self.use_corpus = True
        self._lock = threading.Lock()

    def add_search(self, query, items, version=None):
        """Synthetic answer for `query`.  Without `version`, each item goes to the engine its title suggests (a "(VF)" title
        or a -vf slug = the VF engine) — the way the site's two engines split their catalogues."""
        for it in items:
            v = version or ("VF" if "(VF)" in it[0] or it[1].rstrip("/").endswith("-vf") else "VOSTFR")
            self.synth.setdefault((v, normalize(query)), []).append(it)

    def searches(self) -> int:
        return len(self.posts)

    def engine_of(self, asid) -> str:
        return {"2": "VF", "3": "VOSTFR"}[str(asid)]

    def post(self, url, data):
        with self._lock:
            self.posts.append(dict(data))
        version = self.engine_of(data["asid"])
        phrase = data["aspp"]
        key = (version, normalize(phrase))
        if key in self.synth:
            return asp_response(self.synth[key])
        f = FIX / f"{version}__{slugify_query(phrase)}.txt"
        if self.use_corpus and f.exists():
            return f.read_text(encoding="utf-8")
        return asp_response([])

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if url.rstrip("/") == BASE.rstrip("/") and (FIX / "home.html").exists():
            return (FIX / "home.html").read_text(encoding="utf-8")
        if url in self.pages:
            return self.pages[url]
        slug = urlparse(url).path.strip("/").split("/")[-1]
        rec = FIX / "pages" / f"{slug}.html"
        if self.use_corpus and rec.exists():
            return rec.read_text(encoding="utf-8")
        lang = "vf" if slug.endswith("-vf") else "vostfr"
        n = EPISODES.get(slug, 12)
        return make_page(abs(hash(slug)) % 900000 + 1000, slug, list(range(1, n + 1)), title=slug.replace("-", " ").title(), lang=lang)


class FakeOutbox:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))
        return len(self.sent)

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))

    def answer(self, callback_id, text=None):
        pass

    def typing(self, chat_id):
        pass


class Members:
    def __init__(self):
        self.member = set()

    def __call__(self, channel, user):
        return "member" if user in self.member else "left"


class Bot:
    def __init__(self, tmp_path: Path, *, required=("@spy_family_2025",), auto_check=True):
        c = sqlite3.connect(str(tmp_path / "bot.sqlite3"), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        db.migrate(c)
        self.conn, self.world, self.out, self.members, self.clock = c, World(), FakeOutbox(), Members(), Clock()
        self.cfg = cfg(required_channels=list(required), user_bot={"required_channels": list(required)})
        self.mgr = RequestManager(c, SourceCatalog(self.cfg, fetch=self.world), now=self.clock)
        self.router = UserBotRouter(c, self.cfg, self.out, self.mgr, SourceSearch(self.cfg, c, fetch=self.world, post=self.world.post),
                                    MembershipService(c, self.members, list(required), now=self.clock),
                                    identify=self._identify, now=self.clock)
        self.auto_check = auto_check
        self.last_query: dict[int, str] = {}

    def _identify(self, url):
        from v2_automation import discovery
        return discovery.identify_anime(self.cfg, url, self.world)

    # -- people ------------------------------------------------------------------------
    def join(self, uid):
        self.members.member.add(uid)
        return self

    def leave(self, uid):
        self.members.member.discard(uid)

    def watch(self, slug, title, post):
        self.conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES (?,?,1,?)",
                          (f"postid:{post}", title, f"{BASE}/anime/{slug}/"))
        self.conn.commit()

    # -- actions ------------------------------------------------------------------------
    def _run(self, uid, inc, kind, value):
        before_msgs, before_search = len(self.out.sent), self.world.searches()
        self.router.handle(inc)
        new = [m for m in self.out.sent[before_msgs:] if m[0] == uid]
        searched = self.world.searches() - before_search
        if self.auto_check:
            self.check(uid, kind, value, new, searched)
        return new

    def say(self, uid, text):
        self.last_query_text = text
        return self._run(uid, Incoming(uid, uid, f"user{uid}", text=text), "text", text)

    def tap(self, uid, data):
        return self._run(uid, Incoming(uid, uid, f"user{uid}", callback=data, callback_id="cb"), "tap", data)

    # -- reading ------------------------------------------------------------------------
    def texts(self, uid):
        return [t for c, t, _ in self.out.sent if c == uid]

    def last(self, uid):
        return [m for m in self.out.sent if m[0] == uid][-1]

    def buttons(self, uid, *, choices_only=True):
        kb = self.last(uid)[2] or []
        return [b for row in kb for b in row if not (choices_only and b.data in ("xs", "reset"))]

    def labels(self, uid):
        return [b.text for b in self.buttons(uid)]

    def request(self, uid):
        r = self.conn.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return dict(r) if r else None

    def step(self, uid):
        r = self.conn.execute("SELECT step FROM conversations WHERE user_id=?", (uid,)).fetchone()
        return r[0] if r else "idle"

    def n(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()[0]

    # -- the invariants -----------------------------------------------------------------
    def check(self, uid, kind, value, new, searched):
        assert new, f"I1 no reply to {kind} {value!r}"
        for _, text, kb in new:
            for row in kb or []:
                for b in row:
                    if b.data is not None:
                        assert len(b.data.encode()) <= 64 and KNOWN_CB.match(b.data), f"I2 bad callback {b.data!r}"
            parts = [normalize(p) for p in re.split(r"\s[—·]\s", text.split("\n")[0]) if len(p.strip()) > 6]
            parts = [re.sub(r"\((vf|vostfr)\)", "", p).strip() for p in parts]
            assert len(parts) == len(set(parts)), f"I3 repeated title in {text!r}"
            if kb and any(b.data and b.data.startswith("s:") for row in kb for b in row):
                q = tokens(self.last_query_text)
                q = [t for t in q if not t.isdigit() and t not in ("saison", "season", "vf", "vostfr", "ep", "episode", "dernier")
                     and t not in OPTIONAL_WORDS] or q
                for row in kb:
                    for b in row:
                        if not (b.data and b.data.startswith("s:")):
                            continue
                        name = re.split(r"\s[🇦-🇿]|\s·", b.text.replace("🎬 ", "").replace("⭐ ", ""))[0]
                        assert not re.search(r"S\d+|\(VF\)|\(VOSTFR\)", name), f"I8 unclean series name {name!r}"
                        if "autre titre" in b.text:
                            continue                                            # found through another title of the anime
                        assert all(w in normalize(name) or any(t.startswith(w) for t in tokens(name)) for w in q),                             f"I4 {name!r} does not contain {q} (query {self.last_query_text!r})"
            assert not re.search(r"(cancelled|completed|failed|expired|queued)", text), f"I9 English state in {text!r}"
        if kind == "text" and classify_text(value)[0] in ("episode", "latest", "noise", "empty", "toolong"):
            assert searched == 0, f"I5 {value!r} triggered {searched} source search(es)"
        assert self.n("SELECT COUNT(*) FROM requests WHERE user_id=? AND state NOT IN "
                      "('COMPLETED','CANCELLED','EXPIRED','FAILED')", uid) <= 1, "I6 two active requests"
        row = self.conn.execute("SELECT data FROM conversations WHERE user_id=?", (uid,)).fetchone()
        if row:
            json.loads(row[0])
