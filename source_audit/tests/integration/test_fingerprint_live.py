from __future__ import annotations

import pytest

from source_audit.detection.fingerprint import (
    ANIME_PAGE_SELECTORS,
    EPISODE_PAGE_SELECTORS,
    HOMEPAGE_SELECTORS,
    compute_fingerprint,
    find_regressed_selectors,
)
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration

# A fully-present fingerprint (every critical selector matches >=1 element) is the
# baseline established across Phases 2-5 against the live site. This test re-checks
# that baseline still holds today -- if it doesn't, that's a real structure-change
# finding to investigate and document, not a bug in this test.
FULLY_PRESENT_HOMEPAGE = {name: True for name in HOMEPAGE_SELECTORS}
FULLY_PRESENT_ANIME = {name: True for name in ANIME_PAGE_SELECTORS}
FULLY_PRESENT_EPISODE = {name: True for name in EPISODE_PAGE_SELECTORS}


def test_live_homepage_matches_phase2_baseline():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get("https://voir-anime.to/")
    assert result.ok

    current = compute_fingerprint(result.text, HOMEPAGE_SELECTORS)
    regressed = find_regressed_selectors(FULLY_PRESENT_HOMEPAGE, current)
    assert regressed == [], f"Homepage structure regressed vs Phase 2 baseline: {regressed}"


def test_live_anime_page_matches_phase3_baseline():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get("https://voir-anime.to/anime/mebius-dust/")
    assert result.ok

    current = compute_fingerprint(result.text, ANIME_PAGE_SELECTORS)
    regressed = find_regressed_selectors(FULLY_PRESENT_ANIME, current)
    assert regressed == [], f"Anime page structure regressed vs Phase 3 baseline: {regressed}"


def test_live_episode_page_matches_phase4_baseline():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get(
            "https://voir-anime.to/anime/mebius-dust/mebius-dust-01-vostfr/"
        )
    assert result.ok

    current = compute_fingerprint(result.text, EPISODE_PAGE_SELECTORS)
    regressed = find_regressed_selectors(FULLY_PRESENT_EPISODE, current)
    assert regressed == [], f"Episode page structure regressed vs Phase 4 baseline: {regressed}"
