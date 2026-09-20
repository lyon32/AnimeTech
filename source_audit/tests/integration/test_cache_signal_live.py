from __future__ import annotations

import pytest

from source_audit.detection.cache_signal import extract_cache_generated_at, is_same_cache_generation
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration


def test_live_homepage_carries_cache_timestamp():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get("https://voir-anime.to/")
    assert result.ok

    timestamp = extract_cache_generated_at(result.text)
    assert timestamp is not None, "expected a WP Fastest Cache comment on the live homepage"


def test_live_back_to_back_fetches_share_the_same_cache_generation():
    """Corroborates the closure-session caching finding directly: two fetches
    moments apart must report the SAME cache-generation timestamp, since the
    measured stable window is >=102 minutes."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        first = client.get("https://voir-anime.to/")
        second = client.get("https://voir-anime.to/")

    assert first.ok and second.ok
    assert is_same_cache_generation(first.text, second.text) is True
