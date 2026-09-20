#!/usr/bin/env python
"""Fetch and parse a voir-anime.to anime page into structured JSON.

Usage:
    python scripts/analyze_anime.py --url "https://voir-anime.to/anime/mebius-dust/"
    python scripts/analyze_anime.py --url "..." --output output/reports/anime.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from source_audit.analysis.anime import parse_anime_page  # noqa: E402
from source_audit.fetch.http_client import HttpClient  # noqa: E402
from source_audit.logging_config import setup_logging  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Anime page URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None, help="Write JSON result to this path instead of stdout")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    logger = logging.getLogger("analyze_anime")

    with HttpClient(timeout_seconds=args.timeout, max_retries=2) as client:
        result = client.get(args.url)

    if not result.ok:
        logger.error(
            "Failed to fetch %s: status=%s error_type=%s attempts=%d",
            args.url,
            result.status_code,
            result.error_type.value,
            result.attempts,
        )
        return 1

    record = parse_anime_page(result.text, args.url)
    logger.info(
        "Parsed anime %r: %d episode(s) observed, %s declared total",
        record.title,
        record.episode_count_observed,
        record.total_episodes_declared,
    )

    payload = {
        "source_url": args.url,
        "fetched_status_code": result.status_code,
        "fetch_elapsed_seconds": result.elapsed_seconds,
        "record": record.model_dump(mode="json"),
    }

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
