#!/usr/bin/env python
"""V2_LOCAL_BOT_API_POC — real upload tests against the LOCAL Bot API server.

Usage:
  python run_upload_tests.py message [--text "..."]
  python run_upload_tests.py upload <size_mb> --label <label> [--keep]
  python run_upload_tests.py verify <evidence_file>
  python run_upload_tests.py stats              # dump 8082 statistics JSON

Evidence JSON files are written under output/evidence/upload/. The bot token is
NEVER written to files or stdout (only presence booleans).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EVID_DIR = ROOT / "output" / "evidence" / "upload"
EVID_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env", override=False)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
LOCAL = "http://127.0.0.1:8081"
CONTAINER = "v2-telegram-bot-api"
CONTAINER_DATA = "/data"

MB = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_blob(path: Path, size_bytes: int, seed: int = 12345) -> str:
    """Deterministic pseudo-random blob of exactly size_bytes."""
    state = (seed * 2654435761) & 0xFFFFFFFF
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        remaining = size_bytes
        while remaining > 0:
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            chunk = (state).to_bytes(16, "big")
            if remaining < len(chunk):
                chunk = chunk[:remaining]
            f.write(chunk)
            remaining -= len(chunk)
    return sha256_file(path)


def post(base, method, params, files=None, timeout=1800):
    r = httpx.post(f"{base}/bot{TOKEN}/{method}",
                   data=params, files=files, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:300]}
    return r.status_code, body


def get_file(client, file_id):
    return post(LOCAL, "getFile", {"file_id": file_id})


def server_sha256(path_in_container: str) -> dict:
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "sha256sum", path_in_container],
        capture_output=True, text=True)
    return {"exit": r.returncode,
            "sha": r.stdout.split()[0] if r.stdout and r.returncode == 0 else "",
            "err": r.stderr[:200] or None}


def download_via_api(client, file_path: str) -> bytes:
    """Download via the local /file endpoint. file_path may be absolute."""
    p = file_path
    if p.startswith(CONTAINER_DATA):
        p = p[len(CONTAINER_DATA):]           # strip server working dir root
    url = f"{LOCAL}/file/bot{TOKEN}{p}"
    r = client.get(url, timeout=1800)
    return r.status_code, r.content


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cmd_message(args):
    params = {"chat_id": CHANNEL, "text": args.text}
    t0 = time.perf_counter()
    code, body = post(LOCAL, "sendMessage", params)
    dt = time.perf_counter() - t0
    ev = {"stage": "sendMessage_local", "http": code, "ok": body.get("ok"),
          "message_id": (body.get("result") or {}).get("message_id"),
          "call_seconds": round(dt, 4),
          "channel_present": bool(CHANNEL),
          "ts": utc()}
    out = EVID_DIR / "sendMessage.json"
    out.write_text(json.dumps(ev, indent=2), encoding="utf-8")
    print(json.dumps(ev, indent=2))
    print("written:", out)


def cmd_upload(size_mb: int, label: str, keep: bool):
    size = size_mb * MB
    blob = ROOT / "downloads" / f"v2_test_{label}_{size_mb}MB.bin"
    write_t0 = time.perf_counter()
    sha = make_blob(blob, size)
    write_s = time.perf_counter() - write_t0

    blob_fh = blob.open("rb")
    files = {"document": (blob.name, blob_fh, "application/octet-stream")}
    params = {"chat_id": CHANNEL,
              "caption": f"V2 LOCAL POC {label} {size_mb} MB",
              "disable_notification": True}
    t0 = time.perf_counter()
    try:
        code, body = post(LOCAL, "sendDocument", params, files=files)
    finally:
        blob_fh.close()
    dt = time.perf_counter() - t0

    ev = {"stage": "upload_local", "label": label,
          "size_bytes": size, "size_mib": round(size / MB, 1),
          "local_sha256": sha,
          "file_write_seconds": round(write_s, 4),
          "upload_seconds": round(dt, 4),
          "upload_mb_per_s": round((size / MB) / dt, 4) if dt > 0 else None,
          "upload_mbps": round(size * 8 / 1e6 / dt, 4) if dt > 0 else None,
          "http": code, "ok": body.get("ok"),
          "error_code": body.get("error_code"),
          "description": (body.get("description") or "")[:160],
          "ts": utc()}
    result = body.get("result") or {}
    if result:
        ev["document"] = {"file_unique_id": result.get("document", {}).get("file_unique_id"),
                          "file_id": result.get("document", {}).get("file_id"),
                          "message_id": result.get("message_id")}

    if code == 200:
        with httpx.Client(timeout=1800) as c:
            fcode, fbody = get_file(c, ev["document"]["file_id"])
            fresult = fbody.get("result") or {}
            ev["getFile"] = {"http": fcode, "ok": fbody.get("ok"),
                             "file_id": fresult.get("file_id"),
                             "file_unique_id": fresult.get("file_unique_id"),
                             "file_size": fresult.get("file_size"),
                             "file_path": fresult.get("file_path"),
                             "expected_size": size,
                             "size_match": fresult.get("file_size") == size}
            server_path = fresult.get("file_path")
            if server_path:
                sv = server_sha256(server_path)
                ev["server_sha256_check"] = {"server_path_abs": server_path,
                                             "exit": sv["exit"],
                                             "sha": sv["sha"] or None,
                                             "match": sv["sha"] == sha}
                dcode, data = download_via_api(c, server_path)
                dl_sha = hashlib.sha256(data).hexdigest() if dcode == 200 else None
                ev["download_check"] = {"http": dcode, "bytes": len(data),
                                        "sha256": dl_sha,
                                        "match": dl_sha == sha if dcode == 200 else None}
                sv_match = (sv.get("sha") == sha) if sv.get("sha") else False
                ev["verification"] = {
                    "method_primary": "getFile file_path abs + sha256sum cote serveur",
                    "method_secondary": "download /file endpoint (N/A en mode local: 404)",
                    "size_match": ev["getFile"]["size_match"],
                    "server_sha_match": sv_match,
                    "download_sha_match": ev["download_check"].get("match"),
                    "result": bool(ev["getFile"]["size_match"] and sv_match)
                                and "PROVEN" or "FAILED"}

    out = EVID_DIR / f"upload_{label}.json"
    out.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    if not keep:
        blob.unlink(missing_ok=True)
    print(json.dumps(ev, indent=2, ensure_ascii=False))
    print("written:", out)
    return ev


def cmd_verify(args):
    ev = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    res = {"file_size": ev.get("getFile", {}).get("expected_size")}
    # re-verify from getFile info stored
    print(json.dumps({"summary": {
        "label": ev.get("label"),
        "http": ev.get("http"),
        "server_sha_match": ev.get("server_sha256_check", {}).get("match"),
        "download_match": ev.get("download_check", {}).get("match"),
        "size_match": ev.get("getFile", {}).get("size_match"),
    }}, indent=2))


def cmd_stats(args):
    try:
        r = httpx.get(f"{LOCAL.replace('8081', '8082')}/status?all=1", timeout=15)
        out = EVID_DIR / "stats_snapshot.txt"
        out.write_text(r.text, encoding="utf-8")
        print(r.text)
        print("written:", out)
    except Exception as e:
        print("stats error:", type(e).__name__, str(e)[:160])
        sys.exit(1)


def cmd_video(args):
    path = Path(args.path)
    if not path.exists():
        print("file not found:", path)
        sys.exit(2)
    size = path.stat().st_size
    sha = sha256_file(path)
    fh = path.open("rb")
    params = {"chat_id": CHANNEL,
              "caption": args.caption or f"V2 LOCAL POC real video {size//MB} MB",
              "disable_notification": True}
    files = {"video": (path.name, fh, "video/mp4")}
    t0 = time.perf_counter()
    try:
        code, body = post(LOCAL, "sendVideo", params, files=files,
                          timeout=max(1800, size // (512*1024)))
    finally:
        fh.close()
    dt = time.perf_counter() - t0
    ev = {"stage": "video_local", "label": args.label, "file": str(path),
          "size_bytes": size, "size_mib": round(size / MB, 1),
          "sha256": sha, "upload_seconds": round(dt, 4),
          "upload_mb_per_s": round(size / MB / dt, 4) if dt > 0 else None,
          "upload_mbps": round(size * 8 / 1e6 / dt, 4) if dt > 0 else None,
          "http": code, "ok": body.get("ok"),
          "error_code": body.get("error_code"),
          "description": (body.get("description") or "")[:200],
          "ts": utc()}
    result = body.get("result") or {}
    if result:
        doc = result.get("video") or result.get("document") or {}
        ev["document"] = {"file_id": doc.get("file_id"),
                          "file_unique_id": doc.get("file_unique_id"),
                          "message_id": result.get("message_id")}
    if code == 200:
        with httpx.Client(timeout=1800) as c:
            fcode, fbody = get_file(c, ev["document"]["file_id"])
            fresult = fbody.get("result") or {}
            ev["getFile"] = {"http": fcode, "ok": fbody.get("ok"),
                             "file_size": fresult.get("file_size"),
                             "file_path": fresult.get("file_path"),
                             "size_match": fresult.get("file_size") == size}
            server_path = fresult.get("file_path")
            if server_path:
                sv = server_sha256(server_path)
                ev["server_sha256_check"] = {"exit": sv["exit"],
                                             "sha": sv.get("sha"),
                                             "match": sv.get("sha") == sha}
                ev["verification"] = {"method": "getFile path abs + sha256sum serveur",
                                      "result": bool(ev["getFile"]["size_match"]
                                                     and ev["server_sha256_check"]["match"]
                                                     and "PROVEN" or "FAILED")}
    out = EVID_DIR / f"video_{args.label}.json"
    out.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(ev, indent=2, ensure_ascii=False))
    print("written:", out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("message")
    m.add_argument("--text", default="[V2 LOCAL BOT API] test message via serveur local")
    m.set_defaults(func=cmd_message)

    u = sub.add_parser("upload")
    u.add_argument("size_mb", type=int)
    u.add_argument("--label", required=True)
    u.add_argument("--keep", action="store_true")
    u.set_defaults(func=lambda a: sys.exit(0 if cmd_upload(a.size_mb, a.label, a.keep).get("http") == 200 else 2))

    v = sub.add_parser("verify")
    v.add_argument("evidence")
    v.set_defaults(func=cmd_verify)

    vid = sub.add_parser("video")
    vid.add_argument("--path", required=True)
    vid.add_argument("--caption", default=None)
    vid.add_argument("--label", required=True)
    vid.set_defaults(func=cmd_video)

    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()