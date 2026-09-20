#!/usr/bin/env python
"""V2_LOCAL_BOT_API_POC — documented logOut migration analysis + execution.

Following the official documentation ("Moving a bot to a local server"), a bot
must be deregistered from https://api.telegram.org by calling logOut before it
will work against a local Bot API server.

Consequences (documented before execution, user authorized):
  * After logOut the token stops working against the cloud (api.telegram.org);
    re-running V1 cloud tests against this token is not possible until the bot
    is logged back on the cloud server.
  * Recovery: call logOut on the LOCAL server, then the bot can be re-registered
    on the cloud server (per Telegram's "moving a bot" documentation pattern).
  * No production feature depends on this bot yet (POC stage).

This script STOPS before executing logOut unless it is invoked with --execute.
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EVID_DIR = ROOT / "output" / "evidence"
EVID_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env", override=False)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
LOCAL_BASE = "http://127.0.0.1:8081"
CLOUD_BASE = f"https://api.telegram.org/bot{TOKEN}"

DRY_RUN = "--execute" not in sys.argv


def call(client, base, method, timeout=30):
    try:
        r = client.post(f"{base}/{method}", timeout=timeout)
    except httpx.HTTPError as e:
        return {"error": type(e).__name__, "detail": str(e)[:160]}
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:200]}
    return {"http": r.status_code, "ok": body.get("ok"),
            "result": body.get("result"),
            "error_code": body.get("error_code"),
            "description": body.get("description")}


analysis = {
    "stage": "logout_migration",
    "doc_ref": "https://core.telegram.org/bots/api#logout",
    "doc_ref_readme": "https://github.com/tdlib/telegram-bot-api (Moving a bot to a local server)",
    "consequences_documented": [
        "Token inoperative vs cloud apres logOut (V1 cloud tests non rejouables)",
        "Recovery possible: logOut sur serveur LOCAL puis re-enregistrement cloud",
        "Bot en cours de POC uniquement, aucune feature de production dependante",
    ],
    "user_authorization": "confirmed",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}

with httpx.Client() as cloud_client:
    with httpx.Client() as local_client:
        analysis["pre_local_getMe_before_migration"] = call(
            local_client, f"{LOCAL_BASE}/bot{TOKEN}", "getMe")
        analysis["cloud_getMe_before"] = call(cloud_client, CLOUD_BASE, "getMe")

        if DRY_RUN:
            analysis["executed"] = False
            analysis["note"] = "DRY-RUN (--execute absent) — logOut non execute."
        else:
            analysis["cloud_logOut"] = call(cloud_client, CLOUD_BASE, "logOut")
            analysis["cloud_getMe_after"] = call(cloud_client, CLOUD_BASE, "getMe")
            analysis["local_getMe_after"] = call(
                local_client, f"{LOCAL_BASE}/bot{TOKEN}", "getMe")
            analysis["executed"] = True

out_file = EVID_DIR / "logout_migration.json"
out_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")

mask = lambda d: {**d, "detail": d.get("detail", "")}
print("executed:", analysis["executed"])
print("pre  local getMe:", analysis["pre_local_getMe_before_migration"].get("http"),
      "->", str(analysis["pre_local_getMe_before_migration"].get("description") or "")[:80])
print("pre  cloud getMe:", analysis["cloud_getMe_before"].get("http"))
if not DRY_RUN:
    print("logOut cloud      :", analysis["cloud_logOut"].get("http"),
          str(analysis["cloud_logOut"].get("description") or "")[:80])
    print("post cloud getMe  :", analysis["cloud_getMe_after"].get("http"))
    print("post local getMe  :", analysis["local_getMe_after"].get("http"))
print("written:", out_file)
if DRY_RUN:
    print("\n[DRY-RUN] Relancer avec --execute pour migrer le bot.")