"""Parses an HLS master manifest into structured rendition facts (Phase 6).

Evidence (2026-09-17, see SESSION_REPORT.md Phase 6), from one real master manifest
fetched live via the signed URL surfaced in cleartext by the embed page (Phase 5;
"The Exiled Heavy Knight..." episode 12):

```
#EXTM3U
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3847819,RESOLUTION=1920x1080,FRAME-RATE=23.974,CODECS="avc1.640028,mp4a.40.2",VIDEO-RANGE=SDR
https://prx-1316-ant.vmget.online/.../index-v1-a1.m3u8?t=...&s=...&e=43200&...

#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=430580,RESOLUTION=1920x1080,CODECS="avc1.640028",URI="...iframes-v1-a1.m3u8?..."
```

**MEASURED facts** (direct read of standard, spec-compliant HLS master playlists —
RFC 8216 §4.3.4.2 `EXT-X-STREAM-INF` — no inference involved), from 3 manifests
sampled across 3 different episodes/anime:
- Protocol: HLS (`.m3u8`) on all 3 samples.
- **Rendition count varies per episode, not fixed sitewide** — correcting an
  earlier single-sample observation rather than replacing it (MASTER_PLAN.md §36):
  the first manifest sampled ("The Exiled Heavy Knight...", ep 12) had **only ONE**
  rendition (1080p, ~3.85 Mbps). Two further samples ("Mebius Dust" ep 1, "Tomb
  Raider King (VF)" ep 1) each had **TWO** renditions: 1080p (~3.0-3.1 Mbps,
  `avc1.640028` High Profile L4.0) and 480p (~0.48-0.50 Mbps, `avc1.4d401f` Main
  Profile L3.1). `INCONCLUSIVE` what drives the difference (only 3 samples) — this
  parser makes no assumption about rendition count (0, 1, or many all handled).
- Audio codec on every rendition seen so far: `mp4a.40.2` = AAC-LC.
- Frame rate on every rendition seen so far: 23.974 fps.
- A separate `EXT-X-I-FRAME-STREAM-INF` entry provides an I-frame-only playlist
  (used by players for seek/scrubbing thumbnails) at the top resolution, on all 3
  samples.
- The media-segment URLs (all entries, all 3 samples) carry a signed, time-limited
  access token (`t=`, `s=`, `e=43200` ≈ 12h validity window on every sample).

**Not measured in this session (NOT_EVALUATED):** container/duration/actual
per-segment bitrate and subtitle-track presence would require either downloading
segments or running `ffprobe` directly against the manifest URL. `ffprobe` was
checked and is **not installed** in this environment (`ffprobe -version` → command
not found) — flagged `BLOCKED` for this session, not silently skipped; MASTER_PLAN.md
§17 asks for ffprobe when pertinent, but this project does not install new system
binaries without being asked. The manifest-text parsing below stands on its own as a
lighter, spec-based alternative that avoids downloading any video bytes, consistent
with §17's "as light as possible" rule and §51 (no bulk downloading in this project).
"""
from __future__ import annotations

import re

from source_audit.models import MediaRendition

_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:(?P<attrs>.+)$", re.MULTILINE)
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _parse_attrs(attr_string: str) -> dict[str, str]:
    attrs = {}
    for match in _ATTR_RE.finditer(attr_string):
        key, value = match.group(1), match.group(2)
        attrs[key] = value.strip('"')
    return attrs


def parse_hls_master_manifest(manifest_text: str) -> list[MediaRendition]:
    """Parses `#EXT-X-STREAM-INF` entries (video renditions) into MediaRendition.

    Deliberately ignores `#EXT-X-I-FRAME-STREAM-INF` (trick-play playlists, not a
    playable rendition) and any other tag type not yet observed on this source.
    """
    lines = manifest_text.splitlines()
    renditions: list[MediaRendition] = []

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _parse_attrs(line[len("#EXT-X-STREAM-INF:") :])

        playlist_url = None
        for next_line in lines[i + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if not stripped.startswith("#"):
                playlist_url = stripped
            break

        codecs_raw = attrs.get("CODECS")
        video_codec = None
        audio_codec = None
        if codecs_raw:
            parts = [c.strip() for c in codecs_raw.split(",")]
            for part in parts:
                if part.startswith("avc1") or part.startswith("hvc1") or part.startswith("hev1"):
                    video_codec = part
                elif part.startswith("mp4a") or part.startswith("ac-3") or part.startswith("ec-3"):
                    audio_codec = part

        bandwidth_raw = attrs.get("BANDWIDTH")
        fps_raw = attrs.get("FRAME-RATE")

        renditions.append(
            MediaRendition(
                resolution=attrs.get("RESOLUTION"),
                bandwidth_bps=int(bandwidth_raw) if bandwidth_raw and bandwidth_raw.isdigit() else None,
                fps=float(fps_raw) if fps_raw else None,
                codecs_raw=codecs_raw,
                video_codec=video_codec,
                audio_codec=audio_codec,
                playlist_url=playlist_url,
            )
        )

    return renditions
