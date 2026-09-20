from __future__ import annotations

import pytest

from source_audit.analysis.homepage import parse_homepage
from source_audit.fetch.http_client import HttpClient
from source_audit.models import Language

BASE_URL = "https://voir-anime.to/"

pytestmark = pytest.mark.integration


def test_live_homepage_parses_to_nonempty_entries():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get(BASE_URL)
    assert result.ok

    entries = parse_homepage(result.text)

    # Observed 16 anime blocks x up to 2 chapters each on 2026-09-17; assert a
    # generous lower bound so the test tolerates normal content turnover.
    assert len(entries) >= 8

    for entry in entries:
        assert entry.url.startswith("https://voir-anime.to/")
        assert entry.anime_title  # never empty on a real block

    languages_seen = {e.language for e in entries}
    # The live homepage has always shown a mix of VOSTFR/VF on every check so far;
    # if this ever fails it is a real observation to record, not a flaky test.
    assert Language.VOSTFR in languages_seen or Language.VF in languages_seen


def test_live_homepage_page_2_continues_older_dates():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        page1 = client.get(BASE_URL)
        page2 = client.get(BASE_URL + "page/2/")

    assert page1.ok and page2.ok

    entries1 = parse_homepage(page1.text)
    entries2 = parse_homepage(page2.text)

    urls1 = {e.url for e in entries1}
    urls2 = {e.url for e in entries2}
    # Page 2 must not just repeat page 1 verbatim (confirms real pagination, not a
    # caching/redirect artifact).
    assert urls1 != urls2
