"""In-memory simulation of the "have we seen this episode before?" logic (Phase 10).

`source_audit` does not implement a real persistence layer or downloader (out of
scope per MASTER_PLAN.md §51) — this module simulates just enough of what a future
V1's DB-backed episode store would need to do, to validate the *identity/dedup
logic* itself (built in Phase 8/9) against the 8 scenarios MASTER_PLAN.md §21 lists.
A real V1 would back this with SQLite or similar; the in-memory dict here stands in
for that so the logic can be exercised without building storage infrastructure this
project isn't meant to own.

Key design decision (tested below): deduplication is keyed **only** on
`episode_key` (the canonicalized URL, from `analysis.identity`), never on title
text. Title strings are known to vary in observed rendering, capitalization, or
whitespace between fetches of the same underlying content (see Phase 2/3 evidence)
and must not be allowed to cause either a false "new" (same episode looks
different) or a false "duplicate" (two different episodes happen to share similar
title text).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EpisodeStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    PUBLISHED = "PUBLISHED"


@dataclass
class EpisodeStoreEntry:
    episode_key: str
    anime_key: str | None
    status: EpisodeStatus


class EpisodeStore:
    """A minimal key -> status store, standing in for a future V1's real DB.

    Deliberately has no concept of a local file on disk: whether a downloaded
    file still exists is a filesystem fact, orthogonal to "has this episode
    already been processed" (a DB fact). Scenario 6 in Phase 10 (local file
    deleted) tests that this separation holds — status is unaffected by anything
    outside `mark_*`/`known_keys`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, EpisodeStoreEntry] = {}

    def is_known(self, episode_key: str) -> bool:
        return episode_key in self._entries

    def status_of(self, episode_key: str) -> EpisodeStatus | None:
        entry = self._entries.get(episode_key)
        return entry.status if entry else None

    def register_discovered(self, episode_key: str, anime_key: str | None) -> None:
        """Registers a new episode as DISCOVERED. A no-op if already known (does
        not downgrade an episode's status back to DISCOVERED on re-discovery —
        scenario 2/7: seeing the same episode again must never regress its state)."""
        if episode_key in self._entries:
            return
        self._entries[episode_key] = EpisodeStoreEntry(episode_key, anime_key, EpisodeStatus.DISCOVERED)

    def mark_status(self, episode_key: str, status: EpisodeStatus) -> None:
        if episode_key not in self._entries:
            raise KeyError(f"Cannot mark status for unknown episode_key: {episode_key}")
        self._entries[episode_key].status = status

    def known_keys(self) -> set[str]:
        return set(self._entries.keys())

    def is_already_published(self, episode_key: str) -> bool:
        entry = self._entries.get(episode_key)
        return entry is not None and entry.status == EpisodeStatus.PUBLISHED

    def snapshot(self) -> dict[str, str]:
        """Serializes to a plain dict (key -> status value), simulating what would
        be persisted to/reloaded from a real DB across a process restart."""
        return {k: v.status.value for k, v in self._entries.items()}

    @classmethod
    def restore(cls, snapshot: dict[str, str], anime_keys: dict[str, str] | None = None) -> "EpisodeStore":
        """Rebuilds a store from a snapshot — simulates scenario 5 (program
        restart, DB conserved)."""
        store = cls()
        anime_keys = anime_keys or {}
        for episode_key, status_value in snapshot.items():
            store._entries[episode_key] = EpisodeStoreEntry(
                episode_key, anime_keys.get(episode_key), EpisodeStatus(status_value)
            )
        return store
