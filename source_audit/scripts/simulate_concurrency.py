#!/usr/bin/env python
"""Phase 15 — concurrency simulation.

Measures wall-clock duration, process CPU time, and RSS memory delta for fetching
a fixed, small pool of real episode pages under a few different worker-count
configurations (ThreadPoolExecutor + HttpClient), to produce measured evidence
for a future V1's concurrency limit instead of picking MAX_WORKERS arbitrarily
(MASTER_PLAN.md §26).

Deliberately reuses the SAME small pool of already-known episode URLs (7, from
Phases 4-6) across all worker-count trials rather than fetching novel URLs each
time, to keep total live request volume bounded and considerate -- consistent
with the Phase 0 authorization decision (rate-limit own requests, no burst
hammering). Total requests across all trials in the default config: 4 worker
counts x 7 URLs = 28 GETs, comparable to a single earlier phase's live sampling.

Usage:
    python scripts/simulate_concurrency.py
    python scripts/simulate_concurrency.py --worker-counts 1 2 4 8 16 --output out.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from source_audit.fetch.http_client import HttpClient  # noqa: E402
from source_audit.logging_config import setup_logging  # noqa: E402

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

DEFAULT_URLS = [
    "https://voir-anime.to/anime/the-exiled-heavy-knight-knows-how-to-game-the-system/the-exiled-heavy-knight-knows-how-to-game-the-system-12-vostfr/",
    "https://voir-anime.to/anime/the-exiled-heavy-knight-knows-how-to-game-the-system/the-exiled-heavy-knight-knows-how-to-game-the-system-01-vostfr/",
    "https://voir-anime.to/anime/mebius-dust/mebius-dust-11-vostfr/",
    "https://voir-anime.to/anime/tomb-raider-king-vf/tomb-raider-king-08-vf/",
    "https://voir-anime.to/anime/tomb-raider-king-jap/tomb-raider-king-jap-11-vostfr/",
    "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4/re-zero-kara-hajimeru-isekai-seikatsu-saison-4-17-vostfr/",
    "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4-vf/re-zero-kara-hajimeru-isekai-seikatsu-saison-4-14-vf/",
]


def fetch_one(url: str, timeout: float) -> dict:
    with HttpClient(timeout_seconds=timeout, max_retries=1) as client:
        result = client.get(url)
    return {"url": url, "status_code": result.status_code, "elapsed_seconds": result.elapsed_seconds}


def run_trial(urls: list[str], worker_count: int, timeout: float) -> dict:
    process = psutil.Process() if _HAS_PSUTIL else None
    rss_before = process.memory_info().rss if process else None
    cpu_before = time.process_time()
    wall_before = time.monotonic()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(lambda u: fetch_one(u, timeout), urls))

    wall_after = time.monotonic()
    cpu_after = time.process_time()
    rss_after = process.memory_info().rss if process else None

    ok_count = sum(1 for r in results if r["status_code"] == 200)

    return {
        "worker_count": worker_count,
        "request_count": len(urls),
        "ok_count": ok_count,
        "wall_clock_seconds": wall_after - wall_before,
        "cpu_time_seconds": cpu_after - cpu_before,
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_delta_bytes": (rss_after - rss_before) if (process and rss_before is not None) else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-counts", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging("DEBUG" if args.verbose else "INFO")
    logger = logging.getLogger("simulate_concurrency")

    if not _HAS_PSUTIL:
        logger.warning("psutil not installed -- RSS memory measurements will be omitted")

    trials = []
    for worker_count in args.worker_counts:
        logger.info("Running trial: worker_count=%d, %d requests", worker_count, len(DEFAULT_URLS))
        trial = run_trial(DEFAULT_URLS, worker_count, args.timeout)
        logger.info(
            "worker_count=%d wall=%.3fs cpu=%.3fs ok=%d/%d rss_delta=%s",
            trial["worker_count"],
            trial["wall_clock_seconds"],
            trial["cpu_time_seconds"],
            trial["ok_count"],
            trial["request_count"],
            trial["rss_delta_bytes"],
        )
        trials.append(trial)

    payload = {"trials": trials, "psutil_available": _HAS_PSUTIL}
    output_text = json.dumps(payload, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_text, encoding="utf-8")
        logger.info("Wrote report to %s", args.output)
    else:
        print(output_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
