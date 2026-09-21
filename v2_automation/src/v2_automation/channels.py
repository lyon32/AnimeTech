"""Multi-channel publication.

EXISTING BEHAVIOR  the download engine publishes thumbnail + video to ONE channel (`TELEGRAM_CHANNEL_ID`).
DESIRED BEHAVIOR   any number of configured channels (`telegram.channels` / TELEGRAM_CHANNELS), none hard-coded, and a
                   failing branch (a channel, or a private delivery) never blocks another.
CHANGE             the proven primary publication is untouched; every additional channel receives the SAME messages by
                   `copyMessage` from the primary one (no second upload, no second download), thumbnail first then video,
                   recorded per channel in `publications` (`copy_thumbnail:<chat>` / `copy_video:<chat>`).

Idempotence without a way to read a channel back: the row is written `sending` BEFORE the copy and `sent` right after;
a `sending` row found at boot is `uncertain` and is never copied again automatically (an operator decides).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from .telegram_publisher import TelegramPublisher, scrub
from .timeutil import now_utc

logger = logging.getLogger(__name__)


def extra_channels(cfg) -> list[str]:
    """Configured channels other than the primary one (the primary is published by the download engine)."""
    primary = str(cfg.channel_id or "").strip()
    return [c for c in (cfg.channels or []) if str(c).strip() and str(c).strip() != primary]


def _row(conn, eid: int, ptype: str):
    return conn.execute("SELECT status, message_id FROM publications WHERE episode_id=? AND publication_type=?",
                        (eid, ptype)).fetchone()


def _claim(conn, eid: int, ptype: str, chat: str, now: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO publications (episode_id, publication_type, status, chat_id, attempted_at, "
                       "attempted_count) VALUES (?,?, 'sending', ?, ?, 1)", (eid, ptype, chat, now))
    conn.commit()
    return cur.rowcount == 1


def _finish(conn, eid, ptype, message_id, now, *, error=None, status="sent"):
    conn.execute("UPDATE publications SET status=?, message_id=?, last_error=?, updated_at=? "
                 "WHERE episode_id=? AND publication_type=?", (status, message_id, error, now, eid, ptype))
    conn.commit()


def fanout(conn: sqlite3.Connection, publisher: TelegramPublisher, cfg, *, now: Callable[[], str] = now_utc,
           limit: int = 20) -> dict[str, Any]:
    """Copy every channel-published media to the additional channels that do not have it yet."""
    out: dict[str, Any] = {"copied": 0, "failed": 0, "skipped": 0}
    extras = extra_channels(cfg)
    if not extras:
        return out
    rows = conn.execute(
        "SELECT e.id, e.video_message_id, e.thumbnail_message_id, p.chat_id FROM episodes e "
        "JOIN publications p ON p.episode_id=e.id AND p.publication_type='first_publication' AND p.status='sent' "
        "WHERE e.publish_channel=1 AND e.video_message_id IS NOT NULL ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
    for r in rows:
        for chan in extras:
            for kind, src in (("thumbnail", r["thumbnail_message_id"]), ("video", r["video_message_id"])):
                if src is None:
                    continue
                ptype = f"copy_{kind}:{chan}"
                if _row(conn, r["id"], ptype) is not None:
                    out["skipped"] += 1                        # sent, or uncertain: never copied twice
                    continue
                ts = now()
                if not _claim(conn, r["id"], ptype, chan, ts):
                    continue
                try:
                    sent = publisher.copy(chan, r["chat_id"], src)
                except Exception as exc:                        # this channel only: the others carry on
                    conn.execute("DELETE FROM publications WHERE episode_id=? AND publication_type=?", (r["id"], ptype))
                    conn.commit()
                    out["failed"] += 1
                    logger.warning("[CHANNELS] media=%s channel=%s %s impossible: %s", r["id"], chan, kind, scrub(exc))
                    break                                       # do not send the video of a channel whose thumbnail failed
                _finish(conn, r["id"], ptype, sent.message_id, now())
                out["copied"] += 1
                logger.info("[CHANNELS] media=%s channel=%s %s copied message=%s", r["id"], chan, kind, sent.message_id)
    return out


def recover_fanout(conn: sqlite3.Connection, *, now: str | None = None) -> list[int]:
    """Boot: a copy left `sending` may already be live in the channel -> `uncertain`, never repeated automatically."""
    now = now or now_utc()
    rows = conn.execute("SELECT episode_id FROM publications WHERE status='sending' AND publication_type LIKE 'copy\\_%' "
                        "ESCAPE '\\'").fetchall()
    conn.execute("UPDATE publications SET status='uncertain', updated_at=?, last_error='arrêt pendant la copie : résultat "
                 "inconnu, pas de nouvelle copie automatique' WHERE status='sending' AND publication_type LIKE 'copy\\_%' "
                 "ESCAPE '\\'", (now,))
    conn.commit()
    return [r["episode_id"] for r in rows]
