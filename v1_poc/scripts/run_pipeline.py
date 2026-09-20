#!/usr/bin/env python
"""V1_POC — end-to-end pipeline: SOURCE -> download -> validate -> Telegram.

Real execution only. If publish is enabled, the episode is uploaded once via the
real Telegram sendVideo endpoint and the published state is recorded so a restart
never double-publishes the same episode.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from source_audit.fetch.http_client import HttpClient  # noqa: E402

from v1_poc.env import load_env, has_secret, get_secret  # noqa: E402
from v1_poc.logging_config import setup_logging  # noqa: E402
from v1_poc.state import PublishedState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="Episode URL (default: config.authorized_episode_url)")
    parser.add_argument("--no-download", action="store_true", help="Skip download+validation (source/manifest only)")
    parser.add_argument("--no-publish", action="store_true", help="Skip Telegram publish")
    parser.add_argument("--keep-segments", action="store_true", help="Keep raw downloaded segments")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else "INFO")
    load_env()

    from v1_poc.config import load_config, evidence_dir, state_dir
    from v1_poc.pipeline import run_end_to_end
    from v1_poc.telegram_client import TelegramClient

    cfg = load_config()
    http_cfg = cfg["http"]
    url = args.url or cfg["source"]["authorized_episode_url"]

    state = PublishedState(state_dir() / "published_state.json")
    telegram = None
    if not args.no_publish:
        if not (has_secret("TELEGRAM_BOT_TOKEN") and has_secret("TELEGRAM_CHANNEL_ID")):
            print("ERROR: --publish selected but TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID are not set in .env")
            return 2
        tg_cfg = cfg.get("telegram", {})
        telegram = TelegramClient(
            get_secret("TELEGRAM_BOT_TOKEN"),
            get_secret("TELEGRAM_CHANNEL_ID"),
            base_url=(tg_cfg.get("api_base_url") or None),
        )

    try:
        with HttpClient(timeout_seconds=http_cfg["timeout_seconds"], max_retries=http_cfg["max_retries"]) as client:
            run = run_end_to_end(
                url,
                client,
                do_download=not args.no_download,
                do_publish=not args.no_publish,
                telegram=telegram,
                state=state if telegram else None,
                keep_segments=args.keep_segments,
            )
    finally:
        if telegram is not None:
            telegram.close()

    rows = [
        (s.name, s.status, s.detail, ", ".join(s.evidence))
        for s in run.steps
        if s.name != "END"
    ]
    for name, status, detail, evidence in rows:
        print(f"{name:<24} {status:<12} {detail}")
        if evidence:
            print(f"{'':<38} evidence: {evidence}")

    stop = run.stop_before_publish
    if stop:
        print(f"\nSTOP_BEFORE_PUBLISH: {stop}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())