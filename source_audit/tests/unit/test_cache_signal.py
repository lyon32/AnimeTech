from datetime import datetime

from source_audit.detection.cache_signal import (
    extract_cache_generated_at,
    is_same_cache_generation,
    parse_cache_generated_at,
)

SAMPLE_HTML = (
    "<html><body>content</body></html>"
    "<!-- WP Fastest Cache file was created in 0.34968113899231 seconds, "
    "on 17-09-26 17:49:07 --><!-- via php -->"
)
SAMPLE_HTML_DIFFERENT_TIME = SAMPLE_HTML.replace("17:49:07", "18:20:00")
SAMPLE_HTML_NO_MARKER = "<html><body>no cache comment here</body></html>"


def test_extract_cache_generated_at():
    assert extract_cache_generated_at(SAMPLE_HTML) == "17-09-26 17:49:07"


def test_extract_cache_generated_at_missing_returns_none():
    assert extract_cache_generated_at(SAMPLE_HTML_NO_MARKER) is None


def test_parse_cache_generated_at():
    dt = parse_cache_generated_at("17-09-26 17:49:07")
    assert dt == datetime(2026, 9, 17, 17, 49, 7)


def test_is_same_cache_generation_true_for_identical_timestamp():
    assert is_same_cache_generation(SAMPLE_HTML, SAMPLE_HTML) is True


def test_is_same_cache_generation_false_for_different_timestamp():
    assert is_same_cache_generation(SAMPLE_HTML, SAMPLE_HTML_DIFFERENT_TIME) is False


def test_is_same_cache_generation_none_when_marker_missing():
    assert is_same_cache_generation(SAMPLE_HTML, SAMPLE_HTML_NO_MARKER) is None
    assert is_same_cache_generation(SAMPLE_HTML_NO_MARKER, SAMPLE_HTML_NO_MARKER) is None
