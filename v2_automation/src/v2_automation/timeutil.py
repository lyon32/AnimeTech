"""UTC timestamp helpers (single source of truth)."""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(iso: str) -> datetime:
    s = iso.strip().rstrip("Z")
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ago_seconds(iso: str) -> float:
    try:
        return max(0.0, (datetime.now(timezone.utc) - _as_utc(iso)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def utc_diff_seconds(iso_later: str, iso_earlier: str) -> float:
    """Signed difference (later - earlier), for expiry comparisons
    (never clamped — unlike ago_seconds)."""
    try:
        return (_as_utc(iso_later) - _as_utc(iso_earlier)).total_seconds()
    except (ValueError, TypeError):
        return float("inf") if iso_earlier else 0.0


def add_seconds(iso: str, seconds: float) -> str:
    from datetime import timedelta
    try:
        return (_as_utc(iso) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return iso