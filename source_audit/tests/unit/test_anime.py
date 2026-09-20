from pathlib import Path

from source_audit.analysis.anime import parse_anime_page

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FULL_FIXTURE = FIXTURES / "anime_page_sample.html"
MISSING_OPTIONAL_FIXTURE = FIXTURES / "anime_page_sample_missing_optional_fields.html"

FULL_URL = "https://voir-anime.to/anime/sample-show-vf/"
MISSING_URL = "https://voir-anime.to/anime/sample-no-optional/"


def test_parses_title_and_core_metadata():
    record = parse_anime_page(FULL_FIXTURE.read_text(encoding="utf-8"), FULL_URL)

    assert record.title == "Sample Show (VF)"
    assert record.url == FULL_URL
    assert record.native_title == "サンプルショー"
    assert record.romaji_title == "Sample Show"
    assert record.english_title == "Sample Show"
    assert record.anime_type_raw == "TV"
    assert record.status_raw == "EN COURS"
    assert record.studios == "Sample Studio"
    assert record.start_date_raw == "Jul 3, 2026"
    assert record.post_id == "777666"


def test_parses_genres_as_list():
    record = parse_anime_page(FULL_FIXTURE.read_text(encoding="utf-8"), FULL_URL)
    assert record.genres == ["Action", "Fantasy"]


def test_total_episodes_declared_vs_observed_can_differ():
    record = parse_anime_page(FULL_FIXTURE.read_text(encoding="utf-8"), FULL_URL)
    # Fixture declares 12 total planned but only lists 3 released episodes.
    assert record.total_episodes_declared == 12
    assert record.episode_count_observed == 3
    assert len(record.episode_links) == 3


def test_episode_links_preserve_url_and_date():
    record = parse_anime_page(FULL_FIXTURE.read_text(encoding="utf-8"), FULL_URL)
    first = record.episode_links[0]
    assert first.url == "https://voir-anime.to/anime/sample-show-vf/sample-show-08-vf/"
    assert first.published_at_raw == "21 hours ago"


def test_missing_optional_fields_are_none_not_guessed():
    record = parse_anime_page(MISSING_OPTIONAL_FIXTURE.read_text(encoding="utf-8"), MISSING_URL)

    assert record.english_title is None
    assert record.total_episodes_declared is None
    assert record.native_title == "サンプル"
    assert record.episode_count_observed == 1


# Phase 13 resilience: a fully empty/malformed page must not raise, everything
# resolves to None/empty rather than a guess.
def test_completely_empty_page_does_not_raise():
    record = parse_anime_page("<html><body></body></html>", "https://voir-anime.to/anime/x/")

    assert record.url == "https://voir-anime.to/anime/x/"
    assert record.title is None
    assert record.post_id is None
    assert record.episode_links == []
    assert record.episode_count_observed == 0
    assert record.genres == []


def test_empty_string_html_does_not_raise():
    record = parse_anime_page("", "https://voir-anime.to/anime/x/")
    assert record.title is None
    assert record.episode_links == []
