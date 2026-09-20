#!/usr/bin/env python
"""V2_LOCAL_BOT_API_POC — record server build evidence (no secrets)."""
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVID_DIR = ROOT / "output" / "evidence"
EVID_DIR.mkdir(parents=True, exist_ok=True)

COMMIT = "e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1"
COMMIT_DATE = "2026-08-25T13:10:16Z"


def sh(cmd, stderr_ok=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout.strip()
    if stderr_ok and not out:
        out = r.stderr.strip()
    return out


evidence = {
    "method": "official_build_from_source",
    "official_repo": "https://github.com/tdlib/telegram-bot-api",
    "pinned_commit": COMMIT,
    "pinned_commit_date": COMMIT_DATE,
    "build_generator_ref": "https://tdlib.github.io/telegram-bot-api/build.html",
    "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "image": {"tag": "v2-telegram-bot-api:latest"},
    "server_version": sh(["docker", "run", "--rm", "--env-file",
                          str(ROOT / ".env"), "v2-telegram-bot-api", "--version"],
                         stderr_ok=True),
    "image_id": sh(["docker", "images", "v2-telegram-bot-api",
                    "--format", "{{.ID}}"]),
    "image_size": sh(["docker", "images", "v2-telegram-bot-api",
                      "--format", "{{.Size}}"]),
    "container_status": sh(["docker", "inspect", "v2-telegram-bot-api",
                            "--format",
                            "{{.State.Status}} ports={{json .NetworkSettings.Ports}}"]),
}

out_file = EVID_DIR / "server_build.json"
out_file.write_text(json.dumps(evidence, indent=2, ensure_ascii=False),
                    encoding="utf-8")
print(json.dumps({k: v for k, v in evidence.items()
                  if k not in ("image",)}, indent=2, ensure_ascii=False))
print("written:", out_file)