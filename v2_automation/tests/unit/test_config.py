"""Config loading + capacity dataclass shape (no network side effects)."""
import os
from pathlib import Path

import pytest

from v2_automation import app_config


def test_config_file_present():
    assert app_config.CONFIG_PATH.exists()
    assert app_config.CONFIG_PATH.stat().st_size > 0


def test_no_real_secrets_in_repo_examples():
    """.env.example files must contain placeholders only (audit finding V1/POC)."""
    root = Path(__file__).resolve().parents[2]                     # v2_automation/
    base = Path(__file__).resolve().parents[3]                     # Anime/
    for p in (root / ".env.example",
              base / "v1_poc" / ".env.example",
              base / "v2_local_bot_api_poc" / ".env.example"):
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            looks_placeholder = (("<" in val and ">" in val)  # <COLLER_LE_TOKEN...> : explicit placeholder (commit 957b6f8)
                                 or "x" in val.lower()        # AAxx... tokens placeholders
                                 or val.endswith("0000")   # -1000000000000 / 0000 hash
                                 or val == "12345678"
                                 or val.isdigit()          # admin ids / api ids placeholders
                                 or val.lower().count("abcdef") > 0)
            assert looks_placeholder, f"{p}: {key}={val!r} semble une vraie valeur"


def test_capacity_object_shape():
    cfg = app_config.load_config()
    caps = cfg.bot_capacity
    assert caps is not None
    assert hasattr(caps, "enabled")
    assert caps.documented_limit_bytes is not None
    assert caps.free_disk_bytes is None or caps.free_disk_bytes > 0
    assert isinstance(cfg.admin_telegram_ids, list)
    assert isinstance(cfg.bot_token or "", str)