from pathlib import Path

from source_audit.analysis.homepage import detect_language, parse_homepage
from source_audit.models import Confidence, Language

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "homepage_sample.html"


def _entries():
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_homepage(html)


def test_parses_expected_entry_count():
    entries = _entries()
    # 2 chapters (vostfr show) + 1 chapter (vf show) + 1 chapter (film vf) = 4
    # the "no chapters" block contributes 0 entries.
    assert len(entries) == 4


def test_vostfr_episode_from_url_suffix():
    entries = _entries()
    ep = next(e for e in entries if e.episode_label == "12")
    assert ep.anime_title == "Sample VOSTFR Show"
    assert ep.language == Language.VOSTFR
    assert ep.url.endswith("-12-vostfr/")
    assert ep.published_at_raw == "1 second ago"


def test_vf_episode_from_url_suffix():
    entries = _entries()
    ep = next(e for e in entries if e.episode_label == "08")
    assert ep.anime_title == "Sample Show (VF)"
    assert ep.language == Language.VF


def test_film_language_from_url_prefix():
    entries = _entries()
    ep = next(e for e in entries if "FILM VF" in (e.episode_label or ""))
    assert ep.language == Language.VF


def test_oav_language_from_url_prefix():
    detection = detect_language(
        "https://voir-anime.to/anime/the-island-of-giant-insects/oav-vostfr-the-island-of-giant-insects/",
        anime_has_vf_badge=False,
    )
    assert detection.language == Language.VOSTFR
    assert detection.basis == "url_film_prefix"


def test_thumbnail_captured():
    entries = _entries()
    ep = next(e for e in entries if e.episode_label == "12")
    assert ep.thumbnail_url == "https://voir-anime.to/wp-content/uploads/2026/06/thumb-vostfr-110x150.jpg"


def test_block_with_no_chapters_contributes_nothing():
    entries = _entries()
    assert all(e.anime_title != "Sample No Chapters" for e in entries)


def test_detect_language_url_suffix_beats_badge():
    detection = detect_language(
        "https://voir-anime.to/anime/x/x-01-vostfr/", anime_has_vf_badge=True
    )
    assert detection.language == Language.VOSTFR
    assert detection.confidence == Confidence.HIGH
    assert detection.basis == "url_suffix"


def test_detect_language_falls_back_to_badge():
    detection = detect_language("https://voir-anime.to/anime/x/x-01/", anime_has_vf_badge=True)
    assert detection.language == Language.VF
    assert detection.confidence == Confidence.LOW
    assert detection.basis == "anime_vf_badge_fallback"


def test_detect_language_unknown_without_any_signal():
    detection = detect_language("https://voir-anime.to/anime/x/x-01/", anime_has_vf_badge=False)
    assert detection.language == Language.UNKNOWN
    assert detection.confidence == Confidence.UNKNOWN


# Phase 13 resilience: completely empty/malformed pages must not raise.
def test_empty_html_returns_empty_list():
    assert parse_homepage("<html><body></body></html>") == []
    assert parse_homepage("") == []


def test_missing_thumbnail_leaves_thumbnail_url_none():
    html = """
    <div class="page-item-detail video">
      <div class="item-summary">
        <div class="post-title"><a href="https://voir-anime.to/anime/x/">X</a></div>
        <div class="list-chapter">
          <div class="chapter-item">
            <a class="btn-link" href="https://voir-anime.to/anime/x/x-01-vostfr/">01</a>
            <span class="post-on">1 hour ago</span>
          </div>
        </div>
      </div>
    </div>
    """
    entries = parse_homepage(html)
    assert len(entries) == 1
    assert entries[0].thumbnail_url is None
    assert entries[0].url == "https://voir-anime.to/anime/x/x-01-vostfr/"
