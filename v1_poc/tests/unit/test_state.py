"""Unit tests: published-state persistence and duplicate detection."""
from __future__ import annotations

import json

from v1_poc.state import PublishedState

KEY = "https://voir-anime.to/anime/x/x-01-vostfr"


def test_empty_state_not_published(tmp_path):
    state = PublishedState(tmp_path / "nope.json")
    assert state.is_published(KEY) is False
    assert state.get(KEY) is None


def test_record_and_reload(tmp_path):
    path = tmp_path / "state.json"
    state = PublishedState(path)
    state.record(KEY, {"telegram_message_id": 42, "sha256": "abc"})
    assert state.is_published(KEY) is True
    assert state.get(KEY)["telegram_message_id"] == 42

    reloaded = PublishedState(path)
    assert reloaded.is_published(KEY) is True
    assert reloaded.get(KEY)["sha256"] == "abc"


def test_corrupt_state_survives(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    state = PublishedState(path)
    assert state.is_published(KEY) is False
    state.record(KEY, {"ok": True})
    reloaded = PublishedState(path)
    assert reloaded.is_published(KEY) is True


def test_different_keys_independent(tmp_path):
    state = PublishedState(tmp_path / "s.json")
    other = "https://voir-anime.to/anime/x/x-02-vf"
    state.record(KEY, {"a": 1})
    assert state.is_published(KEY) is True
    assert state.is_published(other) is False
    state.record(other, {"a": 2})
    assert state.is_published(other) is True


def test_state_never_regresses_on_duplicate_record(tmp_path):
    state = PublishedState(tmp_path / "s.json")
    state.record(KEY, {"v": 1})
    state.record(KEY, {"v": 2})
    assert state.get(KEY)["v"] == 2  # latest metadata wins, entry stays unique