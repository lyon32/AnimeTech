"""Derives stable identity keys for anime/episode records (Phase 8).

Evidence (2026-09-17, see SESSION_REPORT.md Phase 8):

- Every anime page's `<body class="... postid-{N} ...">` carries the underlying
  WordPress post ID (standard `body_class()` output). Confirmed on 5 anime pages
  (e.g. "The Exiled Heavy Knight..." -> postid-114033).
- **Every episode page of a given anime reports the SAME postid as its parent
  anime page** (checked: episode 12 and episode 1 of "The Exiled Heavy Knight..."
  both report `postid-114033`, matching the anime page itself; same confirmed for
  Tomb Raider King VF and JAP). This means WP-Manga stores chapters as sub-content
  of the manga's single post, not as separate posts with their own IDs — the
  post ID identifies the **anime** (already language/season-scoped per Phase 3's
  "each language/season is a separate post" finding), never a single episode on
  its own.
- The same post ID is also exposed on **listing pages** (homepage, `/nouveaux-
  ajouts/`, etc.) via `.item-thumb[data-post-id]` on each anime block. **Re-fetched
  the homepage twice in the same session (~1h+ apart) and found 0/13 mismatches**
  between the two fetches for the same anime URLs — the ID is stable across
  requests, not regenerated per page load.
- **Consequence: `anime_key` = the WordPress post ID is a single, HIGH-confidence,
  MEASURED identifier that already implicitly encodes language and season**
  (since each is its own post per Phase 3), extractable from 3 independent places
  (homepage listing, anime page, any of its episode pages) that were all confirmed
  to agree.
- Episode-level identity therefore cannot use the post ID alone (shared by every
  episode of one anime). Two independent, cross-checkable signals for the episode
  number were found:
  1. The **URL slug's trailing number** for regular (non-film/oav) episodes, e.g.
     `...-12-vostfr/` -> `12`. Covers ~93.8% of episodes per the Phase 7 sample
     (210/224). Extraction fails gracefully to `None` for the film/oav-prefixed
     URL shape (Phase 2/7 finding) rather than guessing.
  2. The anime page's chapter **label text**, which independently ends in the same
     trailing number twice (e.g. `"... - 12 VOSTFR - 12"`) on every regular episode
     checked — used only as a cross-check, not the primary signal, since it
     requires the anime page rather than being derivable from the episode URL
     alone.
- The full episode URL itself (after light canonicalization: lowercase
  scheme+host, strip a single trailing slash) is used as `episode_key` rather than
  a constructed `(anime_key, episode_number)` tuple, because (a) it is always
  present (unlike the parsed number, which can be `None` for films/OAVs) and
  (b) Phase 3 already established episode URLs cannot be reconstructed from the
  anime slug, so the URL is the only universally-available per-episode fact.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from source_audit.models import Confidence

_BODY_POSTID_RE = re.compile(r"\bpostid-(\d+)\b")
_URL_TRAILING_NUMBER_RE = re.compile(r"-(\d+)-(?:vf|vostfr)/?$")
_LABEL_TRAILING_NUMBER_RE = re.compile(r"-\s*(\d+)\s*$")


def extract_post_id_from_body_class(html: str) -> str | None:
    """Extracts the WordPress post ID from `<body class="... postid-N ...">`.

    Cheap regex over raw HTML rather than a full BeautifulSoup parse, since this
    is meant to be usable as a fast standalone check.
    """
    match = _BODY_POSTID_RE.search(html)
    return match.group(1) if match else None


def canonicalize_url(url: str) -> str:
    """Lowercases scheme+host and strips a single trailing slash.

    Deliberately conservative: does not touch path casing (WordPress slugs are
    case-sensitive in practice) or query strings (none observed on this site's
    content URLs so far; a query string is preserved as-is if ever present).
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def build_anime_key(post_id: str) -> str:
    return f"postid:{post_id}"


def build_episode_key(episode_url: str) -> str:
    return canonicalize_url(episode_url)


def extract_episode_number_from_url(episode_url: str) -> int | None:
    match = _URL_TRAILING_NUMBER_RE.search(episode_url)
    return int(match.group(1)) if match else None


def extract_episode_number_from_label(label: str) -> int | None:
    match = _LABEL_TRAILING_NUMBER_RE.search(label.strip())
    return int(match.group(1)) if match else None


def language_pair_confidence(
    native_a: str | None, romaji_a: str | None, native_b: str | None, romaji_b: str | None
) -> Confidence:
    """Determines confidence that two AnimeRecords (e.g. a VF page and a VOSTFR
    page) are the same underlying work in different languages.

    Evidence (closure session): the anime page's own "Native" and "Romaji"
    metadata fields (Phase 3) describe the *original Japanese/Korean* work and
    are identical between a title's VF and VOSTFR/JAP pages -- confirmed exact
    match on all 3 known real pairs checked ("Tomb Raider King" VF/JAP,
    "Re:Zero ... S4" VF/VOSTFR, "Détective Conan" VF/VOSTFR). This is a much
    stronger signal than the Phase 7 URL-slug heuristic (which only matched
    71% of real pairs sampled): unlike a dub-title slug, the native/romaji
    fields do not get re-worded for a VF release.

    Priority, per MASTER_PLAN.md §20's confidence-level requirement -- never
    treat "titles look similar" as a match on its own:
    - HIGH: both native AND romaji present on both records and match exactly.
    - MEDIUM: only one of the two fields is present on both records and matches
      (weaker: a single short romaji title has more collision risk than the
      native+romaji pair together).
    - LOW: neither field matches, but this function was called at all
      (a caller-level heuristic, e.g. slug matching, provided the candidate --
      that heuristic's own confidence should be used instead, not this one).
    - UNKNOWN: not enough data on one or both sides to compare (e.g. one
      record has neither field populated).
    """
    have_native = native_a is not None and native_b is not None
    have_romaji = romaji_a is not None and romaji_b is not None

    if not have_native and not have_romaji:
        return Confidence.UNKNOWN

    native_match = have_native and native_a == native_b
    romaji_match = have_romaji and romaji_a == romaji_b

    if have_native and have_romaji:
        if native_match and romaji_match:
            return Confidence.HIGH
        if native_match or romaji_match:
            return Confidence.MEDIUM
        return Confidence.LOW

    # Only one of the two fields is available on both sides.
    if native_match or romaji_match:
        return Confidence.MEDIUM
    return Confidence.LOW
