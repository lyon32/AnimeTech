from source_audit.detection.discovery import diff_known_episode_keys, entries_to_episode_keys
from source_audit.models import HomepageEntry


def _entry(url: str) -> HomepageEntry:
    return HomepageEntry(url=url, anime_title="X", episode_label="1")


def test_diff_returns_only_unknown_entries():
    known = {"https://voir-anime.to/anime/foo/foo-01-vostfr"}
    current = [
        _entry("https://voir-anime.to/anime/foo/foo-01-vostfr/"),  # known (after canonicalization)
        _entry("https://voir-anime.to/anime/foo/foo-02-vostfr/"),  # new
    ]
    new_entries = diff_known_episode_keys(current, known)
    assert len(new_entries) == 1
    assert new_entries[0].url.endswith("foo-02-vostfr/")


def test_diff_returns_empty_when_nothing_new():
    known = {"https://voir-anime.to/anime/foo/foo-01-vostfr"}
    current = [_entry("https://voir-anime.to/anime/foo/foo-01-vostfr/")]
    assert diff_known_episode_keys(current, known) == []


def test_diff_treats_everything_as_new_on_empty_known_set():
    current = [_entry("https://voir-anime.to/anime/foo/foo-01-vostfr/")]
    assert len(diff_known_episode_keys(current, set())) == 1


def test_entries_to_episode_keys_roundtrips_with_diff():
    entries = [
        _entry("https://voir-anime.to/anime/foo/foo-01-vostfr/"),
        _entry("https://voir-anime.to/anime/foo/foo-02-vostfr/"),
    ]
    known = entries_to_episode_keys(entries)
    # Nothing new against the keys derived from the same entries.
    assert diff_known_episode_keys(entries, known) == []
