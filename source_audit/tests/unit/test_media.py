from pathlib import Path

from source_audit.analysis.media import parse_hls_master_manifest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hls_master_sample.m3u8"


def test_parses_single_rendition():
    text = FIXTURE.read_text(encoding="utf-8")
    renditions = parse_hls_master_manifest(text)

    assert len(renditions) == 1
    r = renditions[0]
    assert r.resolution == "1920x1080"
    assert r.bandwidth_bps == 3847819
    assert r.fps == 23.974
    assert r.video_codec == "avc1.640028"
    assert r.audio_codec == "mp4a.40.2"
    assert r.playlist_url is not None
    assert r.playlist_url.startswith("https://cdn.example.invalid/")


def test_ignores_iframe_stream_inf():
    text = FIXTURE.read_text(encoding="utf-8")
    renditions = parse_hls_master_manifest(text)
    # Only the EXT-X-STREAM-INF entry, not the EXT-X-I-FRAME-STREAM-INF trick-play one.
    assert len(renditions) == 1


def test_empty_manifest_returns_empty_list():
    assert parse_hls_master_manifest("#EXTM3U\n") == []


# Phase 13 resilience: manifest lines missing attributes ("unknown resolution")
# must degrade to None, not raise or guess a value.
def test_stream_inf_missing_resolution_and_codecs():
    text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nlow.m3u8\n"
    renditions = parse_hls_master_manifest(text)
    assert len(renditions) == 1
    r = renditions[0]
    assert r.resolution is None
    assert r.video_codec is None
    assert r.audio_codec is None
    assert r.bandwidth_bps == 1000000
    assert r.playlist_url == "low.m3u8"


def test_truncated_manifest_missing_playlist_url():
    # STREAM-INF is the very last line -- no playlist URL follows (truncated response).
    text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080\n"
    renditions = parse_hls_master_manifest(text)
    assert len(renditions) == 1
    assert renditions[0].resolution == "1920x1080"
    assert renditions[0].playlist_url is None


def test_completely_malformed_manifest_does_not_raise():
    assert parse_hls_master_manifest("not an m3u8 file at all") == []
    assert parse_hls_master_manifest("") == []


# Closure session finding: real manifests can have DIFFERENT fps per rendition
# (Detective Conan ep 158: 24.39 top-tier, 23.974 lower-tier) -- must not assume
# fps is uniform across a manifest's renditions.
def test_renditions_can_have_different_fps():
    text = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=1428947,RESOLUTION=832x624,FRAME-RATE=24.39,CODECS="avc1.640029,mp4a.40.2"\n'
        "high.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=605277,RESOLUTION=640x480,FRAME-RATE=23.974,CODECS="avc1.4d401f,mp4a.40.2"\n'
        "low.m3u8\n"
    )
    renditions = parse_hls_master_manifest(text)
    assert len(renditions) == 2
    assert renditions[0].fps == 24.39
    assert renditions[1].fps == 23.974


def test_multi_rendition_manifest():
    text = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"\n'
        "low.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
        "high.m3u8\n"
    )
    renditions = parse_hls_master_manifest(text)
    assert len(renditions) == 2
    assert [r.resolution for r in renditions] == ["1280x720", "1920x1080"]
    assert [r.playlist_url for r in renditions] == ["low.m3u8", "high.m3u8"]
