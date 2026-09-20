#!/usr/bin/env python
"""V1_POC — source -> manifest inspection only (no download, no publish)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from source_audit.fetch.http_client import HttpClient  # noqa: E402

from v1_poc.env import load_env  # noqa: E402
from v1_poc.logging_config import setup_logging  # noqa: E402
from v1_poc.source_client import SourceAccessError, extract_source  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else "INFO")
    load_env()

    from v1_poc.config import load_config

    cfg = load_config()["http"]
    with HttpClient(timeout_seconds=cfg["timeout_seconds"], max_retries=cfg["max_retries"]) as client:
        try:
            ext = extract_source(args.url, client)
        except SourceAccessError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 1

        payload = {
            "episode_url": ext.episode_url,
            "episode_key": ext.episode_key,
            "anime_key": ext.anime_key,
            "episode_number": ext.episode_number,
            "player_library": ext.player_library,
            "has_obfuscated_script": ext.has_obfuscated_script,
            "manifest_url_found": ext.manifest_url is not None,
            "manifest_fingerprint": __import__("v1_poc.evidence", fromlist=["fingerprint"]).fingerprint(ext.manifest_text or ""),
            "renditions": [
                {
                    "resolution": r.resolution,
                    "bandwidth_bps": r.bandwidth_bps,
                    "fps": r.fps,
                    "video_codec": r.video_codec,
                    "audio_codec": r.audio_codec,
                }
                for r in ext.renditions
            ],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())