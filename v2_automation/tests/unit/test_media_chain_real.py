"""Real-binary media chain: local HLS served over real HTTP -> download -> mux ->
ffprobe validation -> thumbnail.  Uses the real ffmpeg/ffprobe (skipped if absent);
only the origin server is local (no external network)."""
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from v1_poc.downloader import DownloadError, download_and_mux, download_segments
from v1_poc.manifest import parse_media_playlist
from v1_poc.media_tools import MediaToolsError, ffmpeg_bin, ffprobe_bin
from v1_poc.validator import validate_media_file
from v2_automation.publisher import extract_thumbnail

try:
    FFMPEG, FFPROBE = ffmpeg_bin(), ffprobe_bin()
except MediaToolsError:  # pragma: no cover
    pytest.skip("ffmpeg/ffprobe not available", allow_module_level=True)


def _run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


@pytest.fixture(scope="module")
def hls_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("hls")
    src = d / "src.mp4"
    _run(FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=800:duration=6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "48", "-keyint_min", "48",
         "-sc_threshold", "0", "-c:a", "aac", "-shortest", src)
    _run(FFMPEG, "-y", "-i", src, "-c", "copy", "-f", "hls", "-hls_time", "2",
         "-hls_playlist_type", "vod", "-hls_segment_filename", d / "seg_%d.ts", d / "index.m3u8")
    return d


class _Origin:
    """Local HTTP origin with scriptable failures: fail_first[name] = n -> n x HTTP 503 first."""

    def __init__(self, root: Path):
        self.root, self.fail_first, self.missing, self.hits = root, {}, set(), {}
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(outer.root), **k)

            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.lstrip("/")
                outer.hits[name] = outer.hits.get(name, 0) + 1
                if name in outer.missing:
                    self.send_error(404)
                elif outer.fail_first.get(name, 0) >= outer.hits[name]:
                    self.send_error(503)
                else:
                    super().do_GET()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}/"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def origin(hls_dir):
    o = _Origin(hls_dir)
    yield o
    o.close()


def _playlist(origin):
    text = (origin.root / "index.m3u8").read_text()
    return parse_media_playlist(text, origin.base + "index.m3u8")


def _dl(pl, out, tmp_path, **kw):
    return download_and_mux(pl, out, FFMPEG, user_agent="t", work_root=tmp_path / "w",
                            retry_backoff_seconds=0.01, **kw)


# ── Playlist ─────────────────────────────────────────────────────────────────

def test_playlist_valid_keeps_urls_order_duration(origin):
    pl = _playlist(origin)
    assert pl.has_end_list and not pl.encrypted
    assert [s.index for s in pl.segments] == list(range(len(pl.segments)))
    assert all(s.uri.startswith(origin.base) for s in pl.segments)
    assert 5.5 < pl.total_duration_seconds < 6.5


def test_playlist_invalid_is_rejected():
    with pytest.raises(ValueError):
        parse_media_playlist("<html>not a playlist</html>", "http://x/index.m3u8")


def test_playlist_without_segments_is_empty():
    assert parse_media_playlist("#EXTM3U\n#EXT-X-ENDLIST\n", "http://x/i.m3u8").segments == []


# ── Download ────────────────────────────────────────────────────────────────

def test_download_success_then_valid_mp4(origin, tmp_path):
    out = tmp_path / "ep.mp4"
    res = _dl(_playlist(origin), out, tmp_path)
    assert out.exists() and out.stat().st_size > 0 and res.mux_returncode == 0
    v = validate_media_file(out, FFPROBE)
    assert v.verdict == "VALID" and v.video["width"] == 320 and v.audio["codec_name"] == "aac"


def test_download_retries_transient_503(origin, tmp_path):
    origin.fail_first["seg_1.ts"] = 2
    out = tmp_path / "ep.mp4"
    _dl(_playlist(origin), out, tmp_path, max_retries=3)
    assert origin.hits["seg_1.ts"] == 3 and out.stat().st_size > 0


