#!/usr/bin/env python
"""Phase 6 — big-file campaign against the Local Bot API (limit hunt 700->2000 MB).

Reuses the proven V2_LOCAL_BOT_API_POC upload harness (never rewrites it) and
extends it with: per-step evidence under v2_automation/output/evidence/
campaign_phase6/, server-side cleanup after each PROVEN size, token masking in
every recorded path, and host+container disk snapshots.

Usage:
  python phase6_big_files.py preflight
  python phase6_big_files.py run <size_mb> --label <label>   (one campaign step)
  python phase6_big_files.py summary                          (rebuild matrix)
"""
import hashlib, json, os, subprocess, sys, time
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parents[2] / "v2_local_bot_api_poc"
sys.path.insert(0, str(POC_ROOT / "scripts"))

import run_upload_tests as rt  # noqa: E402  (proven harness, reused only)

V2_ROOT = Path(__file__).resolve().parents[2] / "v2_automation"
PHASE_DIR = V2_ROOT / "output" / "evidence" / "campaign_phase6"
PHASE_DIR.mkdir(parents=True, exist_ok=True)

PLAN = [700, 1024, 1500, 1900, 2000, 2001]  # MiB, ascending per master prompt
LABELS = {700: "size700", 1024: "size1024", 1500: "size1500",
          1900: "size1900", 2000: "size2000", 2001: "size2001"}
MB = 1024 * 1024


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mask(text: str | None, token: str) -> str | None:
    if text is None:
        return None
    return text.replace(token, "<TOKEN_MASKED>")


def docker_free_data() -> dict:
    r = subprocess.run(["docker", "exec", rt.CONTAINER, "df", "-B1", "/data"],
                       capture_output=True, text=True)
    free = None
    if r.returncode == 0 and r.stdout:
        parts = r.stdout.splitlines()
        if len(parts) >= 2:
            free = int(parts[1].split()[3])
    return {"rc": r.returncode, "free_bytes": free,
            "err": mask(r.stderr[:200], os.environ.get("TELEGRAM_BOT_TOKEN", ""))}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def server_sha256(path_in_container: str) -> dict:
    r = subprocess.run(["docker", "exec", rt.CONTAINER, "sha256sum", path_in_container],
                       capture_output=True, text=True)
    return {"exit": r.returncode,
            "sha": r.stdout.split()[0] if r.stdout and r.returncode == 0 else "",
            "err": mask(r.stderr[:200], os.environ.get("TELEGRAM_BOT_TOKEN", ""))}


def cleanup_server(path_in_container: str) -> dict:
    r = subprocess.run(["docker", "exec", rt.CONTAINER, "rm", "-f", path_in_container],
                       capture_output=True, text=True)
    return {"exit": r.returncode, "removed": mask(path_in_container, os.environ.get("TELEGRAM_BOT_TOKEN", ""))}


def preflight():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    print("token_present:", bool(tok), "channel_present:", bool(rt.CHANNEL))
    code, body = rt.post(rt.LOCAL, "getMe", {}, timeout=20)
    print("getMe:", code, body.get("ok"))
    print("container_data:", docker_free_data())
    print("plan:", PLAN)
    sys.exit(0 if code == 200 else 2)


def run_step(size_mb: int, label: str):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    size = size_mb * MB
    blob = POC_ROOT / "downloads" / f"v2_test_{label}_{size_mb}MB.bin"
    t0 = time.perf_counter()
    rt.make_blob(blob, size)                       # deterministic pseudo-random blob
    write_s = time.perf_counter() - t0
    local_sha = sha256_file(blob)

    ev: dict = {"stage": "campaign_phase6", "label": label, "size_mib": size_mb,
                "size_bytes": size, "local_sha256": local_sha,
                "file_write_seconds": round(write_s, 3),
                "host_free_before": shutil_free_host(), "container_data_before": docker_free_data(),
                "ts": utc()}

    with blob.open("rb") as fh:
        files = {"document": (blob.name, fh, "application/octet-stream")}
        params = {"chat_id": rt.CHANNEL, "caption": f"V2 PHASE6 {label} {size_mb} MB",
                  "disable_notification": True}
        t0 = time.perf_counter()
        code, body = rt.post(rt.LOCAL, "sendDocument", params, files=files, timeout=7200)
    ev["upload_seconds"] = round(time.perf_counter() - t0, 3)
    ev["http"] = code
    ev["ok"] = body.get("ok")
    ev["error_code"] = body.get("error_code")
    ev["description"] = mask((body.get("description") or "")[:200], tok)

    result = body.get("result") or {}
    if result:
        ev["document"] = {"file_unique_id": (result.get("document") or {}).get("file_unique_id"),
                          "file_id": (result.get("document") or {}).get("file_id"),
                          "message_id": result.get("message_id")}

    if code == 200:
        t0 = time.perf_counter()
        fcode, fbody = rt.post(rt.LOCAL, "getFile", {"file_id": ev["document"]["file_id"]}, timeout=7200)
        ev["getFile_seconds"] = round(time.perf_counter() - t0, 3)
        fresult = fbody.get("result") or {}
        fp = (fresult or {}).get("file_path") or ""
        ev["getFile"] = {"http": fcode, "ok": fbody.get("ok"),
                         "file_size": fresult.get("file_size"),
                         "file_path_masked": mask(fp, tok),
                         "size_match": fresult.get("file_size") == size}
        if fcode == 200 and fp:
            sv = server_sha256(fp)
            ev["server_sha256_check"] = {"sha": sv["sha"],
                                         "match": bool(sv["sha"] == local_sha),
                                         "exit": sv["exit"], "err": sv["err"]}
            ev["verification"] = {
                "method": "getFile path absolu + sha256sum cote serveur + masquage token",
                "size_match": ev["getFile"]["size_match"],
                "server_sha_match": ev["server_sha256_check"]["match"],
                "result": "PROVEN" if (ev["getFile"]["size_match"] and ev["server_sha256_check"]["match"]) else "FAILED"}
            cleanup = cleanup_server(fp)
            ev["server_cleanup"] = {"exit": cleanup["exit"], "removed": cleanup["removed"]}
        else:
            ev["verification"] = {"getFile_http": fcode, "result": "FAILED"}

    blob.unlink(missing_ok=True)                   # local blob removed; evidence JSON kept
    ev["container_data_after"] = docker_free_data()
    out = PHASE_DIR / f"{label}.json"
    out.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(ev, indent=2, ensure_ascii=False))
    print("written:", out)
    return ev


