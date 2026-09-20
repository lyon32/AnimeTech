from __future__ import annotations

import pytest

from source_audit.analysis.homepage import parse_homepage
from source_audit.fetch.http_client import HttpClient
from source_audit.models import Language

pytestmark = pytest.mark.integration

LISTING_URLS = [
    "https://voir-anime.to/",
    "https://voir-anime.to/page/2/",
    "https://voir-anime.to/page/3/",
    "https://voir-anime.to/nouveaux-ajouts/",
    "https://voir-anime.to/nouveaux-ajouts/page/2/",
]


def test_live_language_detection_resolves_nearly_all_entries():
    """Phase 7: broader sampling found 0/224 unresolved entries after generalizing
    detect_language() to the (film|oav)-{lang}- prefix pattern. Assert a tolerant
    but meaningful bound (>=95%) rather than 100%, since site content changes and a
    future unseen pattern is a real, expected possibility -- not a test failure."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        all_entries = []
        for url in LISTING_URLS:
            result = client.get(url)
            assert result.ok, f"fetch failed for {url}: {result.error_type}"
            all_entries.extend(parse_homepage(result.text))

    assert len(all_entries) > 50

    resolved = [e for e in all_entries if e.language is not None]
    unresolved = [e for e in all_entries if e.language is None]

    resolved_ratio = len(resolved) / len(all_entries)
    assert resolved_ratio >= 0.95, (
        f"language resolved for only {resolved_ratio:.1%} of entries; "
        f"unresolved examples: {[e.url for e in unresolved[:5]]}"
    )

    languages_seen = {e.language for e in resolved}
    assert Language.VOSTFR in languages_seen
    assert Language.VF in languages_seen
