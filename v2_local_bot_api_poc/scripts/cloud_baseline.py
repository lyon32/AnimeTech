#!/usr/bin/env python
"""V2_LOCAL_BOT_API_POC — cloud baseline BEFORE migration (getMe + getWebhookInfo).

Never writes or prints the token.
"""
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
assert TOKEN, "TELEGRAM_BOT_TOKEN missing"
CHANNEL = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()

BASE = f"https://api.telegram.org/bot{TOKEN}"
MASKED = BASE.replace(TOKEN, "<TOKEN>")


def call(client, method):
    r = client.post(f"{BASE}/{method}", timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:200]}
    return {"http": r.status_code, "ok": body.get("ok"), "result": body.get("result"),
            "error_code": body.get("error_code"), "description": body.get("description")}


evidence = {
    "stage": "pre_migration_cloud_baseline",
    "server": MASKED,
    "channel": CHANNEL,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}

with httpx.Client() as c:
    evidence["getMe"] = call(c, "getMe")
    evidence["getWebhookInfo"] = call(c, "getWebhookInfo")

out_file = ROOT / "output" / "evidence" / "pre_migration_cloud_baseline.json"
out_file.parent.mkdir(parents=True, exist_ok=True)
out_file.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")

print("getMe http:", evidence["getMe"]["http"], "ok:", evidence["getMe"]["ok"])
user = (evidence["getMe"].get("result") or {}).get("username")
print("bot username:", user)
print("getWebhookInfo http:", evidence["getWebhookInfo"]["http"],
      "webhook_set:", bool((evidence["getWebhookInfo"].get("result") or {}).get("url")))
print("written:", out_file)