"""Finding an anime for a user — the way the SITE does it.

HOW THE SITE SEARCHES (verified against the live site and in a browser, see V2_BOT_SEARCH_VERIFICATION.md)
  The two search boxes of voir-anime.to ("Rechercher en VOSTFR…" / "Rechercher en VF…") are two separate *Ajax Search Pro*
  engines with their own catalogues, not a filter.  Each box sends
      POST /wp-admin/admin-ajax.php   action=ajaxsearchpro_search  aspp=<text>  asid=<engine id>  asp_inst_id=<id>_1
                                      options=<the engine's form settings>
  and gets back HTML + a JSON block (title, link, image, relevance, content) capped at ~17 results, no pagination.
  * The engine that answers IS the version: many VF pages carry no "(VF)" ("Wakfu S2", "Avatar, Le Dernier Maître De L'air").
  * Titles the site indexes include alternative names ("attack on titan" -> Shingeki no Kyojin).
  * A season is its own page ("Wakfu S1…S4"); its number exists only when the title says so ("S2", "Saison 2").
  The engine ids and options are read from the home page (cached), never assumed; 2 = VF / 3 = VOSTFR are only a fallback.

WHAT THIS MODULE DOES WITH THE ANSWER
  * watched anime (`animes` table) are merged with the site's results, flagged, never hiding anything;
  * hits are grouped into series -> seasons -> versions from what the pages really say (a season number only if written;
    a trailing number without marker — "Kimetsu no Yaiba 2" — stays a separate entry, never guessed);
  * ranking: titles containing every word of the query first, then matches by another title, then the site's own order;
    films / specials / OVA (recognised from the title) are kept apart from the series.
"""
from __future__ import annotations

import html as _html
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, quote, quote_plus, urlparse

from .media import normalize_version
from .parser import normalize

ADMIN_AJAX = "/wp-admin/admin-ajax.php"
FALLBACK_ENGINES = {"VF": 2, "VOSTFR": 3}
FALLBACK_OPTIONS = ("current_page_id=15&qtranslate_lang=0&filters_changed=0&filters_initial=1&asp_gen%5B%5D=title"
                    "&asp_gen%5B%5D=content&asp_gen%5B%5D=excerpt&aspf%5Bvf__1%5D=vf")
ENGINES_TTL_S = 3600
RESULTS_TTL_S = 600
SITE_RESULT_CAP = 15            # the site returns at most ~17-18: at this size the answer is probably cut

# what a person means by "the anime" vs what is shown apart
MAIN_KINDS = ("TV", "ONA", "")
_SPECIAL_RX = re.compile(r"\b(special|specials|spécial|speciaux|spéciaux|recap|ova|oav|oad|omake|extra|bonus|episode of|tv special)\b", re.I)
_MOVIE_RX = re.compile(r"\b(movie|film|gekijouban|the movie)\b", re.I)


def kind_of_title(title: str) -> str:
    """MOVIE / SPECIAL / TV from the words of the title (the result list carries no type)."""
    if _MOVIE_RX.search(title):
        return "MOVIE"
    if _SPECIAL_RX.search(title):
        return "SPECIAL"
    return "TV"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str                    # https://host/anime/<slug>/
    version: str                # VF | VOSTFR  (= the engine that returned it, or the watched anime's own language)
    season: int | None          # only when the title says so
    watched_key: str | None = None   # anime_key when it is in the watched list
    kind: str = ""              # TV | MOVIE | SPECIAL ...
    episodes: int | None = None
    alt: bool = False           # found through another title of the anime (the words are not in this title)
    rank: int = 0               # position in the site's own answer


@dataclass
class SeasonOption:
    label: str
    season: int | None
    versions: dict[str, SearchHit] = field(default_factory=dict)      # VF / VOSTFR -> hit


@dataclass
class Series:
    name: str
    seasons: list[SeasonOption] = field(default_factory=list)
    kind: str = "TV"
    watched: bool = False
    score: float = 0.0
    alt: bool = True             # every page of the series was found through another title

    @property
    def is_main(self) -> bool:
        return self.kind in MAIN_KINDS

    @property
    def versions(self) -> list[str]:
        have = {v for o in self.seasons for v in o.versions}
        return [v for v in ("VF", "VOSTFR") if v in have]


