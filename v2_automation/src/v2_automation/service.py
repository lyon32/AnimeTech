"""Shared admin/recovery service — the ONLY place that performs manual state
changes.  Used by the web panel (Phase 7) and the Telegram admin (Phase 8) so
both surfaces behave identically and provably.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import repo
from .timeutil import add_seconds, now_utc

RECOVERABLE = ("retry_wait", "failed", "structure_changed", "blocked")


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status").fetchall()}
    total_published = sum(n for s, n in counts.items() if s in ("published", "cleanup_pending", "cleaned"))
    rows = conn.execute(
        "SELECT anime_key, COUNT(*) AS queued FROM queue_items "
        "WHERE status='queued' GROUP BY anime_key ORDER BY anime_key").fetchall()
    cap = repo.load_capacity(conn)
    return {
        "ts": now_utc(),
        "episodes": {"total": sum(counts.values()), "by_status": counts,
                     "published_incl_pending": total_published},
        "queue": {"depth": sum(r["queued"] for r in rows), "by_anime": [dict(r) for r in rows]},
        "capacity": cap,
        "limits_mib": {
            "documented": _as_int(cap.get("documented_upload_limit_mib")),
            "tested": _as_int(cap.get("tested_upload_limit_mib")),
            "first_failed": _as_int(cap.get("first_failed_upload_mib")),
        },
    }


def episodes_query(conn: sqlite3.Connection, status: str | None, limit: int,
                   offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if status:
        rows = conn.execute(
            "SELECT id, anime_key, episode_number, language, label, status, retry_count, "
            "published_at, updated_at, last_error, file_size, video_message_id "
            "FROM episodes WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, anime_key, episode_number, language, label, status, retry_count, "
            "published_at, updated_at, last_error, file_size, video_message_id "
            "FROM episodes ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def requeue_episode(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any]:
    ep = repo.get(conn, episode_id)
    if ep is None:
        return {"ok": False, "message": "inconnu"}
    if ep.status not in RECOVERABLE:
        return {"ok": False, "message": f"statut {ep.status} non re-ergabilisable"}
    repo.transition(conn, episode_id, "queued")
    repo.set_retry_until(conn, episode_id, None, 0)
    conn.execute("UPDATE episodes SET next_retry_at=NULL WHERE id=?", (episode_id,))     # "Relancer" means NOW, not at the old backoff
    repo.enqueue(conn, ep.anime_key, episode_id)
    conn.commit()
    return {"ok": True, "episode_id": episode_id, "status": "queued",
            "message": f"épisode {episode_id} → queued"}


def fail_episode(conn: sqlite3.Connection, episode_id: int, reason: str = "forcé manuellement") -> dict[str, Any]:
    ep = repo.get(conn, episode_id)
    if ep is None:
        return {"ok": False, "message": "inconnu"}
    if ep.status != "retry_wait":
        return {"ok": False, "message": f"seul retry_wait est faillible (actuel {ep.status})"}
    repo.mark_failed(conn, episode_id, reason)
    conn.commit()
    return {"ok": True, "episode_id": episode_id, "status": "failed",
            "message": f"épisode {episode_id} → failed"}


def bulk_requeue(conn: sqlite3.Connection, *statuses: str) -> dict[str, Any]:
    """Manual recovery sweep: requeue every episode currently in one of the
    given recoverable statuses.  Returns per-status counts."""
    target = set(statuses) & set(RECOVERABLE)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM episodes WHERE status IN ({','.join('?' * len(target))})",
        tuple(sorted(target))).fetchall()] if target else []
    done, failed = 0, 0
    for eid in ids:
        ep = repo.get(conn, eid)
        try:
            repo.transition(conn, eid, "queued")
            repo.set_retry_until(conn, eid, None, 0)
            repo.enqueue(conn, ep.anime_key, eid)
            done += 1
        except Exception:
            failed += 1
    conn.commit()
    return {"target": sorted(target), "requeued": done, "failed": failed}


def summary_text(conn: sqlite3.Connection) -> str:
    o = overview(conn)
    by = o["episodes"]["by_status"]
    tasty = {
        "published": by.get("published", 0), "cleanup_pending": by.get("cleanup_pending", 0),
        "cleaned": by.get("cleaned", 0), "retry_wait": by.get("retry_wait", 0),
        "failed": by.get("failed", 0), "structure_changed": by.get("structure_changed", 0),
        "downloading": by.get("downloading", 0), "downloaded": by.get("downloaded", 0),
    }
    vers = o["capacity"].get("http_server_version") or "?"
    return (
        "📋 V2 — aperçu\n"
        f"épisodes : {o['episodes']['total']} (publiés {o['episodes']['published_incl_pending']})\n"
        f"{_fmt_status(tasty)}\n"
        f"file : {o['queue']['depth']} (têtes {len(o['queue']['by_anime'])} anime)\n"
        f"limite testée : {o['limits_mib']['tested'] or '?'} MiB"
        f" / 1ère taille en crash : {o['limits_mib']['first_failed'] or '?'} MiB\n"
        f"serveur : {vers}\n"
    )


def _fmt_status(by: dict) -> str:
    parts = [f"{k.replace('_', ' ')}={v}" for k, v in by.items() if v]
    return " · ".join(parts) if parts else "(aucun épisode)"


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── operational control (pause / animes) ───────────────────────────────────────

CONTROL_PAUSED = "paused"


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (CONTROL_PAUSED,)).fetchone()
    return bool(row and row["cvalue"] == "1")


def set_paused(conn: sqlite3.Connection, paused: bool) -> dict[str, Any]:
    val = "1" if paused else "0"
    conn.execute(
        "INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(ckey) DO UPDATE SET cvalue=excluded.cvalue, "
        "updated_at=excluded.updated_at",
        (CONTROL_PAUSED, val, now_utc()))
    conn.commit()
    return {"paused": paused, "message": "file " + ("PAUSE (plus aucun téléchargement)" if paused
                                                    else "reprend son cours")}


def anime_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT a.anime_key, a.title, a.enabled, a.updated_at, a.source_url, a.language,
               a.last_checked_at, a.last_successful_check_at, a.last_check_error, a.force_check,
               COALESCE((SELECT COUNT(*) FROM queue_items q WHERE q.anime_key=a.anime_key
                         AND q.status='queued'), 0) AS queued,
               COALESCE((SELECT COUNT(*) FROM episodes e WHERE e.anime_key=a.anime_key
                         AND e.status IN ('published','cleanup_pending','cleaned')), 0) AS published,
               EXISTS (SELECT 1 FROM control c WHERE c.ckey = 'auto_added:' || a.anime_key) AS auto_added
        FROM animes a ORDER BY a.anime_key""").fetchall()
    return [dict(r) for r in rows]


