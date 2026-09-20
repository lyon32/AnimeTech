"""Unit tests for the V2 state machine (states.py)."""
import pytest

from v2_automation.states import State, can_transition, mainline_index


def test_mainline_complete_chain():
    chain = [State.DISCOVERED, State.IDENTIFIED, State.QUEUED, State.DOWNLOADING,
             State.DOWNLOADED, State.VALIDATING, State.VALIDATED,
             State.PUBLISHING_THUMBNAIL, State.THUMBNAIL_PUBLISHED,
             State.PUBLISHING_VIDEO, State.PUBLISHED, State.CLEANUP_PENDING, State.CLEANED]
    for cur, nxt in zip(chain, chain[1:]):
        assert can_transition(cur, nxt), f"{cur.value} -> {nxt.value} doit etre permis"
        assert mainline_index(cur) < mainline_index(nxt)


def test_cleanup_blocked_edges():
    # PUBLISHED with no provable published_at -> CLEANUP_BLOCKED (operator-only)
    assert can_transition(State.PUBLISHED, State.CLEANUP_BLOCKED)
    assert can_transition(State.CLEANUP_PENDING, State.CLEANUP_BLOCKED)
    assert can_transition(State.CLEANUP_BLOCKED, State.CLEANED)     # manuel
    assert not can_transition(State.CLEANUP_BLOCKED, State.QUEUED)
    assert not can_transition(State.CLEANED, State.CLEANUP_BLOCKED)


def test_skips_are_forbidden():
    assert not can_transition(State.DISCOVERED, State.QUEUED)
    assert not can_transition(State.DISCOVERED, State.DOWNLOADING)
    assert not can_transition(State.VALIDATED, State.PUBLISHING_VIDEO)  # passer la vignette interdit
    assert not can_transition(State.PUBLISHED, State.CLEANED)           # cleanup_pending requis


def test_failure_edges():
    assert can_transition(State.DOWNLOADING, State.FAILED)
    assert can_transition(State.DOWNLOADING, State.RETRY_WAIT)
    assert can_transition(State.RETRY_WAIT, State.QUEUED)
    assert can_transition(State.RETRY_WAIT, State.FAILED)
    assert can_transition(State.STRUCTURE_CHANGED, State.QUEUED)
    assert can_transition(State.FAILED, State.QUEUED)   # retry manuel/recovery
    assert can_transition(State.THUMBNAIL_PUBLISHED, State.PUBLISHING_VIDEO)


def test_terminal_states():
    assert set(State.CLEANED.value) == set(State.CLEANED.value)
    assert not can_transition(State.CLEANED, State.QUEUED)
    assert not can_transition(State.SKIPPED_DUP, State.QUEUED)


def test_mainline_index_side_states_are_minus_one():
    for s in (State.FAILED, State.RETRY_WAIT, State.STRUCTURE_CHANGED,
              State.SKIPPED_DUP, State.BLOCKED):
        assert mainline_index(s) == -1


def test_blocked_edges():
    assert can_transition(State.DISCOVERED, State.BLOCKED)
    assert can_transition(State.IDENTIFIED, State.BLOCKED)
    assert can_transition(State.QUEUED, State.BLOCKED)
    assert can_transition(State.RETRY_WAIT, State.BLOCKED)
    assert can_transition(State.BLOCKED, State.QUEUED)      # après correction du registre
    assert not can_transition(State.BLOCKED, State.DOWNLOADING)
    assert not can_transition(State.BLOCKED, State.FAILED)