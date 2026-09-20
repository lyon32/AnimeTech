#!/usr/bin/env python
"""Fetch and parse voir-anime.to's homepage (or a pagination page) into structured JSON.

Usage:
    python scripts/analyze_homepage.py
    python scripts/analyze_homepage.py --page 2 --output output/reports/homepage_page2.json
    python scripts/analyze_homepage.py --url https://voir-anime.to/ --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from source_audit.analysis.homepage import parse_homepage  # noqa: E402
from source_audit.fetch.http_client import HttpClient  # noqa: E402
from source_audit.logging_config import setup_logging  # noqa: E402

DEFAULT_BASE_URL = "https://voir-anime.to/"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="Base site URL (default: %(default)s)")
    parser.add_argument("--page", type=int, default=None, help="Pagination page number (e.g. 2 for /page/2/)")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None, help="Write JSON result to this path instead of stdout")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    logger = logging.getLogger("analyze_homepage")

    target_url = args.url
    if args.page:
        target_url = target_url.rstrip("/") + f"/page/{args.page}/"

    with HttpClient(timeout_seconds=args.timeout, max_retries=2) as client:
        result = client.get(target_url)

    if not result.ok:
        logger.error(
            "Failed to fetch %s: status=%s error_type=%s attempts=%d",
            target_url,
            result.status_code,
            result.error_type.value,
            result.attempts,
        )
        return 1

    entries = parse_homepage(result.text)
    logger.info("Parsed %d episode entries from %s", len(entries), target_url)

    payload = {
        "source_url": target_url,
        "fetched_status_code": result.status_code,
        "fetch_elapsed_seconds": result.elapsed_seconds,
        "entry_count": len(entries),
        "entries": [e.model_dump(mode="json") for e in entries],
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
