#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test: fetch anime page and post a minateur (summary) to Telegram.
Never posts the source video."""
from __future__ import annotations

import sys
import re
import os
from pathlib import Path

os.environ["PYTHONUTF8"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v2_automation import app_config


def main() -> int:
    cfg = app_config.load_config()
    token = cfg.bot_token.strip()
    channel = cfg.channel_id.strip()
    if len(token) <= 8 or not channel:
        print("SKIPPED - TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID absentes.")
        return 2

    from v2_automation.publisher import V2TelegramClient
    import httpx

    target_url = "https://voir-anime.to/anime/one-piece-film-red-2/film-vostfr-one-piece-red/"

    # 1) Fetch the anime page
    print(f"Fetching: {target_url}")
    client = httpx.Client(timeout=20, follow_redirects=True)
    try:
        resp = client.get(target_url)
        resp.raise_for_status()
        html = resp.text
        print(f"Page fetched - status {resp.status_code}, {len(html)} chars")

        # 2) Extract metadata from HTML
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else "One Piece Film Red 2"

        desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
        description = desc_match.group(1).strip() if desc_match else ""
        clean_desc = re.sub(r'<[^>]+>', '', description)[:300] if description else ""

        print(f"Title: {title}")
        print(f"Description found: {bool(description)}")

        # 3) Post the minateur to Telegram
        bot = V2TelegramClient(token, channel)

        # Use only ASCII to avoid encoding issues on Windows
        minateur = (
            f"[ANIME] {title}\n\n"
            f"[DESCRIPTION] {clean_desc}\n\n"
            f"[SOURCE] voir-anime.to\n"
            f"[NOTE] This is a minateur - NO source video was posted."
        )

        print("\nPosting minateur to Telegram...")
        msg = bot.send_message(minateur)
        print(f"OK - Minateur posted! Message ID: {msg.message_id}")
        bot.close()
        return 0

    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
