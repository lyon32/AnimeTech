"""Capacity measurement from V2 evidence (in-memory, no network)."""
import json
import tempfile
from pathlib import Path

import pytest

from v2_automation import capacities


@pytest.fixture
def fake_evidence(monkeypatch, tmp_path: Path):
    upload = tmp_path / "upload"
    upload.mkdir(parents=True)
    files = {
        "upload_10.json": {"http": 200, "size_bytes": 10 * 1024 * 1024},
        "upload_700.json": {"http": 200, "size_bytes": 700 * 1024 * 1024},
        "upload_fail.json": {"http": 400, "size_bytes": 9999999,
                             "getFile": {"expected_size": 9999999}},
    }
    for name, data in files.items():
        (upload / name).write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(capacities, "EVID_UPLOAD", upload)
    return upload


def test_tested_limit_uses_max_success(fake_evidence):
    assert capacities.tested_limit_bytes_from_evidence() == 700 * 1024 * 1024


def test_no_evidence_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(capacities, "EVID_UPLOAD", tmp_path / "missing")
    assert capacities.tested_limit_bytes_from_evidence() is None


def test_measure_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(capacities, "EVID_UPLOAD", tmp_path / "upload")
    m = capacities.measure()
    assert m["schema_version"] >= 1
    assert "local_bot_api_enabled" in m
    assert "documented_upload_limit_bytes" in m
    assert "measured_at" in m