def upsert_anime(conn: sqlite3.Connection, anime_key: str, *,
                 title: str | None = None, enabled: bool | None = None) -> dict[str, Any]:
    key = anime_key.strip()
    if not key:
        return {"ok": False, "message": "anime_key vide"}
    cur = conn.execute("SELECT title, enabled FROM animes WHERE anime_key=?", (key,)).fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO animes (anime_key, title, enabled, updated_at) VALUES (?, ?, ?, ?)",
            (key, title or "", 1 if enabled is None else int(enabled), now_utc()))
    else:
        new_title = title if title is not None else cur["title"]
        new_enabled = int(enabled) if enabled is not None else cur["enabled"]
        conn.execute("UPDATE animes SET title=?, enabled=?, updated_at=? WHERE anime_key=?",
                     (new_title, new_enabled, now_utc(), key))
    conn.commit()
    return {"ok": True, "anime_key": key,
            "enabled": (enabled if enabled is not None
                        else bool(conn.execute("SELECT enabled FROM animes WHERE anime_key=?",
                                               (key,)).fetchone()["enabled"]))}


def set_anime_enabled(conn: sqlite3.Connection, anime_key: str, enabled: bool) -> dict[str, Any]:
    cur = conn.execute("SELECT 1 FROM animes WHERE anime_key=?", (anime_key,)).fetchone()
    if cur is None:
        return upsert_anime(conn, anime_key, enabled=enabled)
    conn.execute("UPDATE animes SET enabled=?, updated_at=? WHERE anime_key=?",
                 (int(enabled), now_utc(), anime_key))
    conn.commit()
    return {"ok": True, "anime_key": anime_key, "enabled": enabled,
            "message": f"{anime_key} → {'ACTIF' if enabled else 'PAUSE'}"}


