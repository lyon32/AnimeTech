from __future__ import annotations

import pytest

from source_audit.analysis.anime import parse_anime_page
from source_audit.detection.ordering import PerAnimeQueue
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration

# 3 real, distinct anime already covered in Phases 3-8, used as 3 "concurrent"
# streams per MASTER_PLAN.md §25/§14's own multi-anime example.
ANIME_URLS = [
    "https://voir-anime.to/anime/the-exiled-heavy-knight-knows-how-to-game-the-system/",
    "https://voir-anime.to/anime/mebius-dust/",
    "https://voir-anime.to/anime/rezero-kara-hajimeru-isekai-seikatsu-s4/",
]


def test_live_multi_anime_episodes_never_mix_and_stay_ordered():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        records = []
        for url in ANIME_URLS:
            result = client.get(url)
            assert result.ok
            records.append(parse_anime_page(result.text, url))

    assert all(r.post_id is not None for r in records)
    assert len({r.post_id for r in records}) == 3  # 3 genuinely distinct anime

    q = PerAnimeQueue()
    for record in records:
        anime_key = f"postid:{record.post_id}"
        # episode_links is newest-first (Phase 3 finding) -- reverse to oldest-first
        # for correct ascending processing order.
        for link in reversed(record.episode_links):
            q.add(anime_key, link.url)

    # Process round-robin across all 3 anime (interleaved), oldest-first per anime.
    pending = {f"postid:{r.post_id}": list(reversed([e.url for e in r.episode_links])) for r in records}
    processed = []
    while any(pending.values()):
        for anime_key, episodes in pending.items():
            if episodes:
                ep = episodes.pop(0)
                q.process(ep)  # raises OutOfOrderError if this would violate per-anime order
                processed.append(ep)

    total_episodes = sum(len(r.episode_links) for r in records)
    assert len(processed) == total_episodes
    assert len(set(processed)) == total_episodes  # no episode processed twice, none mixed up
