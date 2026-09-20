"""Covers the 3 crash/restart scenarios in MASTER_PLAN.md section 22."""
import pytest

from source_audit.analysis.identity import build_episode_key
from source_audit.detection.deduplication import EpisodeStatus, EpisodeStore
from source_audit.detection.restart_recovery import RecoveryAction, determine_recovery_action

URL = "https://voir-anime.to/anime/foo/foo-01-vostfr/"


def test_every_episode_status_has_a_defined_recovery_action():
    """MASTER_PLAN.md §22: every state must have a recovery strategy -- this must
    hold for the full enum, not just the 3 scenarios spelled out in the plan."""
    for status in EpisodeStatus:
        action = determine_recovery_action(status)  # must not raise
        assert isinstance(action, RecoveryAction)


# Scenario: START -> DISCOVERED -> DOWNLOADING -> crash -> restart.
def test_crash_during_downloading_restarts_the_download():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.mark_status(key, EpisodeStatus.DOWNLOADING)

    # Simulate crash + restart: persist and reload.
    snapshot = store.snapshot()
    restarted = EpisodeStore.restore(snapshot)

    action = determine_recovery_action(restarted.status_of(key))
    assert action == RecoveryAction.RESTART_DOWNLOAD


# Scenario: START -> DOWNLOADED -> crash -> restart.
def test_crash_after_downloaded_validates_then_publishes():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.mark_status(key, EpisodeStatus.DOWNLOADED)

    snapshot = store.snapshot()
    restarted = EpisodeStore.restore(snapshot)

    action = determine_recovery_action(restarted.status_of(key))
    assert action == RecoveryAction.VALIDATE_THEN_PUBLISH


# Scenario: START -> PUBLISHED -> crash -> restart.
def test_crash_after_published_takes_no_action():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    store.mark_status(key, EpisodeStatus.PUBLISHED)

    snapshot = store.snapshot()
    restarted = EpisodeStore.restore(snapshot)

    action = determine_recovery_action(restarted.status_of(key))
    assert action == RecoveryAction.NO_ACTION
    # Consistent with Phase 10's dedup guarantee: still reported as published.
    assert restarted.is_already_published(key)


def test_discovered_but_never_started_also_restarts_download():
    store = EpisodeStore()
    key = build_episode_key(URL)
    store.register_discovered(key, anime_key="postid:1")
    # No further status change -- crash happened between DISCOVERED and DOWNLOADING.

    snapshot = store.snapshot()
    restarted = EpisodeStore.restore(snapshot)

    action = determine_recovery_action(restarted.status_of(key))
    assert action == RecoveryAction.RESTART_DOWNLOAD


def test_recovery_action_for_none_status_is_a_caller_error():
    with pytest.raises(KeyError):
        determine_recovery_action(None)  # type: ignore[arg-type]
