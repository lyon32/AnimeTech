"""Capacity measurement at startup — persisted to bot_capacity (schema v1).

Reads the running Local Bot API server state and the V2 POC upload evidence to
establish a trustworthy snapshot:  local enabled/version, documented limit,
tested limit (max proven from evidence), free disk.  Used by the scheduler to
fail fast on uploads that exceed the tested ceiling until Phase 6 raises it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import app_config, db, repo
from .timeutil import now_utc

EVID_UPLOAD = app_config.EVIDENCE_DIR / "upload"


def _walk_evidence() -> list[dict[str, Any]]:
    """Re-read V2 upload evidence (proven facts; never invent)."""
    if not EVID_UPLOAD.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(EVID_UPLOAD.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def tested_limit_bytes_from_evidence() -> int | None:
    """Max successful upload size proved by the V2 POC evidence files."""
    best: int | None = None
    for ev in _walk_evidence():
        if ev.get("http") != 200:
            continue
        size = ev.get("size_bytes") or (ev.get("getFile") or {}).get("expected_size")
        if isinstance(size, int) and size > 0:
            best = size if best is None else max(best, size)
    return best


def measure() -> dict[str, Any]:
    cfg = app_config.load_config()
    caps = cfg.bot_capacity
    tested = tested_limit_bytes_from_evidence()
    payload: dict[str, Any] = {
        "schema_version": __import__("v2_automation").schema.current_schema_version(),
        "local_bot_api_enabled": caps.enabled,
        "local_bot_api_getme_ok": caps.getme_ok,
        "http_server_version": caps.http_server_version or "unknown",
        "documented_upload_limit_bytes": caps.documented_limit_bytes,
        "tested_upload_limit_bytes": tested or caps.tested_limit_bytes,
        "tested_proven_from_evidence": bool(tested),
        "free_disk_bytes": caps.free_disk_bytes,
        "evidence_count": len(_walk_evidence()),
        "measured_at": now_utc(),
    }
    return payload


def persist() -> dict[str, Any]:
    payload = measure()
    conn = db.connect()
    db.migrate(conn)
    repo.save_capacity(conn, payload)
    conn.commit()
    return payload


def doctor() -> dict[str, Any]:
    """Read-only well-being diagnostic (no DB write, no source contact)."""
    return measure()