"""Queue layer — strict intra-anime FIFO, cross-anime independence.

The SQLite `queue_items` table is the source of truth (survives restarts).
QueueManager adds a thread-safe in-memory view of the heads for the scheduler:

  - one queue per anime_key (position column, ascending),
  - only the head of an anime's queue is pop-able, so E01 always precedes E02,
  - multiple anime queues advance in parallel (independent ordering domains).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from . import repo


@dataclass
class QueueSnapshot:
    anime_key: str
    position: int
    episode_id: int
    episode_number: int | None
    status: str


class QueueManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    # ── persistence primitives (DB = source of truth) ──────────────────────────

    def enqueue(self, anime_key: str, episode_id: int) -> int:
        with self._lock:
            return repo.enqueue(self._conn, anime_key, episode_id)

    def dequeue_head_of(self, anime_key: str) -> int | None:
        """Atomically mark the head as processing and return its episode_id.
        Enforces the admin control state (paused / anime disabled) at the same
        single chokepoint as `repo.next_heads`."""
        with self._lock:
            row = self._conn.execute("""
                SELECT q.id, q.episode_id, q.position
                FROM queue_items q
                JOIN episodes e ON e.id = q.episode_id
                LEFT JOIN animes a ON a.anime_key = q.anime_key
                WHERE q.anime_key = ? AND q.status = 'queued'
                  AND e.status IN ('queued', 'retry_wait')
                  AND (a.enabled IS NULL OR a.enabled = 1)
                  AND NOT EXISTS (SELECT 1 FROM control WHERE ckey = 'paused' AND cvalue = '1')
                ORDER BY q.position ASC LIMIT 1""", (anime_key,)).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE queue_items SET status='processing' WHERE id=?",
                (row["id"],))
            self._conn.commit()
            return row["episode_id"]

    def dequeue_episode(self, episode_id: int) -> bool:
        """Mark ONE specific queued item as processing.  Safe under the single
        leased worker (exact-claim; strict FIFO already guaranteed by next_heads).
        Enforces the same paused/anime-enabled gates as every dequeue path."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue_items SET status='processing' "
                "WHERE episode_id=? AND status='queued' "
                "AND NOT EXISTS (SELECT 1 FROM control WHERE ckey='paused' AND cvalue='1') "
                "AND NOT EXISTS (SELECT 1 FROM animes a WHERE a.anime_key = "
                "(SELECT q3.anime_key FROM queue_items q3 WHERE q3.episode_id=?) "
                "AND a.enabled=0)",
                (episode_id, episode_id))
            self._conn.commit()
            return cur.rowcount > 0

    def unqueue(self, anime_key: str, episode_id: int, status: str = "queued") -> None:
        """Return an item to its queue (after retry_wait/failure) or drop it."""
        with self._lock:
            self._conn.execute(
                "UPDATE queue_items SET status=? WHERE anime_key=? AND episode_id=?",
                (status, anime_key, episode_id))
            self._conn.commit()

    def pending_by_anime(self) -> dict[str, list[dict]]:
        """Ordered per-anime pending lists (ascending position)."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT q.anime_key, q.position, q.episode_id, q.status
                FROM queue_items q
                JOIN episodes e ON e.id = q.episode_id
                WHERE q.status = 'queued'
                  AND e.status IN ('queued', 'retry_wait')
                ORDER BY q.anime_key, q.position""").fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["anime_key"], []).append(dict(r))
        return out

    def anime_count(self) -> int:
        return len(self.pending_by_anime())

    def busy_anime_count(self) -> int:
        """Anime with an episode waiting OR already running: what the dynamic limit must be sized on.  Counting only
        the waiting ones let a new episode detected during a download wait for it to finish (limit = 1)."""
        return self._conn.execute("SELECT COUNT(DISTINCT anime_key) FROM queue_items "
                                  "WHERE status IN ('queued', 'processing')").fetchone()[0]

    def depth(self) -> int:
        return repo.queue_depth(self._conn)

    # ── in-memory head set (callers mark via dequeue_head_of) ──────────────────

    def heads(self, limit: int) -> list[int]:
        """One head per anime, oldest enqueued anime first. Does not mutate."""
        by_anime = self.pending_by_anime()
        first_seen: list[tuple[int, int]] = []      # (min created, episode_id)
        for anime, items in by_anime.items():
            head = items[0]
            created = self._conn.execute(
                "SELECT created_at AS c FROM queue_items WHERE episode_id=? AND anime_key=?",
                (head["episode_id"], anime)).fetchone()["c"]
            first_seen.append((created, head["episode_id"]))
        first_seen.sort(key=lambda t: t[0])
        return [eid for _, eid in first_seen[:limit]]