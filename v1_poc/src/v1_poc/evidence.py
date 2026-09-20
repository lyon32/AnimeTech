"""Evidence helpers: safe (token-redacted) URL representation + JSON writers."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_SIGNED_PARAM_RE = re.compile(r"^(t|s|e|md5|sig|expires|exp|signature)=[^&]*", re.IGNORECASE)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_url(url: str) -> str:
    """Removes time-limited/signed query params so logs and evidence don't carry
    credentials, keeping host + path + non-sensitive params (POC spec clause 12)."""
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    kept = [p for p in query.split("&") if not _SIGNED_PARAM_RE.match(p)]
    if kept and any(kept):
        return f"{base}?{'&'.join(kept)}"
    return base


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path