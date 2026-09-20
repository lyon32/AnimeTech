"""Environment / credentials handling.

Credentials live exclusively in the gitignored .env file or in process
environment variables. They are never written into source code, tests,
reports, logs, or evidence files.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def load_env() -> None:
    """Loads the gitignored .env file into os.environ (does not override existing vars)."""
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)


def get_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Missing required secret {name!r}. Set it in the gitignored .env "
            f"({ENV_FILE}) or as an environment variable."
        )
    return value


def has_secret(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())