def cancel_episode(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any]:
    """Abandonner un épisode : retiré de la file + marqué failed (jamais publié
    automatiquement, mais visible dans le panneau pour décision manuelle)."""
    ep = repo.get(conn, episode_id)
    if ep is None:
        return {"ok": False, "message": "inconnu"}
    if ep.status not in ("queued", "retry_wait", "failed", "structure_changed", "blocked"):
        return {"ok": False, "message": f"statut {ep.status} non annulable"}
    conn.execute("DELETE FROM queue_items WHERE episode_id=?", (episode_id,))
    conn.execute("UPDATE episodes SET retry_until_at=NULL, next_retry_at=NULL WHERE id=?",
                 (episode_id,))
    repo.mark_failed(conn, episode_id, "annulé manuellement")
    conn.commit()
    return {"ok": True, "episode_id": episode_id, "status": "failed",
            "message": f"épisode {episode_id} annulé → failed (hors file)"}

# ── watched animes: source URL, checks (automatic mode) ────────────────────────

def add_anime_from_url(conn: sqlite3.Connection, cfg, source_url: str, *, fetch=None,
                       title: str | None = None, language: str | None = None) -> dict[str, Any]:
    """Validate the URL, identify the anime (one page fetch), save it and activate it.  The first
    check (baseline) happens at the next scheduler tick, or immediately with a force-check.  No job
    is created here: a job only exists for an episode actually detected."""
    from . import discovery
    try:
        info = discovery.identify_anime(cfg, source_url, fetch or discovery.default_fetch(cfg))
    except (ValueError, discovery.DiscoveryError) as exc:
        return {"ok": False, "message": str(exc)}
    key = info["anime_key"]
    existing = conn.execute("SELECT anime_key FROM animes WHERE anime_key=?", (key,)).fetchone()
    now = now_utc()
    if existing is None:
        conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url, language, created_at, updated_at) "
                     "VALUES (?, ?, 1, ?, ?, ?, ?)", (key, title or info["title"], info["source_url"], language, now, now))
    else:
        conn.execute("UPDATE animes SET title=COALESCE(NULLIF(?, ''), title), source_url=?, "
                     "language=COALESCE(?, language), enabled=1, updated_at=? WHERE anime_key=?",
                     (title or "", info["source_url"], language, now, key))
    conn.commit()
    return {"ok": True, "anime_key": key, "title": title or info["title"], "source_url": info["source_url"],
            "created": existing is None, "message": f"{key} surveillé"}


def update_anime(conn: sqlite3.Connection, anime_key: str, *, title: str | None = None,
                 source_url: str | None = None, language: str | None = None) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM animes WHERE anime_key=?", (anime_key,)).fetchone() is None:
        return {"ok": False, "message": f"anime inconnu : {anime_key}"}
    conn.execute("UPDATE animes SET title=COALESCE(?, title), source_url=COALESCE(?, source_url), "
                 "language=COALESCE(?, language), updated_at=? WHERE anime_key=?",
                 (title, source_url, language, now_utc(), anime_key))
    conn.commit()
    return {"ok": True, "anime_key": anime_key, "message": f"{anime_key} modifié"}


def request_force_check(conn: sqlite3.Connection, anime_key: str) -> dict[str, Any]:
    """Ask the scheduler for an immediate check (picked up at its next tick, never concurrently)."""
    cur = conn.execute("UPDATE animes SET force_check=1, updated_at=? WHERE anime_key=? AND source_url IS NOT NULL "
                       "AND source_url <> ''", (now_utc(), anime_key))
    conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "message": f"{anime_key}: inconnu ou sans URL source"}
    return {"ok": True, "anime_key": anime_key, "message": f"contrôle immédiat demandé pour {anime_key}"}


