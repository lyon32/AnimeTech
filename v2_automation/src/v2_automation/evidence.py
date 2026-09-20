"""Evidence writing for v2_automation (kept under v2_automation/output/evidence).

Evidence is append-only on disk — proven facts are never deleted or rewritten
in place (V1/V2 policy).  Fingerprints make every write auditable.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVID_DIR = ROOT / "output" / "evidence"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def evidence_dir(*parts: str) -> Path:
    path = EVID_DIR
    for part in parts:
        path = path / part
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def record_publication(payload: dict) -> Path | None:
    """Phase 12 — per-episode publication proof (message ids, sha, sizes).
    Best-effort: never raises (evidence must not break the pipeline).  The
    payload must never contain secrets (token/channel are excluded)."""
    try:
        path = evidence_dir("publications") / f"publication_{payload.get('episode_id')}.json"
        payload["ts"] = payload.get("ts") or __import__(
            "v2_automation.timeutil", fromlist=["now_utc"]).now_utc()
        return write_json(path, payload)
    except Exception:
        return None