"""Structure-change fingerprinting (Phase 12).

MASTER_PLAN.md §39: a future V1 must be able to say "I no longer recognize this
structure" and stop, rather than guess. This module gives that a concrete,
testable shape: a **fingerprint** is a record of whether each selector a parser
(Phases 2-5) actually depends on currently matches at least one element on a given
page. If a selector that used to match now matches zero elements, that selector is
"regressed" — a strong signal the site's markup changed underneath the parser.

The selector lists below are imported directly from the analysis modules
(`analysis.homepage`, `analysis.anime`, `analysis.episode`) rather than
re-typed here, so there is exactly one place selectors are defined — a selector
changed in the parser is automatically reflected in the fingerprint, with no risk
of the two drifting apart.

This intentionally checks presence/absence, not exact counts: counts are expected
to vary legitimately page-to-page (Phase 2/3 evidence: entry counts, chapter
counts, and optional metadata fields all vary by design) and would produce noisy
false positives if used as the fingerprint signal instead.
"""
from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup

from source_audit.analysis import anime as anime_analysis
from source_audit.analysis import episode as episode_analysis
from source_audit.analysis import homepage as homepage_analysis

HOMEPAGE_SELECTORS: dict[str, str] = {
    "anime_block": homepage_analysis._ANIME_BLOCK_SELECTOR,
    "title": homepage_analysis._TITLE_SELECTOR,
    "chapter_item": homepage_analysis._CHAPTER_ITEM_SELECTOR,
    "chapter_date": homepage_analysis._CHAPTER_DATE_SELECTOR,
    "thumbnail": homepage_analysis._THUMBNAIL_SELECTOR,
}
"""`vf_badge` is deliberately excluded: it is legitimately absent on VOSTFR-only
listing pages (Phase 2 evidence), so its absence is not itself a structure-change
signal the way the others are."""

ANIME_PAGE_SELECTORS: dict[str, str] = {
    "title": anime_analysis._TITLE_SELECTOR,
    "metadata_item": anime_analysis._METADATA_ITEM_SELECTOR,
    "metadata_label": anime_analysis._METADATA_LABEL_SELECTOR,
    "metadata_value": anime_analysis._METADATA_VALUE_SELECTOR,
    "episode_item": anime_analysis._EPISODE_ITEM_SELECTOR,
}

EPISODE_PAGE_SELECTORS: dict[str, str] = {
    "iframe": episode_analysis._IFRAME_SELECTOR,
}
"""`nav_links` is deliberately excluded: legitimately absent for a single-episode
anime (no prev/next possible) per Phase 4 evidence."""


def compute_fingerprint(html: str, selectors: dict[str, str]) -> dict[str, bool]:
    """For each named selector, records whether it matches >=1 element."""
    soup = BeautifulSoup(html, "lxml")
    return {name: len(soup.select(selector)) > 0 for name, selector in selectors.items()}


def fingerprint_hash(fingerprint: dict[str, bool]) -> str:
    """Stable short hash of a fingerprint, for cheap equality checks/logging."""
    canonical = repr(sorted(fingerprint.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def find_regressed_selectors(baseline: dict[str, bool], current: dict[str, bool]) -> list[str]:
    """Selector names that matched in `baseline` but no longer match in `current`.

    Only reports True -> False regressions, not False -> True (a selector newly
    matching that didn't before is not itself evidence of a breaking change).
    Selector names present in one fingerprint but not the other are ignored here —
    that would indicate the caller compared fingerprints from different selector
    sets, a caller bug, not a site structure change.
    """
    regressed = []
    for name, was_present in baseline.items():
        if was_present and not current.get(name, False):
            regressed.append(name)
    return sorted(regressed)


def is_structure_changed(baseline: dict[str, bool], current: dict[str, bool]) -> bool:
    """True if any critical selector regressed -- MASTER_PLAN.md §39's signal to
    stop a critical pipeline and alert, rather than continue as if nothing changed."""
    return len(find_regressed_selectors(baseline, current)) > 0
