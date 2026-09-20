"""Domain models for v2_automation (SQLite-backed)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .timeutil import now_utc


def _now() -> str:
    return now_utc()


@dataclass
class Episode:
    id: int | None = None
    anime_key: str = ""
    episode_key: str = ""                 # primary dedup (canonicalized source URL)
    source: str = "voir-anime.to"
    canonical_episode_url: str = ""
    language: str = "vostfr"
    season: int | None = None
    episode_number: int | None = None
    label: str | None = None
    episode_url: str = ""
    status: str = "discovered"
    queued_at: str | None = None
    first_seen_at: str = field(default_factory=_now)
    retry_until_at: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    last_error_at: str | None = None
    media_hash: str | None = None
    file_size: int | None = None
    file_path: str | None = None
    thumbnail_path: str | None = None
    thumbnail_sha256: str | None = None
    thumbnail_size: int | None = None
    thumbnail_message_id: int | None = None
    video_sha256: str | None = None
    video_message_id: int | None = None
    published_at: str | None = None
    cleanup_at: str | None = None
    attempt_count: int = 0
    first_attempt_at: str | None = None
    last_attempt_at: str | None = None
    next_retry_at: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def internal_link(self) -> str:
        return f"anime://{self.anime_key}" if self.anime_key else "anime://?"