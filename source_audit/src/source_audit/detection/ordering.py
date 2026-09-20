"""Per-anime processing order, independent across anime (Phase 14).

MASTER_PLAN.md §25 requires that concurrent processing of multiple anime never
mixes their episodes and that each anime's own episodes stay in order (Anime A:
E120 -> E121 in order, independently of whatever Anime B is doing at the same
time). This module is pure ordering logic — it does not implement real
concurrency/threading (out of scope, `source_audit` doesn't build the pipeline
itself, §51) — it validates that the *queueing rule* can be enforced correctly
given interleaved arrival, using the `anime_key`/`episode_key` identity already
built in Phase 8.

The rule enforced: for a given `anime_key`, episodes must be processed in the
order they were added to that anime's own queue (FIFO **per anime_key**) — an
episode from anime A can be processed at any time relative to anime B's episodes
(no cross-anime ordering constraint), but never out of order relative to another
episode of the *same* anime_key.
"""
from __future__ import annotations

from collections import deque


class OutOfOrderError(Exception):
    """Raised when an attempt is made to process an episode before an earlier
    episode of the same anime_key that is still queued ahead of it."""


class PerAnimeQueue:
    def __init__(self) -> None:
        self._queues: dict[str, deque[str]] = {}
        self._anime_of: dict[str, str] = {}
        self._processed: list[str] = []

    def add(self, anime_key: str, episode_key: str) -> None:
        """Adds an episode to the back of its anime's own queue. Safe to call
        with episodes from different anime interleaved in any order -- each
        anime_key gets its own independent queue."""
        if episode_key in self._anime_of:
            return  # already queued (or processed) -- Phase 10-style no-op on redundant add
        self._queues.setdefault(anime_key, deque()).append(episode_key)
        self._anime_of[episode_key] = anime_key

    def can_process(self, episode_key: str) -> bool:
        anime_key = self._anime_of.get(episode_key)
        if anime_key is None:
            return False
        queue = self._queues[anime_key]
        return bool(queue) and queue[0] == episode_key

    def process(self, episode_key: str) -> None:
        """Processes (dequeues) an episode. Raises OutOfOrderError if this
        episode is not at the front of its own anime's queue -- i.e. an earlier
        episode of the same anime hasn't been processed yet."""
        if not self.can_process(episode_key):
            anime_key = self._anime_of.get(episode_key)
            raise OutOfOrderError(
                f"{episode_key!r} is not next in queue for anime_key={anime_key!r}"
            )
        anime_key = self._anime_of[episode_key]
        self._queues[anime_key].popleft()
        self._processed.append(episode_key)

    def processed_order(self) -> list[str]:
        return list(self._processed)

    def pending_for(self, anime_key: str) -> list[str]:
        return list(self._queues.get(anime_key, []))
