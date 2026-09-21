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
            "published_at, updated_at, last_error, file_size, video_message_id, media_ref, origin "
            "FROM episodes WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, anime_key, episode_number, language, label, status, retry_count, "
            "published_at, updated_at, last_error, file_size, video_message_id, media_ref, origin "
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
        from pathlib import Path
        raw = (getattr(cfg, "downloads", None) or {}).get("dir")      # le disque qui reçoit les vidéos
        p = Path(raw) if raw else app_config.DATA_DIR
        while not p.exists() and p != p.parent:
            p = p.parent
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
        ORDER BY e.published_at DESC LIMIT 8""", PUBLISHED_STATES).fetchall()]
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


# ── V2 user side: requests, users, deliveries, Telegram — shared by the web panel and the Telegram admin ──────────

def requests_overview(conn: sqlite3.Connection, *, state: str | None = None, user_id: int | None = None,
                      limit: int = 100) -> list[dict[str, Any]]:
    """Requests with their user, anime, episode, version, state, progress, creation/expiry and error."""
    where, args = [], []
    if state:
        where.append("r.state=?")
        args.append(state.upper())
    if user_id is not None:
        where.append("r.user_id=?")
        args.append(user_id)
    rows = conn.execute(f"""
        SELECT r.id, r.user_id, u.username, r.kind, r.anime_key, r.title, r.season, r.episode_number, r.version, r.state,
               r.created_at, r.updated_at, r.expires_at, r.completed_at, r.error_code, r.last_error,
               (SELECT COUNT(*) FROM request_items i WHERE i.request_id=r.id) AS items_total,
               (SELECT COUNT(*) FROM request_items i WHERE i.request_id=r.id AND i.state='COMPLETED') AS items_done
        FROM requests r LEFT JOIN users u ON u.telegram_id=r.user_id
        {('WHERE ' + ' AND '.join(where)) if where else ''} ORDER BY r.id DESC LIMIT ?""", (*args, max(1, min(limit, 300)))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["progress"] = {"done": d["items_done"], "total": d["items_total"],
                         "percent": round(100 * d["items_done"] / d["items_total"]) if d["items_total"] else 0}
        out.append(d)
    return out


def request_detail(conn: sqlite3.Connection, request_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if r is None:
        return None
    items = [dict(i) for i in conn.execute(
        "SELECT i.*, e.status AS media_status, e.media_key AS media_key FROM request_items i "
        "LEFT JOIN episodes e ON e.id=i.episode_id WHERE i.request_id=? ORDER BY i.episode_number", (request_id,)).fetchall()]
    deliveries = [dict(d) for d in conn.execute("SELECT * FROM deliveries WHERE request_id=? ORDER BY id", (request_id,)).fetchall()]
    return {"request": dict(r), "items": items, "deliveries": deliveries}


def cancel_request(conn: sqlite3.Connection, request_id: int, *, confirm: bool = False) -> dict[str, Any]:
    """Cancel a user's request (its job stays if anyone else — or the channel — still needs the media)."""
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "message": "confirmation requise"}
    from .requests_mgr import RequestManager
    res = RequestManager(conn, None).cancel(request_id)
    return {**res, "request_id": request_id, "message": "demande annulée" if res.get("ok") else res.get("message")}


