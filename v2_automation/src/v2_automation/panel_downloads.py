"""Panel: find an anime the way the site does, list its real episodes, and queue a download that is published to the channel.

Same identity as everything else: every episode goes through `media.ensure_media` (one media_key, one job, whoever asks —
watcher, user or admin).  No request row and no user: the admin's download is a channel publication, not a private delivery.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from . import media, repo
from .catalog import SourceCatalog
from .search import SourceSearch, group
from .timeutil import now_utc

logger = logging.getLogger(__name__)

MODES = ("episode", "selection", "season", "last_n")
MAX_EPISODES = 400                      # one action never queues more than this


def _ser(s) -> dict[str, Any]:
    return {"name": s.name, "kind": s.kind, "watched": s.watched, "is_main": s.is_main, "alt": s.alt, "versions": s.versions,
            "seasons": [{"label": o.label, "season": o.season,
                         "versions": {v: {"title": h.title, "url": h.url, "watched_key": h.watched_key}
                                      for v, h in o.versions.items()}} for o in s.seasons]}


def search(conn: sqlite3.Connection, cfg, query: str, *, searcher: SourceSearch | None = None) -> dict[str, Any]:
    query = (query or "").strip()
    if len(query) < 2:
        return {"ok": False, "message": "Écrivez au moins 2 lettres."}
    searcher = searcher or SourceSearch(cfg, conn)
    try:
        hits = searcher.search(query)
    except Exception as exc:                                       # the site is the only thing that can fail here
        logger.warning("[PANEL] search failed: %s", exc)
        return {"ok": False, "message": "La recherche du site ne répond pas. Réessayez dans un instant."}
    series = [s for s in group(hits, query)]
    return {"ok": True, "query": query, "truncated": bool(getattr(searcher, "truncated", False)),
            "approximate": bool(getattr(searcher, "approximate", False)),
            "items": [_ser(s) for s in series]}


def _known(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    ep = repo.get_by_episode_key(conn, media.SOURCE_NAME, key)
    if ep is None:
        return None
    return {"status": ep.status, "published": ep.status in media.PUBLISHED_STATUSES, "job": ep.id, "origin": ep.origin}


def episodes(conn: sqlite3.Connection, cfg, source_url: str, *, catalog: SourceCatalog | None = None) -> dict[str, Any]:
    catalog = catalog or SourceCatalog(cfg)
    try:
        listing = catalog.episodes(source_url)
        det = catalog.details(source_url) or {}
    except Exception as exc:
        return {"ok": False, "message": f"Page illisible : {exc}"}
    items = [{"number": e.number, "label": e.label, "url": e.url, "known": _known(conn, e.key)} for e in listing]
    return {"ok": True, "source_url": source_url, "version": det.get("version"), "type": det.get("type"),
            "status": det.get("status"), "declared": det.get("declared"), "items": items}


def _wanted(listing, mode: str, numbers: list[int], n: int) -> list:
    numbered = [e for e in listing if e.number is not None]
    if mode == "season":
        return numbered
    if mode == "last_n":
        return numbered[-max(1, n):]
    wanted = set(numbers)
    return [e for e in numbered if e.number in wanted]


def download(conn: sqlite3.Connection, cfg, *, source_url: str, mode: str, version: str, numbers: list[int] | None = None,
             n: int = 1, title: str | None = None, watch: bool = False, fetch: Callable[[str], str] | None = None,
             catalog: SourceCatalog | None = None) -> dict[str, Any]:
    """Queue the wanted episodes of one anime page (= one season in one version) for publication in the channel."""
    from . import discovery
    if mode not in MODES:
        return {"ok": False, "message": "Choisissez un épisode, une sélection, la saison ou les derniers épisodes."}
    version = media.normalize_version(version)
    numbers = [int(x) for x in (numbers or [])]
    if mode in ("episode", "selection") and not numbers:
        return {"ok": False, "message": "Aucun épisode choisi."}
    fetch = fetch or discovery.default_fetch(cfg)
    try:
        info = discovery.identify_anime(cfg, source_url, fetch)
    except (ValueError, discovery.DiscoveryError) as exc:
        return {"ok": False, "message": str(exc)}
    catalog = catalog or SourceCatalog(cfg, fetch=fetch)
    try:
        listing = catalog.episodes(info["source_url"])
    except Exception as exc:
        return {"ok": False, "message": f"Page illisible : {exc}"}
    wanted = _wanted(listing, mode, numbers, n)
    if not wanted:
        return {"ok": False, "message": "Aucun de ces épisodes n'est listé par le site pour l'instant."}
    if len(wanted) > MAX_EPISODES:
        return {"ok": False, "message": f"Trop d'épisodes d'un coup ({len(wanted)}). Maximum {MAX_EPISODES}."}
    key = info["anime_key"]
    out = {"created": [], "already_queued": [], "already_published": [], "unavailable": [], "missing": []}
    for num in sorted(set(numbers) - {e.number for e in wanted}) if mode in ("episode", "selection") else []:
        out["missing"].append(num)
    for e in wanted:
        before = _known(conn, e.key)
        res = media.ensure_media(conn, anime_key=key, episode_number=e.number, version=version, episode_url=e.url,
                                 episode_key=e.key, label=e.label, origin="panel", publish_channel=True)
        bucket = {"created": "created", "revived": "created", "joined": "already_queued",
                  "ready": "already_published", "unavailable": "unavailable"}.get(res.action, "already_queued")
        if bucket == "already_queued" and before and before["status"] in ("discovered", "identified"):
            bucket = "created"                                     # a known-but-never-queued episode is now really queued
        if res.action == "ready" and res.status not in media.PUBLISHED_STATUSES:
            bucket = "unavailable"                                 # downloaded for a user only: not a channel publication
        out[bucket].append({"number": e.number, "job": res.episode_id, "media_key": res.media_key})
    watched = False
    if watch:
        from . import service
        w = service.add_anime_from_url(conn, cfg, info["source_url"], fetch=fetch, title=title, language=version.lower())
        watched = bool(w.get("ok"))
    total = len(out["created"])
    logger.info("[PANEL] download anime=%s version=%s mode=%s created=%d queued=%d published=%d",
                key, version, mode, total, len(out["already_queued"]), len(out["already_published"]))
    parts = [f"{total} épisode(s) ajouté(s) à la file"]
    if out["already_queued"]:
        parts.append(f"{len(out['already_queued'])} déjà en file")
    if out["already_published"]:
        parts.append(f"{len(out['already_published'])} déjà publié(s)")
    if out["missing"]:
        parts.append(f"{len(out['missing'])} introuvable(s) sur le site")
    return {"ok": True, "anime_key": key, "title": title or info["title"], "version": version, "watched": watched,
            "message": " · ".join(parts) + ".", **out, "at": now_utc()}
