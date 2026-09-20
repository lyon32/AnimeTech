#!/usr/bin/env python
"""Phase 6 preflight — never writes the token; prints only masked paths."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_upload_tests as rt
from dotenv import load_dotenv

load_dotenv(rt.ROOT / ".env", override=False)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

print("token_present:", bool(TOKEN))
print("channel_present:", bool(rt.CHANNEL))

code, body = rt.post(rt.LOCAL, "getMe", {})
print("getMe http:", code, "ok:", body.get("ok"),
      "user:", (body.get("result") or {}).get("username"))

prev = json.loads((rt.EVID_DIR / "upload_size700.json").read_text(encoding="utf-8"))
fid = (prev.get("document") or {}).get("file_id")
fcode, fbody = rt.post(rt.LOCAL, "getFile", {"file_id": fid})
res = fbody.get("result") or {}
fp = (res or {}).get("file_path") or ""
safe = fp.replace(TOKEN, "<TOKEN_MASKED>") if TOKEN else fp
print("getFile http:", fcode, "file_size:", res.get("file_size"),
      "path_contains_token:", bool(TOKEN and TOKEN in fp))
print("file_path(masked):", safe)