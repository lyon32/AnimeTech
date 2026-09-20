"""Parses a single voir-anime.to anime page.

Evidence backing the selectors below (2026-09-17, see SESSION_REPORT.md Phase 3),
based on 6 real anime pages fetched live: an ongoing VOSTFR-only show (12 episodes),
a VF/JAP pair of the same title with independently numbered episode lists (VF: 8/12
released, JAP: 11/12 released), a small 11-episode show, and a VOSTFR/VF pair of a
season-4 franchise entry (VOSTFR: 17/19 released, VF: 14/19 released):

- Title: `.post-title h1`.
- Metadata fields: `.post-content_item`, each with a `.summary-heading` label and a
  `.summary-content` value. The **label set is not fixed** across anime — observed
  labels: Native, Romaji, English, Note, Type, Status, Studios, Episodes, Start date,
  Genre(s). "English" and "Episodes" were each ABSENT on at least one of the 6 pages
  sampled (e.g. "Mebius Dust" has no English title; "The Exiled Heavy Knight..." has
  no "Episodes" total). Parsing must key off the label text, not field position.
- "Episodes" field = the season's total *planned* episode count when the site knows
  it (MEASURED: "Tomb Raider King" VF and JAP pages both declare 12, even though the
  VF page currently lists only 8 released episodes and the JAP page lists 11 — i.e.
  this field is NOT the same as the number of `.wp-manga-chapter` entries actually
  present, and dubs visibly lag behind subs in release count).
- Episode list: `.listing-chapters_wrap ul.main li.wp-manga-chapter`, each with a
  single `a[href]` (link text is verbose: "{title} - {number} {LANG} - {number}") and
  a `.chapter-release-date i` (date text, same two formats seen on the homepage).
  Order observed: descending (newest/highest episode number first). The list appeared
  to be the FULL episode list on every page sampled (max 17 episodes observed) — no
  chapter-list pagination control (`.chapters-pagination` or similar) was found on any
  of the 6 pages. **INCONCLUSIVE** whether long-running shows (100+ episodes) paginate
  this list; not sampled (this site does not appear to host such a title, or it was
  not found in the pages checked).
- No season-selector element was found on any page (`.season-name`,
  `.wp-manga-season`, `.select-season`, etc. all matched 0) — consistent with the
  "each season is a separate anime page" structural finding above.
- Episode URL slugs are **not derivable from the anime URL slug by simple
  concatenation**: e.g. anime slug `rezero-kara-hajimeru-isekai-seikatsu-s4` but its
  episode URLs use `re-zero-kara-hajimeru-isekai-seikatsu-saison-4-{N}-vostfr` (extra
  hyphen in "re-zero", "s4" spelled out as "saison-4"). Episode URLs must always be
  read from `.wp-manga-chapter a[href]`, never constructed.

Do not assume this structure is stable forever: `detection/fingerprint.py` (Phase 12)
is responsible for detecting when it changes.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from source_audit.analysis.identity import extract_post_id_from_body_class
from source_audit.models import AnimeEpisodeLink, AnimeRecord

logger = logging.getLogger(__name__)

_TITLE_SELECTOR = ".post-title h1"
_METADATA_ITEM_SELECTOR = ".post-content_item"
_METADATA_LABEL_SELECTOR = ".summary-heading"
_METADATA_VALUE_SELECTOR = ".summary-content"
_EPISODE_ITEM_SELECTOR = ".listing-chapters_wrap li.wp-manga-chapter"
_EPISODE_DATE_SELECTOR = ".chapter-release-date"

_LABEL_TO_FIELD = {
    "native": "native_title",
    "romaji": "romaji_title",
    "english": "english_title",
    "type": "anime_type_raw",
    "status": "status_raw",
    "studios": "studios",
    "start date": "start_date_raw",
}

_EPISODES_TOTAL_RE = re.compile(r"\d+")


def _extract_metadata_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in soup.select(_METADATA_ITEM_SELECTOR):
        label_el = item.select_one(_METADATA_LABEL_SELECTOR)
        value_el = item.select_one(_METADATA_VALUE_SELECTOR)
        if label_el is None or value_el is None:
            continue
        label = label_el.get_text(strip=True)
        value = value_el.get_text(" ", strip=True)
        fields[label] = value
    return fields


def parse_anime_page(html: str, url: str) -> AnimeRecord:
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one(_TITLE_SELECTOR)
    title = title_el.get_text(strip=True) if title_el else None
    if title is None:
        logger.warning("Anime page has no title element at %s", url)

    fields = _extract_metadata_fields(soup)

    kwargs: dict[str, object] = {}
    for label, field_name in _LABEL_TO_FIELD.items():
        for observed_label, value in fields.items():
            if observed_label.strip().lower() == label:
                kwargs[field_name] = value
                break

    genres_raw = None
    for observed_label, value in fields.items():
        if observed_label.strip().lower().startswith("genre"):
            genres_raw = value
            break
    genres = [g.strip() for g in genres_raw.split(",") if g.strip()] if genres_raw else []

    total_episodes_declared = None
    for observed_label, value in fields.items():
        if observed_label.strip().lower() == "episodes":
            match = _EPISODES_TOTAL_RE.search(value)
            if match:
                total_episodes_declared = int(match.group())
            break

    episode_links: list[AnimeEpisodeLink] = []
    for item in soup.select(_EPISODE_ITEM_SELECTOR):
        link_el = item.select_one("a")
        if link_el is None or not link_el.get("href"):
            logger.warning("Episode item without a link on anime page %s", url)
            continue
        date_el = item.select_one(_EPISODE_DATE_SELECTOR)
        episode_links.append(
            AnimeEpisodeLink(
                label=link_el.get_text(strip=True),
                url=link_el["href"],
                published_at_raw=date_el.get_text(strip=True) if date_el else None,
            )
        )

    if not episode_links:
        logger.warning("Anime page has zero episode links: %s", url)

    return AnimeRecord(
        url=url,
        post_id=extract_post_id_from_body_class(html),
        title=title,
        status_raw=kwargs.get("status_raw"),
        anime_type_raw=kwargs.get("anime_type_raw"),
        native_title=kwargs.get("native_title"),
        romaji_title=kwargs.get("romaji_title"),
        english_title=kwargs.get("english_title"),
        studios=kwargs.get("studios"),
        start_date_raw=kwargs.get("start_date_raw"),
        genres=genres,
        total_episodes_declared=total_episodes_declared,
        episode_links=episode_links,
        episode_count_observed=len(episode_links),
    )
