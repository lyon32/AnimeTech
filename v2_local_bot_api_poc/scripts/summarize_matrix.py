#!/usr/bin/env python
"""V2_LOCAL_BOT_API_POC — compile per-size evidence into one summary matrix."""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E = ROOT / "output" / "evidence" / "upload"

rows = []
for f in ["upload_small.json", "upload_size100.json", "upload_size300.json",
          "upload_size500.json", "upload_size700.json", "upload_dup.json",
          "video_blacktorch10.json"]:
    path = E / f
    if not path.exists():
        continue
    ev = json.load(open(path, encoding="utf-8"))
    g = ev.get("getFile") or {}
    sv = ev.get("server_sha256_check") or {}
    rows.append({
        "label": ev.get("label"),
        "stage": ev.get("stage"),
        "size_bytes": ev.get("size_bytes"),
        "size_mib": round((ev.get("size_bytes") or 0) / 1048576, 1),
        "upload_seconds": ev.get("upload_seconds"),
        "mb_per_s": ev.get("upload_mb_per_s"),
        "mbps": ev.get("upload_mbps"),
        "http": ev.get("http"),
        "ok": ev.get("ok"),
        "error": (ev.get("description") or "")[:120],
        "message_id": (ev.get("document") or {}).get("message_id"),
        "file_unique_id": (ev.get("document") or {}).get("file_unique_id"),
        "getFile_size_match": g.get("size_match"),
        "server_sha_match": sv.get("match"),
        "result": ("PROVEN"
                   if (ev.get("verification") or {}).get("result") is True
                   else ("FAILED"
                         if (ev.get("verification") or {}).get("result") is False
                         else (ev.get("verification") or {}).get("result"))),
    })

summary = {
    "stage": "upload_matrix_summary",
    "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "server": "local Bot API 10.3 (official build, --local)",
    "rows": rows,
}
out = ROOT / "output" / "evidence" / "upload" / "summary_matrix.json"
out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

header = "{:<12}{:>9}{:>12}{:>10}{:>9}{:>9}{:>5}{:>26}{:>10}".format(
    "label", "MiB", "seconds", "MB/s", "Mbps", "http", "ok", "file_unique_id", "result")
print(header)
for r in rows:
    f = lambda k: "" if r[k] is None else r[k]
    print("{:<12}{:>9}{:>12}{:>10}{:>9}{:>5}{!s:>5}{:>26}{:>10}".format(
        f("label"), f("size_mib"), f("upload_seconds"), f("mb_per_s"), f("mbps"),
        f("http"), f("ok"), str(f("file_unique_id"))[:24], f("result")))
print("\nwritten:", out)