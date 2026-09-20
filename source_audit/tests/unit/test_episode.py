from pathlib import Path

from source_audit.analysis.episode import parse_episode_page

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "episode_page_sample.html"
URL = "https://voir-anime.to/anime/sample-show-vf/sample-show-08-vf/"


def test_parses_page_title_and_iframe():
    record = parse_episode_page(FIXTURE.read_text(encoding="utf-8"), URL)

    assert record.url == URL
    assert record.page_title_raw == "Sample Show - Sample Show - 08 VF - 08 - Voiranime"
    assert record.player_iframe_url == "https://voembed.net/embed-sample1234.html"


def test_parses_prev_next_navigation():
    record = parse_episode_page(FIXTURE.read_text(encoding="utf-8"), URL)

    assert record.prev_episode_url == "https://voir-anime.to/anime/sample-show-vf/sample-show-07-vf/"
    assert record.next_episode_url == "https://voir-anime.to/anime/sample-show-vf/sample-show-09-vf/"


def test_parses_identity_keys():
    record = parse_episode_page(FIXTURE.read_text(encoding="utf-8"), URL)

    assert record.anime_post_id == "999888"
    assert record.anime_key == "postid:999888"
    assert record.episode_key == "https://voir-anime.to/anime/sample-show-vf/sample-show-08-vf"
    assert record.episode_number == 8


def test_no_iframe_leaves_player_url_none():
    html = "<html><head><title>t</title></head><body>no player here</body></html>"
    record = parse_episode_page(html, URL)
    assert record.player_iframe_url is None


# Phase 13 resilience.
def test_no_nav_links_leaves_prev_next_none():
    html = '<html><head><title>t</title></head><body><iframe src="https://voembed.net/embed-x.html"></iframe></body></html>'
    record = parse_episode_page(html, URL)
    assert record.prev_episode_url is None
    assert record.next_episode_url is None
    assert record.player_iframe_url == "https://voembed.net/embed-x.html"


def test_completely_empty_page_does_not_raise():
    record = parse_episode_page("<html><body></body></html>", URL)
    assert record.url == URL
    assert record.page_title_raw is None
    assert record.player_iframe_url is None
    assert record.episode_key is not None  # always derivable from the URL alone


def test_empty_string_html_does_not_raise():
    record = parse_episode_page("", URL)
    assert record.player_iframe_url is None