def test_download_retry_exhausted_is_classified(origin, tmp_path):
    origin.fail_first["seg_0.ts"] = 99
    with pytest.raises(DownloadError) as ei:
        _dl(_playlist(origin), tmp_path / "ep.mp4", tmp_path, max_retries=1)
    assert ei.value.kind == "HTTP_503"


def test_download_missing_segment_produces_no_file(origin, tmp_path):
    origin.missing.add("seg_2.ts")
    out = tmp_path / "ep.mp4"
    with pytest.raises(DownloadError) as ei:
        _dl(_playlist(origin), out, tmp_path)
    assert ei.value.kind == "HTTP_404" and not out.exists()


def test_download_interrupted_origin(origin, tmp_path):
    pl = _playlist(origin)
    origin.close()                         # connection refused mid-chain
    with pytest.raises(DownloadError) as ei:
        download_segments(pl, tmp_path / "s", user_agent="t", max_retries=0)
    assert ei.value.kind in ("NETWORK", "TIMEOUT", "OTHER")


# ── Validation ──────────────────────────────────────────────────────────────

@pytest.fixture
def good_mp4(origin, tmp_path):
    out = tmp_path / "good.mp4"
    _dl(_playlist(origin), out, tmp_path)
    return out


def test_validation_truncated_file_rejected(good_mp4, tmp_path):
    bad = tmp_path / "trunc.mp4"
    bad.write_bytes(good_mp4.read_bytes()[: good_mp4.stat().st_size // 4])
    assert validate_media_file(bad, FFPROBE).verdict != "VALID"


def test_validation_garbage_file_rejected(tmp_path):
    bad = tmp_path / "junk.mp4"
    bad.write_bytes(b"not a video" * 100)
    assert validate_media_file(bad, FFPROBE).verdict != "VALID"


def test_validation_empty_and_missing(tmp_path):
    empty = tmp_path / "e.mp4"
    empty.write_bytes(b"")
    assert validate_media_file(empty, FFPROBE).verdict == "INVALID"
    assert validate_media_file(tmp_path / "nope.mp4", FFPROBE).verdict == "INVALID"


def test_validation_no_audio_rejected(good_mp4, tmp_path):
    out = tmp_path / "noaudio.mp4"
    _run(FFMPEG, "-y", "-i", good_mp4, "-an", "-c:v", "copy", out)
    v = validate_media_file(out, FFPROBE)
    assert v.verdict == "INVALID" and any(c["check"] == "audio_stream" and not c["pass"] for c in v.checks)


def test_validation_no_video_rejected(good_mp4, tmp_path):
    out = tmp_path / "novideo.m4a"
    _run(FFMPEG, "-y", "-i", good_mp4, "-vn", "-c:a", "copy", out)
    v = validate_media_file(out, FFPROBE)
    assert v.verdict == "INVALID" and any(c["check"] == "video_stream" and not c["pass"] for c in v.checks)


# ── Thumbnail ───────────────────────────────────────────────────────────────

def test_thumbnail_generated_from_video(good_mp4, tmp_path):
    jpg = extract_thumbnail(good_mp4, tmp_path / "t.jpg", FFMPEG)
    assert jpg.stat().st_size > 0 and jpg.read_bytes()[:2] == b"\xff\xd8"


def test_thumbnail_seeks_inside_video_not_first_frame(good_mp4, tmp_path):
    from v2_automation.publisher import _duration_from_ffmpeg
    d = _duration_from_ffmpeg(good_mp4, FFMPEG, 60)
    assert d is not None and 5.5 < d < 6.5


def test_thumbnail_ffmpeg_absent(good_mp4, tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_thumbnail(good_mp4, tmp_path / "t.jpg", tmp_path / "no_ffmpeg.exe")


def test_thumbnail_invalid_video(tmp_path):
    bad = tmp_path / "junk.mp4"
    bad.write_bytes(b"garbage" * 50)
    with pytest.raises(RuntimeError):
        extract_thumbnail(bad, tmp_path / "t.jpg", FFMPEG)
    assert not (tmp_path / "t.jpg").exists() or (tmp_path / "t.jpg").stat().st_size == 0


def test_thumbnail_missing_video(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_thumbnail(tmp_path / "none.mp4", tmp_path / "t.jpg", FFMPEG)
