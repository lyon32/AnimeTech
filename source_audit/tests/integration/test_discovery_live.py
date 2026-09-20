from __future__ import annotations

import pytest

from source_audit.analysis.homepage import parse_homepage
from source_audit.detection.discovery import diff_known_episode_keys, entries_to_episode_keys
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration


def test_live_discovery_against_real_homepage():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get("https://voir-anime.to/")
    assert result.ok

    entries = parse_homepage(result.text)
    assert len(entries) > 0

    known = entries_to_episode_keys(entries)
    # Nothing "new" against keys derived from the same snapshot.
    assert diff_known_episode_keys(entries, known) == []
    # Everything is "new" against an empty known set.
    assert len(diff_known_episode_keys(entries, set())) == len(entries)


def test_live_homepage_served_from_origin_cache():
    """Phase 9 finding: Last-Modified stays identical across back-to-back requests
    (origin-side page cache, TTL >= tens of minutes measured this session), and
    CF-Cache-Status is DYNAMIC (ruling out Cloudflare edge caching as the cause)."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        first = client.get("https://voir-anime.to/")
        second = client.get("https://voir-anime.to/")

    assert first.ok and second.ok
    lm1 = first.headers.get("last-modified") if first.headers else None
    lm2 = second.headers.get("last-modified") if second.headers else None
    assert lm1 is not None
    assert lm1 == lm2
