"""Integration tests that perform real network requests against the authorized source.

These are separated from tests/unit so the unit suite stays fast and offline. Run
explicitly with: pytest tests/integration -q
"""
from __future__ import annotations

import pytest

from source_audit.fetch.http_client import FetchErrorType, HttpClient

BASE_URL = "https://voir-anime.to/"

pytestmark = pytest.mark.integration


def test_live_homepage_200():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get(BASE_URL)

    assert result.status_code == 200
    assert result.error_type == FetchErrorType.NONE
    assert result.text is not None
    assert len(result.text) > 500
    assert result.elapsed_seconds > 0


def test_live_nonexistent_page_404():
    with HttpClient(timeout_seconds=15.0, max_retries=1) as client:
        result = client.get(BASE_URL + "this-path-should-not-exist-source-audit-test/")

    assert result.status_code == 404
    assert result.error_type == FetchErrorType.HTTP_404


def test_live_robots_txt_reachable():
    with HttpClient(timeout_seconds=15.0, max_retries=1) as client:
        result = client.get(BASE_URL + "robots.txt")

    assert result.ok
    assert "User-agent" in (result.text or "")
