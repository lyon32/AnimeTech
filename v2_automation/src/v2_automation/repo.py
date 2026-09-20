"""Repositories over the SQLite schema (CRUD + dedup + queue primitives)."""
from __future__ import annotations

import sqlite3
from typing import Any

from .models import Episode
from .schema import SCHEMA_VERSION
from .timeutil import now_utc

_EPISODE_COLS = ("anime_key", "episode_key", "source", "canonical_episode_url", "language",
                 "season", "episode_number", "label", "episode_url", "status", "queued_at",
                 "first_seen_at", "retry_until_at", "retry_count", "last_error",
                 "last_error_at", "media_hash", "file_size", "file_path", "thumbnail_path",
                 "thumbnail_sha256", "thumbnail_size", "thumbnail_message_id", "video_sha256",
                 "video_message_id", "published_at", "cleanup_at", "attempt_count",
                 "first_attempt_at", "last_attempt_at", "next_retry_at",
                 "created_at", "updated_at")


def _row_to_episode(row: sqlite3.Row) -> Episode:
    d = dict(row)
    return Episode(id=d.pop("id"), **d)


def upsert_episode(conn: sqlite3.Connection, ep: Episode) -> tuple[int, bool]:
    """Insert or update by (source, episode_key). Returns (episode_id, is_new).

    Raises sqlite3.IntegrityError if canonical URL conflicts with another row."""
    now = now_utc()
    existing = get_by_episode_key(conn, ep.source, ep.episode_key)
    if existing is not None:
        # metadata-only update: state changes go through transition() exclusively,
        # so the DB's current status is preserved (never a stale in-memory copy).
        cur = conn.execute("""
            UPDATE episodes SET anime_key=?, canonical_episode_url=?, language=?, season=?,
                   episode_number=?, label=?, episode_url=?, status=?, queued_at=?,
                   first_seen_at=?, retry_until_at=?, retry_count=?, last_error=?,
                   last_error_at=?, media_hash=?, file_size=?, file_path=?, thumbnail_path=?,
                   thumbnail_sha256=?, thumbnail_size=?, thumbnail_message_id=?,
                   video_sha256=?, video_message_id=?, published_at=?, cleanup_at=?,
                   attempt_count=?, first_attempt_at=?, last_attempt_at=?, next_retry_at=?,
                   updated_at=?
            WHERE id=?""",
            (ep.anime_key, ep.canonical_episode_url, ep.language, ep.season,
             ep.episode_number, ep.label, ep.episode_url, existing.status, ep.queued_at,
             ep.first_seen_at, ep.retry_until_at, ep.retry_count, ep.last_error,
             ep.last_error_at, ep.media_hash, ep.file_size, ep.file_path, ep.thumbnail_path,
             ep.thumbnail_sha256, ep.thumbnail_size, ep.thumbnail_message_id,
             ep.video_sha256, ep.video_message_id, ep.published_at or existing.published_at,
             ep.cleanup_at or existing.cleanup_at, ep.attempt_count or existing.attempt_count,
             ep.first_attempt_at or existing.first_attempt_at,
             ep.last_attempt_at or existing.last_attempt_at,
             ep.next_retry_at or existing.next_retry_at, now, existing.id))
        return existing.id, False
    cur = conn.execute(f"""
        INSERT INTO episodes ({", ".join(_EPISODE_COLS)})
        VALUES ({", ".join("?" * len(_EPISODE_COLS))})""",
        (ep.anime_key, ep.episode_key, ep.source, ep.canonical_episode_url, ep.language,
         ep.season, ep.episode_number, ep.label, ep.episode_url, ep.status, ep.queued_at,
         ep.first_seen_at, ep.retry_until_at, ep.retry_count, ep.last_error,
         ep.last_error_at, ep.media_hash, ep.file_size, ep.file_path, ep.thumbnail_path,
         ep.thumbnail_sha256, ep.thumbnail_size, ep.thumbnail_message_id, ep.video_sha256,
         ep.video_message_id, ep.published_at, ep.cleanup_at, ep.attempt_count,
         ep.first_attempt_at, ep.last_attempt_at, ep.next_retry_at,
         ep.created_at, ep.updated_at))
    return cur.lastrowid, True


