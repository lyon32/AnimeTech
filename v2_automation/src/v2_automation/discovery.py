"""Automatic discovery: watched anime -> new episodes -> queued jobs, on a fixed polling interval.

Reuses the parsers already proven in `source_audit` (`parse_anime_page`, the episode identity
helpers); nothing is fetched by the parsers themselves, so every rule here is unit-testable
with a fake `fetch`.

Rules
  * An anime is checked at most once per interval and never twice at the same time: the check is
    CLAIMED with one atomic UPDATE on `animes.last_checked_at` (also safe across processes), and an
    in-process set refuses a second concurrent run of the same anime.
  * The first successful check of an anime is a BASELINE: episodes already listed are recorded as
    known (`discovered`, never queued) unless `source.backfill_latest_on_first_check` asks for the
    newest N.  A job is created only for an episode that appears AFTER the baseline.
  * Identity = the canonical episode key (`build_episode_key`, stable across URL variants); the
    same key twice can never create a second episode or a second job (UNIQUE constraints + lookup).
  * New episodes of one anime are queued in ascending episode order (FIFO per anime).

The GLOBAL WATCHER
  The cycle belongs to the watcher, not to an anime.  Every `poll_interval_seconds` (1800 in production)
  ONE cycle starts (claimed atomically in `control`, so two processes never run two cycles) and it visits
  EVERY active anime: load actives -> for each, fetch the source and diff it with the database -> collect
  all new episodes -> one job per new episode.  "No new episode" is a normal result, never an error, and
  one failing anime never stops the others.  Bootstrap vs incremental mode: the first successful check of
  an anime is its BOOTSTRAP (already-listed episodes become known, no job, unless
  `backfill_latest_on_first_check` = N asks for the N newest); every later check is INCREMENTAL (only what
  the database does not know yet becomes a job).  An anime added between two cycles, or force-checked, is
  visited immediately without waiting for the next cycle.
  CATCH-UP (`source.catchup_today`): at the bootstrap, an episode released TODAY (local midnight -> now, read
  from the source's date text by `release_date`) is queued instead of baselined, whatever its release time;
  yesterday and older stay baseline, an unreadable date counts as "not today".
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from . import alerts, repo
from .metadata import detect_language
from .models import Episode
from .queues import QueueManager
from .release_date import released_today
from .timeutil import add_seconds, now_utc

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 1800          # production: 30 minutes
DEFAULT_TEST_POLL_INTERVAL_S = 10       # only when V2_TEST_MODE=1
SOURCE_NAME = "voir-anime.to"


class DiscoveryError(RuntimeError):
    """The anime page could not be fetched/parsed; original exception chained."""


def poll_interval_s(cfg) -> float:
    """30 min in production (`source.poll_interval_seconds`).  A short interval exists ONLY for tests
    and only when the environment explicitly says so (V2_TEST_MODE=1) — the two never mix."""
    source = getattr(cfg, "source", None) or {}
    if os.getenv("V2_TEST_MODE") == "1":
        return float(source.get("test_poll_interval_seconds", DEFAULT_TEST_POLL_INTERVAL_S))
    return float(source.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_S))


# ── fetching ─────────────────────────────────────────────────────────────────────

def default_fetch(cfg) -> Callable[[str], str]:
    from source_audit.fetch.http_client import HttpClient

    def fetch(url: str) -> str:
        with HttpClient(timeout_seconds=20, max_retries=2, retry_backoff_seconds=1.5,
                        user_agent="v2_automation-research-bot/2.0 (+contact: lionelyvan24@gmail.com)") as client:
            res = client.get(url)
        if not res.ok:
            raise DiscoveryError(f"HTTP {res.status_code} ({res.error_type.value}) on the anime page")
        return res.text

    return fetch


# ── identification of an anime page (adding an anime) ────────────────────────────

def validate_source_url(cfg, url: str) -> str:
    """Only an anime page of the configured source is accepted (`https://host/anime/<slug>/`)."""
    u = urlparse((url or "").strip())
    base = urlparse((getattr(cfg, "source", None) or {}).get("base_url", ""))
    parts = [p for p in u.path.split("/") if p]
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("URL invalide")
    if base.netloc and u.netloc.lower() != base.netloc.lower():
        raise ValueError(f"source non autorisée: {u.netloc}")
    if len(parts) != 2 or parts[0] != "anime":
        raise ValueError("ce n'est pas une page d'anime (attendu: https://hôte/anime/<nom>/)")
    return f"{u.scheme}://{u.netloc}/anime/{parts[1]}/"


def identify_anime(cfg, url: str, fetch: Callable[[str], str]) -> dict[str, Any]:
    """Fetch the page once: stable anime_key (`postid:N`), title, language hint."""
    from source_audit.analysis.identity import build_anime_key
    from source_audit.analysis.anime import parse_anime_page
    url = validate_source_url(cfg, url)
    try:
        record = parse_anime_page(fetch(url), url)
    except Exception as exc:
        raise DiscoveryError(f"page anime illisible: {exc}") from exc
    if not record.post_id:
        raise DiscoveryError("identifiant d'anime introuvable sur la page")
    title = record.title or record.romaji_title or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    return {"anime_key": build_anime_key(record.post_id), "title": title, "source_url": url,
            "episodes_listed": len(record.episode_links)}


# ── due animes + atomic claim ────────────────────────────────────────────────────

def due_animes(conn: sqlite3.Connection, interval_s: float, *, now: str | None = None) -> list[str]:
    now = now or now_utc()
    cutoff = add_seconds(now, -interval_s)
    rows = conn.execute("""
        SELECT anime_key FROM animes
        WHERE enabled = 1 AND source_url IS NOT NULL AND source_url <> ''
          AND (force_check = 1 OR last_checked_at IS NULL OR last_checked_at <= ?)
        ORDER BY COALESCE(last_checked_at, '') ASC, anime_key ASC""", (cutoff,)).fetchall()
    return [r["anime_key"] for r in rows]


def claim_check(conn: sqlite3.Connection, anime_key: str, interval_s: float, *,
                now: str | None = None) -> bool:
    """One atomic UPDATE decides who runs the check: a second request for the same anime (another
    tick, another process) finds the interval already consumed and is ignored."""
    now = now or now_utc()
    cutoff = add_seconds(now, -interval_s)
    cur = conn.execute("""
        UPDATE animes SET last_checked_at=?, force_check=0, updated_at=?
        WHERE anime_key=? AND enabled=1 AND source_url IS NOT NULL AND source_url <> ''
          AND (force_check = 1 OR last_checked_at IS NULL OR last_checked_at <= ?)""",
                       (now, now, anime_key, cutoff))
    conn.commit()
    return cur.rowcount == 1


# ── the check itself ─────────────────────────────────────────────────────────────

def _auto_added(conn: sqlite3.Connection, anime_key: str) -> bool:
    from .site_feed import is_auto_added
    return is_auto_added(conn, anime_key)


def _episode_sort_key(item: dict) -> tuple:
    n = item["number"]
    return (0, n, item["order"]) if n is not None else (1, 0, item["order"])


def check_anime(conn: sqlite3.Connection, cfg, anime_key: str, fetch: Callable[[str], str], *,
                alert: Callable[..., Any] | None = None, now: str | None = None) -> dict[str, Any]:
    """Compare what the source lists with what is known; queue only what is genuinely new."""
    from source_audit.analysis.anime import parse_anime_page
    from source_audit.analysis.identity import build_episode_key, extract_episode_number_from_url
    now = now or now_utc()
    row = conn.execute("SELECT * FROM animes WHERE anime_key=?", (anime_key,)).fetchone()
    if row is None or not row["source_url"]:
        raise DiscoveryError(f"anime {anime_key} sans URL source")
    url = row["source_url"]
    report: dict[str, Any] = {"anime_key": anime_key, "new": [], "baseline": 0, "known": 0, "skipped": 0}
    try:
        record = parse_anime_page(fetch(url), url)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:500]
        conn.execute("UPDATE animes SET last_check_error=?, updated_at=? WHERE anime_key=?", (err, now, anime_key))
        conn.commit()
        logger.warning("[DISCOVERY] anime=%s check FAILED: %s", anime_key, err)
        raise DiscoveryError(err) from exc

    first_check = row["last_successful_check_at"] is None
    source_cfg = getattr(cfg, "source", None) or {}
    backfill_n = int(source_cfg.get("backfill_latest_on_first_check", 0) or 0)

    items: list[dict] = []
    for order, link in enumerate(reversed(record.episode_links)):     # pages list newest first
        if not link.url:
            continue
        ep_url = urljoin(url, link.url)
        items.append({"url": ep_url, "key": build_episode_key(ep_url),
                      "number": extract_episode_number_from_url(ep_url), "label": link.label, "order": order,
                      "released_raw": getattr(link, "published_at_raw", None)})
    items.sort(key=_episode_sort_key)

    newest_to_backfill = {it["key"] for it in items[-backfill_n:]} if (first_check and backfill_n > 0) else set()
    # CATCH-UP: at the first check, an episode released TODAY (since local midnight) is queued, not baselined,
    # even if its release time has passed.  Older ones (yesterday included) stay baseline; an unreadable date
    # counts as "not today".  Later checks are incremental anyway: only what the database does not know.
    catchup_on = first_check and bool(source_cfg.get("catchup_today", False))
    catchup_keys = {it["key"] for it in items if released_today(it["released_raw"])} if catchup_on else set()
    report["catchup"] = 0

    for it in items:
        known = repo.get_by_episode_key(conn, SOURCE_NAME, it["key"])
        if known is None:
            known = repo.get_by_canonical_url(conn, SOURCE_NAME, it["key"] + "/")
        if known is not None:
            report["known"] += 1
            continue
        ep = Episode(anime_key=anime_key, episode_key=it["key"], source=SOURCE_NAME,
                     canonical_episode_url=it["key"] + "/",
                     language=(detect_language(it["url"], row["language"]) or "unknown").lower(),
                     episode_number=it["number"], label=it["label"], episode_url=it["url"], status="discovered")
        try:
            eid, is_new = repo.upsert_episode(conn, ep)
        except sqlite3.IntegrityError:
            report["skipped"] += 1                    # canonical URL already owned by another row
            continue
        if not is_new:
            report["known"] += 1
            continue
        is_catchup = it["key"] in catchup_keys
        if first_check and it["key"] not in newest_to_backfill and not is_catchup:
            report["baseline"] += 1                   # known from now on, never queued
            continue
        repo.transition(conn, eid, "identified")
        repo.transition(conn, eid, "queued")
        QueueManager(conn).enqueue(anime_key, eid)
        report["new"].append({"episode_id": eid, "episode_number": it["number"], "url_key": it["key"],
                              "catchup": is_catchup})
        report["catchup"] += 1 if is_catchup else 0
        logger.info("[QUEUE] anime=%s episode=%s job=%s%s", anime_key, it["number"], eid,
                    " (rattrapage du jour)" if is_catchup else "")
        if alert is not None:
            try:
                alert(alerts.KIND_NEW_EPISODE, f"ep:{eid}",
                      f"Nouvel épisode : {row['title'] or anime_key} E{it['number'] if it['number'] is not None else '?'}",
                      ("sorti aujourd'hui, mis en file (rattrapage du jour)" if is_catchup else "détecté et mis en file")
                      + (" — nouvel anime détecté sur le site" if _auto_added(conn, anime_key) else ""))
            except Exception as exc:                  # a notification problem never blocks discovery
                logger.warning("alerte nouvel épisode impossible: %s", exc)

    conn.execute("UPDATE animes SET last_successful_check_at=?, last_check_error=NULL, "
                 "title=COALESCE(NULLIF(?, ''), title), updated_at=? WHERE anime_key=?",
                 (now, (record.title or "").strip(), now, anime_key))
    conn.commit()
    report["discovered_count"] = report["known"] + report["baseline"] + report["skipped"] + len(report["new"])
    mode = ("bootstrap+catchup" if catchup_on else "bootstrap") if first_check else "incremental"
    logger.info("[DISCOVERY] anime=%s discovered_count=%d new_count=%d (mode=%s) new=%d baseline=%d known=%d catchup=%d",
                anime_key, report["discovered_count"], len(report["new"]), mode,
                len(report["new"]), report["baseline"], report["known"], report["catchup"])
    return report


# ── the global cycle ─────────────────────────────────────────────────────────────

CYCLE_KEY = "watcher:last_cycle_at"
LAST_CYCLE_KEY = "watcher:last_cycle"
CYCLES_KEY = "watcher:cycles"
CYCLES_KEPT = 20
FEED_TOKEN = "__site_feed__"


def claim_cycle(conn: sqlite3.Connection, interval_s: float, *, now: str | None = None) -> bool:
    """Atomically decide that a new global cycle starts now (once per interval, across processes)."""
    now = now or now_utc()
    cur = conn.execute(
        "INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(ckey) DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at "
        "WHERE control.cvalue <= ?", (CYCLE_KEY, now, now, add_seconds(now, -interval_s)))
    conn.commit()
    return cur.rowcount == 1


def active_animes(conn: sqlite3.Connection) -> list[str]:
    """Every anime the watcher must visit: enabled and with a source page."""
    return [r["anime_key"] for r in conn.execute(
        "SELECT anime_key FROM animes WHERE enabled = 1 AND source_url IS NOT NULL AND source_url <> '' "
        "ORDER BY anime_key")]


def recent_cycles(conn: sqlite3.Connection, n: int = CYCLES_KEPT) -> list[dict[str, Any]]:
    """The last finished cycles, newest first (what the panels show)."""
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (CYCLES_KEY,)).fetchone()
    try:
        items = json.loads(row["cvalue"]) if row else []
    except ValueError:
        items = []
    return list(reversed(items))[:n]


def last_cycle(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (LAST_CYCLE_KEY,)).fetchone()
    try:
        return json.loads(row["cvalue"]) if row else None
    except ValueError:
        return None


# ── the scheduler ────────────────────────────────────────────────────────────────

class DiscoveryScheduler:
    """Runs `tick()` from the worker loop: every due anime is claimed (atomically) and checked on a
    small thread pool so a slow source never blocks downloads.  One check per anime at a time."""

    def __init__(self, conn: sqlite3.Connection, cfg, *, fetch: Callable[[str], str] | None = None,
                 interval_s: float | None = None, alert: Callable[..., Any] | None = None,
                 max_parallel_checks: int = 2):
        self.conn, self.cfg = conn, cfg
        self.fetch = fetch or default_fetch(cfg)
        self.interval_s = interval_s if interval_s is not None else poll_interval_s(cfg)
        self.alert = alert
        self._pool = ThreadPoolExecutor(max_workers=max_parallel_checks, thread_name_prefix="discovery")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self.results: list[dict[str, Any]] = []
        self.cycles: list[dict[str, Any]] = []        # finished global cycles (newest last)
        self._cycle: dict[str, Any] | None = None

    def tick(self) -> list[str]:
        """Start what is due; returns the anime keys started now.  When the interval elapsed a GLOBAL cycle
        starts and visits every active anime; otherwise only the individually due ones (a new anime,
        a forced check) are visited."""
        started: list[str] = []
        with self._lock:
            cycle_running = self._cycle is not None
        if not cycle_running and claim_cycle(self.conn, self.interval_s):
            keys = active_animes(self.conn)
            logger.info("[WATCHER] cycle_started")
            logger.info("[WATCHER] anime_count=%d", len(keys))
            feed_on = bool((getattr(self.cfg, "source", None) or {}).get("site_feed_enabled", False))
            with self._lock:
                self._cycle = {"started_at": now_utc(), "anime_count": len(keys), "pending": set(), "checked": 0,
                               "new_episodes": 0, "jobs_created": 0, "errors": 0, "animes": {}, "feed": None}
                if feed_on:
                    self._cycle["pending"].add(FEED_TOKEN)      # the cycle is not finished before the feed is read
            if feed_on:
                self._pool.submit(self._run_feed)
            for key in keys:
                self._start(key, started, interval_s=0, in_cycle=True)
            self._maybe_finish_cycle()
        for key in due_animes(self.conn, self.interval_s):
            self._start(key, started, interval_s=self.interval_s, in_cycle=False)
        return started

    def _start(self, key: str, started: list[str], *, interval_s: float, in_cycle: bool) -> None:
        with self._lock:
            if key in self._inflight:
                return                                # a check of this anime is already running: merged
            if not claim_check(self.conn, key, interval_s):
                return
            self._inflight.add(key)
            if in_cycle and self._cycle is not None:
                self._cycle["pending"].add(key)
        started.append(key)
        self._pool.submit(self._run, key, in_cycle)

    def _maybe_finish_cycle(self) -> None:
        with self._lock:
            c = self._cycle
            if c is None or c["pending"]:
                return
            self._cycle = None
        c["finished_at"] = now_utc()
        summary = {k: c[k] for k in ("started_at", "finished_at", "anime_count", "checked", "new_episodes",
                                     "jobs_created", "errors", "animes", "feed")}
        self.cycles.append(summary)
        logger.info("[WATCHER] cycle_finished checked_animes=%d new_episodes=%d jobs_created=%d errors=%d",
                    c["checked"], c["new_episodes"], c["jobs_created"], c["errors"])
        try:
            hist = list(reversed(recent_cycles(self.conn, CYCLES_KEPT)))[-(CYCLES_KEPT - 1):] + [summary]
            for key, value in ((LAST_CYCLE_KEY, summary), (CYCLES_KEY, hist)):
                self.conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) "
                                  "ON CONFLICT(ckey) DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at",
                                  (key, json.dumps(value), now_utc()))
            self.conn.commit()
        except Exception as exc:
            logger.warning("[WATCHER] résumé de cycle non enregistré: %s", exc)

    def check_now(self, key: str) -> dict[str, Any]:
        """Synchronous check (tests / admin force-check); still honours the no-overlap rule."""
        with self._lock:
            if key in self._inflight:
                return {"anime_key": key, "skipped": "already-running"}
            self._inflight.add(key)
        try:
            return self._check(key)
        finally:
            with self._lock:
                self._inflight.discard(key)

    def _run_feed(self) -> None:
        """Read the site's "latest episodes" feed once per cycle; anime we do not know yet are added and visited now."""
        from . import site_feed
        summary = None
        try:
            res = site_feed.scan(self.conn, self.cfg, self.fetch, alert=self.alert)
            summary = {k: v for k, v in res.items() if k not in ("new_anime_keys",)}
            for key in res["new_anime_keys"]:
                self._start(key, [], interval_s=0, in_cycle=True)     # bootstrap + catch-up of the day, right now
        except Exception as exc:
            logger.warning("[WATCHER] site_feed impossible: %s", exc)
            summary = {"error": str(exc)[:200]}
        finally:
            with self._lock:
                c = self._cycle
                if c is not None:
                    c["feed"] = summary
                    c["pending"].discard(FEED_TOKEN)
            self._maybe_finish_cycle()

    def _run(self, key: str, in_cycle: bool = False) -> None:
        logger.info("[WATCHER] checking anime=%s", key)
        res: dict[str, Any] = {}
        try:
            res = self._check(key)
        finally:
            with self._lock:
                self._inflight.discard(key)
                c = self._cycle
                if in_cycle and c is not None and key in c["pending"]:
                    c["pending"].discard(key)
                    c["checked"] += 1
                    n = len(res.get("new", []))
                    c["new_episodes"] += n
                    c["jobs_created"] += n
                    c["errors"] += 1 if res.get("error") else 0
                    c["animes"][key] = {"discovered": res.get("discovered_count"), "new": n,
                                        "catchup": res.get("catchup", 0), "error": res.get("error")}
            if in_cycle:
                self._maybe_finish_cycle()

    def _check(self, key: str) -> dict[str, Any]:
        try:
            res = check_anime(self.conn, self.cfg, key, self.fetch, alert=self.alert)
        except Exception as exc:
            res = {"anime_key": key, "error": str(exc)[:300]}
            if self.alert is not None:
                try:
                    self.alert(alerts.KIND_DISCOVERY_ERROR, f"anime:{key}", f"contrôle impossible — {key}", str(exc)[:500])
                except Exception:
                    pass
        self.results.append(res)
        return res

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
