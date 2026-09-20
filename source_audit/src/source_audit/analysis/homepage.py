"""Parses voir-anime.to's homepage "En cours" (latest-updated) listing.

Evidence backing the selectors below (2026-09-17, see SESSION_REPORT.md Phase 2):

- Each anime block is `.page-item-detail.video` inside a `.page-listing-item` /
  `.row.row-eq-height` grid: 16 blocks per page (homepage and `/page/N/`).
- Anime title + anime URL: `.item-summary .post-title a`.
- Up to 2 recent episodes per anime block: `.list-chapter .chapter-item`, each with
  an `a.btn-link` (episode label text + episode URL) and a `.post-on` (relative or
  absolute date string, e.g. "1 second ago" / "September 10, 2026" — TWO different
  date formats observed, never assume one).
- VF badge: `.item-thumb .manga-vf-flag` (text "VF") — present on the anime
  thumbnail when the anime block IS a VF (dubbed) listing. Confirmed on 2026-09-17
  against 5 known-VF anime blocks (e.g. "Tomb Raider King (VF)").
- Per-episode URL slug pattern (Phase 7 update, 2026-09-17: broadened to 224 entries
  across homepage pages 1-5 and `/nouveaux-ajouts/` pages 1-3, up from the original
  64-entry Phase 2 sample): **210/224 (93.8%) end in a trailing `-vf`/`-vostfr`
  suffix** (HIGH confidence). **13/224 (5.8%) use a `film-{lang}-...` prefix**
  instead of a suffix, and **1/224 (0.4%) uses an `oav-{lang}-...` prefix** — i.e. a
  THIRD pattern was found on broader sampling, resolving the Phase 2 INCONCLUSIVE:
  the prefix pattern is `(film|oav)-(vf|vostfr)-...`, not `film-` only.
  `detect_language()` matches both known type markers explicitly (not a generic
  wildcard, to avoid false-positives on ordinary slug text). **1/224 (0.4%) matched
  neither pattern** (`oav-vostfr-the-island-of-giant-insects` — inspected: this URL
  IS the `oav-` prefix case; it was double-counted while `detect_language()` still
  only recognized `film-`, before this update. Post-update it now correctly resolves
  via the prefix branch — verified by test, see `test_homepage.py`.) This project
  treats the URL-suffix signal as authoritative when it matches; the URL-prefix
  signal next; the anime-level VF badge only as a last-resort fallback. Neither
  positional signal is used blindly — see `detect_language()`.
- `/page/2/`, `/page/3/`, ... continues the SAME feed in descending recency order
  (page 1: "N seconds/minutes/hours ago"; page 2: "1 day ago" onward) — confirmed by
  comparing page 1 and page 2 item dates. This is the homepage's own pagination and is
  distinct from `/nouveaux-ajouts/` (newly *added series*, not latest episodes; dates
  observed weeks/months old) and `/prochainement/` (upcoming anime with zero chapters
  listed) — see Phase 9 discovery-strategy notes for how these three differ.
- **Phase 7 addition:** `/nouveaux-ajouts/` can itself list anime blocks with a
  completely empty `.list-chapter` (zero episodes released yet) — observed for 3
  distinct anime ("Ninja Batman", its VF variant, "Prism Rondo") across 3
  `/nouveaux-ajouts/` pages sampled. So `/nouveaux-ajouts/` is not guaranteed to be
  an episode source either, same caveat as `/prochainement/` — a "new series added"
  signal is not automatically a "new episode available" signal. `parse_homepage()`
  already handles this correctly (contributes 0 entries for such a block, logs a
  warning) — this is a documented site behavior, not a parser bug.

Do not assume this structure is stable forever: `detection/fingerprint.py` (Phase 12)
is responsible for detecting when it changes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from source_audit.models import Confidence, HomepageEntry, Language

logger = logging.getLogger(__name__)

_ANIME_BLOCK_SELECTOR = ".page-item-detail.video"
_TITLE_SELECTOR = ".item-summary .post-title a"
_CHAPTER_ITEM_SELECTOR = ".list-chapter .chapter-item"
_CHAPTER_LINK_SELECTOR = "a"
_CHAPTER_DATE_SELECTOR = ".post-on"
_THUMBNAIL_SELECTOR = ".item-thumb img"
_VF_BADGE_SELECTOR = ".item-thumb .manga-vf-flag"

# Regular episode URLs: language encoded as a trailing slug suffix.
_URL_LANGUAGE_SUFFIX_RE = re.compile(r"-(vf|vostfr)/?$")
# Film/OAV episode URLs: language encoded as a leading slug prefix instead, after a
# content-type marker. Only "film" and "oav" have been observed as the type marker
# (Phase 7 sample of 224 homepage/nouveaux-ajouts entries: 13 "film-", 1 "oav-") —
# kept as an explicit set rather than a generic `[a-z0-9]+` match to avoid false
# positives on ordinary slug text that happens to contain "-vf-"/"-vostfr-".
_URL_LANGUAGE_PREFIX_RE = re.compile(r"/(?:film|oav)-(vf|vostfr)-[^/]+/?$")


@dataclass
class LanguageDetection:
    language: Language
    confidence: Confidence
    basis: str


def detect_language(episode_url: str, anime_has_vf_badge: bool) -> LanguageDetection:
    """Determine language from the strongest available signal.

    Priority: per-episode URL slug (most specific, observed 100% consistent for
    non-film episodes) > anime-level VF badge (coarser: applies to the whole anime
    block, not necessarily to a specific chapter) > UNKNOWN.
    """
    suffix_match = _URL_LANGUAGE_SUFFIX_RE.search(episode_url)
    if suffix_match:
        lang = Language.VF if suffix_match.group(1) == "vf" else Language.VOSTFR
        return LanguageDetection(lang, Confidence.HIGH, "url_suffix")

    prefix_match = _URL_LANGUAGE_PREFIX_RE.search(episode_url)
    if prefix_match:
        lang = Language.VF if prefix_match.group(1) == "vf" else Language.VOSTFR
        return LanguageDetection(lang, Confidence.MEDIUM, "url_film_prefix")

    if anime_has_vf_badge:
        return LanguageDetection(Language.VF, Confidence.LOW, "anime_vf_badge_fallback")

    return LanguageDetection(Language.UNKNOWN, Confidence.UNKNOWN, "no_signal")


def parse_homepage(html: str) -> list[HomepageEntry]:
    """Parse a homepage (or `/page/N/`) listing page into one entry per episode link.

    Each anime block typically exposes its 1-2 most recent chapters; this function
    emits one HomepageEntry per chapter link, not one per anime, so downstream
    dedup/discovery logic operates at episode granularity.
    """
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select(_ANIME_BLOCK_SELECTOR)
    entries: list[HomepageEntry] = []

    for block in blocks:
        title_el = block.select_one(_TITLE_SELECTOR)
        anime_title = title_el.get_text(strip=True) if title_el else None
        if title_el is None:
            logger.warning("Anime block without a title element: %s", block.get("id"))

        thumb_el = block.select_one(_THUMBNAIL_SELECTOR)
        thumbnail_url = thumb_el.get("src") if thumb_el else None

        has_vf_badge = block.select_one(_VF_BADGE_SELECTOR) is not None

        chapter_items = block.select(_CHAPTER_ITEM_SELECTOR)
        if not chapter_items:
            logger.warning("Anime block with no chapter items: %s (%s)", anime_title, block.get("id"))
            continue

        for chapter in chapter_items:
            link_el = chapter.select_one(_CHAPTER_LINK_SELECTOR)
            if link_el is None or not link_el.get("href"):
                logger.warning("Chapter item without a link for anime %s", anime_title)
                continue
            episode_url = link_el["href"]
            episode_label = link_el.get_text(strip=True)

            date_el = chapter.select_one(_CHAPTER_DATE_SELECTOR)
            published_at_raw = date_el.get_text(strip=True) if date_el else None

            detection = detect_language(episode_url, has_vf_badge)

            entries.append(
                HomepageEntry(
                    anime_title=anime_title,
                    episode_label=episode_label or None,
                    language=detection.language if detection.language != Language.UNKNOWN else None,
                    url=episode_url,
                    published_at_raw=published_at_raw,
                    thumbnail_url=thumbnail_url,
                    source_selector=f"{_ANIME_BLOCK_SELECTOR} > {_CHAPTER_ITEM_SELECTOR} (lang_basis={detection.basis})",
                )
            )

    return entries
