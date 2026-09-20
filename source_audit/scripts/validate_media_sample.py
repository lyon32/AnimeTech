#!/usr/bin/env python
"""Phase (closure) — real ffprobe media validation.

Pipeline: episode page -> player iframe -> HLS master manifest (URL handed to
any viewer's browser in cleartext, per Phase 5/6 evidence) -> a tiny (2 second)
temporary clip cut from the stream via ffmpeg -> ffprobe on that local file ->
delete the temp file. This is NOT a downloader: the clip is a few hundred KB,
written to the OS temp directory, and deleted immediately after probing. No
protection is bypassed -- the manifest URL is the same one a normal viewer's
browser receives.

Requires a portable ffmpeg/ffprobe build at tools/ffmpeg/bin/ (see SESSION_REPORT.md
closure session for why this project uses a portable build rather than a system
install).

Usage:
    python scripts/validate_media_sample.py --url "<episode-page-url>" [--url "..." ...]
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from source_audit.analysis.episode import parse_episode_page  # noqa: E402
from source_audit.analysis.player import parse_embed_page  # noqa: E402
from source_audit.fetch.http_client import HttpClient  # noqa: E402
from source_audit.logging_config import setup_logging  # noqa: E402

FFMPEG = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffprobe.exe"

logger = logging.getLogger("validate_media_sample")


def resolve_manifest_url(episode_url: str, client: HttpClient) -> str | None:
    ep_result = client.get(episode_url)
    if not ep_result.ok:
        logger.error("Failed to fetch episode page %s: %s", episode_url, ep_result.error_type.value)
        return None
    episode = parse_episode_page(ep_result.text, episode_url)
    if not episode.player_iframe_url:
        logger.error("No player iframe found on %s", episode_url)
        return None

    embed_result = client.get(episode.player_iframe_url)
    if not embed_result.ok:
        logger.error("Failed to fetch embed page %s: %s", episode.player_iframe_url, embed_result.error_type.value)
        return None
    player = parse_embed_page(embed_result.text, episode.player_iframe_url)
    if not player.manifest_url:
        logger.error("No manifest URL found in embed page for %s", episode_url)
        return None
    return player.manifest_url


def determine_verdict(clip_bytes: int, streams: list[dict]) -> str:
    """Pure verdict logic, kept separate from subprocess/IO for testability.

    VALID: non-zero clip size AND at least one video stream AND at least one
    audio stream. Anything else observed so far is INCONCLUSIVE (not INVALID --
    INVALID is reserved for ffmpeg/ffprobe themselves failing, handled by the
    caller before this function is reached).
    """
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return "VALID" if (has_video and has_audio and clip_bytes > 0) else "INCONCLUSIVE"


def probe_tiny_clip(manifest_url: str, clip_seconds: float = 2.0) -> dict:
    """Cuts a tiny clip from the HLS stream to a temp file, probes it with
    ffprobe, then deletes the temp file. Returns the ffprobe JSON output plus a
    validity verdict."""
    with tempfile.TemporaryDirectory(prefix="source_audit_media_") as tmpdir:
        clip_path = Path(tmpdir) / "clip.mp4"

        ffmpeg_cmd = [
            str(FFMPEG), "-y", "-loglevel", "error",
            "-i", manifest_url,
            "-t", str(clip_seconds),
            "-c", "copy",
            str(clip_path),
        ]
        proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not clip_path.exists():
            return {
                "verdict": "INVALID",
                "reason": "ffmpeg failed to cut clip",
                "stderr": proc.stderr[-2000:],
            }

        clip_bytes = clip_path.stat().st_size

        ffprobe_cmd = [
            str(FFPROBE), "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(clip_path),
        ]
        probe_proc = subprocess.run(ffprobe_cmd, capture_output=True, text=True, timeout=30)
        # clip_path is deleted automatically when the TemporaryDirectory context exits.

        if probe_proc.returncode != 0:
            return {
                "verdict": "INVALID",
                "reason": "ffprobe failed on cut clip",
                "stderr": probe_proc.stderr[-2000:],
                "clip_bytes": clip_bytes,
            }

        try:
            probe_json = json.loads(probe_proc.stdout)
        except json.JSONDecodeError:
            return {"verdict": "INCONCLUSIVE", "reason": "ffprobe output not valid JSON", "clip_bytes": clip_bytes}

        streams = probe_json.get("streams", [])
        verdict = determine_verdict(clip_bytes, streams)

        return {
            "verdict": verdict,
            "clip_bytes": clip_bytes,
            "format": probe_json.get("format", {}),
            "streams": streams,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", required=True, dest="urls", help="Episode page URL (repeatable)")
    parser.add_argument("--clip-seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging("DEBUG" if args.verbose else "INFO")

    if not FFPROBE.exists() or not FFMPEG.exists():
        logger.error("ffmpeg/ffprobe not found at %s -- see tools/ffmpeg/bin/", FFMPEG.parent)
        return 1

    results = []
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        for url in args.urls:
            logger.info("Resolving manifest for %s", url)
            manifest_url = resolve_manifest_url(url, client)
            if manifest_url is None:
                results.append({"episode_url": url, "verdict": "BLOCKED", "reason": "manifest not resolved"})
                continue

            logger.info("Probing %.1fs clip for %s", args.clip_seconds, url)
            probe_result = probe_tiny_clip(manifest_url, args.clip_seconds)
            probe_result["episode_url"] = url
            logger.info("Verdict for %s: %s", url, probe_result["verdict"])
            results.append(probe_result)

    output_text = json.dumps(results, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_text, encoding="utf-8")
        logger.info("Wrote report to %s", args.output)
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
