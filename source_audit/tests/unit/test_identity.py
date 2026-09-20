from source_audit.analysis.identity import (
    build_anime_key,
    build_episode_key,
    canonicalize_url,
    extract_episode_number_from_label,
    extract_episode_number_from_url,
    extract_post_id_from_body_class,
    language_pair_confidence,
)
from source_audit.models import Confidence


def test_extract_post_id_from_body_class():
    html = '<html><body class="page-template-default page postid-114033 logged-out">'
    assert extract_post_id_from_body_class(html) == "114033"


def test_extract_post_id_missing_returns_none():
    html = "<html><body class=\"home\">"
    assert extract_post_id_from_body_class(html) is None


def test_build_anime_key():
    assert build_anime_key("114033") == "postid:114033"


def test_canonicalize_url_strips_trailing_slash_and_lowercases_host():
    a = canonicalize_url("HTTPS://Voir-Anime.To/anime/Foo/foo-01-vostfr/")
    b = "https://voir-anime.to/anime/Foo/foo-01-vostfr"
    assert a == b


def test_canonicalize_url_idempotent_on_variants():
    variants = [
        "https://voir-anime.to/anime/foo/foo-01-vostfr/",
        "https://voir-anime.to/anime/foo/foo-01-vostfr",
        "  https://voir-anime.to/anime/foo/foo-01-vostfr/  ",
        "HTTPS://VOIR-ANIME.TO/anime/foo/foo-01-vostfr/",
    ]
    keys = {canonicalize_url(v) for v in variants}
    assert len(keys) == 1


def test_build_episode_key_matches_canonicalize():
    url = "https://voir-anime.to/anime/foo/foo-01-vostfr/"
    assert build_episode_key(url) == canonicalize_url(url)


def test_extract_episode_number_from_url_regular_suffix():
    assert extract_episode_number_from_url("https://voir-anime.to/anime/foo/foo-12-vostfr/") == 12
    assert extract_episode_number_from_url("https://voir-anime.to/anime/foo/foo-08-vf/") == 8


def test_extract_episode_number_from_url_none_for_film_prefix():
    url = "https://voir-anime.to/anime/foo-vf/film-vf-foo/"
    assert extract_episode_number_from_url(url) is None


def test_extract_episode_number_from_label():
    assert extract_episode_number_from_label("The Exiled Heavy Knight... - 12 VOSTFR - 12") == 12


def test_extract_episode_number_from_label_none_for_film_label():
    label = "(FILM VF) SHIBOYUGI Playing Death Games to Put Food on the Table 44 CLOUDY BEACH"
    assert extract_episode_number_from_label(label) is None


def test_language_pair_high_confidence_when_both_fields_match():
    conf = language_pair_confidence("道굴왕", "Dogul Wang", "道굴왕", "Dogul Wang")
    assert conf == Confidence.HIGH


def test_language_pair_medium_when_only_romaji_present_and_matches():
    conf = language_pair_confidence(None, "Dogul Wang", None, "Dogul Wang")
    assert conf == Confidence.MEDIUM


def test_language_pair_medium_when_native_matches_but_romaji_differs():
    conf = language_pair_confidence("A", "Romaji1", "A", "Romaji2")
    assert conf == Confidence.MEDIUM


def test_language_pair_low_when_neither_matches():
    conf = language_pair_confidence("A", "B", "C", "D")
    assert conf == Confidence.LOW


def test_language_pair_unknown_when_no_data_at_all():
    conf = language_pair_confidence(None, None, None, None)
    assert conf == Confidence.UNKNOWN
