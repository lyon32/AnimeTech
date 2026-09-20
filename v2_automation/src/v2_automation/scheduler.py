"""Dynamic concurrency scheduler — the opposite of a static MAX_WORKERS=10.

Two independent pools: downloads and publications.  The download allowance is
recomputed every tick from live signals:

  - cross-anime parallelism ceiling (one slot per anime with a pending head),
  - disk headroom (a reservation holds MIN_FREE_DISK in reserve),
  - adaptive boost: sustained fast transfers + ample disk add slots, capped at
    2x the configured base; slowness or failures shrink back toward the base.

Publications stay bounded by their own small pool (thumbnail+video are short
synchronous calls, not bandwidth-bound).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class Allowance:
    downloads: int
    publications: int
    paused: bool
    reason: str


class DownloadBandwidthEstimator:
    """EWMA of observed per-transfer bytes/sec (the 'burst' signal)."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._rate: float | None = None
        self._samples = 0

    def add_sample(self, bytes_sec: float) -> None:
        self._samples += 1
        if self._rate is None:
            self._rate = bytes_sec
        else:
            self._rate = self.alpha * bytes_sec + (1 - self.alpha) * self._rate

    @property
    def rate(self) -> float | None:
        return self._rate

    @property
    def samples(self) -> int:
        return self._samples


def schedule(
    *,
    base_downloads: int,
    max_publications: int,
    pending_animes: int,
    free_disk_bytes: int,
    min_free_disk_bytes: int,
    next_estimated_bytes: int | None,
    estimator: DownloadBandwidthEstimator | None,
    burst_floor_bytes_per_s: float = 2_000_000,     # ~2 MB/s sustained = healthy
    cpu_percent: float | None = None,
    ram_percent: float | None = None,
    max_cpu_percent: float = 90.0,
    max_ram_percent: float = 90.0,
) -> Allowance:
    if base_downloads < 1:
        base_downloads = 1

    # Disk gate first: never start a download without headroom.
    if free_disk_bytes < min_free_disk_bytes:
        return Allowance(downloads=0, publications=max(1, max_publications),
                         paused=True, reason="disk-bas (garde-fou)")

    if next_estimated_bytes is not None and next_estimated_bytes > free_disk_bytes:
        return Allowance(downloads=0, publications=max(1, max_publications),
                         paused=True, reason="fichier-estime-plus-grand-que-disque")

    # Cross-anime ceiling.
    max_d = max(1, pending_animes)

    # Adaptive boost (never > 2x base).
    boosted = base_downloads
    if estimator is not None and estimator.rate is not None:
        if estimator.rate >= burst_floor_bytes_per_s and estimator.samples >= 3:
            boosted = base_downloads * 2

    downloads = min(boosted, max_d)
    reason = "normal"
    # Host pressure: an overloaded CPU/RAM never gets extra parallel downloads (one at a time).
    if (cpu_percent is not None and cpu_percent > max_cpu_percent) or             (ram_percent is not None and ram_percent > max_ram_percent):
        downloads = 1
        reason = "cpu/ram-eleves"
    if downloads == max_d:
        reason = "borne-par-nb-animes"
    if boosted > base_downloads:
        reason += "+boost"
    return Allowance(downloads=downloads, publications=max(1, max_publications),
                     paused=False, reason=reason)


class DynamicScheduler:
    """Thread-safe wrapper holding the EWMA estimator and last allowance."""

    def __init__(self, config: dict):
        self._base = max(1, int(config.get("max_concurrent_downloads", 1)))
        self._pub = max(1, int(config.get("max_concurrent_publications", 1)))
        self._min_free = int(config.get("min_free_disk_bytes", 0))
        self._max_cpu = float(config.get("max_cpu_percent", 90))
        self._max_ram = float(config.get("max_ram_percent", 90))
        self._estimator = DownloadBandwidthEstimator()
        self._lock = threading.RLock()
        self.last: Allowance | None = None

    def observe_transfer(self, bytes_sec: float) -> None:
        with self._lock:
            self._estimator.add_sample(bytes_sec)

    def tick(self, *, pending_animes: int, free_disk_bytes: int,
             next_estimated_bytes: int | None = None,
             cpu_percent: float | None = None, ram_percent: float | None = None) -> Allowance:
        with self._lock:
            self.last = schedule(
                base_downloads=self._base,
                max_publications=self._pub,
                pending_animes=pending_animes,
                free_disk_bytes=free_disk_bytes,
                min_free_disk_bytes=self._min_free,
                next_estimated_bytes=next_estimated_bytes,
                estimator=self._estimator,
                cpu_percent=cpu_percent, ram_percent=ram_percent,
                max_cpu_percent=self._max_cpu, max_ram_percent=self._max_ram,
            )
            return self.last