"""Private notifications to the administrators (never to the public channel).

Four independent switches, stored in the existing `control` table (all ON by default):
  published    an episode was published
  new_episode  the watcher detected a new episode and queued it
  problems     failure, interrupted publication, low disk, worker stopped, source error …
  daily        one summary per day at a local hour

`filtered(conn, dispatch)` wraps the alert dispatcher so a switched-off group is never pushed (the alert
is still recorded).  Nothing here is a second alert system: it sits in front of `alerts.raise_alert`.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import alerts, service
from .timeutil import now_utc

logger = logging.getLogger(__name__)

KEYS = ("published", "new_episode", "problems", "daily")
DAILY_LAST_SENT = "notify:daily_last_sent"


def group_of(kind: str) -> str:
    if kind == alerts.KIND_PUBLISHED:
        return "published"
    if kind == alerts.KIND_NEW_EPISODE:
        return "new_episode"
    return "problems"


def enabled(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (f"notify:{key}",)).fetchone()
    return True if row is None else row["cvalue"] == "1"


def set_enabled(conn: sqlite3.Connection, key: str, on: bool) -> None:
    if key not in KEYS:
        raise ValueError(f"notification inconnue: {key}")
    conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(ckey) DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at",
                 (f"notify:{key}", "1" if on else "0", now_utc()))
    conn.commit()


def toggle(conn: sqlite3.Connection, key: str) -> bool:
    new = not enabled(conn, key)
    set_enabled(conn, key, new)
    return new


def filtered(conn: sqlite3.Connection, dispatch: Callable[[str, str, str], None] | None):
    """Dispatcher that honours the switches; None stays None (no admin configured)."""
    if dispatch is None:
        return None

    def _dispatch(kind: str, title: str, body: str = "") -> None:
        if enabled(conn, group_of(kind)):
            dispatch(kind, title, body)
    return _dispatch


# ── daily summary ────────────────────────────────────────────────────────────────

def daily_summary_text(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz=None) -> str:
    from . import admin_views as v
    now = now or now_utc()
    since = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    published = conn.execute(
        "SELECT e.episode_number, COALESCE(a.title, e.anime_key) AS t FROM episodes e "
        "LEFT JOIN animes a ON a.anime_key=e.anime_key WHERE e.published_at >= ? AND e.status IN "
        "('published','cleanup_pending','cleaned') ORDER BY e.published_at", (since,)).fetchall()
    d = service.dashboard(conn, cfg, now=now)
    lines = ["🗓 Résumé de la journée", ""]
    lines.append(f"✅ {len(published)} épisode(s) publié(s) sur 24 h" + (":" if published else ""))
    for r in published[:10]:
        lines.append(f" • {r['t']} E{r['episode_number'] if r['episode_number'] is not None else '?'}")
    lines.append(f"⏳ {len(d['waiting'])} en attente · 📥 {len(d['running'])} en cours")
    lines.append(f"⚠️ {len(d['attention'])} à traiter · {d['alerts_open']} alerte(s) ouverte(s)")
    if d.get("next_check"):
        lines.append(f"📡 Prochain contrôle {v.hm(d['next_check'], tz)}")
    disk = d["system"].get("disk_free_bytes")
    if disk is not None:
        lines.append(f"💾 Disque libre : {v.size_label(int(disk))}")
    return "\n".join(lines)


def maybe_send_daily(conn: sqlite3.Connection, cfg, send: Callable[[str], None] | None, *,
                     hour: int = 9, now: str | None = None, tz=None) -> bool:
    """Send the summary once per local day, from `hour` on; remembered in `control` so a restart
    never sends it twice.  Returns True when it was sent."""
    if send is None or not enabled(conn, "daily"):
        return False
    now = now or now_utc()
    local_now = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(tz)
    if local_now.hour < hour:
        return False
    today = local_now.strftime("%Y-%m-%d")
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (DAILY_LAST_SENT,)).fetchone()
    if row is not None and row["cvalue"] == today:
        return False
    try:
        send(daily_summary_text(conn, cfg, now=now, tz=tz))
    except Exception as exc:                      # a Telegram outage never breaks the worker; retried next loop
        logger.warning("résumé quotidien non envoyé: %s", exc)
        return False
    conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(ckey) DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at",
                 (DAILY_LAST_SENT, today, now))
    conn.commit()
    return True
