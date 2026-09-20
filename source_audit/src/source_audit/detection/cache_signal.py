"""Extracts the site's own cache-generation timestamp (closure session, Priority 1).

Evidence: every page on voir-anime.to embeds an HTML comment from the "WP
Fastest Cache" WordPress plugin recording exactly when that page's cached copy
was generated, e.g.:

    <!-- WP Fastest Cache file was created in 0.349s, on 17-09-26 17:49:07 -->

This is a more precise and more directly-sourced signal than inferring
staleness from the `Last-Modified` HTTP header (which this session confirmed
tracks the same underlying value, but the header could in principle be altered
by an intermediate proxy/CDN layer, whereas this comment is baked into the
origin's own cached HTML). See SESSION_REPORT.md's closure session and
`output/evidence/cache_ttl_analysis.md` for the full measurement this is based
on: one regeneration boundary observed at ~33 minutes, and a separate stable
window observed lasting >=102 minutes with no regeneration -- i.e. the TTL is
not assumed fixed, only bounded from below.
"""
from __future__ import annotations

import re
from datetime import datetime

_CACHE_TIMESTAMP_RE = re.compile(
    r"WP Fastest Cache file was created in [\d.]+ seconds, on (\d{2}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2})"
)


def extract_cache_generated_at(html: str) -> str | None:
    """Returns the raw "DD-MM-YY HH:MM:SS" string from the WP Fastest Cache
    comment, or None if the page doesn't carry one (e.g. a non-cached response,
    or the plugin is disabled/removed -- a structure change worth flagging via
    detection.fingerprint if it ever happens sitewide)."""
    match = _CACHE_TIMESTAMP_RE.search(html)
    return match.group(1) if match else None


def parse_cache_generated_at(raw_timestamp: str) -> datetime:
    """Parses the "DD-MM-YY HH:MM:SS" format into a naive datetime (the site
    does not expose a timezone for this value; treat comparisons as relative,
    not absolute, unless cross-checked against the Date/Last-Modified headers
    fetched in the same request, which are timezone-aware)."""
    return datetime.strptime(raw_timestamp, "%d-%m-%y %H:%M:%S")


def is_same_cache_generation(html_a: str, html_b: str) -> bool | None:
    """True/False if both pages carry a cache timestamp and can be compared;
    None if either is missing the marker (INCONCLUSIVE, not a guessed False)."""
    ts_a = extract_cache_generated_at(html_a)
    ts_b = extract_cache_generated_at(html_b)
    if ts_a is None or ts_b is None:
        return None
    return ts_a == ts_b