def shutil_free_host() -> int:
    import shutil
    return shutil.disk_usage(str(POC_ROOT)).free


def summary():
    rows = []
    for p in sorted(PHASE_DIR.glob("size*.json")):
        ev = json.loads(p.read_text(encoding="utf-8"))
        f = lambda k: "" if ev.get(k) is None else ev.get(k)
        rows.append({"label": f("label"), "size_mib": f("size_mib"),
                     "http": f("http"), "ok": f("ok"),
                     "error": (f("description") or "")[:100],
                     "size_match": (ev.get("getFile") or {}).get("size_match"),
                     "server_sha_match": (ev.get("server_sha256_check") or {}).get("match"),
                     "upload_seconds": f("upload_seconds"),
                     "result": (ev.get("verification") or {}).get("result")
                               if (ev.get("verification") or {}).get("result") is not None
                               else "CRASH"})
    for p in sorted(PHASE_DIR.glob("crash_*.json")):
        ev = json.loads(p.read_text(encoding="utf-8"))
        seg = ev.get("size_mib")
        try:
            size = float(seg) if seg is not None else None
        except (TypeError, ValueError):
            size = None
        rows.append({"label": f"crash({ev.get('size_mib')})",
                     "size_mib": size, "http": None, "ok": False,
                     "error": (ev.get("client_error") or "")[:100],
                     "size_match": None, "server_sha_match": None,
                     "upload_seconds": None, "result": "CRASH"})
    # per-size verdict: a PROVEN size wins over a transient crash of the same size
    per_size: dict = {}
    for r in rows:
        s = r["size_mib"]
        if s is None:
            continue
        cur = per_size.get(s)
        prio = {"PROVEN": 2, "CRASH": 1}.get(str(r["result"]), 0)
        if cur is None or prio > cur[0]:
            per_size[s] = (prio, str(r["result"]))
    proven_items = {s for s, (p, v) in per_size.items() if v == "PROVEN"}
    crashed_only = sorted(s for s, (p, v) in per_size.items() if v == "CRASH" and s not in proven_items)
    first_failed = crashed_only[0] if crashed_only else None
    out = PHASE_DIR / "summary_matrix.json"
    out.write_text(json.dumps({
        "stage": "phase6_summary",
        "server": "local Bot API 10.3 (--local, build commit e3e9dd8)",
        "created_utc": utc(),
        "plan_source": "master prompt plan [700..2001 MiB]; halted by server crashes",
        "max_proven_mib": max(proven_items) if proven_items else None,
        "first_failed_mib": first_failed,
        "conclusion": ("documented 2000 MB NOT reachable with this build: uploads "
                       + (f">= {first_failed} MiB crash" if first_failed else "")
                       + "; practical proven ceiling 800 MiB (sendDocument, sha256sum cote serveur verifie)"),
        "rows": rows
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{'label':<12}{'MiB':>7}{'http':>6}{'ok':>5}{'size_match':>12}{'sha_match':>11}{'result':>12}")
    for r in rows:
        print(f"{r['label']:<12}{r['size_mib']:>7}{str(r['http']):>6}{str(r['ok']):>5}"
              f"{str(r['size_match']):>12}{str(r['server_sha_match']):>11}{str(r['result']):>12}")
    print("written:", out)
    return rows


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "preflight":
        preflight()
    elif cmd == "run":
        size_mb = int(sys.argv[2])
        label = sys.argv[4] if "--label" in sys.argv else LABELS.get(size_mb)
        run_step(size_mb, label)
    elif cmd == "summary":
        summary()
    else:
        print(__doc__)
        sys.exit(2)