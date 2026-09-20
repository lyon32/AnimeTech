from __future__ import annotations

import pytest

from source_audit.analysis.anime import parse_anime_page
from source_audit.analysis.identity import extract_episode_number_from_url
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration

# Détective Conan (VOSTFR) -- the largest anime found on this site during the
# closure-session search, 1208+ episodes on a single, unpaginated page.
LONG_ANIME_URL = "https://voir-anime.to/anime/meitantei-conan/"


def test_live_long_running_anime_has_no_pagination_and_parses_fully():
    with HttpClient(timeout_seconds=30.0, max_retries=2) as client:
        result = client.get(LONG_ANIME_URL)
    assert result.ok

    record = parse_anime_page(result.text, LONG_ANIME_URL)

    # A regression guard, not a hardcoded expectation of the exact current
    # count: the site's catalog can grow, so assert a generous lower bound
    # rather than an exact number.
    assert len(record.episode_links) > 500

    numbers = [extract_episode_number_from_url(e.url) for e in record.episode_links]
    assert all(n is not None for n in numbers), "every episode number should be parseable"
    assert numbers == sorted(numbers, reverse=True), "episode list must stay descending"
    assert len(numbers) == len(set(numbers)), "no duplicate episode numbers"
