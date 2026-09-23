"""The user bot (@OtakuuVerse_bot): a private conversation that ends with the requested media in the user's chat.

The router below is Telegram-free (it talks to an `Outbox`), so the whole conversation is testable; `TelegramOutbox` and
`run_user_bot` are the thin real-Telegram layer.  Every rule that matters lives in the modules the router calls:
  membership.py (access, checked on every interaction)  ·  parser.py (free text)  ·  search.py (watched list, then source)
  requests_mgr.py (one active request, waiting/expiry, cancel, history)  ·  delivery.py (private delivery)

Conversation state is persisted (`conversations`), so a restart never loses where a user was.  Callback data stays under
Telegram's 64-byte limit: buttons carry an index into the choices stored in the conversation, never a title or a URL.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from . import errors, media
from .membership import Access, MembershipService, join_url
from .parser import ParsedQuery, parse_query
from .requests_mgr import ActiveRequestExists, NewRequest, RequestManager, RequestState as RS, TERMINAL
from .search import SearchHit, SourceSearch, base_title, group
from .timeutil import now_utc

logger = logging.getLogger(__name__)

PAGE_SIZE = 20          # episodes per page in the picker
PAGE_SERIES = 6         # series proposed per page
MAX_SERIES = 40         # ranked series kept in the conversation
WAIT_MARGIN = 3         # an episode this far past the last listed one is "coming"; further away it does not exist yet
MAX_QUERY_CHARS = 100

_EP_ONLY = re.compile(r"^(?:(?:e|ep|eps|episode|épisode)\s*\.?\s*(?:n\s*°?\s*)?)?(\d{1,5})$", re.I)
_LATEST_ONLY = re.compile(r"^(?:le\s+)?(?:dernier|derniere|dernière|last|latest)(?:\s+(?:episode|épisode|ep))?$", re.I)


def classify_text(text: str) -> tuple[str, int | None]:
    """What a free-text message IS, before anything is searched: empty | toolong | noise | episode(n) | latest | search.
    A bare number or "ep 5" is an EPISODE answer, never a title; punctuation / a single letter is noise (no source call)."""
    t = (text or "").strip()
    if not t:
        return "empty", None
    if len(t) > MAX_QUERY_CHARS:
        return "toolong", None
    m = _EP_ONLY.match(t)
    if m:
        return "episode", int(m.group(1))
    if _LATEST_ONLY.match(t):
        return "latest", None
    if sum(ch.isalpha() for ch in t) < 2:
        return "noise", None
    return "search", None


@dataclass
class Button:
    text: str
    data: str | None = None
    url: str | None = None


Keyboard = list[list[Button]]


@dataclass
class Incoming:
    user_id: int
    chat_id: int
    username: str | None = None
    text: str | None = None
    callback: str | None = None
    callback_id: str | None = None
    message_id: int | None = None


class Outbox(Protocol):
    def send(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int | None: ...
    def edit(self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None) -> None: ...
    def answer(self, callback_id: str, text: str | None = None) -> None: ...
    def typing(self, chat_id: int) -> None: ...


class UserBotRouter:
    def __init__(self, conn: sqlite3.Connection, cfg, outbox: Outbox, mgr: RequestManager, search: SourceSearch,
                 membership: MembershipService, *, identify: Callable[[str], dict] | None = None,
                 now: Callable[[], str] = now_utc):
        self.conn, self.cfg, self.out, self.mgr, self.search, self.members = conn, cfg, outbox, mgr, search, membership
        self.now = now
        self._identify = identify

    # ── conversation state ─────────────────────────────────────────────────────────
    def _load(self, uid: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT step, data FROM conversations WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            return {"step": "idle"}
        d = json.loads(row["data"] or "{}")
        d["step"] = row["step"]
        return d

    def _save(self, uid: int, st: dict[str, Any]) -> None:
        step = st.get("step", "idle")
        data = json.dumps({k: v for k, v in st.items() if k != "step"}, ensure_ascii=False)
        self.conn.execute("INSERT INTO conversations (user_id, step, data, updated_at) VALUES (?,?,?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET step=excluded.step, data=excluded.data, "
                          "updated_at=excluded.updated_at", (uid, step, data, self.now()))
        self.conn.commit()

    def _reset(self, uid: int) -> None:
        self._save(uid, {"step": "idle"})

    # ── entry point ────────────────────────────────────────────────────────────────
    def handle(self, inc: Incoming) -> None:
        self.mgr.upsert_user(inc.user_id, inc.username)
        try:
            if inc.callback is not None:
                if inc.callback_id:
                    self.out.answer(inc.callback_id)
                self._on_callback(inc)
            elif inc.text is not None:
                self._on_text(inc)
        except Exception as exc:
            code = errors.classify(exc)
            logger.exception("[USERBOT] user=%s erreur %s", inc.user_id, code)
            self.out.send(inc.chat_id, f"⚠️ Une erreur est survenue ({code}). Réessayez dans un instant.")

    # ── access ─────────────────────────────────────────────────────────────────────
    def _gate(self, inc: Incoming) -> bool:
        access = self.members.check(inc.user_id)
        if access.ok:
            return True
        self._reset(inc.user_id)
        self._deny(inc.chat_id, access)
        return False

    def _deny(self, chat_id: int, a: Access) -> None:
        if a.status == "check_failed":
            self.out.send(chat_id, "⚠️ Je ne peux pas vérifier votre accès pour le moment. Réessayez plus tard.")
            return
        rows: Keyboard = [[Button(f"📢 Rejoindre {c}", url=join_url(c))] for c in a.missing if join_url(c)]
        rows.append([Button("✅ J'ai rejoint", data="chk")])
        self.out.send(chat_id, "🔒 Pour utiliser ce service, rejoignez au moins un de ces canaux, puis appuyez sur "
                               "« J'ai rejoint ».", rows)

    # ── text ───────────────────────────────────────────────────────────────────────
    def _on_text(self, inc: Incoming) -> None:
        text = (inc.text or "").strip()
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else None
        if cmd == "/help":
            return self._help(inc)
        if not self._gate(inc):
            return
        if cmd == "/start":
            return self.out.send(inc.chat_id, "👋 Bienvenue ! Écrivez le nom d'un anime, par exemple « One Piece 1150 » "
                                              "ou « Bleach saison 1 ». /history pour vos demandes, /cancel pour annuler.")
        if cmd == "/history":
            return self._history(inc)
        if cmd in ("/cancel", "/annuler"):
            return self._cancel_cmd(inc)
        if cmd == "/status":
            return self._status(inc)
        if cmd:
            return self._help(inc)
        st = self._load(inc.user_id)
        kind, n = classify_text(text)
        if kind in ("empty", "noise", "toolong"):
            return self.out.send(inc.chat_id, "🤔 Je n'ai pas compris. Écrivez le nom d'un anime, par exemple « one piece 1150 » "
                                              "ou « Bleach saison 1 ».")
        if kind in ("episode", "latest"):
            if st.get("hit"):                                    # the anime is chosen: this is the answer to "Quel épisode ?"
                if kind == "latest":
                    return self._create(inc, st, latest=True)
                return self._create(inc, st, episode=n)
            if st.get("series"):                                  # still choosing series / season / version
                return self.out.send(inc.chat_id, "👆 Choisissez d'abord avec les boutons ci-dessus (ou écrivez un autre "
                                                  "nom pour chercher).", [[Button("✖ Annuler la recherche", data="xs")]])
            return self.out.send(inc.chat_id, "Écrivez d'abord le nom de l'anime, par exemple « one piece 1150 ».")
        note = "🔄 Nouvelle recherche.\n" if st.get("step") not in (None, "idle") else ""
        self._begin_search(inc, parse_query(text), note)

    def _help(self, inc: Incoming) -> None:
        self.out.send(inc.chat_id, "ℹ️ Écrivez un nom d'anime (« one piece 1150 », « Bleach saison 1 », « naruto dernier "
                                   "épisode »).\n/history — mes demandes\n/status — ma demande en cours\n/cancel — annuler")

    # ── search flow ────────────────────────────────────────────────────────────────
    def _begin_search(self, inc: Incoming, q: ParsedQuery, note: str = "") -> None:
        if not q.title:
            return self.out.send(inc.chat_id, "Quel anime cherchez-vous ? Écrivez son nom.")
        active = self.mgr.active_request(inc.user_id)
        if active is not None:
            return self._refuse_second(inc.chat_id, active)
        self.out.typing(inc.chat_id)
        loader_id = self.out.send(inc.chat_id, f"{note}🔎 Recherche en cours…")
        try:
            hits = self.search.search(q.title)
            approx = getattr(self.search, "approximate", False)
            if q.episode is not None and q.full_title == f"{q.title} {q.episode}" and not approx \
                    and any(base_title(h.title) == q.full_title for h in hits):
                # "mob psycho 100": a page of the site is titled exactly like the whole text -> the number belongs to the
                # title, it is not an episode
                q = ParsedQuery(q.full_title, q.full_title, q.season, None, q.latest, q.version, q.whole_season, q.raw)
            elif not hits and q.full_title != q.title:
                hits = self.search.search(q.full_title)
                approx = getattr(self.search, "approximate", False)
                if hits:
                    q = ParsedQuery(q.full_title, q.full_title, q.season, None, q.latest, q.version, q.whole_season, q.raw)
            truncated = getattr(self.search, "truncated", False)
        except Exception as exc:
            code = errors.classify(exc)
            logger.warning("[USERBOT] recherche impossible (%s): %s", code, exc)
            msg = f"⚠️ La recherche du site ne répond pas ({code}). Réessayez dans un instant."
            if loader_id is not None:
                return self.out.edit(inc.chat_id, loader_id, msg)
            return self.out.send(inc.chat_id, msg)
        if not hits:
            msg = (f"{note}😕 Aucun anime trouvé pour « {q.title} », ni en VF ni en VOSTFR.\n"
                   "Le site connaît aussi les titres japonais et anglais : essayez un autre nom.")
            if loader_id is not None:
                return self.out.edit(inc.chat_id, loader_id, msg)
            return self.out.send(inc.chat_id, msg)
        series = group(hits, q.title)[:MAX_SERIES]
        st = {"step": "choose_series", "query": asdict(q), "series": [_ser(s) for s in series], "approx": approx,
              "truncated": truncated}
        self._save(inc.user_id, st)
        head = f"{note}🔎 Résultats pour « {q.title} » (VF + VOSTFR)"
        if approx:
            head += "\nAucun titre ne contient exactement ces mots : ce sont des correspondances par un autre titre."
        if truncated:
            head += "\n⚠️ Le site limite ses réponses : précisez le titre pour affiner."
        self._show_series(inc, st, "main", 0, head, edit_message_id=loader_id)

    def _show_series(self, inc: Incoming, st: dict, view: str, page: int, head: str,
                      edit_message_id: int | None = None) -> None:
        series = st["series"]
        main = [i for i, s in enumerate(series) if s.get("is_main", True)]
        other = [i for i, s in enumerate(series) if not s.get("is_main", True)]
        if view == "main" and not main:
            view = "other"
        pool = main if view == "main" else other
        pages = max(1, (len(pool) - 1) // PAGE_SERIES + 1)
        page = max(0, min(page, pages - 1))
        rows: Keyboard = []
        for i in pool[page * PAGE_SERIES:(page + 1) * PAGE_SERIES]:
            s = series[i]
            flags = "".join(_ICON_OF[v] for v in s.get("versions", []))
            seasons = len(s["seasons"])
            label = ("⭐ " if s.get("watched") else "") + s["name"][:40] + (f" {flags}" if flags else "") \
                + (f" · {seasons} saisons" if seasons > 1 else "") + (" · autre titre" if s.get("alt") else "")
            rows.append([Button(f"🎬 {label}", data=f"s:{i}")])
        nav = []
        if page > 0:
            nav.append(Button("‹ Précédent", data=f"sp:{view}:{page - 1}"))
        if page < pages - 1:
            nav.append(Button("Plus de résultats ›", data=f"sp:{view}:{page + 1}"))
        if nav:
            rows.append(nav)
        if view == "main" and other:
            rows.append([Button(f"🎞 Films & spéciaux ({len(other)})", data="sv:other")])
        elif view == "other" and main:
            rows.append([Button("↩ Séries", data="sv:main")])
        rows.append([Button("✖ Annuler la recherche", data="xs")])
        text = (head + ("\nChoisissez l'anime :" if not head.endswith(":") else "")
                + (f"\n(page {page + 1}/{pages})" if pages > 1 else ""))
        if edit_message_id is not None:
            return self.out.edit(inc.chat_id, edit_message_id, text, rows)
        self.out.send(inc.chat_id, text, rows)

    # ── the steps: series -> season -> version -> episode.  Every step is SHOWN; nothing is chosen for the user. ──
    @staticmethod
    def _crumb(st: dict, *, season: bool = True, version: bool = True) -> str:
        s = st["series"][st["series_idx"]]
        parts = [f"🎬 {s['name']}"]
        if season and st.get("season_idx") is not None:
            o = s["seasons"][st["season_idx"]]
            if len(s["seasons"]) > 1 or o["season"] is not None:
                parts.append(o["label"])
        if version and st.get("version"):
            parts.append(_FLAG[st["version"]])
        return " › ".join(parts)

    def _pick_series(self, inc: Incoming, st: dict, i: int) -> None:
        s = st["series"][i]
        st.update(series_idx=i, season_idx=None, version=None, hit=None)
        q = st["query"]
        seasons = s["seasons"]
        if q.get("season") is not None:
            match = [k for k, o in enumerate(seasons) if o["season"] == q["season"]]
            if match:
                st["prefilled"] = f"Saison {q['season']} retenue d'après votre message."
                return self._pick_season(inc, st, match[0])
            st["step"] = "choose_season"
            self._save(inc.user_id, st)
            have = ", ".join(o["label"] for o in seasons)
            return self._show_seasons(inc, st, f"⚠️ La saison {q['season']} n'existe pas pour « {s['name']} ». Disponible : {have}.")
        st["step"] = "choose_season"
        self._save(inc.user_id, st)
        self._show_seasons(inc, st, f"{self._crumb(st)}\n📅 Quelle saison ?")

    def _show_seasons(self, inc: Incoming, st: dict, head: str) -> None:
        s = st["series"][st["series_idx"]]
        seasons = s["seasons"]
        urls = [next(iter(o["versions"].values()))["url"] for o in seasons[:24]]
        self.out.typing(inc.chat_id)
        if urls:
            if inc.message_id is not None:
                self.out.edit(inc.chat_id, inc.message_id, "⏳ Chargement des saisons…")
                loader_id = inc.message_id
            else:
                loader_id = self.out.send(inc.chat_id, "⏳ Chargement des saisons…")
        else:
            loader_id = inc.message_id
        details = self.mgr.catalog.details_many(urls) if urls else {}
        rows: Keyboard = []
        for k, o in enumerate(seasons[:24]):
            d = details.get(urls[k]) or {}
            flags = "".join(_ICON_OF[v] for v in ("VF", "VOSTFR") if v in o["versions"])
            eps = f" · {d['listed']} ép." if d.get("listed") else ""
            rows.append([Button(f"📅 {o['label']}{eps} {flags}", data=f"n:{k}")])
        rows.append([Button("✖ Annuler la recherche", data="xs")])
        only = "\n(seule saison disponible)" if len(seasons) == 1 else ""
        text = head + only
        if loader_id is not None:
            return self.out.edit(inc.chat_id, loader_id, text, rows)
        self.out.send(inc.chat_id, text, rows)

    def _pick_season(self, inc: Incoming, st: dict, j: int) -> None:
        s = st["series"][st["series_idx"]]
        o = s["seasons"][j]
        st.update(season_idx=j, version=None)
        q = st["query"]
        versions = o["versions"]
        if q.get("version") and q["version"] in versions:
            note = (st.pop("prefilled", "") + " " if st.get("prefilled") else "") + f"Version {q['version']} retenue d'après votre message."
            st["prefilled"] = note.strip()
            return self._pick_version(inc, st, q["version"])
        st["step"] = "choose_version"
        self._save(inc.user_id, st)
        head = f"{self._crumb(st, version=False)}\n🎙️ Quelle version ?"
        if q.get("version"):
            head = f"⚠️ La version {q['version']} n'existe pas pour cette saison.\n" + head
        if st.get("prefilled"):
            head = st.pop("prefilled") + "\n" + head
        only = "\n(seule version disponible)" if len(versions) == 1 else ""
        self.out.send(inc.chat_id, head + only,
                      [[Button(_FLAG[v], data=f"v:{v}") for v in ("VF", "VOSTFR") if v in versions],
                       [Button("✖ Annuler la recherche", data="xs")]])

    def _pick_version(self, inc: Incoming, st: dict, version: str) -> None:
        s = st["series"][st["series_idx"]]
        o = s["seasons"][st["season_idx"]]
        if version not in o["versions"]:
            raise KeyError(version)
        st.update(version=version, hit=o["versions"][version], season=o["season"], season_label=o["label"], title=s["name"])
        q = st["query"]
        st["step"] = "choose_episode"
        self._save(inc.user_id, st)                        # buttons of the next messages need the chosen page
        if q.get("whole_season"):                          # a whole season is many downloads: ask before creating it
            d = self.mgr.catalog.details(st["hit"]["url"])
            n = f" ({d['listed']} épisodes)" if d.get("listed") else ""
            pre = (st.pop("prefilled") + "\n") if st.get("prefilled") else ""
            self._save(inc.user_id, st)
            return self.out.send(inc.chat_id, f"{pre}{self._crumb(st)}\n📦 Vous demandez la saison complète{n}.",
                                 [[Button("✅ Demander la saison complète", data="all")],
                                  [Button("📺 Choisir un épisode", data="ep")], [Button("✖ Annuler la recherche", data="xs")]])
        if q.get("episode") is not None:
            return self._create(inc, st, episode=q["episode"])
        if q.get("latest"):
            return self._create(inc, st, latest=True)
        d = self.mgr.catalog.details(st["hit"]["url"])
        info = (f"\n{d['listed']} épisode(s) disponible(s)" + (f" · {d['status'].title()}" if d.get("status") else "")) if d.get("listed") else ""
        pre = (st.pop("prefilled") + "\n") if st.get("prefilled") else ""
        self._save(inc.user_id, st)
        self.out.send(inc.chat_id, f"{pre}{self._crumb(st)}{info}\nÉcrivez le numéro de l'épisode, ou :",
                      [[Button("📺 Quel épisode ?", data="ep")], [Button("✖ Annuler la recherche", data="xs")]])

    # ── episode picker ─────────────────────────────────────────────────────────────
    def _episode_menu(self, inc: Incoming, st: dict) -> None:
        self.out.send(inc.chat_id, "📺 Quel épisode ?", [
            [Button("🔥 Dernier épisode", data="last")],
            [Button("🔢 Saisir un numéro", data="num")],
            [Button("📚 Choisir dans la liste", data="list:0")],
            [Button("📦 Saison complète", data="all")],
            [Button("✖ Annuler la recherche", data="xs")]])

    def _episode_list(self, inc: Incoming, st: dict, page: int) -> None:
        try:
            eps = [e.number for e in self.mgr.catalog.episodes(st["hit"]["url"]) if e.number is not None]
        except Exception as exc:
            return self.out.send(inc.chat_id, f"⚠️ Liste indisponible ({errors.classify(exc)}).")
        if not eps:
            return self.out.send(inc.chat_id, "Aucun épisode n'est encore disponible pour cette saison.")
        pages = (len(eps) - 1) // PAGE_SIZE + 1
        page = max(0, min(page, pages - 1))
        chunk = eps[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        rows: Keyboard = [[Button(f"E{n}", data=f"e:{n}") for n in chunk[k:k + 5]] for k in range(0, len(chunk), 5)]
        nav = []
        if page > 0:
            nav.append(Button("‹ Précédent", data=f"list:{page - 1}"))
        if page < pages - 1:
            nav.append(Button("Suivant ›", data=f"list:{page + 1}"))
        if nav:
            rows.append(nav)
        self.out.send(inc.chat_id, f"📚 Épisodes disponibles ({len(eps)}) — page {page + 1}/{pages}", rows)

    # ── callbacks ──────────────────────────────────────────────────────────────────
    def _on_callback(self, inc: Incoming) -> None:
        data = inc.callback or ""
        if data == "chk":
            if self._gate(inc):
                self.out.send(inc.chat_id, "✅ Accès confirmé. Écrivez le nom d'un anime.")
            return
        if not self._gate(inc):
            return
        if data.startswith("xr:"):
            r = self.mgr.cancel(int(data[3:]), user_id=inc.user_id)
            return self.out.send(inc.chat_id, "🚫 Demande annulée." if r["ok"] else f"ℹ️ {r['message']}")
        st = self._load(inc.user_id)
        kind, _, arg = data.partition(":")
        if kind in ("reset", "xs"):
            self._reset(inc.user_id)
            return self.out.send(inc.chat_id, "✖ Recherche annulée. Écrivez le nom d'un anime." if kind == "xs"
                                 else "🔄 On repart de zéro. Écrivez le nom d'un anime.")
        stale = [[Button("🔄 Recommencer", data="reset")]]
        try:
            if kind == "s" and st.get("series"):
                return self._pick_series(inc, st, int(arg))
            if kind == "sp" and st.get("series"):
                view, _, pg = arg.partition(":")
                return self._show_series(inc, st, view, int(pg or 0), "🔎 Résultats :")
            if kind == "sv" and st.get("series"):
                return self._show_series(inc, st, arg, 0, "🔎 Résultats :")
            if kind == "n" and st.get("series") and st.get("series_idx") is not None:
                return self._pick_season(inc, st, int(arg))
            if kind == "v" and st.get("series") and st.get("season_idx") is not None:
                return self._pick_version(inc, st, arg)
        except (IndexError, KeyError, ValueError):
            return self.out.send(inc.chat_id, "⚠️ Ce bouton ne correspond plus à votre recherche.", stale)
        if not st.get("hit"):
            return self.out.send(inc.chat_id, "⚠️ Ce bouton n'est plus valable (recherche terminée ou annulée).", stale)
        if kind == "ep":
            return self._episode_menu(inc, st)
        if kind == "last":
            return self._create(inc, st, latest=True)
        if kind == "all":
            return self._create(inc, st, whole_season=True)
        if kind == "num":
            st["step"] = "await_number"
            self._save(inc.user_id, st)
            return self.out.send(inc.chat_id, "🔢 Quel numéro d'épisode ?")
        if kind == "list":
            return self._episode_list(inc, st, int(arg or 0))
        if kind == "e":
            return self._create(inc, st, episode=int(arg))
        if kind == "wait":
            return self._create(inc, st, episode=int(arg), force=True)

    # ── creating the request ───────────────────────────────────────────────────────
    def _anime_key(self, hit: dict) -> str:
        if hit.get("watched_key"):
            return hit["watched_key"]
        if self._identify is not None:
            return self._identify(hit["url"])["anime_key"]
        from . import discovery
        return discovery.identify_anime(self.cfg, hit["url"], discovery.default_fetch(self.cfg))["anime_key"]

    def _create(self, inc: Incoming, st: dict, *, episode: int | None = None, latest: bool = False,
                whole_season: bool = False, force: bool = False) -> None:
        active = self.mgr.active_request(inc.user_id)
        if active is not None:
            return self._refuse_second(inc.chat_id, active)
        hit = st["hit"]
        if episode is not None and episode < 1:
            return self.out.send(inc.chat_id, "🔢 Le numéro d'épisode doit être 1 ou plus.")
        if episode is not None and not force and not self._plausible(inc, st, hit, episode):
            return
        try:
            key = self._anime_key(hit)
        except Exception as exc:
            code = errors.classify(exc)
            return self.out.send(inc.chat_id, f"⚠️ Cet anime est introuvable sur la source ({code}).")
        try:
            req = self.mgr.create(NewRequest(
                user_id=inc.user_id, kind="season" if whole_season else "episode", anime_key=key, title=st["title"],
                version=st["version"], source_url=hit["url"], season=st.get("season"), episode_number=episode,
                latest=latest))
        except ActiveRequestExists as exc:
            return self._refuse_second(inc.chat_id, exc.request)
        self._reset(inc.user_id)
        self.out.typing(inc.chat_id)
        loader_id = self.out.send(inc.chat_id, "⏳ Traitement de votre demande…")
        req = self.mgr.process(req["id"])
        text, kb = self._created_text(req), [[Button("❌ Annuler", data=f"xr:{req['id']}")]]
        if loader_id is not None:
            return self.out.edit(inc.chat_id, loader_id, text, kb)
        self.out.send(inc.chat_id, text, kb)

    def _plausible(self, inc: Incoming, st: dict, hit: dict, episode: int) -> bool:
        """An episode far beyond the last one the source lists will not appear in 20 minutes: say so, let the user decide."""
        try:
            nums = [e.number for e in self.mgr.catalog.episodes(hit["url"]) if e.number is not None]
        except Exception:
            return True                                   # cannot tell: create it, the request handles source errors
        if not nums or episode <= max(nums) + WAIT_MARGIN:
            return True
        last = max(nums)
        self.out.send(inc.chat_id, f"⚠️ {st['title']} : le dernier épisode disponible est le {last}. "
                                   f"L'épisode {episode} n'existe pas encore.",
                      [[Button(f"🔥 Dernier épisode (E{last})", data="last")],
                       [Button(f"⏳ Attendre l'épisode {episode}", data=f"wait:{episode}")],
                       [Button("✖ Annuler la recherche", data="xs")]])
        return False

    def _created_text(self, req: dict) -> str:
        what = self._label(req)
        st = req["state"]
        if st == RS.WAITING_FOR_MEDIA.value:
            mins = int(self.mgr.wait_timeout_s // 60)
            return (f"⏳ {what} n'est pas encore disponible. Je le surveille pendant {mins} min et je vous l'envoie "
                    "dès qu'il apparaît.")
        if st in (RS.FAILED.value, RS.EXPIRED.value):
            return f"❌ Demande impossible : {req.get('last_error') or req.get('error_code') or 'source indisponible'}."
        if st == RS.SEARCHING.value:
            return f"🔎 Recherche de {what}… je vous préviens dès que c'est prêt."
        return f"✅ Demande enregistrée : {what}. Je vous l'envoie ici dès qu'il est prêt."

    def _refuse_second(self, chat_id: int, active: dict) -> None:
        self.out.send(chat_id, f"⛔ Vous avez déjà une demande en cours : {self._label(active)} ({_STATE_FR.get(active['state'], active['state'])}).\n"
                               "Attendez qu'elle soit terminée ou annulez-la.",
                      [[Button("❌ Annuler ma demande", data=f"xr:{active['id']}")]])

    @staticmethod
    def _label(req: dict) -> str:
        v = req.get("version") or ""
        if req["kind"] == "season":
            return f"{req['title']} — saison {req['season']}" + (f" ({v})" if v else "") if req.get("season") is not None \
                else f"{req['title']} — saison complète ({v})"
        n = f"E{req['episode_number']}" if req.get("episode_number") is not None else "dernier épisode"
        if req.get("season") is not None:                       # the season the user chose stays visible ("Wakfu · Saison 2 · E5")
            return f"{req['title']} · Saison {req['season']} · {n} — {v}"
        return f"{req['title']} {n} — {v}"

    # ── /history /status /cancel ───────────────────────────────────────────────────
    def _history(self, inc: Incoming) -> None:
        rows = self.mgr.history(inc.user_id, 15)
        if not rows:
            return self.out.send(inc.chat_id, "📚 Mes demandes\n\nAucune demande pour l'instant.")
        lines = ["📚 Mes demandes", ""]
        for r in rows:
            icon = _ICON.get(r["state"], "⏳")
            extra = f" ({r['items_done']}/{r['items_total']})" if r["kind"] == "season" and r["items_total"] else ""
            lines.append(f"{icon} {self._label(r)}{extra}")
        self.out.send(inc.chat_id, "\n".join(lines))

    def _status(self, inc: Incoming) -> None:
        a = self.mgr.active_request(inc.user_id)
        if a is None:
            return self.out.send(inc.chat_id, "Aucune demande en cours.")
        self.out.send(inc.chat_id, f"⏳ {self._label(a)} — {_STATE_FR.get(a['state'], a['state'])}",
                      [[Button("❌ Annuler", data=f"xr:{a['id']}")]])

    def _cancel_cmd(self, inc: Incoming) -> None:
        a = self.mgr.active_request(inc.user_id)
        if a is None:
            return self.out.send(inc.chat_id, "Aucune demande à annuler.")
        self.mgr.cancel(a["id"], user_id=inc.user_id)
        self.out.send(inc.chat_id, f"🚫 Demande annulée : {self._label(a)}.")

    # ── state-change notifications (called by the worker loop) ─────────────────────
    def notify_changes(self) -> int:
        """Tell each user what changed on their request since we last told them.  The media itself is the "done" message
        for an episode; texts are only for what the user could not otherwise see (expired, failed, season finished, the
        awaited episode appearing)."""
        sent = 0
        rows = self.conn.execute("SELECT id, user_id, state, notified_state, kind, title, episode_number, version, season, "
                                 "error_code, last_error FROM requests WHERE state IS NOT notified_state "
                                 "ORDER BY id").fetchall()
        for r in rows:
            r = dict(r)
            text = None
            label = self._label(r)
            if r["state"] == RS.EXPIRED.value:
                text = f"⌛ {label} n'est pas apparu à temps. Vous pouvez refaire une demande plus tard."
            elif r["state"] == RS.FAILED.value:
                text = f"❌ {label} : {r.get('last_error') or errors.UNKNOWN_ERROR}"
            elif r["state"] == RS.COMPLETED.value and r["kind"] == "season":
                text = f"✅ {label} : tous les épisodes disponibles ont été envoyés."
            elif r["state"] in (RS.QUEUED.value, RS.PROCESSING.value) and r["notified_state"] == RS.WAITING_FOR_MEDIA.value:
                text = f"✅ {label} est apparu, je le prépare."
            if text:
                try:
                    self.out.send(r["user_id"], text)
                    sent += 1
                except Exception as exc:
                    logger.warning("[USERBOT] notification impossible user=%s: %s", r["user_id"], exc)
                    continue                                   # not marked: tried again next tick
            self.conn.execute("UPDATE requests SET notified_state=? WHERE id=?", (r["state"], r["id"]))
        self.conn.commit()
        return sent


_FLAG = {"VF": "🇫🇷 VF", "VOSTFR": "🇯🇵 VOSTFR"}
_ICON_OF = {"VF": "🇫🇷", "VOSTFR": "🇯🇵"}          # compact, for lists
_ICON = {"COMPLETED": "✅", "CANCELLED": "🚫", "EXPIRED": "⌛", "FAILED": "❌"}
_STATE_FR = {"PENDING": "en attente", "SEARCHING": "recherche", "FOUND": "trouvé", "QUEUED": "en file",
             "PROCESSING": "en préparation", "WAITING_FOR_MEDIA": "en attente de l'épisode", "DELIVERING": "envoi",
             "COMPLETED": "terminée", "CANCELLED": "annulée", "EXPIRED": "expirée", "FAILED": "échouée"}


def _ser(s) -> dict:
    return {"name": s.name, "kind": s.kind, "watched": s.watched, "is_main": s.is_main, "alt": s.alt, "versions": s.versions,
            "seasons": [
                {"label": o.label, "season": o.season,
                 "versions": {v: {"title": h.title, "url": h.url, "watched_key": h.watched_key} for v, h in o.versions.items()}}
                for o in s.seasons]}


# ── real Telegram layer ─────────────────────────────────────────────────────────────

class TelegramOutbox:
    def __init__(self, transport):
        self.t = transport

    @staticmethod
    def _markup(kb: Keyboard | None):
        if not kb:
            return None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([[InlineKeyboardButton(b.text, callback_data=b.data, url=b.url) for b in row]
                                     for row in kb])

    def send(self, chat_id, text, keyboard=None):
        return self.t.send_text(chat_id, text, self._markup(keyboard)).message_id

    def edit(self, chat_id, message_id, text, keyboard=None):
        async def _impl():
            return await self.t.bot().edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                                        reply_markup=self._markup(keyboard))
        self.t.run(_impl())

    def answer(self, callback_id, text=None):
        async def _impl():
            return await self.t.bot().answer_callback_query(callback_query_id=callback_id, text=text)
        try:
            self.t.run(_impl())
        except Exception:
            pass                                            # a stale button is not an error

    def typing(self, chat_id):
        async def _impl():
            return await self.t.bot().send_chat_action(chat_id=chat_id, action="typing")
        try:
            self.t.run(_impl())
        except Exception:
            pass                                            # a missing typing indicator is not an error


def to_incoming(u) -> Incoming | None:
    if u.callback_query is not None:
        cq = u.callback_query
        chat = cq.message.chat_id if cq.message else cq.from_user.id
        return Incoming(cq.from_user.id, chat, cq.from_user.username, callback=cq.data, callback_id=cq.id,
                        message_id=cq.message.message_id if cq.message else None)
    if u.message is not None and u.message.text is not None and u.message.chat.type == "private":
        m = u.message
        return Incoming(m.from_user.id, m.chat_id, m.from_user.username, text=m.text, message_id=m.message_id)
    return None


def run_user_bot(cfg, stop: threading.Event | None = None, *, transport=None, conn=None) -> None:
    """Blocking long-poll loop for the user bot; run it in a daemon thread of the worker process."""
    from . import db
    from .telegram_publisher import transport_from_config
    if not cfg.user_bot_token and transport is None:
        logger.info("[USERBOT] USER_BOT_TOKEN absent : bot utilisateur non démarré")
        return
    conn = conn or db.connect()
    t = transport or transport_from_config(cfg, cfg.user_bot_token)
    router = UserBotRouter(conn, cfg, TelegramOutbox(t), RequestManager.from_config(conn, cfg), SourceSearch(cfg, conn),
                           MembershipService(conn, t.member_status, cfg.required_channels))
    offset = None
    while not (stop is not None and stop.is_set()):
        try:
            async def _poll():
                return await t.bot().get_updates(offset=offset, timeout=30, limit=50,
                                                 allowed_updates=["message", "callback_query"])
            updates = t.run(_poll()) or []
        except Exception as exc:
            logger.warning("[USERBOT] getUpdates échec: %s", exc)
            time.sleep(3)
            continue
        for u in updates:
            offset = u.update_id + 1
            inc = to_incoming(u)
            if inc is not None:
                router.handle(inc)
