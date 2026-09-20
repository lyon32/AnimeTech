from __future__ import annotations

import pytest

from source_audit.analysis.anime import parse_anime_page
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration

# 5+ distinct anime pages per MASTER_PLAN.md Phase 3, covering: VOSTFR-only,
# a VF/JAP language pair of the same title, and a VOSTFR/VF pair of a season-4
# franchise entry.
ANIME_URLS = [
    "https://voir-anime.to/anime/the-exiled-heavy-knight-knows-how-to-game-the-system/",
    "https://voir-anime.to/anime/tomb-raider-king-vf/",
    "https://voir-anime.to/anime/tomb-raider-king-jap/",
    "https://voir-anime.to/anime/mebius-dust/",
    "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4/",
    "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4-vf/",
]


def test_live_anime_pages_all_parse_with_title_and_episodes():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        for url in ANIME_URLS:
            result = client.get(url)
            assert result.ok, f"fetch failed for {url}: {result.error_type}"

            record = parse_anime_page(result.text, url)
            assert record.title, f"no title parsed for {url}"
            assert len(record.episode_links) > 0, f"no episodes parsed for {url}"
            for ep in record.episode_links:
                assert ep.url.startswith("https://voir-anime.to/")


def test_live_vf_and_jap_variants_are_independent_anime_entities():
    """Confirms the Phase 3 finding: VF/VOSTFR of the same title are separate pages
    with independently numbered episode lists, not tabs on one page."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        vf_result = client.get("https://voir-anime.to/anime/tomb-raider-king-vf/")
        jap_result = client.get("https://voir-anime.to/anime/tomb-raider-king-jap/")

    assert vf_result.ok and jap_result.ok

    vf_record = parse_anime_page(vf_result.text, "https://voir-anime.to/anime/tomb-raider-king-vf/")
    jap_record = parse_anime_page(jap_result.text, "https://voir-anime.to/anime/tomb-raider-king-jap/")

    assert vf_record.url != jap_record.url
    vf_urls = {e.url for e in vf_record.episode_links}
    jap_urls = {e.url for e in jap_record.episode_links}
    assert vf_urls.isdisjoint(jap_urls)
