"""Operational alerts (closure) — DB-backed, deduplicated, throttle-aware.

Every significant event writes or updates a row in `alerts`, keyed by
(kind, akey), so repeated identical problems roll up into ONE row with a
count instead of flooding the bus.  Dispatch (Telegram push) happens only
for a NEW alert or when the last raise is older than `dispatch_every_s`.

Dispatch is injectable so unit tests never touch the network.  The default
factory sends to the configured channel via the proven V2TelegramClient and
swallows errors — an alert must never break the worker loop.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .timeutil import now_utc

logger = logging.getLogger(__name__)
_PUSH_LOCK = threading.Lock()

# canonical kinds (registry for docs + tests)
KIND_NEW_EPISODE = "new_episode"
KIND_DOWNLOAD_STARTED = "download_started"
KIND_DOWNLOAD_FINISHED = "download_finished"
KIND_PUBLISHED = "published"
KIND_RETRY = "retry"
KIND_DEFINITIVE_FAILURE = "definitive_failure"
KIND_TELEGRAM_ERROR = "telegram_error"
KIND_STRUCTURE_CHANGED = "structure_changed"
KIND_RECOVERY_AFTER_CRASH = "recovery_after_crash"
KIND_CLEANUP_BLOCKED = "cleanup_blocked"
KIND_LOW_DISK = "low_disk"
KIND_WORKER_STOPPED = "worker_stopped"
KIND_DISCOVERY_ERROR = "discovery_error"
KIND_SCHEDULER_PROBLEM = "scheduler_problem"

KINDS = {
    KIND_NEW_EPISODE, KIND_DOWNLOAD_STARTED, KIND_DOWNLOAD_FINISHED, KIND_PUBLISHED,
    KIND_RETRY, KIND_DEFINITIVE_FAILURE, KIND_TELEGRAM_ERROR, KIND_STRUCTURE_CHANGED,
    KIND_RECOVERY_AFTER_CRASH, KIND_CLEANUP_BLOCKED, KIND_LOW_DISK, KIND_WORKER_STOPPED,
    KIND_DISCOVERY_ERROR, KIND_SCHEDULER_PROBLEM,
}

# informational kinds are pushed (if enabled) but never stay "open": they are not something to handle
INFO_KINDS = {KIND_NEW_EPISODE, KIND_PUBLISHED, KIND_DOWNLOAD_STARTED, KIND_DOWNLOAD_FINISHED}
# problems that stop being true once the episode is finally published
RESOLVED_BY_PUBLICATION = (KIND_RETRY, KIND_DEFINITIVE_FAILURE, KIND_RECOVERY_AFTER_CRASH,
                           KIND_STRUCTURE_CHANGED, KIND_TELEGRAM_ERROR)
_PUSH_ICON = {KIND_PUBLISHED: "✅", KIND_NEW_EPISODE: "🆕"}

DEFAULT_DISPATCH_EVERY_S = 3600.0  # never push the same (kind, akey) more than hourly


# ── dispatch ────────────────────────────────────────────────────────────────────

def default_dispatcher(cfg):
    """Build a dispatch callable that pushes alerts to the ADMINISTRATORS' private chats
    (`ADMIN_TELEGRAM_IDS`) — never to the public content channel.

    Returns None when there is no token or no administrator configured: alerts then stay
    DB-only (visible in the web panel and via /alerts).  The returned callable NEVER raises —
    alerts are fire-and-forget."""
    admins = list(getattr(cfg, "admin_telegram_ids", None) or [])
    token = cfg.notify_token()
    if not token or not admins:
        return None
    from .publisher import V2TelegramClient, local_bot_base_url

    base = cfg.telegram.get("api_base_url") or ""
    clients = [V2TelegramClient(
        token, str(admin_id),
        local_bot_base_url(base, token) if base else None,
        connect_timeout=15, read_timeout=30, write_timeout=30) for admin_id in admins]

    def _dispatch(kind: str, title: str, body: str = "") -> None:
        with _PUSH_LOCK:            # the Telegram client owns ONE event loop: two threads at once -> "loop is already running"
            for client in clients:
                try:
                    client.send_message(f"{_PUSH_ICON.get(kind, '⚠️')} {title}\n{body[:1500]}")
                except Exception as exc:  # outages must never crash the worker
                    logger.warning("alerte non envoyee (%s): %s", kind, exc)

    return _dispatch


# ── core ────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return now_utc()


def _iso_diff_seconds(a: str, b: str) -> float:
    def p(s: str) -> datetime:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromisoformat(s)
    return (p(b) - p(a)).total_seconds()


def raise_alert(conn: sqlite3.Connection, kind: str, akey: str, title: str,
                body: str = "", *, now: str | None = None,
                dispatch_every_s: float = DEFAULT_DISPATCH_EVERY_S,
                dispatch: Callable[[str, str, str], None] | None = None) -> dict[str, Any]:
    """Record an alert, deduplicated on (kind, akey); returns
    {"new": bool, "dispatch": bool}.  `dispatch` is invoked when a push is due."""
    now = now or _now()
    row = conn.execute(
        "SELECT id, last_raised_at FROM alerts WHERE kind=? AND akey=?",
        (kind, akey)).fetchone()
    is_new = row is None
    if is_new:
        conn.execute(
            "INSERT INTO alerts(kind, akey, title, body, status, raised_at, "
            "last_raised_at, count) VALUES (?,?,?,?, ?, ?, ?, 1)",
            (kind, akey, title, body[:2000], "ack" if kind in INFO_KINDS else "open", now, now))
        should_dispatch = True
    else:
        due = _iso_diff_seconds(row["last_raised_at"], now) >= dispatch_every_s
        conn.execute(
            "UPDATE alerts SET title=?, body=?, status=?, last_raised_at=?, "
            "count=count+1, updated_at=? WHERE id=?",
            (title, body[:2000], "ack" if kind in INFO_KINDS else "open", now, now, row["id"]))
        should_dispatch = is_new or due
    if should_dispatch and dispatch is not None:
        try:
            dispatch(kind, title, body)
        except Exception as exc:  # a broken dispatcher must not corrupt the alert row
            logger.warning("dispatch alerte echoue (%s): %s", kind, exc)
            should_dispatch = False
    return {"new": is_new, "dispatch": should_dispatch}


def list_alerts(conn: sqlite3.Connection, limit: int = 50,
                status: str | None = "open") -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    if status:
        rows = conn.execute(
            "SELECT id, kind, akey, title, body, status, count, raised_at, "
            "last_raised_at FROM alerts WHERE status=? ORDER BY last_raised_at DESC "
            "LIMIT ?", (status, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, akey, title, body, status, count, raised_at, "
            "last_raised_at FROM alerts ORDER BY last_raised_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE status='open'").fetchone()["n"]


def ack(conn: sqlite3.Connection, alert_id: int) -> bool:
    cur = conn.execute("UPDATE alerts SET status='ack', updated_at=? WHERE id=? AND status='open'",
                       (_now(), alert_id))
    return cur.rowcount > 0


def clear_closed(conn: sqlite3.Connection) -> int:
    """Force-ack everything still open (operator: 'j'ai traité')."""
    cur = conn.execute("UPDATE alerts SET status='ack', updated_at=? WHERE status='open'",
                       (_now(),))
    return cur.rowcount

# ── alerts that close themselves ─────────────────────────────────────────────────

def resolve_for_episode(conn: sqlite3.Connection, episode_id: int) -> int:
    """The episode is published: its retry / failure / interruption alerts are no longer true."""
    marks = ",".join("?" * len(RESOLVED_BY_PUBLICATION))
    cur = conn.execute(f"UPDATE alerts SET status='ack', updated_at=? WHERE status='open' AND akey=? "
                       f"AND kind IN ({marks})", (_now(), f"ep:{episode_id}", *RESOLVED_BY_PUBLICATION))
    return cur.rowcount


def raise_persistent_errors(conn: sqlite3.Connection, *, minutes: float = 20.0, retry_window_hours: float = 24.0,
                            dispatch: Callable[[str, str, str], None] | None = None,
                            now: str | None = None) -> int:
    """An episode whose video stays inaccessible (404, extraction failure, download error ...) retries alone and
    silently; once the SAME problem has lasted `minutes`, ONE alert is raised (Telegram push + panel).  An episode
    that merely waits for its official release (NOT_AVAILABLE_YET) is not a problem and is never signalled.
    The first failure time is the start of the 24 h retry window (retry_until_at - window).  One alert per
    episode, ever: an acknowledged alert is not raised again."""
    now = now or _now()
    rows = conn.execute("""
        SELECT e.id, e.episode_number, e.retry_until_at, e.last_error, COALESCE(NULLIF(a.title, ''), e.anime_key) AS name
        FROM episodes e LEFT JOIN animes a ON a.anime_key = e.anime_key
        WHERE e.status = 'retry_wait' AND e.retry_until_at IS NOT NULL
          AND COALESCE(e.last_error, '') NOT LIKE 'NOT_AVAILABLE_YET%'
          AND NOT EXISTS (SELECT 1 FROM alerts al WHERE al.kind='retry' AND al.akey = 'ep:' || e.id)""").fetchall()
    raised = 0
    for r in rows:
        try:
            first_failure = datetime.fromisoformat(r["retry_until_at"].replace("Z", "+00:00"))
            if first_failure.tzinfo is None:
                first_failure = first_failure.replace(tzinfo=timezone.utc)
            first_failure -= timedelta(hours=retry_window_hours)
            elapsed_min = (datetime.fromisoformat(now.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
                           - first_failure).total_seconds() / 60
        except ValueError:
            continue
        if elapsed_min < minutes:
            continue
        ep = f"E{r['episode_number']}" if r["episode_number"] is not None else "E?"
        raise_alert(conn, KIND_RETRY, f"ep:{r['id']}", f"Vidéo inaccessible depuis {round(elapsed_min)} min — {r['name']} {ep}",
                    f"Nouvelles tentatives automatiques en cours, rien n'est publié. Dernière erreur : "
                    f"{re.sub(r'https?://[^ ]+', '[lien]', str(r['last_error'] or ''))[:300]}", dispatch=dispatch)
        raised += 1
    conn.commit()
    return raised


def sweep_stale(conn: sqlite3.Connection) -> int:
    """Close open alerts that no longer describe reality: episodes published since, a wait for the
    source that is not a problem, informational alerts left open by older versions."""
    now = _now()
    n = 0
    marks = ",".join("?" * len(RESOLVED_BY_PUBLICATION))
    n += conn.execute(f"""
        UPDATE alerts SET status='ack', updated_at=? WHERE status='open' AND kind IN ({marks})
          AND akey LIKE 'ep:%' AND CAST(substr(akey, 4) AS INTEGER) IN
              (SELECT id FROM episodes WHERE status IN ('published', 'cleanup_pending', 'cleaned'))""",
                      (now, *RESOLVED_BY_PUBLICATION)).rowcount
    n += conn.execute("""
        UPDATE alerts SET status='ack', updated_at=? WHERE status='open' AND kind='retry' AND akey LIKE 'ep:%'
          AND CAST(substr(akey, 4) AS INTEGER) IN
              (SELECT id FROM episodes WHERE last_error LIKE 'NOT_AVAILABLE_YET%')""", (now,)).rowcount
    info = ",".join("?" * len(INFO_KINDS))
    n += conn.execute(f"UPDATE alerts SET status='ack', updated_at=? WHERE status='open' AND kind IN ({info})",
                      (now, *sorted(INFO_KINDS))).rowcount
    return n


def default_sender(cfg):
    """`send(text)` to the administrators' private chats (plain text, never the public channel);
    None when there is no token or no administrator."""
    admins = list(getattr(cfg, "admin_telegram_ids", None) or [])
    token = cfg.notify_token()
    if not token or not admins:
        return None
    from .publisher import V2TelegramClient, local_bot_base_url
    base = cfg.telegram.get("api_base_url") or ""
    clients = [V2TelegramClient(token, str(a), local_bot_base_url(base, token) if base else None,
                                connect_timeout=15, read_timeout=30, write_timeout=30) for a in admins]

    def _send(text: str) -> None:
        with _PUSH_LOCK:
            for client in clients:
                client.send_message(text)
    return _send
