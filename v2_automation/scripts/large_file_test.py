#!/usr/bin/env python
"""Large-file ladder through the PRODUCTION publish path (V2TelegramClient.send_video).

Real, valid MP4s of growing size are built by looping the real downloaded episode with
`ffmpeg -stream_loop -c copy` (no re-encode), validated with ffprobe, uploaded, then the
test message is deleted from the channel.  Stops at the first failure.  When the connection
drops, the message is SEARCHED (never re-sent) before concluding.  Per size it records the
server state (restart count, "Idle timeout expired" lines in /data/server.log, free volume space).
One JSON per size + summary under output/evidence/large_file/.
Usage: large_file_test.py 1000 1500 1800
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent / "v1_poc" / "src"))
sys.path.insert(0, str(ROOT.parent / "source_audit" / "src"))

from v2_automation import app_config, evidence  # noqa: E402
from v2_automation.publisher import V2TelegramClient, connection_lost, local_bot_base_url  # noqa: E402
from v1_poc.media_tools import ffmpeg_bin, ffprobe_bin  # noqa: E402
from v1_poc.validator import sha256_file, validate_media_file  # noqa: E402

MIB = 1024 * 1024
WAIT_LATE_S = 1800


def build(src: Path, target_mib: int, out: Path) -> Path:
    """Loop the real episode and cut at the duration that gives ~target size (stream copy)."""
    dur = validate_media_file(src, ffprobe_bin()).duration_seconds
    seconds = dur * target_mib * MIB / src.stat().st_size
    loops = int(seconds // dur)
    subprocess.run([str(ffmpeg_bin()), "-y", "-loglevel", "error", "-stream_loop", str(loops),
                    "-i", str(src), "-t", f"{seconds:.2f}", "-c", "copy", "-movflags", "+faststart", str(out)],
                   check=True, timeout=3600)
    return out


def _docker(container: str, *args: str) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    return (r.stdout or "").strip()


def server_state(container: str) -> dict:
    inspect = _docker(container, "inspect", container, "--format",
                      "{{.State.StartedAt}}|{{.RestartCount}}|{{.State.Status}}")
    started, restarts, status = (inspect.split("|") + ["?", "?", "?"])[:3]
    idle = _docker(container, "exec", container, "sh", "-c",
                   "grep -c 'Idle timeout expired' /data/server.log 2>/dev/null || true")
    free = _docker(container, "exec", container, "sh", "-c", "df -B1M /data | tail -1 | awk '{print $4}'")
    return {"started_at": started, "restart_count": restarts, "status": status,
            "idle_timeout_lines": int(idle or 0), "volume_free_mib": int(free) if free.isdigit() else None}


def next_message_ids() -> range:
    """Candidate ids for a test message: just after the highest id this project ever recorded."""
    db = sqlite3.connect(str(app_config.DATA_DIR / "v2.sqlite3"))
    top = db.execute("SELECT COALESCE(MAX(message_id), 0) FROM publications").fetchone()[0]
    return range(top + 1, top + 12)


def main(sizes: list[int]) -> int:
    cfg = app_config.load_config()
    container = cfg.telegram.get("local_upload_container") or "v2-telegram-bot-api"
    src = max((evidence.evidence_dir("media", "downloads")).glob("*.mp4"), key=lambda p: p.stat().st_size)
    api = cfg.telegram["api_base_url"]
    out_dir = evidence.evidence_dir("large_file")
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    rows = []
    for mib in sizes:
        row = {"target_mib": mib, "result": "NOT_RUN", "server_before": server_state(container)}
        rows.append(row)
        f = work / f"large_{mib}.mp4"
        client, t1, caption = None, None, f"TEST large file {mib} MiB (supprimé automatiquement)"
        try:
            t0 = time.monotonic()
            build(src, mib, f)
            v = validate_media_file(f, ffprobe_bin())
            row.update(size_bytes=f.stat().st_size, size_mib=round(f.stat().st_size / MIB, 1),
                       ffprobe=v.verdict, duration_s=v.duration_seconds, build_s=round(time.monotonic() - t0, 1))
            if v.verdict != "VALID":
                row.update(result="INVALID_TEST_FILE", error=str(v.checks))
                break
            row["sha256"] = sha256_file(f)
            candidates = next_message_ids()
            client = V2TelegramClient(cfg.bot_token, cfg.channel_id, base_url=local_bot_base_url(api, cfg.bot_token),
                                      read_timeout=7200, write_timeout=7200)
            client.local_container = container
            t1 = time.monotonic()
            row["upload_started"] = time.strftime("%H:%M:%S")
            message_id = None
            try:
                msg = client.send_video(f, caption=caption)
                message_id = msg.message_id
                row.update(result="PASS", telegram_size=getattr(msg.video, "file_size", None))
                row["size_match"] = row["telegram_size"] == row["size_bytes"]
            except Exception as exc:
                row.update(failed_after_s=round(time.monotonic() - t1, 1), error=f"{type(exc).__name__}: {exc}"[:400],
                           connection_lost=connection_lost(exc))
                if connection_lost(exc):          # outcome unknown: SEARCH the message, never re-send
                    message_id = client.wait_for_message(candidates, caption, timeout_s=WAIT_LATE_S, poll_s=20)
                    row["found_late"] = message_id
                    row["result"] = "PASS_LATE" if message_id else "FAIL_UNKNOWN"
                    client.cleanup_remote(f)
                else:
                    row["result"] = "FAIL"
            row["upload_s"] = round(time.monotonic() - t1, 1)
            row["message_id"] = message_id
            if message_id:
                async def _del():
                    return await client._bot().delete_message(cfg.channel_id, message_id)
                row["deleted"] = bool(client._run(_del()))
        except Exception as exc:                       # never masked: type + message recorded
            row.update(result="FAIL", error=f"{type(exc).__name__}: {exc}"[:400])
        finally:
            if client is not None:
                client.close()
            f.unlink(missing_ok=True)
            row["server_after"] = server_state(container)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            evidence.write_json(out_dir / f"size_{mib}.json", row)
        if row["result"] != "PASS":
            break
    evidence.write_json(out_dir / "summary.json", {"rows": rows})
    return 0 if rows and all(r["result"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main([int(a) for a in sys.argv[1:]] or [1000, 1500]))
