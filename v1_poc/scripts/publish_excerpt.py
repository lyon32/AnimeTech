#!/usr/bin/env python
"""V1_POC — publish a REAL excerpt of the most recently downloaded episode.

Full episode files (often >600 MB) exceed the Telegram Bot API limits: 50 MiB
for sendVideo uploads and 20 MB for getFile re-download (both measured on this
POC; see evidence/telegram/oversize_probe.json and the earlier "File is too big"
observation). This script cuts a real 45 s excerpt (stream copy — no re-encode),
uploads it with the real sendVideo method, then re-downloads it via getFile and
compares size and SHA-256 with the local excerpt. The published-state record is
keyed by the episode so a pipeline restart never double-publishes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from source_audit.fetch.http_client import HttpClient  # noqa: E402

from v1_poc.env import load_env  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cut_excerpt(ffmpeg_bin: Path, src: Path, out: Path, seconds: int) -> None:
    cmd = [
        str(ffmpeg_bin), "-y", "-loglevel", "error", "-ss", "0", "-t", str(seconds),
        "-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"excerpt cut failed: {proc.stderr[-1500:]}")


def _detect_language(episode_url: str) -> str:
    low = episode_url.lower()
    if "-vostfr" in low:
        return "VOSTFR"
    if "-vf/" in low or low.rstrip("/").endswith("-vf"):
        return "VF"
    return "UNKNOWN"


def _episode_slug(episode_key: str) -> str:
    return episode_key.replace("https://", "").replace("/", "_")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="Episode URL (default: config.authorized_episode_url)")
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging = __import__("v1_poc.logging_config", fromlist=["setup_logging"]).setup_logging
    setup_logging("DEBUG" if args.verbose else "INFO")

    from v1_poc.config import evidence_dir, load_config, state_dir
    from v1_poc.env import get_secret
    from v1_poc.evidence import write_json
    from v1_poc.media_tools import ffmpeg_bin
    from v1_poc.source_client import extract_source
    from v1_poc.state import PublishedState
    from v1_poc.telegram_client import TelegramClient, TelegramPublishError

    load_env()
    cfg = load_config()
    http_cfg = cfg.get("http", {})
    url = args.url or cfg["source"]["authorized_episode_url"]

    dl_meas = json.loads((evidence_dir("media") / "download_measurements.json").read_text(encoding="utf-8"))
    full_path = Path(dl_meas["output_path"])
    if not full_path.exists():
        print(f"ERROR: full episode file not found: {full_path}")
        return 2
    full_sha = json.loads((evidence_dir("media") / "sha256.json").read_text(encoding="utf-8"))["sha256"]
    full_size = dl_meas["final_file_size"]

    with HttpClient(timeout_seconds=http_cfg["timeout_seconds"], max_retries=http_cfg["max_retries"]) as client:
        ext = extract_source(url, client)
    episode_key = ext.episode_key
    episode_number = ext.episode_number
    language = _detect_language(url)
    anime_title = url.split("/")[4].replace("-", " ").title() if len(url.split("/")) > 4 else episode_key
    slug = _episode_slug(episode_key)

    excerpt_path = PROJECT_ROOT / "downloads" / f"excerpt_{slug.split('/')[-1]}_{args.seconds}s.mp4"
    cut_excerpt(ffmpeg_bin(), full_path, excerpt_path, args.seconds)
    excerpt_sha = sha256(excerpt_path)
    excerpt_size = excerpt_path.stat().st_size
    print(f"excerpt: {excerpt_path} size={excerpt_size} sha256={excerpt_sha}")

    tg_cfg = cfg.get("telegram", {})
    client = TelegramClient(
        get_secret("TELEGRAM_BOT_TOKEN"),
        get_secret("TELEGRAM_CHANNEL_ID"),
        base_url=(tg_cfg.get("api_base_url") or None),
    )

    caption = (
        f"Extrait {args.seconds}s (preuve) - {anime_title} épisode {episode_number} {language}\n"
        f"SHA256(full)={full_sha}\n"
        f"full_size={full_size} bytes\n"
        f"SHA256(excerpt)={excerpt_sha}"
    )
    try:
        msg = client.send_video(excerpt_path, caption=caption)
    except TelegramPublishError as exc:
        client.close()
        print(f"EXCERPT PUBLISH FAIL - {exc.kind}: {exc}")
        write_json(
            evidence_dir("telegram") / f"excerpt_publish__{slug}.json",
            {
                "result": "FAIL",
                "episode_key": episode_key,
                "error_kind": exc.kind,
                "error": str(exc),
                "excerpt_seconds": args.seconds,
                "excerpt_size": excerpt_size,
                "excerpt_sha256": excerpt_sha,
                "full_episode_sha256": full_sha,
            },
        )
        return 1

    video = msg.video
    ver = client.verify_upload(msg, excerpt_size, excerpt_sha, full_byte_check=True)
    client.close()

    evidence = {
        "result": "PASS",
        "episode_key": episode_key,
        "episode_url": url,
        "anime_title": anime_title,
        "episode_number": episode_number,
        "language": language,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caption": msg.caption,
        "episode_full_file": str(full_path),
        "full_episode_size_bytes": full_size,
        "full_episode_sha256": full_sha,
        "excerpt_file": str(excerpt_path),
        "excerpt_seconds": args.seconds,
        "excerpt_size_bytes": excerpt_size,
        "excerpt_sha256": excerpt_sha,
        "message_id": msg.message_id,
        "chat_id": str(msg.chat.id),
        "chat_title": ver.channel_title,
        "chat_type": ver.channel_type,
        "video_file_id": video.file_id if video else None,
        "api_video_size": video.file_size if video else None,
        "api_video_duration": video.duration if video else None,
        "api_video_width": video.width if video else None,
        "api_video_height": video.height if video else None,
        "telegram_file_size": ver.telegram_file_size,
        "upload_bytes_match_local": ver.upload_bytes_match_local,
        "upload_sha256_match_local": ver.upload_sha256_match_local,
    }
    write_json(evidence_dir("telegram") / f"excerpt_publish__{slug}.json", evidence)

    ok = ver.upload_bytes_match_local is True and ver.upload_sha256_match_local is True
    if ok:
        state = PublishedState(state_dir() / "published_state.json")
        state.record(
            episode_key,
            {
                "episode_url": url,
                "episode_key": episode_key,
                "published_at": evidence["published_at"],
                "telegram_message_id": msg.message_id,
                "sha256": full_sha,
                "file_size": full_size,
                "channel_id": str(msg.chat.id),
                "published_artifact": f"excerpt_{args.seconds}s",
                "excerpt_sha256": excerpt_sha,
                "excerpt_size_bytes": excerpt_size,
            },
        )
    print(f"EXCERPT PUBLISH {'PASS' if ok else 'FAIL'} - message_id={msg.message_id} bytes_match={ver.upload_bytes_match_local} sha256_match={ver.upload_sha256_match_local}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())