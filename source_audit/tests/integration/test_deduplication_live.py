from __future__ import annotations

import pytest

from source_audit.analysis.homepage import parse_homepage
from source_audit.analysis.identity import build_episode_key
from source_audit.detection.deduplication import EpisodeStatus, EpisodeStore
from source_audit.detection.discovery import diff_known_episode_keys
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration


def test_live_full_discovery_to_dedup_flow():
    """End-to-end: fetch the real homepage, register everything as PUBLISHED (as
    if a prior V1 run had already processed it), then confirm re-fetching the
    same page yields zero "new" candidates -- the realistic steady-state case."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get("https://voir-anime.to/")
    assert result.ok

    entries = parse_homepage(result.text)
    assert len(entries) > 0

    store = EpisodeStore()
    for entry in entries:
        key = build_episode_key(entry.url)
        store.register_discovered(key, anime_key=None)
        store.mark_status(key, EpisodeStatus.PUBLISHED)

    # Re-parsing the same fetch and diffing against the store's known keys must
    # find nothing new -- this is the steady-state behavior a real poller relies on.
    new_entries = diff_known_episode_keys(entries, store.known_keys())
    assert new_entries == []

    # And every entry is reported as already published.
    for entry in entries:
        assert store.is_already_published(build_episode_key(entry.url))
