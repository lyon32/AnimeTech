"""Configuration loading (config.yaml, no secrets) + project-wide paths."""
from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

_cached: dict | None = None


def load_config() -> dict:
    global _cached
    if _cached is None:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            _cached = yaml.safe_load(fh)
    return _cached


def output_dir() -> Path:
    return PROJECT_ROOT / "output"


def evidence_dir(*parts: str) -> Path:
    path = output_dir() / "evidence"
    for part in parts:
        path = path / part
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    path = output_dir() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir() -> Path:
    path = PROJECT_ROOT / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir() -> Path:
    path = output_dir() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path