def get_by_episode_key(conn: sqlite3.Connection, source: str, episode_key: str) -> Episode | None:
    row = conn.execute(
        "SELECT * FROM episodes WHERE source=? AND episode_key=?", (source, episode_key)).fetchone()
    return _row_to_episode(row) if row else None


def get_by_canonical_url(conn: sqlite3.Connection, source: str, url: str) -> Episode | None:
    row = conn.execute(
        "SELECT * FROM episodes WHERE source=? AND canonical_episode_url=?",
        (source, url)).fetchone()
    return _row_to_episode(row) if row else None


def get(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return _row_to_episode(row) if row else None


def transition(conn: sqlite3.Connection, episode_id: int, new_status: str) -> bool:
    """Write a state transition to an episode; returns success."""
    from .states import State, can_transition
    ep = get(conn, episode_id)
    if ep is None:
        return False
    current = State(ep.status)
    target = State(new_status)
    if not current == target and not can_transition(current, target):
        raise ValueError(f"interdit: {current.value} -> {target.value}")
    conn.execute(
        "UPDATE episodes SET status=?, updated_at=? WHERE id=?",
        (new_status, _now_utc(), episode_id))
    return True


def set_retry_until(conn: sqlite3.Connection, episode_id: int, until_iso: str, retry_count: int = 0,
                    error: str | None = None, next_retry_at: str | None = None) -> None:
    conn.execute(
        "UPDATE episodes SET retry_until_at=?, retry_count=?, last_error=?, last_error_at=?, "
        "next_retry_at=COALESCE(?, next_retry_at), updated_at=? WHERE id=?",
        (until_iso, retry_count, error, _now_utc(), next_retry_at, _now_utc(), episode_id))


def bump_attempt(conn: sqlite3.Connection, episode_id: int) -> None:
    """Record one processing attempt (retry observability)."""
    now = _now_utc()
    conn.execute(
        "UPDATE episodes SET attempt_count=attempt_count+1, "
        "first_attempt_at=COALESCE(first_attempt_at, ?), last_attempt_at=?, "
        "updated_at=? WHERE id=?",
        (now, now, now, episode_id))


def set_cleanup_at(conn: sqlite3.Connection, episode_id: int, iso: str) -> None:
    conn.execute("UPDATE episodes SET cleanup_at=?, updated_at=? WHERE id=?",
                 (iso, _now_utc(), episode_id))


def mark_failed(conn: sqlite3.Connection, episode_id: int, error: str) -> None:
    transition(conn, episode_id, "failed")
    conn.execute(
        "UPDATE episodes SET last_error=?, last_error_at=?, updated_at=? WHERE id=?",
        (error[:2048], _now_utc(), _now_utc(), episode_id))


def commit_publication(conn: sqlite3.Connection, episode_id: int, publication_type: str,
                       chat_id: str, message_id: int, media_kind: str,
                       media_sha256: str | None, file_size: int | None) -> None:
    conn.execute("""
        INSERT INTO publications (episode_id, publication_type, status, chat_id, message_id,
                                  media_kind, media_sha256, file_size, attempted_at)
        VALUES (?, ?, 'sent', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(episode_id, publication_type) DO UPDATE SET
            status='sent', message_id=excluded.message_id, media_kind=excluded.media_kind,
            media_sha256=excluded.media_sha256, file_size=excluded.file_size,
            attempted_at=excluded.attempted_at, updated_at=datetime('now')
    """, (episode_id, publication_type, chat_id, message_id, media_kind, media_sha256,
          file_size, _now_utc()))
    conn.execute(
        "UPDATE episodes SET updated_at=? WHERE id=?", (_now_utc(), episode_id))


def enqueue(conn: sqlite3.Connection, anime_key: str, episode_id: int) -> int:
    """Enqueue; FIFO position = max(existing)+1 for that anime. Returns the
    episode's position (existing position if already queued)."""
    existing = conn.execute(
        "SELECT position FROM queue_items WHERE anime_key=? AND episode_id=?",
        (anime_key, episode_id)).fetchone()
    if existing is not None:
        return existing["position"]
    row = conn.execute(
        "SELECT position FROM queue_items WHERE anime_key=? ORDER BY position DESC LIMIT 1",
        (anime_key,)).fetchone()
    position = (row["position"] + 1) if row else 1
    conn.execute(
        "INSERT INTO queue_items (anime_key, episode_id, position) VALUES (?, ?, ?)",
        (anime_key, episode_id, position))
    return position


def next_heads(conn: sqlite3.Connection, limit: int, *, now: str | None = None) -> list[int]:
    """Episodes that may start now: at most ONE per anime, strictly in queue order.

    The head of an anime is its lowest-numbered episode that is still active (queued OR being
    processed; queue position breaks ties), so an older episode detected late (E36) still goes before
    one that is waiting for its source (E49).  It is startable only if it is itself queued and eligible (retry time reached,
    anime enabled, not paused).  So E02 never overtakes E01 — not while E01 is being processed and
    not while E01 waits for its retry — while other animes advance in parallel."""
    now = now or now_utc()
    rows = conn.execute("""
        SELECT q.episode_id
        FROM queue_items q
        JOIN episodes e ON e.id = q.episode_id
        LEFT JOIN animes a ON a.anime_key = q.anime_key
        WHERE q.status = 'queued'
          AND e.status IN ('queued', 'retry_wait')
          AND (e.next_retry_at IS NULL OR e.next_retry_at <= ?)
          AND (a.enabled IS NULL OR a.enabled = 1)
          AND NOT EXISTS (SELECT 1 FROM control WHERE ckey = 'paused' AND cvalue = '1')
          AND q.id = (SELECT q2.id FROM queue_items q2 JOIN episodes e2 ON e2.id = q2.episode_id
                      WHERE q2.anime_key = q.anime_key AND q2.status IN ('queued', 'processing')
                      ORDER BY COALESCE(e2.episode_number, 999999), q2.position LIMIT 1)
        ORDER BY q.created_at ASC, q.id ASC
        LIMIT ?
    """, (now, limit)).fetchall()
    return [r["episode_id"] for r in rows]


def release_queue_item(conn: sqlite3.Connection, episode_id: int) -> None:
    """Remove an episode's queue item once the episode reached an end state, so the next episode of
    the same anime becomes the head."""
    conn.execute("DELETE FROM queue_items WHERE episode_id=?", (episode_id,))


def _now_utc() -> str:
    return now_utc()


# ── diagnostics ──────────────────────────────────────────────────────────────────────────────────────

def queue_depth(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM queue_items WHERE status='queued'").fetchone()["n"]


def queue_by_anime(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT q.anime_key, COUNT(*) AS queued
        FROM queue_items q
        WHERE q.status='queued'
        GROUP BY q.anime_key ORDER BY q.anime_key""").fetchall()
    return [dict(r) for r in rows]


def errors_recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT id, episode_url, status, last_error, last_error_at, retry_count, updated_at
        FROM episodes
        WHERE status IN ('failed', 'retry_wait', 'structure_changed') AND last_error IS NOT NULL
        ORDER BY last_error_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def history(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT id, anime_key, episode_number, language, status, video_message_id,
               thumbnail_message_id, published_at, updated_at
        FROM episodes ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def save_capacity(conn: sqlite3.Connection, payload: dict) -> None:
    now = _now_utc()
    for key, value in payload.items():
        conn.execute(
            "INSERT OR REPLACE INTO bot_capacity(key, value, measured_at) VALUES (?, ?, ?)",
            (key, str(value) if value is not None else "", now))


def load_capacity(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM bot_capacity").fetchall()
    return {r["key"]: r["value"] for r in rows}