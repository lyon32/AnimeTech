from __future__ import annotations

import pytest

from source_audit.analysis.episode import parse_episode_page
from source_audit.analysis.media import parse_hls_master_manifest
from source_audit.analysis.player import parse_embed_page
from source_audit.fetch.http_client import HttpClient

pytestmark = pytest.mark.integration

EPISODE_URL = (
    "https://voir-anime.to/anime/the-exiled-heavy-knight-knows-how-to-game-the-system/"
    "the-exiled-heavy-knight-knows-how-to-game-the-system-01-vostfr/"
)


def test_live_episode_page_has_player_iframe():
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        result = client.get(EPISODE_URL)
    assert result.ok

    record = parse_episode_page(result.text, EPISODE_URL)
    assert record.player_iframe_url is not None
    assert "voembed.net" in record.player_iframe_url
    # Episode 1 should have a Next link and no Prev link.
    assert record.next_episode_url is not None
    assert record.prev_episode_url is None


def test_live_embed_and_manifest_chain():
    """End-to-end structural check: episode page -> embed page -> HLS manifest.
    Read-only GETs of URLs the site itself hands out in cleartext; no auth/DRM/
    CAPTCHA bypass involved (see analysis/player.py module docstring)."""
    with HttpClient(timeout_seconds=15.0, max_retries=2) as client:
        ep_result = client.get(EPISODE_URL)
        assert ep_result.ok
        record = parse_episode_page(ep_result.text, EPISODE_URL)
        assert record.player_iframe_url is not None

        embed_result = client.get(record.player_iframe_url)
        assert embed_result.ok

        player_obs = parse_embed_page(embed_result.text, record.player_iframe_url)
        assert player_obs.iframe_domain == "voembed.net"
        assert player_obs.player_library == "jwplayer"
        assert player_obs.manifest_url_found
        assert player_obs.manifest_url is not None

        manifest_result = client.get(player_obs.manifest_url)
        assert manifest_result.ok

        renditions = parse_hls_master_manifest(manifest_result.text)
        assert len(renditions) >= 1
        r = renditions[0]
        assert r.resolution is not None
        assert r.video_codec is not None
        assert r.audio_codec is not None
