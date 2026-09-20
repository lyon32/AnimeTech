"""New-episode discovery via the homepage/`page/N/` feed (Phase 9 Strategy A).

Evidence (2026-09-17, see SESSION_REPORT.md Phase 9): Strategy C (sitemap/RSS/REST
API) was tested and found not viable on this site — the sitemap's `lastmod` does not
update when a new chapter is added to an existing anime post (measured directly: a
known anime with an episode published "seconds ago" still showed a 2-month-stale
sitemap `lastmod`), the default RSS feed is empty (WP-Manga chapters are a custom
post type outside the default feed), and the WP REST API returns 403 (not probed
further, per the non-bypass rule). This leaves the homepage/`page/N/` feed
(Phase 2/7's `analysis/homepage.py`) as the primary viable discovery signal.

This module does not fetch anything itself — it operates on `HomepageEntry` lists
already produced by `analysis.homepage.parse_homepage()`, so it can be unit-tested
without a network call and reused for any snapshot source (single page, several
pages, or a saved DB snapshot converted to the same shape).
"""
from __future__ import annotations

from source_audit.analysis.identity import build_episode_key
from source_audit.models import HomepageEntry


def diff_known_episode_keys(
    current_entries: list[HomepageEntry], known_episode_keys: set[str]
) -> list[HomepageEntry]:
    """Returns the subset of `current_entries` whose episode_key is not already in
    `known_episode_keys` — i.e. candidates for "this looks new".

    Deliberately does not mutate `known_episode_keys` or decide what counts as
    "confirmed new" beyond key membership: a caller in the future V1 (out of scope
    here) is expected to still validate each candidate against the anime page's own
    episode list (Strategy B) before treating it as ready to process, since a
    homepage entry alone doesn't carry every fact a downstream pipeline needs.
    """
    new_entries = []
    for entry in current_entries:
        key = build_episode_key(entry.url)
        if key not in known_episode_keys:
            new_entries.append(entry)
    return new_entries


def entries_to_episode_keys(entries: list[HomepageEntry]) -> set[str]:
    """Converts a list of homepage entries into the key set `diff_known_episode_keys`
    expects for `known_episode_keys` on a subsequent call — i.e. what a poller would
    persist after processing a snapshot."""
    return {build_episode_key(e.url) for e in entries}
