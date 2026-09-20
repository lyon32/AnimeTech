#!/usr/bin/env python
"""V1_POC — Telegram connection smoke test (sendMessage), POC spec clause 8."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from v1_poc.env import load_env  # noqa: E402
from v1_poc.logging_config import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging("DEBUG" if args.verbose else "INFO")

    from v1_poc.env import get_secret
    from v1_poc.evidence import write_json
    from v1_poc.config import evidence_dir, load_config
    from v1_poc.telegram_client import TelegramClient, TelegramPublishError

    load_env()
    token = get_secret("TELEGRAM_BOT_TOKEN")
    channel = get_secret("TELEGRAM_CHANNEL_ID")
    cfg = load_config()
    tg_cfg = cfg.get("telegram", {})
    client = TelegramClient(
        token, channel, base_url=(tg_cfg.get("api_base_url") or None),
        read_timeout=120.0, write_timeout=120.0,
    )

    me = client.get_me()
    chat = client.get_chat()
    print(f"bot={me['username']} channel={channel!r} chat_type={chat['type']} title={chat['title']!r} id={chat['id']}")

    text = f"V1_POC — TELEGRAM CONNECTION TEST\n{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    try:
        msg = client.send_message(text)
    except TelegramPublishError as exc:
        print(f"SMOKE TEST FAIL — {exc.kind}: {exc}")
        client.close()
        write_json(
            evidence_dir("telegram") / "smoke_test.json",
            {"result": "FAIL", "error_kind": exc.kind, "error": str(exc)},
        )
        return 1

    client.close()
    write_json(
        evidence_dir("telegram") / "smoke_test.json",
        {
            "result": "PASS",
            "bot_username": me["username"],
            "channel_id": channel,
            "chat_type": chat["type"],
            "chat_title": chat["title"],
            "message_id": msg.message_id,
            "message_text": msg.text,
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    print(f"SMOKE TEST PASS — message_id={msg.message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())