# ── jobs (progress) and host state, shared by the web panel and the Telegram admin ──

# state -> pipeline step out of 7 ([1/7] source … [7/7] telegram)
_PROGRESS_STEP = {"discovered": 0, "identified": 0, "queued": 0, "retry_wait": 0, "downloading": 3,
                  "downloaded": 4, "validating": 4, "validated": 5, "publishing_thumbnail": 6,
                  "thumbnail_published": 6, "publishing_video": 7, "published": 7, "cleanup_pending": 7,
                  "cleanup_blocked": 7, "cleaned": 7}
ACTIVE_STATES = ("queued", "retry_wait", "downloading", "downloaded", "validating", "validated",
                 "publishing_thumbnail", "thumbnail_published", "publishing_video")


def job_progress(status: str) -> dict[str, Any]:
    step = _PROGRESS_STEP.get(status, 0)
    return {"step": step, "of": 7, "percent": round(step * 100 / 7)}


def jobs(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """Active jobs (queued, waiting, downloading, publishing) with their anime, episode and progress."""
    marks = ",".join("?" * len(ACTIVE_STATES))
    rows = conn.execute(f"""
        SELECT e.id, e.anime_key, COALESCE(a.title, '') AS anime_title, e.episode_number, e.language, e.status,
               e.retry_count, e.next_retry_at, e.last_error, e.file_size, q.position
        FROM episodes e LEFT JOIN animes a ON a.anime_key = e.anime_key
        LEFT JOIN queue_items q ON q.episode_id = e.id
        WHERE e.status IN ({marks})
        ORDER BY e.anime_key, COALESCE(q.position, 0), e.id LIMIT ?""", (*ACTIVE_STATES, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["progress"] = job_progress(d["status"])
        out.append(d)
    return out


def system_status(conn: sqlite3.Connection, cfg) -> dict[str, Any]:
    """CPU, RAM, disk, network counters, worker lease and polling settings (no secret)."""
    from . import app_config, discovery
    snap: dict[str, Any] = {"ts": now_utc()}
    try:
        import psutil
        p = app_config.DATA_DIR if app_config.DATA_DIR.exists() else app_config.DATA_DIR.parent
        du = psutil.disk_usage(str(p))
        net = psutil.net_io_counters()
        snap.update(cpu_percent=psutil.cpu_percent(interval=None), ram_percent=psutil.virtual_memory().percent,
                    disk_free_bytes=du.free, disk_total_bytes=du.total, disk_percent=du.percent,
                    net_bytes_sent=net.bytes_sent, net_bytes_recv=net.bytes_recv)
    except Exception as exc:  # psutil missing/unreadable: the panel still works
        snap["host_error"] = f"{type(exc).__name__}: {exc}"
    lease = conn.execute("SELECT owner, last_heartbeat, expires_at FROM leases WHERE name='worker'").fetchone()
    snap["worker"] = dict(lease) if lease else None
    snap["poll_interval_seconds"] = discovery.poll_interval_s(cfg)
    snap["paused"] = is_paused(conn)
    snap["jobs_active"] = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE status IN ('downloading','downloaded','validating','validated',"
        "'publishing_thumbnail','thumbnail_published','publishing_video')").fetchone()[0]
    snap["queued"] = conn.execute("SELECT COUNT(*) FROM queue_items WHERE status='queued'").fetchone()[0]
    return snap


# ── dashboard data for the Telegram admin (pure data, no formatting) ────────────

RUNNING_STATES = ("downloading", "downloaded", "validating", "validated",
                  "publishing_thumbnail", "thumbnail_published", "publishing_video")
PUBLISHED_STATES = ("published", "cleanup_pending", "cleaned")
ATTENTION_STATES = ("failed", "structure_changed", "blocked", "cleanup_blocked")


def waiting_reason(job: dict[str, Any]) -> str:
    """Why a queued / retrying job is not running, in plain words."""
    err = job.get("last_error") or ""
    if err.startswith("NOT_AVAILABLE_YET"):
        return "source pas encore prête"
    if err.startswith("SOURCE_VIDEO_PROCESSING"):
        return "vidéo en cours de préparation par la source"
    if job["status"] == "retry_wait":
        return "nouvelle tentative prévue" + (" après une erreur" if err else "")
    return "en file"


def local_midnight_utc(now: str | None = None) -> str:
    """Start of the current LOCAL day, as a UTC timestamp comparable with the database columns."""
    from datetime import datetime, timezone
    ref = datetime.fromisoformat((now or now_utc()).replace("Z", "+00:00")).astimezone()
    return ref.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_counts(conn: sqlite3.Connection, now: str | None = None) -> dict[str, Any]:
    """What the watcher did since local midnight: episodes detected (queued as jobs) and published."""
    since = local_midnight_utc(now)
    marks = ",".join("?" * len(PUBLISHED_STATES))
    detected = conn.execute("SELECT COUNT(*) FROM episodes WHERE created_at >= ? AND status <> 'discovered'",
                            (since,)).fetchone()[0]
    published = conn.execute(f"SELECT COUNT(*) FROM episodes WHERE status IN ({marks}) AND published_at >= ?",
                             (*PUBLISHED_STATES, since)).fetchone()[0]
    return {"since": since, "detected": detected, "published": published}


def dashboard(conn: sqlite3.Connection, cfg, *, now: str | None = None) -> dict[str, Any]:
    """Everything the admin home needs.  Baseline episodes (`discovered`) are known, not jobs: they are
    counted apart and never shown as episodes to process."""
    from . import alerts, discovery
    now = now or now_utc()
    sysinfo = system_status(conn, cfg)
    all_jobs = jobs(conn, 100)
    running = [j for j in all_jobs if j["status"] in RUNNING_STATES]
    waiting = [dict(j, reason=waiting_reason(j)) for j in all_jobs if j["status"] in ("queued", "retry_wait")]
    marks = ",".join("?" * len(PUBLISHED_STATES))
    recent = [dict(r) for r in conn.execute(f"""
        SELECT e.id, e.anime_key, COALESCE(a.title, '') AS anime_title, e.episode_number, e.published_at,
               e.file_size, e.cleanup_at
        FROM episodes e LEFT JOIN animes a ON a.anime_key = e.anime_key
        WHERE e.status IN ({marks}) AND e.published_at IS NOT NULL
        ORDER BY e.published_at DESC LIMIT 3""", PUBLISHED_STATES).fetchall()]
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status").fetchall()}
    attention = [dict(r) for r in conn.execute(f"""
        SELECT e.id, e.anime_key, COALESCE(a.title, '') AS anime_title, e.episode_number, e.status, e.last_error
        FROM episodes e LEFT JOIN animes a ON a.anime_key = e.anime_key
        WHERE e.status IN ({",".join("?" * len(ATTENTION_STATES))}) ORDER BY e.updated_at DESC LIMIT 20""",
                                        ATTENTION_STATES).fetchall()]
    animes = anime_list(conn)
    interval = discovery.poll_interval_s(cfg)
    checked = [a for a in animes if a["enabled"] and a.get("source_url")]
    # the cycle belongs to the watcher: next check = last global cycle + interval (an anime never checked, or a
    # forced check, is visited at once, so "now")
    cyc = conn.execute("SELECT cvalue FROM control WHERE ckey='watcher:last_cycle_at'").fetchone()
    if cyc and cyc["cvalue"]:
        next_check = add_seconds(cyc["cvalue"], interval)
    else:                                              # no global cycle recorded yet: per-anime clocks
        next_check = None
        for a in checked:
            due = add_seconds(a["last_checked_at"], interval) if a["last_checked_at"] else now
            next_check = due if next_check is None or due < next_check else next_check
    if any((not a["last_checked_at"]) or a.get("force_check") for a in checked):
        next_check = now
    last_ok = max((a["last_successful_check_at"] for a in checked if a["last_successful_check_at"]), default=None)
    worker = sysinfo.get("worker")
    alive = bool(worker and worker.get("expires_at") and worker["expires_at"] > now)
    return {
        "now": now, "worker_alive": alive, "paused": sysinfo.get("paused", False),
        "running": running, "waiting": waiting, "recent": recent, "attention": attention,
        "animes": animes, "animes_watched": len(checked),
        "published_total": sum(counts.get(s, 0) for s in PUBLISHED_STATES),
        "baseline_known": counts.get("discovered", 0),
        "alerts_open": alerts.open_count(conn),
        "next_check": next_check, "last_check_ok": last_ok, "today": today_counts(conn, now),
        "last_check_error": next((a["last_check_error"] for a in checked if a["last_check_error"]), None),
        "poll_interval_seconds": interval, "system": sysinfo,
    }


# ── worker control: clean stop request, start through the scheduled task ─────────────

CONTROL_WORKER_STOP = "worker_stop"
WORKER_TASK = "V2AutomationWorker"


def worker_stop_requested(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (CONTROL_WORKER_STOP,)).fetchone()
    return bool(row and row["cvalue"] == "1")


def _set_worker_stop(conn: sqlite3.Connection, on: bool) -> None:
    conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(ckey) DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at",
                 (CONTROL_WORKER_STOP, "1" if on else "0", now_utc()))
    conn.commit()


def clear_worker_stop(conn: sqlite3.Connection) -> None:
    _set_worker_stop(conn, False)


def worker_state(conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, Any]:
    """alive = a worker holds a fresh lease; stopping = it was asked to stop and is finishing its jobs."""
    now = now or now_utc()
    lease = conn.execute("SELECT owner, expires_at FROM leases WHERE name='worker'").fetchone()
    alive = bool(lease and lease["expires_at"] and lease["expires_at"] > now)
    stopping = alive and worker_stop_requested(conn)
    active = conn.execute("SELECT COUNT(*) FROM episodes WHERE status IN ('downloading','downloaded','validating',"
                          "'validated','publishing_thumbnail','thumbnail_published','publishing_video')").fetchone()[0]
    return {"alive": alive, "stopping": stopping, "active_jobs": active}


def request_worker_stop(conn: sqlite3.Connection) -> dict[str, Any]:
    st = worker_state(conn)
    if not st["alive"]:
        clear_worker_stop(conn)
        return {"ok": False, "message": "le worker est déjà arrêté"}
    if st["stopping"]:
        return {"ok": True, "message": "arrêt déjà demandé : le worker termine ses jobs en cours"}
    _set_worker_stop(conn, True)
    n = st["active_jobs"]
    return {"ok": True, "message": ("arrêt demandé : le worker termine " + (f"{n} job{'s' if n > 1 else ''} en cours puis s'éteint"
                                    if n else "sa boucle puis s'éteint"))}


def start_worker(conn: sqlite3.Connection, *, runner=None, platform: str | None = None) -> dict[str, Any]:
    """Start the worker through its Windows scheduled task (installed once by scripts/install_tasks.ps1)."""
    import subprocess
    import sys
    st = worker_state(conn)
    if st["alive"]:
        return {"ok": False, "message": "arrêt en cours : attendez qu'il soit terminé" if st["stopping"]
                else "le worker est déjà actif"}
    if (platform or sys.platform) != "win32":
        return {"ok": False, "message": "le démarrage depuis le panneau n'est prévu que sous Windows"}
    clear_worker_stop(conn)
    try:
        r = (runner or subprocess.run)(["schtasks", "/Run", "/TN", WORKER_TASK], capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return {"ok": False, "message": f"démarrage impossible : {type(exc).__name__}"}
    if r.returncode != 0:
        return {"ok": False, "message": "tâche planifiée introuvable : exécutez une fois scripts/install_tasks.ps1"}
    return {"ok": True, "message": "démarrage demandé : le worker sera actif dans quelques secondes"}
