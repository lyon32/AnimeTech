#!/usr/bin/env python
"""Fetch and parse a voir-anime.to episode page (and, optionally, follow its player
iframe and HLS manifest) into structured JSON.

Usage:
    python scripts/analyze_episode.py --url "https://voir-anime.to/anime/.../ep-01-vostfr/"
    python scripts/analyze_episode.py --url "..." --follow-player --output out.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from source_audit.analysis.episode import parse_episode_page  # noqa: E402
from source_audit.analysis.media import parse_hls_master_manifest  # noqa: E402
from source_audit.analysis.player import parse_embed_page  # noqa: E402
from source_audit.fetch.http_client import HttpClient  # noqa: E402
from source_audit.logging_config import setup_logging  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Episode page URL")
    parser.add_argument(
        "--follow-player",
        action="store_true",
        help="Also fetch the player iframe and, if found, its HLS master manifest",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None, help="Write JSON result to this path instead of stdout")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    logger = logging.getLogger("analyze_episode")

    payload: dict[str, object] = {"source_url": args.url}

    with HttpClient(timeout_seconds=args.timeout, max_retries=2) as client:
        result = client.get(args.url)
        if not result.ok:
            logger.error(
                "Failed to fetch %s: status=%s error_type=%s", args.url, result.status_code, result.error_type.value
            )
            return 1

        record = parse_episode_page(result.text, args.url)
        payload["episode"] = record.model_dump(mode="json")
        logger.info("Parsed episode page: iframe=%s", record.player_iframe_url)

        if args.follow_player and record.player_iframe_url:
            embed_result = client.get(record.player_iframe_url)
            if not embed_result.ok:
                logger.warning("Failed to fetch player iframe %s: %s", record.player_iframe_url, embed_result.error_type.value)
            else:
                player_obs = parse_embed_page(embed_result.text, record.player_iframe_url)
                payload["player"] = player_obs.model_dump(mode="json")
                logger.info(
                    "Parsed player: library=%s manifest_found=%s",
                    player_obs.player_library,
                    player_obs.manifest_url_found,
                )

                if player_obs.manifest_url:
                    manifest_result = client.get(player_obs.manifest_url)
                    if not manifest_result.ok:
                        logger.warning(
                            "Failed to fetch manifest %s: %s",
                            player_obs.manifest_url,
                            manifest_result.error_type.value,
                        )
                    else:
                        renditions = parse_hls_master_manifest(manifest_result.text)
                        payload["media_renditions"] = [r.model_dump(mode="json") for r in renditions]
                        logger.info("Parsed %d media rendition(s)", len(renditions))

    output_text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_text, encoding="utf-8")
        logger.info("Wrote report to %s", args.output)
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
