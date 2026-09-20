#!/usr/bin/env python
"""Phase (closure) — Cache TTL measurement.

Polls a fixed set of pages at a fixed interval, recording Last-Modified/Date/
CF-Cache-Status/Age headers and a content fingerprint (sha256 of the response
body) for each sample. Intended to run for an extended, unattended period
(tens of minutes to a few hours) to observe cache regeneration boundaries
directly, rather than assuming a TTL from 2 data points as the prior session did.

Deliberately polls only 2 pages (homepage, one anime page) at a conservative
interval (default 5 minutes) to keep total request volume low across a long
run, consistent with the Phase 0 authorization decision (no burst hammering).

Usage:
    python scripts/measure_cache_ttl.py --duration-minutes 60 --interval-seconds 300 --output output/evidence/cache_ttl_samples.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from source_audit.fetch.http_client import HttpClient  # noqa: E402
from source_audit.logging_config import setup_logging  # noqa: E402

URLS = {
    "homepage": "https://voir-anime.to/",
    "anime_mebius_dust": "https://voir-anime.to/anime/mebius-dust/",
}


def sample_once(client: HttpClient) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    entry = {"sampled_at": now, "pages": {}}
    for name, url in URLS.items():
        result = client.get(url)
        content_hash = hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16] if result.text else None
        headers = result.headers or {}
        entry["pages"][name] = {
            "status_code": result.status_code,
            "elapsed_seconds": result.elapsed_seconds,
            "last_modified": headers.get("last-modified"),
            "date": headers.get("date"),
            "age": headers.get("age"),
            "cf_cache_status": headers.get("cf-cache-status"),
            "content_hash": content_hash,
            "content_bytes": result.content_bytes,
        }
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-minutes", type=float, default=60.0)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--output", type=Path, default=Path("output/evidence/cache_ttl_samples.jsonl"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging("DEBUG" if args.verbose else "INFO")
    logger = logging.getLogger("measure_cache_ttl")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration_minutes * 60
    sample_count = 0

    with HttpClient(timeout_seconds=15.0, max_retries=2) as client, args.output.open("a", encoding="utf-8") as f:
        while True:
            entry = sample_once(client)
            f.write(json.dumps(entry) + "\n")
            f.flush()
            sample_count += 1
            logger.info(
                "sample %d at %s: homepage last-modified=%s hash=%s",
                sample_count,
                entry["sampled_at"],
                entry["pages"]["homepage"]["last_modified"],
                entry["pages"]["homepage"]["content_hash"],
            )
            if time.monotonic() + args.interval_seconds > deadline:
                break
            time.sleep(args.interval_seconds)

    logger.info("Done: %d samples written to %s", sample_count, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
