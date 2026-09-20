"""Site-wide detection: the "latest episodes" feed of the source (homepage + `page/N/`).

The watcher used to look only at the anime pages of a configured list, so an anime it did not know was
invisible.  The feed lists what the site has just posted, for ANY anime.  Once per cycle:

  1. read the homepage and its next pages (`source.max_pages`), one entry per episode with its date text;
  2. keep the entries released TODAY (`release_date.released_today`; unreadable / yesterday -> ignored);
  3. an episode already in the database is skipped (same identity as the anime-page check, so a second signal
     never creates a duplicate);
  4. an anime we do not know yet is identified from its own page and ADDED (marked "auto-added"); its first
     check is the existing bootstrap + catch-up, which queues today's episodes and baselines the older ones.

Nothing here downloads or publishes: it only feeds the same queue as every other detection.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Callable
from urllib.parse import urljoin

from . import discovery, repo
from .release_date import released_today
from .timeutil import now_utc

logger = logging.getLogger(__name__)

AUTO_PREFIX = "auto_added:"
_ANIME_SLUG = re.compile(r"/anime/([^/]+)/")


def is_auto_added(conn: sqlite3.Connection, anime_key: str) -> bool:
    return conn.execute("SELECT 1 FROM control WHERE ckey=?", (AUTO_PREFIX + anime_key,)).fetchone() is not None


def _mark_auto_added(conn: sqlite3.Connection, anime_key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO control (ckey, cvalue, updated_at) VALUES (?, '1', ?)",
                 (AUTO_PREFIX + anime_key, now_utc()))


def feed_urls(cfg) -> list[str]:
    source = getattr(cfg, "source", None) or {}
    base = source.get("base_url", "").rstrip("/") + "/"
    pages = max(1, int(source.get("max_pages", 3) or 3))
    return [base] + [urljoin(base, f"page/{n}/") for n in range(2, pages + 1)]


def anime_page_url(cfg, episode_url: str) -> str | None:
    """`https://host/anime/<slug>/<slug>-12-vostfr/` -> `https://host/anime/<slug>/` (configured host only)."""
    m = _ANIME_SLUG.search(episode_url or "")
    base = (getattr(cfg, "source", None) or {}).get("base_url", "").rstrip("/")
    return f"{base}/anime/{m.group(1)}/" if (m and base) else None


def scan(conn: sqlite3.Connection, cfg, fetch: Callable[[str], str], *,
         alert: Callable[..., Any] | None = None) -> dict[str, Any]:
    """One pass over the feed.  Returns the numbers shown in the panels and the list of anime to visit now."""
    from source_audit.analysis.homepage import parse_homepage
    from source_audit.analysis.identity import build_episode_key
    result: dict[str, Any] = {"pages": 0, "entries": 0, "today": 0, "already_known": 0, "new_anime": 0,
                              "new_episodes": 0, "errors": 0, "new_anime_keys": [], "titles": []}
    entries = []
    for url in feed_urls(cfg):
        try:
            page = parse_homepage(fetch(url))
        except Exception as exc:                       # one page down never stops the others nor the cycle
            result["errors"] += 1
            logger.warning("[WATCHER] site_feed page illisible (%s): %s", url, exc)
            continue
        result["pages"] += 1
        entries.extend(page)
    result["entries"] = len(entries)

    seen: set[str] = set()
    for e in entries:
        if not released_today(e.published_at_raw):
            continue
        result["today"] += 1
        key = build_episode_key(e.url)
        if key in seen:
            continue
        seen.add(key)
        if (repo.get_by_episode_key(conn, discovery.SOURCE_NAME, key)
                or repo.get_by_canonical_url(conn, discovery.SOURCE_NAME, key + "/")):
            result["already_known"] += 1
            continue
        page_url = anime_page_url(cfg, e.url)
        if page_url is None:
            continue
        known = conn.execute("SELECT anime_key, enabled FROM animes WHERE source_url=?", (page_url,)).fetchone()
        if known is not None:
            if known["enabled"]:                        # a watched anime: its own check sees the episode next
                conn.execute("UPDATE animes SET force_check=1 WHERE anime_key=?", (known["anime_key"],))
                result["new_episodes"] += 1
            continue
        try:
            info = discovery.identify_anime(cfg, page_url, fetch)
        except Exception as exc:                        # this anime is skipped, the others go on
            result["errors"] += 1
            logger.warning("[WATCHER] site_feed anime non identifié (%s): %s", page_url, exc)
            continue
        same = conn.execute("SELECT anime_key FROM animes WHERE anime_key=?", (info["anime_key"],)).fetchone()
        now = now_utc()
        if same is None:
            conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url, created_at, updated_at) "
                         "VALUES (?, ?, 1, ?, ?, ?)", (info["anime_key"], info["title"], info["source_url"], now, now))
            _mark_auto_added(conn, info["anime_key"])
            result["new_anime"] += 1
            result["new_anime_keys"].append(info["anime_key"])
            result["titles"].append(info["title"])
        else:                                           # same anime under another address: reuse it, never duplicate
            conn.execute("UPDATE animes SET source_url=COALESCE(NULLIF(source_url, ''), ?), enabled=1, force_check=1 "
                         "WHERE anime_key=?", (info["source_url"], info["anime_key"]))
            result["new_anime_keys"].append(info["anime_key"])
        result["new_episodes"] += 1
    conn.commit()
    logger.info("[WATCHER] site_feed pages=%d entries=%d today=%d new_anime=%d new_episodes=%d already_known=%d errors=%d",
                result["pages"], result["entries"], result["today"], result["new_anime"], result["new_episodes"],
                result["already_known"], result["errors"])
    return result
