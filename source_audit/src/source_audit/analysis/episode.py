"""Parses a single voir-anime.to episode page.

Evidence backing the selectors below (2026-09-17, see SESSION_REPORT.md Phase 4),
based on 7 real episode pages fetched live across 5 anime (VOSTFR, VF, "JAP", and a
VOSTFR/VF franchise pair):

- The `<title>` tag reliably encodes "{AnimeTitle} - {AnimeTitle} - {N} {LANG} - {N} -
  Voiranime" on every page checked — redundant but consistent, usable as a fallback
  identity signal.
- Episode pages have **no on-page `<h1>`/description/synopsis** (checked
  `.entry-title`, `h1`, `.description-summary`, `.summary__content`, `.post-content`
  — all 0 matches). All descriptive metadata lives on the *anime* page (Phase 3), not
  the episode page.
- Player: exactly one `<iframe>` per episode page on all 7 pages checked, always on
  `voembed.net`, with a unique `embed-{id}.html` path per episode. **No alternate
  server/source selector UI was found** (checked `.server-item`, `.list-server`,
  `.anime_muti_link`, etc. — all 0) — single fixed provider, not a multi-host
  fallback list.
- **No direct download link/button was found anywhere on an episode page**
  (`a[href*=download]`, `.download`, `.btn-download` all 0 matches) — the embedded
  streaming iframe is the only access path this site itself exposes.
- Prev/Next navigation: `.nav-links a` (appears twice per page — likely duplicate
  desktop/mobile nav blocks, both with identical hrefs). Confirmed boundary-correct:
  episode 1 of "The Exiled Heavy Knight..." has only a "Next" link (no "Prev"); the
  latest episode (12) has only a "Prev" link (no "Next"). This corroborates but is
  redundant with anime-page episode-list ordering (Phase 3) — kept as an independent
  cross-check signal, not the primary ordering source.
- **Phase 8 addition:** every episode page's `body.postid-N` reports the SAME
  post ID as its parent anime page (confirmed for "The Exiled Heavy Knight..." ep 1
  and ep 12, and for Tomb Raider King VF/JAP) — see `analysis/identity.py` for the
  full identity-key derivation this enables.

Do not assume this structure is stable forever: `detection/fingerprint.py` (Phase 12)
is responsible for detecting when it changes.
"""
from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from source_audit.analysis.identity import (
    build_anime_key,
    build_episode_key,
    extract_episode_number_from_url,
    extract_post_id_from_body_class,
)
from source_audit.models import EpisodeRecord

logger = logging.getLogger(__name__)

_IFRAME_SELECTOR = "iframe"
_NAV_LINKS_SELECTOR = ".nav-links a"


def parse_episode_page(html: str, url: str) -> EpisodeRecord:
    soup = BeautifulSoup(html, "lxml")

    page_title_raw = soup.title.get_text(strip=True) if soup.title else None

    iframes = soup.select(_IFRAME_SELECTOR)
    player_iframe_url = None
    if len(iframes) == 0:
        logger.warning("Episode page has no iframe (no player found): %s", url)
    elif len(iframes) > 1:
        logger.warning("Episode page has %d iframes, expected 1: %s", len(iframes), url)
        player_iframe_url = iframes[0].get("src")
    else:
        player_iframe_url = iframes[0].get("src")

    prev_url = None
    next_url = None
    for link in soup.select(_NAV_LINKS_SELECTOR):
        text = link.get_text(strip=True).lower()
        href = link.get("href")
        if not href:
            continue
        if text == "prev" and prev_url is None:
            prev_url = href
        elif text == "next" and next_url is None:
            next_url = href

    # Language is not re-derived here: it's already available with a documented
    # confidence basis from analysis.homepage.detect_language() on the URL, which
    # this episode's own url is. Callers combine the two.

    post_id = extract_post_id_from_body_class(html)

    return EpisodeRecord(
        url=url,
        episode_key=build_episode_key(url),
        anime_key=build_anime_key(post_id) if post_id else None,
        anime_post_id=post_id,
        episode_number=extract_episode_number_from_url(url),
        page_title_raw=page_title_raw,
        player_iframe_url=player_iframe_url,
        prev_episode_url=prev_url,
        next_episode_url=next_url,
    )