_SEASON_RE = [re.compile(p) for p in (r"\b(?:saison|season)\s*(\d{1,2})\b", r"\b(\d{1,2})(?:st|nd|rd|th|e|eme)\s+(?:saison|season)\b",
                                      r"\bs(\d{1,2})\b")]
_VERSION_TAG = re.compile(r"\(\s*(?:vf|vostfr)\s*\)", re.I)


def season_of_title(title: str) -> int | None:
    t = normalize(title)
    for rx in _SEASON_RE:
        m = rx.search(t)
        if m:
            return int(m.group(1))
    return None


def version_of(title: str, url: str) -> str:
    """Fallback only (watched anime without a stored language): the site's own engine is the real source of the version."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    return "VF" if (re.search(r"\(\s*vf\s*\)", title, re.I) or slug.endswith("-vf") or "-vf-" in slug) else "VOSTFR"


def clean_name(title: str) -> str:
    """Display name of a series: the title without version tag and season marker ("Wakfu S2" -> "Wakfu")."""
    t = _VERSION_TAG.sub(" ", title)
    t = re.sub(r"\(\s*(?:saison|season)\s*\d+\s*\)", " ", t, flags=re.I)
    t = re.sub(r"\b(?:saison|season)\s*\d{1,2}\b", " ", t, flags=re.I)
    t = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+season\b", " ", t, flags=re.I)
    t = re.sub(r"\bS\d{1,2}\b", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -–:,")


def base_title(title: str) -> str:
    """Grouping key (normalised clean name)."""
    t = normalize(clean_name(title))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:]", " ", t)).strip()


def slugify_query(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize(q)).strip("_") or "empty"


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w]+", normalize(text)) if t]


# words a person adds or the site writes differently ("Spy x Family" is "SPY×FAMILY", "My Hero Academia" is "Boku no Hero Academia")
OPTIONAL_WORDS = {"x", "the", "of", "no", "wa", "ga", "de", "la", "le", "les", "a", "an", "to", "and", "my", "in", "on", "wo", "ni"}


def title_match(query: str, title: str) -> tuple[bool, float]:
    """(does the title contain every required word of the query, closeness score)."""
    q = tokens(query)
    tw = tokens(title)
    required = [w for w in q if w not in OPTIONAL_WORDS] or q
    if not required:
        return False, 0.0
    present = [w for w in required if w in tw or (len(w) >= 3 and any(t.startswith(w) for t in tw))]
    ok = len(present) == len(required)
    score = 100.0 * len(present) / len(required)
    bt = base_title(title)
    qs = " ".join(required)
    bt_required = " ".join(w for w in tokens(bt) if w not in OPTIONAL_WORDS) or bt
    if bt == qs or bt_required == qs:                       # "shingeki no kyojin" IS "shingeki kyojin" once "no" is optional
        score += 200
    elif bt.startswith(qs):
        score += 40
    elif ok:
        score += 20
    return ok, score


# ── the site's search engines ────────────────────────────────────────────────────────────────────────────────

@dataclass
class AspHit:
    title: str
    url: str
    version: str
    relevance: float = 0.0
    rank: int = 0
    content: str = ""


class AspEngine:
    """The two search boxes of the site, called exactly as the site's own script calls them."""

    def __init__(self, cfg, get: Callable[[str], str], post: Callable[[str, dict], str]):
        self.cfg, self._get, self._post = cfg, get, post
        self._engines: dict[str, dict] | None = None
        self._engines_at = 0.0
        self._cache: dict[tuple[str, str], tuple[float, list[AspHit]]] = {}
        self._lock = threading.Lock()

    @property
    def base(self) -> str:
        return ((getattr(self.cfg, "source", None) or {}).get("base_url") or "").rstrip("/")

    def engines(self, refresh: bool = False) -> dict[str, dict]:
        """{'VF': {'asid': 2, 'options': '…'}, 'VOSTFR': {...}} read from the home page (cached one hour)."""
        with self._lock:
            if self._engines and not refresh and time.monotonic() - self._engines_at < ENGINES_TTL_S:
                return self._engines
        from bs4 import BeautifulSoup
        found: dict[str, dict] = {}
        try:
            soup = BeautifulSoup(self._get(self.base + "/"), "lxml")
            for box in soup.select("div.ajaxsearchpro[data-id]"):
                asid = box.get("data-id")
                def version_in(text: str) -> str | None:
                    t = text.lower()
                    return "VOSTFR" if "vostfr" in t else "VF" if re.search(r"\bvf\b", t) else None
                # what the visitor READS in the box decides; the internal name is only a fallback
                version = (version_in(" ".join((i.get("placeholder") or "") for i in box.select("input")))
                           or version_in(box.get("data-name") or ""))
                if not (asid and asid.isdigit() and version) or version in found:
                    continue
                sett = soup.select_one(f"#__original__ajaxsearchprosettings{asid}_1") or soup.select_one(f"#ajaxsearchprosettings{asid}_1")
                pairs = []
                for inp in (sett.select("input") if sett else []):
                    name, typ = inp.get("name"), (inp.get("type") or "")
                    if not name or (typ in ("checkbox", "radio") and not inp.has_attr("checked")):
                        continue
                    pairs.append(f"{quote(name, safe='')}={quote(inp.get('value') or '', safe='')}")
                found[version] = {"asid": int(asid), "options": "&".join(pairs) or FALLBACK_OPTIONS}
        except Exception:
            found = {}
        if set(found) != {"VF", "VOSTFR"}:                      # the page changed or was unreadable: known values, flagged
            for v, asid in FALLBACK_ENGINES.items():
                found.setdefault(v, {"asid": asid, "options": FALLBACK_OPTIONS, "fallback": True})
        with self._lock:
            self._engines, self._engines_at = found, time.monotonic()
        return found

    def payload(self, query: str, info: dict) -> dict:
        return {"action": "ajaxsearchpro_search", "aspp": query, "asid": info["asid"], "asp_inst_id": f"{info['asid']}_1",
                "options": info["options"]}

    def _one(self, query: str, version: str, info: dict) -> list[AspHit]:
        key = (version, normalize(query))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < RESULTS_TTL_S:
                return hit[1]
        raw = self._post(self.base + ADMIN_AJAX, self.payload(query, info))
        hits = parse_asp_response(raw, version)
        with self._lock:
            self._cache[key] = (time.monotonic(), hits)
        return hits

    def search(self, query: str) -> dict[str, list[AspHit]]:
        """Both engines, in parallel: {'VF': [...], 'VOSTFR': [...]}.  A failing engine raises (the caller says so)."""
        engines = self.engines()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {v: pool.submit(self._one, query, v, info) for v, info in engines.items()}
            return {v: f.result() for v, f in futures.items()}


