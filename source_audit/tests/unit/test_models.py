import pytest
from pydantic import ValidationError

from source_audit.models import EpisodeRecord, EvidenceStatus, HomepageEntry, Language


def test_evidence_status_values_are_exact():
    assert {s.value for s in EvidenceStatus} == {
        "TESTED",
        "MEASURED",
        "NOT_EVALUATED",
        "INCONCLUSIVE",
        "BLOCKED",
        "FAILED",
    }


def test_homepage_entry_requires_url():
    with pytest.raises(ValidationError):
        HomepageEntry(url="")


def test_homepage_entry_unknown_fields_default_to_none():
    entry = HomepageEntry(url="https://example.test/ep/1")
    assert entry.anime_title is None
    assert entry.language is None


def test_episode_record_language_enum():
    ep = EpisodeRecord(url="https://example.test/ep/1", language=Language.VOSTFR)
    assert ep.language == Language.VOSTFR
