#!/usr/bin/env python
"""Closure campaign — Local Bot API uploads via LOCAL PATH (file:// in --local mode).

Root-cause hypothesis (Phase 6 + PTB issue #4339 + hermes-agent findings): the
multipart path buffers the whole file client-side (InputFile.read()) and re-buffers
server-side, crashing the worker around ~900 MiB.  In --local mode the server reads
a file:// path directly from its own disk — no multipart at all.  This campaign
re-measures 900 -> 2000+ MiB through that mechanism with the SAME build (e3e9dd8).

Each step:
  1. deterministic blob on the host (reuses the proven POC harness make_blob)
  2. docker cp into <container>:/data/campaign/<label>.bin  (server's own disk)
  3. sendDocument with document=file:///data/campaign/<label>.bin (no file field)
  4. getFile -> absolute path -> sha256sum inside the container (primary method)
  5. verdict PROVEN / CRASH / FAILED; container bounced on worker crash + getMe wait

Evidence ->  v2_automation/output/evidence/clos_location_campaign/{label}.json
Summary   ->  .../clos_location_campaign/summary_matrix_localpath.json
No secret is ever written (token/channel masked).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

POC_ROOT = Path(__file__).resolve().parents[2] / "v2_local_bot_api_poc"
sys.path.insert(0, str(POC_ROOT / "scripts"))
import run_upload_tests as rt  # noqa: E402  (proven harness, reused only)

V2_ROOT = Path(__file__).resolve().parents[2] / "v2_automation"
OUT = V2_ROOT / "output" / "evidence" / "closure_localpath_campaign"
OUT.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = "/data/campaign"
PLAN = [900, 1024, 1500, 1900, 2000, 2001]
MB = 1024 * 1024


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mask(text: str | None) -> str | None:
    if text is None:
        return None
    return text.replace(rt.TOKEN, "<TOKEN_MASKED>") if rt.TOKEN else text


def docker_free_data() -> dict:
    r = subprocess.run(["docker", "exec", rt.CONTAINER, "df", "-B1", "/data"],
                       capture_output=True, text=True)
    free = None
    if r.returncode == 0 and r.stdout:
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) > 3:
                free = int(parts[3])
    return {"rc": r.returncode, "free_bytes": free}


def container_health() -> dict:
    r = subprocess.run(["docker", "inspect", rt.CONTAINER, "--format", "{{.State.Status}}"],
                       capture_output=True, text=True)
    return {"rc": r.returncode, "status": r.stdout.strip()}


def restart_and_wait(attempts: int = 30, delay: float = 5.0) -> bool:
    subprocess.run(["docker", "restart", rt.CONTAINER], capture_output=True, text=True)
    for _ in range(attempts):
        try:
            code, body = rt.post(rt.LOCAL, "getMe", {}, timeout=15)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def server_sha256(path_in_container: str) -> dict:
    r = subprocess.run(["docker", "exec", rt.CONTAINER, "sha256sum", path_in_container],
                       capture_output=True, text=True)
    return {"exit": r.returncode,
            "sha": r.stdout.split()[0] if r.stdout and r.returncode == 0 else ""}


def run_step(size_mib: int, label: str) -> dict:
    ev: dict = {"stage": "closure_localpath_campaign", "mechanism": "file:// local path",
                "label": label, "size_mib": size_mib, "size_bytes": size_mib * MB,
                "server": "local Bot API 10.3 (--local, build e3e9dd8)",
                "ts": utc(), "container_before": container_health(),
                "data_free_before": docker_free_data()}

    blob = POC_ROOT / "downloads" / f"v2_lp_{label}_{size_mib}MB.bin"
    t0 = time.perf_counter()
    sha = rt.make_blob(blob, size_mib * MB)
    ev["file_write_seconds"] = round(time.perf_counter() - t0, 3)
    ev["local_sha256"] = sha

    t0 = time.perf_counter()
    subprocess.run(["docker", "exec", rt.CONTAINER, "mkdir", "-p", UPLOAD_DIR],
                   capture_output=True, text=True)
    cp = subprocess.run(["docker", "cp", str(blob), f"{rt.CONTAINER}:{UPLOAD_DIR}/{label}.bin"],
                        capture_output=True, text=True)
    ev["docker_cp"] = {"rc": cp.returncode, "seconds": round(time.perf_counter() - t0, 3)}
    if cp.returncode != 0:
        ev["result"] = "FAILED"
        return ev

    uri = f"{UPLOAD_DIR}/{label}.bin"          # server-side absolute path (no token)
    params = {"chat_id": rt.CHANNEL,
              "caption": f"V2 CLOSURE {label} {size_mib} MB (file:// local path)",
              "disable_notification": True,
              "document": f"file://{uri}"}
    t0 = time.perf_counter()
    code, body = None, {}
    try:
        code, body = rt.post(rt.LOCAL, "sendDocument", params, timeout=max(7200, size_mib * 10))
        ev["upload_seconds"] = round(time.perf_counter() - t0, 3)
        ev["http"] = code
        ev["ok"] = body.get("ok")
        ev["error_code"] = body.get("error_code")
        ev["description"] = mask((body.get("description") or "")[:200])
    except Exception as exc:
        ev["upload_seconds"] = round(time.perf_counter() - t0, 3)
        ev["http"] = None
        ev["ok"] = False
        ev["client_error"] = f"{type(exc).__name__}: {mask(str(exc))[:200]}"

    ev["container_after"] = container_health()

    result = body.get("result") or {}
    if ev.get("http") == 200 and result:
        ev["document"] = {"file_unique_id": (result.get("document") or {}).get("file_unique_id"),
                          "file_id": (result.get("document") or {}).get("file_id"),
                          "message_id": result.get("message_id")}
        fcode, fbody = rt.post(rt.LOCAL, "getFile", {"file_id": ev["document"]["file_id"]}, timeout=1800)
        fresult = fbody.get("result") or {}
        fp = (fresult or {}).get("file_path") or ""
        ev["getFile"] = {"http": fcode, "ok": fbody.get("ok"),
                         "file_size": fresult.get("file_size"),
                         "expected_size": size_mib * MB,
                         "size_match": fresult.get("file_size") == size_mib * MB,
                         "server_path": mask(fp)
                         if (rt.TOKEN and fp) else fp}
        sv = None
        if fcode == 200 and fp:
            sv = server_sha256(fp)
            ev["server_sha256_check"] = {"sha": sv["sha"], "exit": sv["exit"],
                                         "match": bool(sv["sha"] == sha)}
            subprocess.run(["docker", "exec", rt.CONTAINER, "rm", "-f", fp],
                           capture_output=True, text=True)
        good = ev["getFile"]["size_match"] and bool(sv and sv["sha"] == sha)
        ev["verification"] = {"method": "getFile path absolu + sha256sum cote serveur",
                              "result": "PROVEN" if good else "FAILED"}
        ev["result"] = "PROVEN" if good else "FAILED"

    # crash path -> bounce the worker so the next step starts healthy
    crashed = ev.get("http") is None or (ev.get("ok") is False and ev.get("error_code") in (None, 502, 500))
    if crashed:
        ev["result"] = "CRASH"
        revived = restart_and_wait()
        ev["worker_recovery"] = {"restart_happened": True, "getme_ok_after": revived,
                                 "container_after_recovery": container_health()}
    if not crashed and ev.get("result") is None:
        ev["result"] = "FAILED"

    blob.unlink(missing_ok=True)                       # host blob removed; evidence kept
    subprocess.run(["docker", "exec", rt.CONTAINER, "rm", "-f", f"{UPLOAD_DIR}/{label}.bin"],
                   capture_output=True, text=True)     # container copy removed
    ev["data_free_after"] = docker_free_data()
    out = OUT / f"{label}.json"
    out.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(ev, indent=2, ensure_ascii=False), flush=True)
    print("written:", out, flush=True)
    return ev


def run_all() -> int:
    tok_ok = len(rt.TOKEN) > 8 and bool(rt.CHANNEL)
    print("token_present:", bool(tok_ok), flush=True)
    code, body = rt.post(rt.LOCAL, "getMe", {}, timeout=20)
    print("getMe:", code, body.get("ok"), flush=True)
    if code != 200 or not tok_ok:
        print("ABORT: server/token not ready", flush=True)
        return 2
    history: list[dict] = []
    for size in PLAN:
        label = f"lp{size}"
        ev = run_step(size, label)
        history.append({"size_mib": size, "result": ev.get("result")})
        if ev.get("result") == "CRASH":
            print(f"=> CRASH at {size} MiB via file:// — campagne stoppee comme en Phase 6", flush=True)
            summary(history)
            return 1
    summary(history)
    return 0


def summary(history: list[dict] | None = None) -> dict:
    rows: list[dict] = []
    for p in sorted(OUT.glob("lp*.json")):
        ev = json.loads(p.read_text(encoding="utf-8"))
        rows.append({"label": ev.get("label"), "size_mib": ev.get("size_mib"),
                     "http": ev.get("http"), "ok": ev.get("ok"),
                     "error": (ev.get("description") or ev.get("client_error") or "")[:100],
                     "size_match": (ev.get("getFile") or {}).get("size_match"),
                     "server_sha_match": (ev.get("server_sha256_check") or {}).get("match"),
                     "upload_seconds": ev.get("upload_seconds"),
                     "result": ev.get("result")})
    proven = [r["size_mib"] for r in rows if r["result"] == "PROVEN"]
    crashed = [r["size_mib"] for r in rows if r["result"] == "CRASH"]
    out = OUT / "summary_matrix_localpath.json"
    out.write_text(json.dumps({
        "stage": "closure_localpath_summary",
        "mechanism": "file:// local path (no multipart)",
        "server": "local Bot API 10.3 (--local, build e3e9dd8)",
        "created_utc": utc(),
        "max_proven_mib": max(proven) if proven else None,
        "first_crash_mib": crashed[0] if crashed else None,
        "conclusion": (
            "2000 MiB REACHABLE via file:// local path upload" if 2000 in proven
            else (f"campagne file:// stppee au crash {crashed[0]} MiB" if crashed
                  else "aucun crash ni succes au dela des tailles testees")),
        "rows": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{'label':<8}{'MiB':>6}{'http':>6}{'ok':>5}{'size_match':>11}{'sha_match':>10}{'result':>10}")
    for r in rows:
        print(f"{str(r['label']):<8}{r['size_mib']:>6}{str(r['http']):>6}{str(r['ok']):>5}"
              f"{str(r['size_match']):>11}{str(r['server_sha_match']):>10}{str(r['result']):>10}")
    print("written:", out)
    return {"rows": rows, "max_proven_mib": max(proven) if proven else None}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run-all"
    if cmd == "run-all":
        raise SystemExit(run_all())
    elif cmd == "run":
        size = int(sys.argv[2])
        run_step(size, f"lp{size}")
    elif cmd == "summary":
        summary()
    else:
        print(__doc__)
        raise SystemExit(2)