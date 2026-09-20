"""Unit tests: ffprobe-based media validation + SHA-256 (deterministic inputs)."""
from __future__ import annotations

import subprocess

import pytest

from v1_poc.config import load_config
from v1_poc.media_tools import ffprobe_bin
from v1_poc.validator import sha256_file, truncate_copy, validate_media_file

pytestmark = pytest.mark.skipif(not ffprobe_bin().exists(), reason="ffprobe not present")


def _make_real_video(path) -> None:
    """Synthesizes a small real MP4 with ffmpeg (deterministic content)."""
    from pathlib import Path

    from v1_poc.media_tools import ffmpeg_bin

    cmd = [
        str(ffmpeg_bin()), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and path.exists(), proc.stderr[-800:]


def test_sha256_stable(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello world" * 1000)
    assert sha256_file(f) == sha256_file(f)


def test_validate_real_video_passes(tmp_path):
    video = tmp_path / "ok.mp4"
    _make_real_video(video)
    result = validate_media_file(video, ffprobe_bin())
    assert result.verdict == "VALID", result.checks
    assert result.video["codec_name"] == "h264"
    assert result.audio["codec_name"] == "aac"


def test_validate_junk_fails(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"NOT A VIDEO" * 50)
    result = validate_media_file(junk, ffprobe_bin())
    assert result.verdict == "INVALID"
    assert result.video["codec_name"] is None


def test_validate_truncated_file_detected(tmp_path):
    video = tmp_path / "full.mp4"
    _make_real_video(video)
    chopped = truncate_copy(video, tmp_path / "chopped.mp4", keep_bytes=max(10_000, videostat := video.stat().st_size // 2))
    expected = {"duration_seconds": 2.0}
    result = validate_media_file(chopped, ffprobe_bin(), expected=expected)
    # ffprobe may still parse the truncated file, but the duration must fail the
    # expected-duration check (truncation is detectable even when parseable).
    dur_checks = [c for c in result.checks if c["check"] == "vs_manifest_duration"]
    assert any(c["pass"] is False for c in dur_checks) or result.verdict == "INVALID"


def test_validate_duration_mismatch_flagged(tmp_path):
    video = tmp_path / "short.mp4"
    _make_real_video(video)  # 2s real file
    expected = {"duration_seconds": 120.0}  # manifest claims 2 minutes
    result = validate_media_file(video, ffprobe_bin(), expected=expected)
    assert result.verdict in ("VALID", "INVALID")
    dur = [c for c in result.checks if c["check"] == "vs_manifest_duration"][0]
    assert dur["pass"] is False
    assert any("duration" in m for m in result.mismatches_vs_manifest)

def test_truncated_vs_manifest_duration_is_invalid(tmp_path):
    video = tmp_path / "short.mp4"
    _make_real_video(video)  # 2s real file
    result = validate_media_file(video, ffprobe_bin(), expected={"duration_seconds": 120.0})
    assert result.verdict == "INVALID"