def users_overview(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT u.telegram_id, u.username, u.first_seen_at, u.last_seen_at, u.access_status, u.access_checked_at, u.blocked,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.telegram_id) AS requests_total,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.telegram_id
                  AND r.state NOT IN ('COMPLETED','CANCELLED','EXPIRED','FAILED')) AS requests_active,
               (SELECT COUNT(*) FROM deliveries d WHERE d.user_id=u.telegram_id AND d.status='sent') AS deliveries_sent
        FROM users u ORDER BY u.last_seen_at DESC LIMIT ?""", (max(1, min(limit, 500)),)).fetchall()
    return [dict(r) for r in rows]


def user_detail(conn: sqlite3.Connection, telegram_id: int) -> dict[str, Any] | None:
    u = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if u is None:
        return None
    return {"user": dict(u), "history": requests_overview(conn, user_id=telegram_id, limit=50)}


def deliveries_recent(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT d.id, d.request_id, d.user_id, d.media_id, e.anime_key, e.episode_number, e.language, d.status, d.method, "
        "d.telegram_message_id, d.attempt_count, d.last_error, d.created_at, d.completed_at FROM deliveries d "
        "LEFT JOIN episodes e ON e.id=d.media_id ORDER BY d.id DESC LIMIT ?", (max(1, min(limit, 300)),)).fetchall()]


def bot_activity(conn: sqlite3.Connection, limit: int = 100) -> dict[str, Any]:
    """What the user bot is doing right now: each user's conversation (step, last search, chosen anime) and request count."""
    import json
    rows = conn.execute("""
        SELECT u.telegram_id, u.username, u.access_status, u.last_seen_at, c.step, c.data, c.updated_at,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.telegram_id) AS requests_total,
               (SELECT COUNT(*) FROM requests r WHERE r.user_id=u.telegram_id
                  AND r.state NOT IN ('COMPLETED','CANCELLED','EXPIRED','FAILED')) AS requests_active
        FROM users u LEFT JOIN conversations c ON c.user_id=u.telegram_id
        ORDER BY COALESCE(c.updated_at, u.last_seen_at) DESC LIMIT ?""", (max(1, min(limit, 300)),)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            data = json.loads(d.pop("data") or "{}")
        except ValueError:
            data = {}
        d["last_search"] = (data.get("query") or {}).get("raw")
        d["chosen"] = ((data.get("hit") or {}).get("title")) or None
        d["version"] = data.get("version")
        items.append(d)
    waiting = sum(1 for i in items if i["step"] not in (None, "idle") and not i["requests_active"])
    return {"items": items, "conversations_in_progress": waiting,
            "requests_total": sum(i["requests_total"] for i in items)}


def telegram_overview(conn: sqlite3.Connection, cfg) -> dict[str, Any]:
    """Configuration and activity of the Telegram side (no token, ever): transports, channels, publications, deliveries."""
    tg = cfg.telegram or {}
    local = tg.get("api_base_url") or ""
    by_chan = {r["chat_id"]: r["n"] for r in conn.execute(
        "SELECT chat_id, COUNT(*) AS n FROM publications WHERE status='sent' GROUP BY chat_id").fetchall()}
    channels = list(cfg.channels or [])
    return {
        "bot_api": {"mode": "local" if local else "standard", "local_url_configured": bool(local),
                    "upload_by_file_path": bool(tg.get("local_upload_container")),
                    "channel_bot_configured": bool(cfg.bot_token), "user_bot_configured": bool(getattr(cfg, "user_bot_token", "")),
                    "admin_bot_configured": bool(cfg.admin_bot_token)},
        "capacity": {"getme_ok": getattr(cfg.bot_capacity, "getme_ok", None),
                     "server_version": getattr(cfg.bot_capacity, "http_server_version", None)},
        "channels": [{"channel": c, "primary": (str(c) == str(cfg.channel_id)) or (i == 0 and not cfg.channel_id)}
                     for i, c in enumerate(channels)],
        "required_channels": list(getattr(cfg, "required_channels", []) or []),
        "publications_by_chat": by_chan,
        "deliveries": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status")},
        "copies": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM publications WHERE publication_type LIKE 'copy\\_%' ESCAPE '\\' GROUP BY status")},
    }


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql, *a):
        return conn.execute(sql, a).fetchone()[0]
    since = local_midnight_utc()
    return {
        "episodes": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM episodes GROUP BY status")},
        "requests": {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM requests GROUP BY state")},
        "deliveries": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status")},
        "users": one("SELECT COUNT(*) FROM users"),
        "published_today": one("SELECT COUNT(*) FROM episodes WHERE published_at >= ?", since),
        "deliveries_today": one("SELECT COUNT(*) FROM deliveries WHERE status='sent' AND completed_at >= ?", since),
        "requests_today": one("SELECT COUNT(*) FROM requests WHERE created_at >= ?", since),
        "shared_media": one("SELECT COUNT(*) FROM (SELECT episode_id FROM request_items GROUP BY episode_id HAVING COUNT(*) > 1)"),
        "audit_actions": one("SELECT COUNT(*) FROM audit_log"),
        "ts": now_utc(),
    }


def download_now(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any]:
    """Make a media eligible immediately: a baseline/failed one is queued, a waiting retry no longer waits for its backoff."""
    ep = repo.get(conn, episode_id)
    if ep is None:
        return {"ok": False, "message": "inconnu"}
    if ep.status in ("discovered", "identified"):
        if ep.status == "discovered":
            repo.transition(conn, episode_id, "identified")
        repo.transition(conn, episode_id, "queued")
        repo.enqueue(conn, ep.anime_key, episode_id)
    elif ep.status in RECOVERABLE:
        return {**requeue_episode.__wrapped_unaudited__(conn, episode_id), "episode_id": episode_id}
    elif ep.status == "retry_wait":
        conn.execute("UPDATE episodes SET next_retry_at=NULL WHERE id=?", (episode_id,))
    elif ep.status != "queued":
        return {"ok": False, "message": f"statut {ep.status} : rien à télécharger"}
    conn.commit()
    return {"ok": True, "episode_id": episode_id, "message": f"épisode {episode_id} : téléchargement immédiat"}


def send_to_user(conn: sqlite3.Connection, episode_id: int, user_id: int, *, confirm: bool = False) -> dict[str, Any]:
    """Deliver an existing media to a user now (a delivery with no request: the user's own request limit is untouched)."""
    from . import media
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "message": "confirmation requise"}
    ep = repo.get(conn, episode_id)
    if ep is None or not media.is_deliverable(ep):
        return {"ok": False, "message": "média inconnu ou pas encore prêt à être livré"}
    now = now_utc()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id, username, first_seen_at, last_seen_at) VALUES (?,NULL,?,?)",
                 (user_id, now, now))
    try:
        conn.execute("INSERT INTO deliveries (request_id, request_item_id, user_id, media_id, status, created_at, updated_at) "
                     "VALUES (NULL, NULL, ?, ?, 'pending', ?, ?)", (user_id, episode_id, now, now))
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"ok": False, "message": "un envoi identique est déjà en cours"}
    conn.commit()
    return {"ok": True, "episode_id": episode_id, "user_id": user_id, "message": "envoi planifié (livraison privée)"}


