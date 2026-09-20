import pytest


@pytest.fixture(autouse=True)
def _isolated_evidence(tmp_path, monkeypatch):
    """Downloads/thumbnails/evidence go to a per-test dir so the media cache never leaks between tests."""
    from v2_automation import evidence
    monkeypatch.setattr(evidence, "EVID_DIR", tmp_path / "evidence")
