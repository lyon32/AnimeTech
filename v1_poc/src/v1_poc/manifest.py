"""HLS manifest handling: rendition selection + media playlist parsing.

Rendition facts are read from the real manifest (POC spec clause 13) — nothing
about resolution/bitrate is assumed. Clause 21 of the source_audit findings
(rendition variance) means selection is always by parsed attributes, never by
position.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from source_audit.analysis.media import MediaRendition

_RESOLUTION_RE = re.compile(r"(\d+)x(\d+)")


def select_best_rendition(renditions: list[MediaRendition]) -> MediaRendition | None:
    """Selects the highest-quality rendition by (pixel count, bandwidth)."""
    if not renditions:
        return None

    def key(r: MediaRendition) -> tuple[int, int]:
        pixels = 0
        if r.resolution:
            m = _RESOLUTION_RE.match(r.resolution)
            if m:
                pixels = int(m.group(1)) * int(m.group(2))
        return (pixels, r.bandwidth_bps or 0)

    return max(renditions, key=key)


@dataclass
class PlaylistSegment:
    uri: str
    duration_seconds: float
    index: int


@dataclass
class MediaPlaylist:
    playlist_url: str
    segments: list[PlaylistSegment] = field(default_factory=list)
    encrypted: bool = False
    encryption_method: str | None = None
    has_end_list: bool = False
    is_fmp4: bool = False
    total_duration_seconds: float = 0.0


_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)")
_MAP_URI_RE = re.compile(r'URI="([^"]+)"')
_KEY_METHOD_RE = re.compile(r'METHOD=([A-Z0-9-]+)')


def parse_media_playlist(text: str, playlist_url: str) -> MediaPlaylist:
    """Parses a media playlist (one rendition) into its real segments."""
    lines = text.splitlines()
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    if not first.lstrip("﻿").startswith("#EXTM3U"):
        raise ValueError("not an HLS playlist (missing #EXTM3U header)")
    segments: list[PlaylistSegment] = []
    has_end_list = False
    is_fmp4 = False
    encrypted = False
    encryption_method = None
    pending_duration = 0.0
    total = 0.0

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            has_end_list = True
            continue
        if line.startswith("#EXT-X-MAP"):
            is_fmp4 = True
            continue
        if line.startswith("#EXT-X-KEY"):
            m = _KEY_METHOD_RE.search(line)
            method = m.group(1) if m else "UNKNOWN"
            if method.upper() != "NONE":
                encrypted = True
                encryption_method = method.upper()
            continue
        extinf = _EXTINF_RE.match(line)
        if extinf:
            pending_duration = float(extinf.group(1))
            continue
        if line.startswith("#"):
            continue
        uri = urljoin(playlist_url, line)
        segments.append(
            PlaylistSegment(uri=uri, duration_seconds=pending_duration, index=len(segments))
        )
        total += pending_duration
        pending_duration = 0.0

    return MediaPlaylist(
        playlist_url=playlist_url,
        segments=segments,
        encrypted=encrypted,
        encryption_method=encryption_method,
        has_end_list=has_end_list,
        is_fmp4=is_fmp4,
        total_duration_seconds=total,
    )