def retry_delivery(conn: sqlite3.Connection, delivery_id: int, *, confirm: bool = False) -> dict[str, Any]:
    """Manual decision on a failed or UNCERTAIN delivery (an uncertain one may already be in the user's chat)."""
    d = conn.execute("SELECT status FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    if d is None:
        return {"ok": False, "message": "livraison inconnue"}
    if d["status"] not in ("failed", "uncertain"):
        return {"ok": False, "message": f"statut {d['status']} : rien à relancer"}
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "message": "confirmation requise (risque de doublon si incertaine)"}
    conn.execute("UPDATE deliveries SET status='pending', attempt_count=0, updated_at=? WHERE id=?", (now_utc(), delivery_id))
    conn.execute("UPDATE request_items SET state='PENDING' WHERE id=(SELECT request_item_id FROM deliveries WHERE id=?)", (delivery_id,))
    conn.commit()
    return {"ok": True, "message": f"livraison {delivery_id} relancée"}


def republish_episode(conn: sqlite3.Connection, episode_id: int, *, confirm: bool = False) -> dict[str, Any]:
    """DANGEROUS, only on explicit request: forget the channel publication so the media is published AGAIN (a duplicate in
    the channel).  Needs the local file or a fresh download."""
    ep = repo.get(conn, episode_id)
    if ep is None:
        return {"ok": False, "message": "inconnu"}
    if ep.status not in ("published", "cleanup_pending", "cleaned", "cleanup_blocked"):
        return {"ok": False, "message": f"statut {ep.status} : pas publié, rien à republier"}
    if not confirm:
        return {"ok": False, "needs_confirmation": True,
                "message": "confirmation requise : cela crée un DOUBLON dans le canal"}
    conn.execute("DELETE FROM publications WHERE episode_id=?", (episode_id,))
    conn.execute("UPDATE episodes SET status='failed', video_message_id=NULL, thumbnail_message_id=NULL, published_at=NULL, "
                 "cleanup_at=NULL, updated_at=? WHERE id=?", (now_utc(), episode_id))
    conn.commit()
    res = requeue_episode.__wrapped_unaudited__(conn, episode_id)
    return {**res, "message": "republication demandée (le média sera publié de nouveau)"}


