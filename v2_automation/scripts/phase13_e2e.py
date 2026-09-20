#!/usr/bin/env python
"""Phase 13 — LIVE end-to-end against the real Local Bot API + real Telegram.

Proves the full publish chain in production wiring:
  FFmpeg     -> extract_thumbnail (real binary)     -> sendPhoto (Local Bot API)
  FFmpeg     -> generate tiny mp4                   -> sendVideo (Local Bot API)
  Local Bot API on 127.0.0.1:8081 (Docker volume /data)

Requires env (`.env` in the project root is read first):
  TELEGRAM_BOT_TOKEN   (BotFather tokens must match the Local Bot API secret)
  TELEGRAM_CHANNEL_ID  (@channel or -100...)

Strictly never prints secrets: token/channel are redacted.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v2_automation import app_config  # noqa: E402   (reads .env via load_dotenv)


def _ffmpeg() -> str:
    local = ROOT.parent / "source_audit" / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else "ffmpeg"


def _make_small_mp4(bin_ff: str, out: Path) -> None:
    import subprocess
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([bin_ff, "-y", "-f", "lavfi", "-i",
                    "testsrc=size=640x360:rate=24:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(out)],
                   check=True, capture_output=True)


def main() -> int:
    cfg = app_config.load_config()
    token, channel = cfg.bot_token.strip(), cfg.channel_id.strip()
    if len(token) <= 8 or not channel:
        print("E2E SKIPPED — TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID absents.")
        print("  Créer v2_automation/.env d'après .env.example puis relancer.")
        return 2

    import subprocess
    from v2_automation import evidence
    from v2_automation.publisher import (extract_thumbnail, Publisher,
                                         V2TelegramClient, local_bot_base_url)

    api_base = cfg.telegram.get("api_base_url", "http://127.0.0.1:8081").strip()
    work = evidence.evidence_dir("e2e")
    video_path = work / "e2e_probe.mp4"
    thumb_path = work / "e2e_probe_thumb.jpg"

    print(f"Local Bot API : {api_base}")
    _make_small_mp4(_ffmpeg(), video_path)
    assert video_path.stat().st_size > 0
    extract_thumbnail(video_path, thumb_path, Path(_ffmpeg()))

    client = V2TelegramClient(token, channel, local_bot_base_url(api_base, token))
    me = client.get_me()
    print(f"getMe ok — bot id {me.get('id')}, username {me.get('username')} (token masqué)")

    pub = Publisher(client, thumbnail_caption="V2 probe thumbnail",
                    video_caption_fn=lambda: "V2 probe end-to-end (evidée)")
    tmsg_id, tfile_id = pub.publish_thumbnail(thumb_path)
    msg = pub.publish_video(video_path, caption="V2 probe end-to-end")
    video = getattr(msg, "video", None)
    pub.close()

    assert tmsg_id and tfile_id, "la photo (thumbnail) n'a pas été publiée"
    assert video is not None and video.file_size == video_path.stat().st_size

    out = work / "e2e_proven.json"
    out.write_text(__import__("json").dumps({
        "stage": "e2e_full_publish", "api_base": api_base,
        "channel": channel, "bot_username": me.get("username"),
        "thumbnail_message_id": tmsg_id, "video_message_id": msg.message_id,
        "video_file_size": video.file_size, "sha256": evidence.sha256_file(video_path),
    }, indent=2), encoding="utf-8")
    print({
        "thumbnail_message_id": tmsg_id, "video_message_id": msg.message_id,
        "video_file_size": video.file_size,
        "preuve": str(out),
    })
    print("E2E PROVEN — publication réelle publiée dans le canal cible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())