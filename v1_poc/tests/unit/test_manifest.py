"""Unit tests: HLS master/media playlist parsing and rendition selection."""
from __future__ import annotations

from pathlib import Path

import pytest

from source_audit.analysis.media import parse_hls_master_manifest
from v1_poc.manifest import parse_media_playlist, select_best_rendition

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

MASTER_TEXT = (FIXTURES / "hls_master_sample.m3u8").read_text(encoding="utf-8")


def test_parse_master_renditions_and_selection():
    renditions = parse_hls_master_manifest(MASTER_TEXT)
    assert len(renditions) == 1
    best = select_best_rendition(renditions)
    assert best is not None
    assert best.resolution == "1920x1080"
    assert best.bandwidth_bps == 3847819
    assert best.video_codec == "avc1.640028"
    assert best.audio_codec == "mp4a.40.2"
    assert "index-v1-a1.m3u8" in best.playlist_url


def test_selection_prefers_pixels_then_bandwidth():
    class R:
        def __init__(self, res, bw):
            self.resolution = res
            self.bandwidth_bps = bw

    plains = [
        R(res, bw)
        for res, bw in [
            ("1920x1080", 1000_000),
            ("1280x720", 2000_000),
            ("852x480", 500_000),
        ]
    ]
    best = select_best_rendition(plains)
    assert best.resolution == "1920x1080"
    assert best.bandwidth_bps == 1000_000

    same_pixels = [R("1280x720", 500_000), R("1280x720", 900_000)]
    assert select_best_rendition(same_pixels).bandwidth_bps == 900_000


def test_select_best_empty():
    assert select_best_rendition([]) is None


def test_parse_media_playlist_relative_resolution_and_encryption():
    text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="https://k.example/key",IV=0x0000
#EXTINF:6.006,
seg_000.ts
#EXTINF:6.006,
seg_001.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:6.006,
seg_002.ts
#EXT-X-ENDLIST
"""
    playlist = parse_media_playlist(text, "https://cdn.example.invalid/play/index-v1-a1.m3u8")
    assert playlist.encrypted is True
    assert playlist.encryption_method == "AES-128"
    assert playlist.has_end_list is True
    assert playlist.is_fmp4 is False
    assert len(playlist.segments) == 3
    assert playlist.segments[0].uri == "https://cdn.example.invalid/play/seg_000.ts"
    assert round(playlist.total_duration_seconds, 2) == 18.02


def test_parse_media_playlist_fmp4_detection():
    text = """#EXTM3U
#EXT-X-MAP:URI="init.mp4",BYTERANGE="616@0"
#EXTINF:4.000,
seg1.m4s
#EXT-X-ENDLIST
"""
    playlist = parse_media_playlist(text, "https://cdn.example.invalid/play/x.m3u8")
    assert playlist.is_fmp4 is True
    assert len(playlist.segments) == 1


def test_parse_media_playlist_unencrypted():
    text = """#EXTM3U
#EXTINF:6.0,
a.ts
#EXTINF:6.0,
b.ts
#EXT-X-ENDLIST
"""
    playlist = parse_media_playlist(text, "https://cdn.example.invalid/play/x.m3u8")
    assert playlist.encrypted is False
    assert playlist.encryption_method is None
    assert len(playlist.segments) == 2


def test_parse_media_playlist_no_end_list():
    text = "#EXTM3U\n#EXTINF:6.0,\na.ts\n"
    playlist = parse_media_playlist(text, "https://cdn.example.invalid/play/x.m3u8")
    assert playlist.has_end_list is False