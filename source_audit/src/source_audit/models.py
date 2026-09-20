from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class EvidenceStatus(str, Enum):
    """Mandatory status vocabulary per MASTER_PLAN.md section 5. Never invent other values."""

    TESTED = "TESTED"
    MEASURED = "MEASURED"
    NOT_EVALUATED = "NOT_EVALUATED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class Language(str, Enum):
    VF = "VF"
    VOSTFR = "VOSTFR"
    UNKNOWN = "UNKNOWN"


class HomepageEntry(BaseModel):
    """A single item observed on the homepage/new-episodes listing. Unknown fields are None, never guessed."""

    anime_title: Optional[str] = None
    episode_label: Optional[str] = None
    language: Optional[Language] = None
    url: str
    published_at_raw: Optional[str] = None
    thumbnail_url: Optional[str] = None
    source_selector: Optional[str] = None

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url must not be empty")
        return v


class AnimeEpisodeLink(BaseModel):
    """One entry from an anime page's episode list (`.wp-manga-chapter`)."""

    label: str
    url: str
    published_at_raw: Optional[str] = None


class AnimeRecord(BaseModel):
    """An anime page's structural data.

    NOTE (Phase 3 finding, 2026-09-17): this site does not expose multiple seasons
    on a single anime page, and does not expose multiple languages on a single anime
    page either — each (title, season, language) combination observed so far is its
    own separate anime page/URL/post ID with its own independent episode list and
    episode count (e.g. "Tomb Raider King (VF)" and "Tomb Raider King (JAP)" are two
    distinct anime pages, not tabs/variants of one page). `season_labels` is kept for
    forward compatibility but is expected to stay empty given this evidence; it must
    not be assumed empty for every anime without being checked.
    """

    url: str
    post_id: Optional[str] = None
    """WordPress post ID from `body.postid-N`. See analysis/identity.py — this is
    the basis of `anime_key` and is shared by every episode of this anime."""
    title: Optional[str] = None
    native_title: Optional[str] = None
    romaji_title: Optional[str] = None
    english_title: Optional[str] = None
    status_raw: Optional[str] = None
    anime_type_raw: Optional[str] = None
    studios: Optional[str] = None
    start_date_raw: Optional[str] = None
    genres: list[str] = Field(default_factory=list)
    total_episodes_declared: Optional[int] = None
    """From the page's own "Episodes" metadata field (total planned for the season),
    when present. Distinct from len(episode_links), which is episodes actually
    released/listed so far."""
    episode_links: list[AnimeEpisodeLink] = Field(default_factory=list)
    season_labels: list[str] = Field(default_factory=list)
    episode_count_observed: Optional[int] = None


class EpisodeRecord(BaseModel):
    url: str
    episode_key: Optional[str] = None
    """Canonicalized episode URL — see analysis/identity.py. Always derivable
    (unlike episode_number, which can be None for films/OAVs); used as the primary
    per-episode identifier."""
    anime_key: Optional[str] = None
    """`postid:{N}` — see analysis/identity.py. Shared by every episode of the same
    anime; already language/season-scoped since each is a separate WP post
    (Phase 3 finding)."""
    anime_post_id: Optional[str] = None
    episode_number: Optional[int] = None
    """Parsed from the URL's trailing `-N-vf`/`-N-vostfr` slug when present (Phase 8).
    None for film/OAV-prefixed URLs — not guessed."""
    anime_title: Optional[str] = None
    season_label: Optional[str] = None
    episode_number_raw: Optional[str] = None
    language: Optional[Language] = None
    language_confidence: Confidence = Confidence.UNKNOWN
    published_at_raw: Optional[str] = None
    player_iframe_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    page_title_raw: Optional[str] = None
    prev_episode_url: Optional[str] = None
    next_episode_url: Optional[str] = None

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url must not be empty")
        return v


class PlayerObservation(BaseModel):
    """Structural facts about the embedded player (Phase 5). Never includes an
    attempt to bypass whatever access-control the embed uses — only what's visible
    in a plain, unauthenticated GET of the pages voir-anime.to itself serves."""

    iframe_url: str
    iframe_domain: Optional[str] = None
    embed_page_title: Optional[str] = None
    player_library: Optional[str] = None
    """e.g. "jwplayer" — detected from a literal library-name string in the embed
    page's own script tags, not from executing/deobfuscating anything."""
    has_obfuscated_script: bool = False
    """True if a script tag matching a known JS-packer signature (e.g.
    `eval(function(p,a,c,k,e,d)`) was present. Recorded as a fact, not deobfuscated."""
    manifest_url_found: bool = False
    manifest_url: Optional[str] = None
    """Only populated when the manifest URL appeared in cleartext in the embed
    page's own script (i.e. was handed to any viewer's browser as part of normal
    page load) — never derived by cracking an obfuscated script or forging a token."""
    status: EvidenceStatus = EvidenceStatus.NOT_EVALUATED


class MediaRendition(BaseModel):
    """One #EXT-X-STREAM-INF entry from an HLS master manifest."""

    resolution: Optional[str] = None
    bandwidth_bps: Optional[int] = None
    fps: Optional[float] = None
    codecs_raw: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    playlist_url: Optional[str] = None


class MediaObservation(BaseModel):
    """Only populated from data the tooling was actually able to observe within the authorized scope."""

    format: Optional[str] = None
    mime_type: Optional[str] = None
    protocol: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    resolution: Optional[str] = None
    fps: Optional[float] = None
    bitrate_bps: Optional[int] = None
    duration_seconds: Optional[float] = None
    size_bytes: Optional[int] = None
    has_subtitles: Optional[bool] = None
    renditions: list[MediaRendition] = Field(default_factory=list)
    status: EvidenceStatus = EvidenceStatus.NOT_EVALUATED