# ── audit: every mutating admin action is logged, with the actor the surface declared ─────────────────────

from . import audit as _audit  # noqa: E402


def panel_download(conn, source_url, *, cfg, **kw):
    """Admin download from the panel (target of the audit line = the anime page)."""
    from . import panel_downloads
    return panel_downloads.download(conn, cfg, source_url=source_url, **kw)



requeue_episode = _audit.audited("retry")(requeue_episode)
fail_episode = _audit.audited("fail")(fail_episode)
bulk_requeue = _audit.audited("retry_bulk")(bulk_requeue)
cancel_episode = _audit.audited("cancel")(cancel_episode)
add_anime_from_url = _audit.audited("anime_add")(add_anime_from_url)
panel_download = _audit.audited("panel_download")(panel_download)
update_anime = _audit.audited("anime_edit")(update_anime)
request_force_check = _audit.audited("force_check")(request_force_check)
request_worker_stop = _audit.audited("worker_stop")(request_worker_stop)
start_worker = _audit.audited("worker_start")(start_worker)
cancel_request = _audit.audited("request_cancel")(cancel_request)
download_now = _audit.audited("download_now")(download_now)
send_to_user = _audit.audited("send_to_user")(send_to_user)
retry_delivery = _audit.audited("retry_delivery")(retry_delivery)
republish_episode = _audit.audited("republish")(republish_episode)

_set_paused_raw = set_paused
_set_enabled_raw = set_anime_enabled


def set_paused(conn: sqlite3.Connection, paused: bool) -> dict[str, Any]:              # noqa: F811
    res = _set_paused_raw(conn, paused)
    who = _audit.current_actor()
    if who:
        _audit.record(conn, admin_id=who[1], surface=who[0], action="pause" if paused else "resume", target="queue",
                      metadata={"message": res.get("message")})
    return res


def set_anime_enabled(conn: sqlite3.Connection, anime_key: str, enabled: bool) -> dict[str, Any]:   # noqa: F811
    res = _set_enabled_raw(conn, anime_key, enabled)
    who = _audit.current_actor()
    if who:
        _audit.record(conn, admin_id=who[1], surface=who[0], action="anime_enable" if enabled else "anime_disable",
                      target=anime_key, result="success" if res.get("ok", True) else "failure",
                      metadata={"message": res.get("message")})
    return res


def trace_media(conn: sqlite3.Connection, media_key: str) -> dict[str, Any] | None:
    """Everything that happened to ONE media, from its identity alone: requests -> media -> job -> download -> validation ->
    publication -> private deliveries.  What the logs, `/history` and the panel show for a `media_key` / `media_ref`."""
    ep = conn.execute("SELECT * FROM episodes WHERE media_key=?", (media_key,)).fetchone()
    if ep is None:
        return None
    e = dict(ep)
    return {
        "media_key": media_key, "media_ref": e["media_ref"], "origin": e["origin"], "publish_channel": bool(e["publish_channel"]),
        "job": {"episode_id": e["id"], "status": e["status"], "attempts": e["attempt_count"], "claimed_by": e["claimed_by"],
                "queue": [dict(r) for r in conn.execute("SELECT status, position FROM queue_items WHERE episode_id=?", (e["id"],))]},
        "download": {"file_path": e["file_path"], "file_size": e["file_size"], "sha256": e["video_sha256"]},
        "requests": [dict(r) for r in conn.execute(
            "SELECT r.id, r.user_id, r.state, r.kind FROM request_items i JOIN requests r ON r.id=i.request_id "
            "WHERE i.media_key=? ORDER BY r.id", (media_key,))],
        "publications": [dict(r) for r in conn.execute(
            "SELECT publication_type, status, chat_id, message_id, media_key FROM publications WHERE episode_id=? ORDER BY id", (e["id"],))],
        "deliveries": [dict(r) for r in conn.execute(
            "SELECT id, user_id, status, method, telegram_message_id FROM deliveries WHERE media_id=? ORDER BY id", (e["id"],))],
    }
