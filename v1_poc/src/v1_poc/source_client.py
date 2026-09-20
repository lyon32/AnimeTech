"""Source access: episode page -> player embed -> HLS master manifest.

Reuses the already-tested parsers from `source_audit` (POC spec clauses 3/4/11:
build on existing, proven extraction code instead of rewriting it).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse
from dataclasses import dataclass, field

from source_audit.analysis.episode import parse_episode_page
from source_audit.analysis.media import MediaRendition, parse_hls_master_manifest
from source_audit.analysis.player import parse_embed_page
from source_audit.fetch.http_client import HttpClient


class SourceAccessError(RuntimeError):
    pass


class SourceNotAvailableError(SourceAccessError):
    """The episode page exists but its player is not the downloadable one yet
    (a YouTube embed = trailer/placeholder): the episode is not published on the source."""


_PLACEHOLDER_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be")


@dataclass
class SourceExtraction:
    episode_url: str
    episode_key: str
    anime_key: str | None
    anime_post_id: str | None
    episode_number: int | None
    player_iframe_url: str | None
    player_library: str | None
    has_obfuscated_script: bool | None
    manifest_url: str | None
    manifest_text: str | None
    renditions: list[MediaRendition] = field(default_factory=list)
    episode_page_status: int | None = None
    embed_page_status: int | None = None
    manifest_status: int | None = None
    page_title_raw: str | None = None
    source_language: str | None = None
    thumbnail_url: str | None = None


def _og_image(html: str) -> str | None:
    m = re.search(r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""", html)
    return m.group(1) if m else None


def extract_source(episode_url: str, client: HttpClient) -> SourceExtraction:
    """Runs the real chain episode page -> embed -> HLS master manifest."""
    ep_page = client.get(episode_url)
    if not ep_page.ok:
        raise SourceAccessError(
            f"episode page fetch failed: status={ep_page.status_code} error={ep_page.error_type.value}"
        )
    record = parse_episode_page(ep_page.text, episode_url)

    if not record.player_iframe_url:
        raise SourceAccessError("no player iframe found on the episode page")

    host = (urlparse(record.player_iframe_url).hostname or "").lower()
    if host.endswith(_PLACEHOLDER_HOSTS):
        raise SourceNotAvailableError(f"player is a {host} embed, not a downloadable stream: episode not published yet")

    embed = client.get(record.player_iframe_url)
    if not embed.ok:
        raise SourceAccessError(
            f"embed page fetch failed: status={embed.status_code} error={embed.error_type.value}"
        )
    player = parse_embed_page(embed.text, record.player_iframe_url)

    if not player.manifest_url:
        raise SourceAccessError("no HLS master manifest URL found in cleartext in the embed page")

    manifest = client.get(player.manifest_url)
    if not manifest.ok:
        raise SourceAccessError(
            f"master manifest fetch failed: status={manifest.status_code} error={manifest.error_type.value}"
        )

    renditions = parse_hls_master_manifest(manifest.text)

    return SourceExtraction(
        episode_url=episode_url,
        episode_key=record.episode_key,
        anime_key=record.anime_key,
        anime_post_id=record.anime_post_id,
        episode_number=record.episode_number,
        player_iframe_url=record.player_iframe_url,
        player_library=player.player_library,
        has_obfuscated_script=player.has_obfuscated_script,
        manifest_url=player.manifest_url,
        manifest_text=manifest.text,
        renditions=renditions,
        episode_page_status=ep_page.status_code,
        embed_page_status=embed.status_code,
        manifest_status=manifest.status_code,
        page_title_raw=record.page_title_raw,
        source_language=getattr(record.language, "value", record.language),
        thumbnail_url=_og_image(ep_page.text),
    )