def parse_asp_response(raw: str, version: str) -> list[AspHit]:
    """Results of one engine, in the site's own order.  The JSON block is preferred; the HTML is the fallback."""
    hits: list[AspHit] = []
    m = re.search(r"___ASPSTART_DATA___(.*?)___ASPEND_DATA___", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            for i, r in enumerate(data.get("results") or []):
                if r.get("link") and "/anime/" in r["link"]:
                    hits.append(AspHit(re.sub(r"\s+", " ", _html.unescape(r.get("title") or r.get("post_title") or "")).strip(), r["link"], version,
                                       float(r.get("relevance") or 0), i, r.get("content") or ""))
            return hits
        except ValueError:
            hits = []
    from bs4 import BeautifulSoup
    mh = re.search(r"___ASPSTART_HTML___(.*?)___ASPEND_HTML___", raw, re.S)
    soup = BeautifulSoup(mh.group(1) if mh else raw, "lxml")
    for i, a in enumerate(soup.select("a.asp_res_url")):
        if a.get("href") and "/anime/" in a["href"]:
            hits.append(AspHit(a.get_text(" ", strip=True), a["href"], version, 0.0, i))
    return hits


# ── search for the bot ───────────────────────────────────────────────────────────────────────────────────────

class SourceSearch:
    def __init__(self, cfg, conn: sqlite3.Connection | None = None, fetch: Callable[[str], str] | None = None,
                 post: Callable[[str, dict], str] | None = None):
        self.cfg, self.conn, self._fetch, self._post_fn = cfg, conn, fetch, post
        self.approximate = False       # set by the last search(): no title contains every word of the query
        self.truncated = False         # the site answered with (nearly) its maximum: the list is probably cut
        self.asp = AspEngine(cfg, self._get, self._post)

    def _get(self, url: str) -> str:
        if self._fetch is None:
            from . import discovery
            self._fetch = discovery.default_fetch(self.cfg)
        return self._fetch(url)

    def _post(self, url: str, data: dict) -> str:
        if self._post_fn is None:
            import httpx
            with httpx.Client(timeout=20, follow_redirects=True,
                              headers={"User-Agent": "v2_automation-research-bot/2.0 (+contact: lionelyvan24@gmail.com)"}) as c:
                r = c.post(url, data=data)
                r.raise_for_status()
                return r.text
        return self._post_fn(url, data)

    # -- watched list ---------------------------------------------------------------------
    def watched(self, query: str) -> list[SearchHit]:
        if self.conn is None:
            return []
        words = [w for w in tokens(query) if w not in OPTIONAL_WORDS] or tokens(query)
        if not words:
            return []
        hits = []
        for r in self.conn.execute("SELECT anime_key, title, source_url, language FROM animes "
                                   "WHERE enabled=1 AND source_url IS NOT NULL AND source_url<>''").fetchall():
            t = normalize(r["title"] or "")
            if all(w in t for w in words):
                lang = normalize_version(r["language"]) if r["language"] else "UNKNOWN"
                hits.append(SearchHit(r["title"] or r["anime_key"], r["source_url"],
                                      lang if lang != "UNKNOWN" else version_of(r["title"] or "", r["source_url"]),
                                      season_of_title(r["title"] or ""), watched_key=r["anime_key"],
                                      kind=kind_of_title(r["title"] or "")))
        return hits

    # -- the site ---------------------------------------------------------------------------
    def source(self, query: str) -> list[SearchHit]:
        result = self.asp.search(query)
        self.truncated = any(len(v) >= SITE_RESULT_CAP for v in result.values())
        hits: list[SearchHit] = []
        for version, asp_hits in result.items():
            for h in asp_hits:
                ok, _ = title_match(query, h.title)
                hits.append(SearchHit(h.title, h.url, version, season_of_title(h.title), kind=kind_of_title(h.title),
                                      alt=not ok, rank=h.rank))
        return hits

    def search(self, query: str) -> list[SearchHit]:
        """Watched anime + both engines of the site, merged by page URL and ranked.  `approximate` is True when no title
        contains every word of the query (all results were found through another title)."""
        merged: dict[str, SearchHit] = {}
        for h in self.source(query):
            merged[h.url.rstrip("/")] = h
        for w in self.watched(query):
            key = w.url.rstrip("/")
            site = merged.get(key)
            if site is not None:                                   # keep the watched key, the site's version and rank
                merged[key] = SearchHit(site.title, site.url, site.version, site.season, w.watched_key, site.kind, site.episodes,
                                        site.alt, site.rank)
            else:
                merged[key] = w
        hits = list(merged.values())
        self.approximate = bool(hits) and all(h.alt for h in hits)
        return hits


def group(hits: list[SearchHit], query: str | None = None) -> list[Series]:
    """Series -> seasons -> versions, from the hits only.  With a query: best series first, title matches before other-title
    matches, the site's own order last."""
    by_series: dict[str, Series] = {}
    for h in hits:
        key = base_title(h.title)
        s = by_series.setdefault(key, Series(name=clean_name(h.title) or h.title, kind=h.kind or kind_of_title(h.title)))
        if len(clean_name(h.title)) < len(s.name):
            s.name = clean_name(h.title)
        opt = next((o for o in s.seasons if o.season == h.season and (h.season is not None or o.label == _label(h))), None)
        if opt is None:
            opt = SeasonOption(label=_label(h), season=h.season)
            s.seasons.append(opt)
        opt.versions.setdefault(h.version, h)
        s.watched = s.watched or bool(h.watched_key)
        s.alt = s.alt and h.alt
        if query is not None:
            ok, sc = title_match(query, h.title)
            if not ok:                                              # other-title match: the plainest name first ("Shingeki no Kyojin" before its sequels)
                sc = -len(base_title(h.title)) * 3.0
            sc += 15 if h.watched_key else 0
            sc -= min(h.rank, 40) * 0.2                             # the site's own order breaks ties
            s.score = max(s.score, sc) if s.score else sc
    for s in by_series.values():
        s.seasons.sort(key=lambda o: (o.season is None, o.season or 0, o.label))
    series = list(by_series.values())
    if query is not None:
        series.sort(key=lambda s: (s.alt, -s.score))
    return series


def _label(h: SearchHit) -> str:
    return f"Saison {h.season}" if h.season is not None else "Saison unique"
