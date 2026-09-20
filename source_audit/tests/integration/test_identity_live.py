from __future__ import annotations

import pytest

from source_audit.analysis.anime import parse_anime_page
from source_audit.analysis.episode import parse_episode_page
from source_audit.analysis.identity import build_anime_key
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration

ANIME_URL = "https://voir-anime.to/anime/mebius-dust/"
EPISODE_URL = "https://voir-anime.to/anime/mebius-dust/mebius-dust-11-vostfr/"
EPISODE_URL_2 = "https://voir-anime.to/anime/mebius-dust/mebius-dust-01-vostfr/"


def test_live_anime_key_matches_across_anime_and_episode_pages():
    """Phase 8: the anime page and every one of its episode pages must report the
    same body.postid-N, confirming anime_key is a valid shared identifier."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        anime_result = client.get(ANIME_URL)
        ep1_result = client.get(EPISODE_URL)
        ep2_result = client.get(EPISODE_URL_2)

    assert anime_result.ok and ep1_result.ok and ep2_result.ok

    anime_record = parse_anime_page(anime_result.text, ANIME_URL)
    ep1_record = parse_episode_page(ep1_result.text, EPISODE_URL)
    ep2_record = parse_episode_page(ep2_result.text, EPISODE_URL_2)

    assert anime_record.post_id is not None
    expected_key = build_anime_key(anime_record.post_id)

    assert ep1_record.anime_key == expected_key
    assert ep2_record.anime_key == expected_key
    # Different episodes of the same anime share anime_key but have distinct
    # episode_key/episode_number.
    assert ep1_record.episode_key != ep2_record.episode_key
    assert ep1_record.episode_number == 11
    assert ep2_record.episode_number == 1


def test_live_episode_key_stable_across_repeated_fetch():
    """Fetching the same episode twice must yield the same episode_key (Phase 10
    dedup prerequisite: re-observing a known episode must be recognizable as such)."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        first = client.get(EPISODE_URL)
        second = client.get(EPISODE_URL)

    assert first.ok and second.ok

    record1 = parse_episode_page(first.text, EPISODE_URL)
    record2 = parse_episode_page(second.text, EPISODE_URL)

    assert record1.episode_key == record2.episode_key
    assert record1.anime_key == record2.anime_key
