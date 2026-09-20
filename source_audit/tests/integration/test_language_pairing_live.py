from __future__ import annotations

import pytest

from source_audit.analysis.anime import parse_anime_page
from source_audit.analysis.identity import language_pair_confidence
from source_audit.fetch.http_client import HttpClient
from source_audit.models import Confidence

pytestmark = pytest.mark.integration

# 3 real, independently-confirmed VF/VOSTFR (or VF/JAP) pairs of the same
# underlying work, established in Phases 3/7.
KNOWN_PAIRS = [
    ("https://voir-anime.to/anime/tomb-raider-king-vf/", "https://voir-anime.to/anime/tomb-raider-king-jap/"),
    (
        "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4-vf/",
        "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4/",
    ),
    ("https://voir-anime.to/anime/meitantei-conan-vf/", "https://voir-anime.to/anime/meitantei-conan/"),
]

# 2 unrelated anime -- must NOT be reported as a high-confidence pair.
UNRELATED_PAIR = (
    "https://voir-anime.to/anime/mebius-dust/",
    "https://voir-anime.to/anime/the-exiled-heavy-knight-knows-how-to-game-the-system/",
)


def test_live_known_pairs_resolve_high_confidence():
    with HttpClient(timeout_seconds=20.0, max_retries=2) as client:
        for url_a, url_b in KNOWN_PAIRS:
            result_a = client.get(url_a)
            result_b = client.get(url_b)
            assert result_a.ok and result_b.ok

            record_a = parse_anime_page(result_a.text, url_a)
            record_b = parse_anime_page(result_b.text, url_b)

            confidence = language_pair_confidence(
                record_a.native_title, record_a.romaji_title, record_b.native_title, record_b.romaji_title
            )
            assert confidence == Confidence.HIGH, f"expected HIGH for {url_a} <-> {url_b}, got {confidence}"


def test_live_unrelated_anime_are_not_a_high_confidence_pair():
    with HttpClient(timeout_seconds=20.0, max_retries=2) as client:
        result_a = client.get(UNRELATED_PAIR[0])
        result_b = client.get(UNRELATED_PAIR[1])
    assert result_a.ok and result_b.ok

    record_a = parse_anime_page(result_a.text, UNRELATED_PAIR[0])
    record_b = parse_anime_page(result_b.text, UNRELATED_PAIR[1])

    confidence = language_pair_confidence(
        record_a.native_title, record_a.romaji_title, record_b.native_title, record_b.romaji_title
    )
    assert confidence in (Confidence.LOW, Confidence.UNKNOWN)
