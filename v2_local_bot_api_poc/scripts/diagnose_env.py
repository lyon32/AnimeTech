#!/usr/bin/env python
"""V2_LOCAL_BOT_API_POC — presence-only check of required environment secrets.

Prints PRESENT / MISSING per variable. NEVER prints values. Stops with a
non-zero exit code if any required variable is missing (POC rule 5).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=False)

REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
]

all_present = True
for name in REQUIRED:
    present = bool(os.environ.get(name, "").strip())
    if not present:
        all_present = False
    print(f"{name} : {'PRESENT' if present else 'MISSING'}")

if not all_present:
    print("\nMISSING variable(s) detected — STOP. Add them to the gitignored "
          f"{ENV_FILE} then re-run.")
    sys.exit(1)

print("\nAll required variables are present.")
sys.exit(0)