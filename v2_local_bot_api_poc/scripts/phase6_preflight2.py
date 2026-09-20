#!/usr/bin/env python
"""Phase 6 preflight v2 — short timeouts, execute step by step."""
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
import httpx

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
LOCAL = "http://127.0.0.1:8081"

def probe(label, fn):
    t0 = time.perf_counter()
    try:
        r = fn()
        print(f"{label}: OK in {time.perf_counter()-t0:.2f}s -> {r}")
    except Exception as e:
        print(f"{label}: FAILED in {time.perf_counter()-t0:.2f}s -> {type(e).__name__}: {str(e)[:200]}")

print("token_present:", bool(TOKEN), "channel_present:", bool(CHANNEL))
probe("getMe", lambda: httpx.post(f"{LOCAL}/bot{TOKEN}/getMe", timeout=15).status_code)
probe("sendMessage", lambda: httpx.post(f"{LOCAL}/bot{TOKEN}/sendMessage",
      data={"chat_id": CHANNEL, "text": "[phase6 preflight]"}, timeout=20).status_code)
try:
    st = httpx.get(f"{LOCAL.replace('8081','8082')}/status?all=1", timeout=10)
    print("stats http:", st.status_code, "len:", len(st.text))
except Exception as e:
    print("stats FAILED:", type(e).__name__, str(e)[:120])