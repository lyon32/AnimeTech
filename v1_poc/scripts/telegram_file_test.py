#!/usr/bin/env python
"""V1_POC — Telegram small-video file test (POC spec clause 9).

Generates a tiny legal test video locally with ffmpeg (synthetic testsrc+sine),
uploads it via the real sendVideo method, then re-downloads it from Telegram's
servers to verify size and SHA-256 match — isolating Telegram before the source.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from v1_poc.env import load_env  # noqa: E402
from v1_poc.logging_config import setup_logging  # noqa: E402


def generate_test_video(ffmpeg_bin: Path, out: Path) -> None:
    cmd = [
        str(ffmpeg_bin), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(out),
    ]
    import subprocess
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg test-video generation failed: {proc.stderr[-1500:]}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging("DEBUG" if args.verbose else "INFO")

    from v1_poc.config import evidence_dir, load_config
    from v1_poc.env import get_secret
    from v1_poc.evidence import write_json
    from v1_poc.media_tools import ffmpeg_bin
    from v1_poc.telegram_client import TelegramClient, TelegramPublishError

    load_env()

    video_path = PROJECT_ROOT / "downloads" / "telegram_file_test.mp4"
    generate_test_video(ffmpeg_bin(), video_path)
    local_size = video_path.stat().st_size
    local_sha = sha256(video_path)
    print(f"test video: {video_path} size={local_size} sha256={local_sha}")

    cfg = load_config()
    tg_cfg = cfg.get("telegram", {})
    client = TelegramClient(
        get_secret("TELEGRAM_BOT_TOKEN"),
        get_secret("TELEGRAM_CHANNEL_ID"),
        base_url=(tg_cfg.get("api_base_url") or None),
    )

    try:
        msg = client.send_video(video_path, caption="V1_POC — SMALL FILE UPLOAD TEST")
    except TelegramPublishError as exc:
        print(f"FILE TEST FAIL — {exc.kind}: {exc}")
        write_json(
            evidence_dir("telegram") / "file_test.json",
            {"result": "FAIL", "error_kind": exc.kind, "error": str(exc), "local_size": local_size, "local_sha256": local_sha},
        )
        return 1

    video = msg.video
    ver = client.verify_upload(msg, local_size, local_sha, full_byte_check=True)
    client.close()

    write_json(
        evidence_dir("telegram") / "file_test.json",
        {
            "result": "PASS",
            "local_size": local_size,
            "local_sha256": local_sha,
            "message_id": msg.message_id,
            "chat_id": str(msg.chat.id),
            "video_file_id": video.file_id,
            "api_video_size": video.file_size,
            "api_video_duration": video.duration,
            "api_video_width": video.width,
            "api_video_height": video.height,
            "telegram_file_size": ver.telegram_file_size,
            "upload_bytes_match_local": ver.upload_bytes_match_local,
            "upload_sha256_match_local": ver.upload_sha256_match_local,
            "caption": msg.caption,
        },
    )

    print(f"FILE TEST PASS — message_id={msg.message_id} bytes_match={ver.upload_bytes_match_local} sha256_match={ver.upload_sha256_match_local}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())