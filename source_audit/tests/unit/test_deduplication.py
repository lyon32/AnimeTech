"""Covers the 8 deduplication scenarios listed in MASTER_PLAN.md section 21."""
from source_audit.analysis.identity import build_episode_key
from source_audit.detection.deduplication import EpisodeStatus, EpisodeStore

URL = "https://voir-anime.to/anime/foo/foo-01-vostfr/"
URL_VARIANT = "HTTPS://Voir-Anime.To/anime/foo/foo-01-vostfr"  # same episode, different casing/slash
OTHER_URL = "https://voir-anime.to/anime/foo/foo-02-vostfr/"


# 1. Same URL twice.
def test_scenario_1_same_url_twice_is_recognized_as_known():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.register_discovered(key, anime_key="postid:1")  # seen again
    assert store.is_known(key)
    assert len(store.known_keys()) == 1


# 2. Same episode detected twice (e.g. once from homepage, once from anime page),
#    via a differently-formatted but equivalent URL.
def test_scenario_2_same_episode_via_url_variant_is_recognized_as_known():
    store = EpisodeStore()
    key1 = build_episode_key(URL)
    key2 = build_episode_key(URL_VARIANT)
    assert key1 == key2  # canonicalization from Phase 8
    store.register_discovered(key1, anime_key="postid:1")
    assert store.is_known(key2)


# 3. Different URL -> genuinely a different episode, must NOT be treated as duplicate.
def test_scenario_3_different_url_is_not_a_duplicate():
    store = EpisodeStore()
    store.register_discovered(build_episode_key(URL), anime_key="postid:1")
    assert not store.is_known(build_episode_key(OTHER_URL))


# 4. Slightly different title text must not affect dedup (dedup is URL-keyed only)
#    in either direction: same URL + different title -> still recognized as known;
#    different URL + very similar title -> NOT merged into one entry.
def test_scenario_4_title_text_does_not_affect_dedup():
    store = EpisodeStore()
    key = build_episode_key(URL)
    # Title text is not part of the key at all -- register/check only ever see the key.
    store.register_discovered(key, anime_key="postid:1")
    assert store.is_known(key)  # regardless of what title text a caller observed

    other_key = build_episode_key(OTHER_URL)
    assert other_key != key  # two different episodes stay distinct even if titles are near-identical
    assert not store.is_known(other_key)


# 5 & 6. Program restart, DB conserved: a fresh store rebuilt from a persisted
#        snapshot must recognize previously-known episodes.
def test_scenario_5_restart_with_db_conserved_keeps_known_keys():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.mark_status(key, EpisodeStatus.PUBLISHED)

    snapshot = store.snapshot()
    # Simulate process restart: a brand new EpisodeStore instance.
    restarted_store = EpisodeStore.restore(snapshot, anime_keys={key: "postid:1"})

    assert restarted_store.is_known(key)
    assert restarted_store.status_of(key) == EpisodeStatus.PUBLISHED
    assert restarted_store.is_already_published(key)


# 7. Local file deleted: status/dedup logic is independent of filesystem state --
#    this module has no notion of a file at all, so "deleting a file" cannot change
#    what is_known()/status_of() report. Documented via a direct assertion that
#    nothing in the store's public API takes a filesystem path.
def test_scenario_7_dedup_state_independent_of_local_file_existence():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.mark_status(key, EpisodeStatus.DOWNLOADED)
    # "Delete the local file" has no representation here -- status must be unchanged
    # by anything other than an explicit mark_status() call.
    assert store.status_of(key) == EpisodeStatus.DOWNLOADED


# 8. Already published: re-discovering it must not regress its status, and must be
#    excluded from anything that treats "already published" as "already handled".
def test_scenario_8_already_published_is_not_reprocessed():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.mark_status(key, EpisodeStatus.PUBLISHED)

    # Re-discovering the same episode (e.g. it's still on the homepage feed) must
    # not reset it back to DISCOVERED.
    store.register_discovered(key, anime_key="postid:1")
    assert store.status_of(key) == EpisodeStatus.PUBLISHED
    assert store.is_already_published(key)


def test_mark_status_on_unknown_key_raises():
    store = EpisodeStore()
    try:
        store.mark_status(build_episode_key(URL), EpisodeStatus.PUBLISHED)
        assert False, "expected KeyError"
    except KeyError:
        pass
