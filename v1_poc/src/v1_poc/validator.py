"""FFprobe-based real media validation and SHA-256 hashing."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_FFPROBE_STDERR = 3000


@dataclass
class ProbeResult:
    returncode: int
    probe: dict | None
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.probe is not None


def run_ffprobe(path: Path, ffprobe_bin: Path, timeout_seconds: int = 120) -> ProbeResult:
    if not path.exists():
        return ProbeResult(returncode=-1, probe=None, stderr_tail="file does not exist")
    cmd = [
        str(ffprobe_bin), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    probe = None
    if proc.stdout.strip():
        try:
            probe = json.loads(proc.stdout)
        except json.JSONDecodeError:
            probe = None
    return ProbeResult(returncode=proc.returncode, probe=probe, stderr_tail=(proc.stderr or "")[-MAX_FFPROBE_STDERR:])


def parse_fps(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if "/" in value:
        try:
            num, den = value.split("/")
            den = float(den)
            return round(float(num) / den, 4) if den else None
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return round(float(value), 4)
    except ValueError:
        return None


def _codec_family(codec_name: str) -> str:
    return codec_name


@dataclass
class ValidationResult:
    verdict: str  # VALID | INVALID | INCONCLUSIVE
    size_bytes: int
    duration_seconds: float | None = None
    format_name: str | None = None
    bitrate_bps: int | None = None
    video: dict = field(default_factory=dict)
    audio: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    mismatches_vs_manifest: list[str] = field(default_factory=list)
    probe_returncode: int | None = None
    probe_stderr_tail: str = ""


def validate_media_file(
    path: Path,
    ffprobe_bin: Path,
    expected: dict | None = None,
) -> ValidationResult:
    """Validates a downloaded file with real ffprobe, comparing against expected
    manifest-derived facts (duration, resolution, codecs) when supplied."""
    result = ValidationResult(verdict="INVALID", size_bytes=0)
    result.video = {"codec_name": None}
    result.audio = {"codec_name": None}

    if not path.exists():
        result.checks.append({"check": "file_exists", "pass": False, "detail": "file missing"})
        return result

    size = path.stat().st_size
    result.size_bytes = size
    result.checks.append({"check": "file_size", "pass": size > 0, "detail": f"{size} bytes"})
    if size <= 0:
        return result

    probe = run_ffprobe(path, ffprobe_bin)
    result.probe_returncode = probe.returncode
    result.probe_stderr_tail = probe.stderr_tail

    if probe.returncode != 0:
        result.checks.append(
            {"check": "ffprobe", "pass": False, "detail": f"rc={probe.returncode}: {probe.stderr_tail[:300]}"}
        )
        return result
    if probe.probe is None:
        result.verdict = "INCONCLUSIVE"
        result.checks.append({"check": "ffprobe_json", "pass": False, "detail": "non-JSON probe output"})
        return result

    fmt = probe.probe.get("format", {}) or {}
    streams = probe.probe.get("streams", []) or []
    result.format_name = fmt.get("format_name")
    result.duration_seconds = _to_float(fmt.get("duration"))
    result.bitrate_bps = _to_int(fmt.get("bit_rate"))

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    result.video = {
        "codec_name": (video or {}).get("codec_name"),
        "codec_long_name": (video or {}).get("codec_long_name"),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "pix_fmt": (video or {}).get("pix_fmt"),
        "fps": parse_fps((video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate")),
        "profile": (video or {}).get("profile"),
    } if video else {"codec_name": None}
    result.audio = {
        "codec_name": (audio or {}).get("codec_name"),
        "sample_rate": (audio or {}).get("sample_rate"),
        "channels": (audio or {}).get("channels"),
    } if audio else {"codec_name": None}

    result.checks.append({"check": "container_id", "pass": bool(result.format_name), "detail": result.format_name})
    result.checks.append({"check": "duration", "pass": (result.duration_seconds or 0) > 0, "detail": f"{result.duration_seconds}s"})
    result.checks.append({"check": "video_stream", "pass": video is not None, "detail": str((video or {}).get("codec_name"))})
    result.checks.append({"check": "audio_stream", "pass": audio is not None, "detail": str((audio or {}).get("codec_name"))})

    if video is not None:
        ok, detail = check_packet_integrity(path, ffprobe_bin)
        result.checks.append({"check": "integrity", "pass": ok, "detail": detail})

    all_pass = all(c["pass"] for c in result.checks if not c["check"].startswith("vs_manifest"))
    result.verdict = "VALID" if all_pass else "INVALID"

    if expected:
        _compare_manifest_expected(result, expected)

    return result


BENIGN_WARNINGS = ("Referenced QT chapter track not found",)


def check_packet_integrity(path: Path, ffprobe_bin: Path, timeout_seconds: int = 900) -> tuple[bool, str]:
    """Reads every video packet (no decode): a truncated/corrupt file yields read errors or
    fewer packets than the container declares (`nb_frames`), even when the moov header is intact."""
    cmd = [str(ffprobe_bin), "-v", "error", "-select_streams", "v:0", "-count_packets",
           "-show_entries", "stream=nb_frames,nb_read_packets", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, "packet scan timed out"
    # Harmless container warnings about a metadata chapter track (seen on direct MP4s from the Stape player): the
    # video packets themselves are fine, so they must not fail the integrity check.
    stderr = "\n".join(l for l in (proc.stderr or "").splitlines()
                       if l.strip() and not any(b in l for b in BENIGN_WARNINGS))
    if proc.returncode != 0 or stderr.strip():
        return False, f"rc={proc.returncode}: {stderr.strip()[:200]}"
    try:
        st = (json.loads(proc.stdout).get("streams") or [{}])[0]
    except json.JSONDecodeError:
        return False, "non-JSON packet scan"
    read, declared = _to_int(st.get("nb_read_packets")), _to_int(st.get("nb_frames"))
    if read is None or read <= 0:
        return False, "no video packets readable"
    if declared is not None and declared != read:
        return False, f"declared {declared} frames but only {read} packets readable (truncated)"
    return True, f"{read} packets"


def _compare_manifest_expected(result: ValidationResult, expected: dict) -> None:
    exp_duration = expected.get("duration_seconds")
    if exp_duration:
        actual = result.duration_seconds or 0.0
        ratio = actual / exp_duration if exp_duration else 0.0
        if ratio < 0.5:
            result.checks.append(
                {"check": "vs_manifest_duration", "pass": False, "detail": f"truncated: actual {actual:.1f}s vs expected {exp_duration:.1f}s"}
            )
            result.mismatches_vs_manifest.append(f"duration too short: {actual:.1f}s vs {exp_duration:.1f}s")
        elif ratio < 0.9:
            result.checks.append(
                {"check": "vs_manifest_duration", "pass": False, "detail": f"shorter than expected: {actual:.1f}s vs {exp_duration:.1f}s"}
            )
            result.mismatches_vs_manifest.append(f"duration {actual:.1f}s vs manifest {exp_duration:.1f}s")
        else:
            result.checks.append(
                {"check": "vs_manifest_duration", "pass": True, "detail": f"{actual:.1f}s vs manifest {exp_duration:.1f}s"}
            )

    exp_res = expected.get("resolution")
    if exp_res:
        w = result.video.get("width")
        h = result.video.get("height")
        actual_res = f"{w}x{h}" if w and h else None
        if exp_res != actual_res:
            result.checks.append(
                {"check": "vs_manifest_resolution", "pass": False, "detail": f"{actual_res} vs manifest {exp_res}"}
            )
            result.mismatches_vs_manifest.append(f"resolution {actual_res} vs manifest {exp_res}")
        else:
            result.checks.append(
                {"check": "vs_manifest_resolution", "pass": True, "detail": f"{actual_res} == manifest {exp_res}"}
            )

    exp_vcodec = expected.get("video_codec")
    if exp_vcodec:
        actual = result.video.get("codec_name")
        match = bool(actual) and actual.lower() == exp_vcodec.lower()
        result.checks.append(
            {"check": "vs_manifest_video_codec", "pass": match, "detail": f"{actual} vs manifest {exp_vcodec}"}
        )
        if not match:
            result.mismatches_vs_manifest.append(f"video codec {actual} vs manifest {exp_vcodec}")

    exp_acodec = expected.get("audio_codec")
    if exp_acodec:
        actual = result.audio.get("codec_name")
        match = bool(actual) and actual.lower() == exp_acodec.lower()
        result.checks.append(
            {"check": "vs_manifest_audio_codec", "pass": match, "detail": f"{actual} vs manifest {exp_acodec}"}
        )
        if not match:
            result.mismatches_vs_manifest.append(f"audio codec {actual} vs manifest {exp_acodec}")


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def truncate_copy(src: Path, out: Path, keep_bytes: int) -> Path:
    """Copies the first keep_bytes of src to out (for truncated-file failure tests)."""
    with open(src, "rb") as fh:
        data = fh.read(keep_bytes)
    out.write_bytes(data)
    return out