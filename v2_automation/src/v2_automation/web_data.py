"""Read models for the web panel: French wording, no raw URL, no secret.

Pure data built on the same sources as the Telegram admin (`service`, `admin_views` labels), so both
surfaces always show the same numbers.  Nothing here writes to the database.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import alerts, app_config, monitoring, notifier, service
from .admin_views import STATE_LABELS
from .timeutil import now_utc

LARGE_FILE_DIR = app_config.ROOT / "output" / "evidence" / "large_file"

# state -> tone used by the interface (ok / info / warn / bad / muted)
STATE_TONES = {
    "published": "ok", "cleanup_pending": "ok", "cleaned": "ok",
    "downloading": "info", "downloaded": "info", "validating": "info", "validated": "info",
    "publishing_thumbnail": "info", "thumbnail_published": "info", "publishing_video": "info",
    "queued": "muted", "retry_wait": "warn", "discovered": "muted", "skipped_dup": "muted",
    "failed": "bad", "structure_changed": "bad", "blocked": "bad", "cleanup_blocked": "warn",
}

ALERT_TITLES = {
    "new_episode": "Nouvel épisode", "published": "Publié", "retry": "Vidéo inaccessible",
    "definitive_failure": "Échec définitif", "telegram_error": "Erreur Telegram",
    "structure_changed": "Structure du site changée", "recovery_after_crash": "Publication interrompue",
    "cleanup_blocked": "Nettoyage bloqué", "low_disk": "Disque presque plein",
    "worker_stopped": "Worker arrêté", "discovery_error": "Surveillance en erreur",
    "scheduler_problem": "Problème du scheduler", "download_started": "Téléchargement",
    "download_finished": "Téléchargé",
}

NOTIF_TEXT = {
    "published": ("Épisode publié", "Un message privé dès qu'un épisode est publié dans le canal."),
    "new_episode": ("Nouvel épisode détecté", "Un message quand la surveillance trouve un nouvel épisode."),
    "problems": ("Problèmes", "Échec, publication interrompue, disque presque plein, worker arrêté…"),
    "daily": ("Résumé quotidien", "Un récapitulatif de la journée à l'heure choisie."),
}

_URL = re.compile(r"https?://\S+")


def scrub(text: str | None, limit: int = 300) -> str:
    """Technical detail safe to show: links removed, length bounded."""
    if not text:
        return ""
    return _URL.sub("[lien]", str(text)).strip()[:limit]


def _name(row: dict) -> str:
    return row.get("anime_title") or row.get("anime_key") or "?"


def _ep(n: Any) -> str:
    return f"E{n}" if n is not None else "E?"


def state_info(status: str) -> dict[str, str]:
    return {"status": status, "status_label": STATE_LABELS.get(status, status),
            "tone": STATE_TONES.get(status, "muted")}


# ── episodes ─────────────────────────────────────────────────────────────────────

def episodes_page(conn: sqlite3.Connection, *, status: str | None, q: str | None, anime: str | None,
                  limit: int, offset: int) -> dict[str, Any]:
    where, args = [], []
    if status == "published":                        # the three "published" states read as one
        where.append("e.status IN ('published','cleanup_pending','cleaned')")
    elif status == "attention":
        where.append("e.status IN ('failed','structure_changed','blocked','cleanup_blocked')")
    elif status == "waiting":
        where.append("e.status IN ('queued','retry_wait')")
    elif status == "running":
        where.append("e.status IN ('downloading','downloaded','validating','validated',"
                     "'publishing_thumbnail','thumbnail_published','publishing_video')")
    elif status:
        where.append("e.status=?")
        args.append(status)
    else:                                            # default view: real jobs and history, not the known baseline
        where.append("e.status <> 'discovered'")
    if anime:
        where.append("e.anime_key=?")
        args.append(anime)
    if q:
        where.append("(e.label LIKE ? OR e.anime_key LIKE ? OR a.title LIKE ? OR CAST(e.episode_number AS TEXT)=?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%", q.lstrip("eE#")]
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    base = "FROM episodes e LEFT JOIN animes a ON a.anime_key=e.anime_key " + wsql
    total = conn.execute(f"SELECT COUNT(*) AS n {base}", args).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT e.id, e.anime_key, COALESCE(a.title, '') AS anime_title, e.episode_number, e.language,
                   e.status, e.retry_count, e.published_at, e.updated_at, e.last_error, e.file_size,
                   e.next_retry_at
            {base} ORDER BY COALESCE(e.published_at, e.updated_at) DESC, e.id DESC LIMIT ? OFFSET ?""",
        args + [limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d.update(state_info(d["status"]))
        d["last_error"] = scrub(d["last_error"])
        items.append(d)
    facets = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM episodes GROUP BY status")}
    groups = {
        "published": sum(facets.get(s, 0) for s in service.PUBLISHED_STATES),
        "running": sum(facets.get(s, 0) for s in service.RUNNING_STATES),
        "waiting": facets.get("queued", 0) + facets.get("retry_wait", 0),
        "attention": sum(facets.get(s, 0) for s in service.ATTENTION_STATES),
        "discovered": facets.get("discovered", 0),
    }
    return {"total": total, "limit": limit, "offset": offset, "items": items, "groups": groups,
            "animes": [{"anime_key": r["anime_key"], "title": r["title"] or r["anime_key"]}
                       for r in conn.execute("SELECT anime_key, title FROM animes ORDER BY title, anime_key")]}


def episode_detail(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any] | None:
    r = conn.execute(
        """SELECT e.id, e.anime_key, COALESCE(a.title, '') AS anime_title, e.episode_number, e.language, e.status,
                  e.retry_count, e.next_retry_at, e.retry_until_at, e.last_error, e.last_error_at, e.file_size,
                  e.published_at, e.cleanup_at, e.updated_at, e.thumbnail_message_id, e.video_message_id
           FROM episodes e LEFT JOIN animes a ON a.anime_key=e.anime_key WHERE e.id=?""", (episode_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d.update(state_info(d["status"]))
    d["last_error"] = scrub(d["last_error"], 600)
    d["progress"] = service.job_progress(d["status"])
    d["publications"] = [dict(p) for p in conn.execute(
        "SELECT publication_type, status, message_id, media_kind, file_size, attempted_at "
        "FROM publications WHERE episode_id=? ORDER BY attempted_at", (episode_id,))]
    row = conn.execute("SELECT cvalue FROM control WHERE ckey=?", (f"player:{episode_id}",)).fetchone()
    d["player"] = row["cvalue"] if row else None
    d["actionable"] = d["status"] in service.RECOVERABLE
    d["cancellable"] = d["status"] in ("queued", "retry_wait", "failed", "structure_changed", "blocked")
    return d


# ── queue: one lane per anime ────────────────────────────────────────────────────

def queue_lanes(conn: sqlite3.Connection) -> dict[str, Any]:
    all_jobs = service.jobs(conn, 300)
    lanes: dict[str, dict[str, Any]] = {}
    titles = {a["anime_key"]: a for a in service.anime_list(conn)}
    for j in all_jobs:
        lane = lanes.setdefault(j["anime_key"], {
            "anime_key": j["anime_key"], "anime_title": j["anime_title"] or j["anime_key"],
            "enabled": bool(titles.get(j["anime_key"], {}).get("enabled", True)), "items": []})
        item = {"id": j["id"], "episode_number": j["episode_number"], "next_retry_at": j["next_retry_at"],
                "progress": j["progress"], "file_size": j["file_size"]}
        item.update(state_info(j["status"]))
        item["reason"] = service.waiting_reason(j) if j["status"] in ("queued", "retry_wait") else ""
        lane["items"].append(item)
    for lane in lanes.values():
        lane["items"].sort(key=lambda i: (i["episode_number"] is None, i["episode_number"] or 0))
    out = sorted(lanes.values(), key=lambda l: l["anime_title"].lower())
    return {"paused": service.is_paused(conn), "lanes": out, "total": sum(len(l["items"]) for l in out)}


# ── problems: episodes to handle + open alerts, in words ─────────────────────────

def _alert_subject(conn: sqlite3.Connection, akey: str) -> str:
    if akey.startswith("ep:") and akey[3:].isdigit():
        r = conn.execute("SELECT e.episode_number, COALESCE(NULLIF(a.title, ''), e.anime_key) AS t FROM episodes e "
                         "LEFT JOIN animes a ON a.anime_key=e.anime_key WHERE e.id=?", (int(akey[3:]),)).fetchone()
        if r:
            return f"{r['t']} {_ep(r['episode_number'])}"
    if akey.startswith("anime:"):
        return akey[6:]
    return akey


def problems(conn: sqlite3.Connection) -> dict[str, Any]:
    marks = ",".join("?" * len(service.ATTENTION_STATES))
    eps = []
    for r in conn.execute(f"""
        SELECT e.id, e.anime_key, COALESCE(a.title, '') AS anime_title, e.episode_number, e.status, e.last_error,
               e.last_error_at, e.retry_count
        FROM episodes e LEFT JOIN animes a ON a.anime_key=e.anime_key
        WHERE e.status IN ({marks}) ORDER BY COALESCE(e.last_error_at, e.updated_at) DESC LIMIT 100""",
                          service.ATTENTION_STATES):
        d = dict(r)
        d.update(state_info(d["status"]))
        d["subject"] = f"{_name(d)} {_ep(d['episode_number'])}"
        d["detail"] = scrub(d.pop("last_error"), 600)
        eps.append(d)
    al = []
    for a in alerts.list_alerts(conn, limit=100, status="open"):
        al.append({"id": a["id"], "kind": a["kind"], "title": ALERT_TITLES.get(a["kind"], a["kind"]),
                   "subject": _alert_subject(conn, a["akey"]), "count": a["count"],
                   "first_at": a["raised_at"], "last_at": a["last_raised_at"], "detail": scrub(a["body"], 600)})
    return {"episodes": eps, "alerts": al, "total": len(eps) + len(al)}


# ── capacity: live values, no frozen snapshot ────────────────────────────────────

def probe_local_server(cfg, timeout: float = 1.5) -> bool | None:
    """Is the Local Bot API server answering?  None when the cloud API is used (nothing local to probe)."""
    base = (cfg.telegram or {}).get("api_base_url") or ""
    if not base:
        return None
    try:
        urllib.request.urlopen(base.rstrip("/") + "/", timeout=timeout)     # any HTTP answer, even 404, proves it is up
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def proven_uploads(directory: Path = LARGE_FILE_DIR) -> list[dict[str, Any]]:
    rows: dict[float, dict[str, Any]] = {}
    files = sorted(directory.glob("size_*.json")) if directory.exists() else []
    summary = directory / "summary.json"
    for f in ([summary] if summary.exists() else []) + files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in (data.get("rows") if isinstance(data, dict) and "rows" in data else [data]):
            if not isinstance(row, dict) or row.get("size_mib") is None or row.get("upload_s") is None:
                continue
            rows[row["target_mib"]] = {
                "target_mib": row["target_mib"], "size_mib": round(row["size_mib"], 1),
                "upload_s": round(row["upload_s"]), "result": row.get("result", "?"),
                "throughput_mibs": round(row["size_mib"] / row["upload_s"], 2) if row["upload_s"] else None}
    return [rows[k] for k in sorted(rows)]


def capacity_live(conn: sqlite3.Connection, cfg, *, probe: Callable[[Any], bool | None] = probe_local_server,
                  uploads: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    sysinfo = service.system_status(conn, cfg)
    limits = cfg.limits or {}
    proven = uploads if uploads is not None else proven_uploads()
    passed = [u["size_mib"] for u in proven if u["result"] == "PASS"]
    return {
        "server": {"reachable": probe(cfg), "mode": "local" if (cfg.telegram or {}).get("api_base_url") else "cloud",
                   "upload_by_file_path": bool((cfg.telegram or {}).get("local_upload_container"))},
        "limits": {"max_safe_publish_mib": limits.get("max_safe_publish_mib"),
                   "documented_mib": limits.get("documented_upload_limit_mb"),
                   "largest_proven_mib": max(passed) if passed else None},
        "proven_uploads": proven,
        "host": {k: sysinfo.get(k) for k in ("cpu_percent", "ram_percent", "disk_percent", "disk_free_bytes",
                                             "disk_total_bytes")},
        "max_concurrent_downloads": (cfg.queues or {}).get("max_concurrent_downloads"),
        "ts": now_utc(),
    }


# ── one call for the home page ───────────────────────────────────────────────────

def banner(dash: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """Global state in one sentence: bad > warn > ok, with the issues that explain it."""
    issues: list[dict[str, str]] = []
    if dash.get("worker", {}).get("stopping"):
        issues.append({"tone": "info", "text": "Arrêt du worker en cours : il termine ses jobs", "page": "sante"})
    elif not dash["worker_alive"]:
        issues.append({"tone": "warn", "text": "Le worker est arrêté : plus rien n'avance ni n'est surveillé",
                       "page": "sante"})
    if dash["paused"]:
        issues.append({"tone": "info", "text": "La file est en pause", "page": "file"})
    if dash["attention"]:
        n = len(dash["attention"])
        issues.append({"tone": "bad", "text": f"{n} épisode{'s' if n > 1 else ''} à traiter", "page": "erreurs"})
    if dash["alerts_open"]:
        n = dash["alerts_open"]
        issues.append({"tone": "warn", "text": f"{n} alerte{'s' if n > 1 else ''} ouverte{'s' if n > 1 else ''}",
                       "page": "erreurs"})
    if dash["last_check_error"]:
        issues.append({"tone": "warn", "text": "La dernière surveillance a échoué", "page": "anime"})
    for name, c in health["checks"].items():
        if not c["ok"] and name != "queue":              # the queue check is already explained by the worker line
            issues.append({"tone": "warn", "text": f"{c['label']} : {c['detail']}", "page": "sante"})
    tones = {i["tone"] for i in issues}
    level = "bad" if "bad" in tones else "warn" if "warn" in tones else "info" if tones else "ok"
    text = {"ok": "Tout fonctionne", "info": "Fonctionne, avec une remarque",
            "warn": "Points d'attention", "bad": "À traiter"}[level]
    return {"level": level, "text": text, "issues": issues}


def live_health(conn: sqlite3.Connection, cfg, free_bytes: int | None) -> dict[str, Any]:
    """Pipeline health with the disk read now (the configured snapshot is only a startup measure)."""
    h = monitoring.pipeline_health(conn, cfg)
    if free_bytes is not None:
        minimum = int((cfg.downloads or {}).get("min_free_disk_bytes", 0))
        c = h["checks"]["disk"]
        c["ok"] = free_bytes >= minimum
        c["detail"] = f"{free_bytes / 1073741824:.1f} Gio libres (réserve minimale {minimum / 1073741824:.0f} Gio)"
        c["explain"] = monitoring.CHECK_TEXT["disk"][1 if c["ok"] else 2]
        h["ok"] = all(x["ok"] for x in h["checks"].values())
    return h


def cycles(conn: sqlite3.Connection, cfg, n: int = 20) -> dict[str, Any]:
    """Recent global cycles with the result for each anime (names, not keys)."""
    from . import discovery
    titles = {a["anime_key"]: (a["title"] or a["anime_key"]) for a in service.anime_list(conn)}
    items = []
    for c in discovery.recent_cycles(conn, n):
        items.append({"started_at": c.get("started_at"), "finished_at": c.get("finished_at"),
                      "anime_count": c.get("anime_count"), "checked": c.get("checked"), "new_episodes": c.get("new_episodes"),
                      "jobs_created": c.get("jobs_created"), "errors": c.get("errors"),
                      "feed": ({k: (c.get("feed") or {}).get(k) for k in ("pages", "entries", "today", "new_anime", "new_episodes",
                                                                           "already_known", "errors")} if c.get("feed") else None),
                      "animes": [{"title": titles.get(k, k), "discovered": v.get("discovered"), "new": v.get("new", 0),
                                  "catchup": v.get("catchup", 0), "error": scrub(v.get("error"), 120)}
                                 for k, v in (c.get("animes") or {}).items()]})
    d = service.dashboard(conn, cfg)
    return {"items": items, "next_cycle": d["next_check"], "interval_seconds": d["poll_interval_seconds"],
            "today": d["today"], "worker_alive": d["worker_alive"]}


def dashboard(conn: sqlite3.Connection, cfg) -> dict[str, Any]:
    d = service.dashboard(conn, cfg)
    health = live_health(conn, cfg, d["system"].get("disk_free_bytes"))
    from . import discovery
    cyc = discovery.last_cycle(conn)
    d["last_cycle"] = ({k: cyc[k] for k in ("finished_at", "anime_count", "checked", "new_episodes", "jobs_created", "errors")}
                       if cyc else None)
    d["worker"] = service.worker_state(conn, now=d["now"])
    d["banner"] = banner(d, health)
    for key in ("running", "waiting", "recent", "attention"):
        d[key] = [dict(j, **state_info(j["status"])) if "status" in j else dict(j) for j in d[key]]
    for j in d["attention"]:
        j.pop("last_error", None)
    for j in d["waiting"] + d["running"]:
        j["last_error"] = scrub(j.get("last_error"), 160)
    per_anime_today = {r["anime_key"]: r["n"] for r in conn.execute(
        "SELECT anime_key, COUNT(*) AS n FROM episodes WHERE created_at >= ? AND status <> 'discovered' GROUP BY anime_key",
        (d["today"]["since"],))}
    for a in d["animes"]:
        a["today"] = per_anime_today.get(a["anime_key"], 0)
        a.pop("source_url", None)                       # raw links never reach the interface
        a["last_check_error"] = scrub(a.get("last_check_error"), 200)
    d["last_check_failed"] = bool(d.pop("last_check_error", None))
    d["health"] = {"ok": health["ok"], "failed": [n for n, c in health["checks"].items() if not c["ok"]]}
    return d


def health_page(conn: sqlite3.Connection, cfg) -> dict[str, Any]:
    d = service.dashboard(conn, cfg)
    h = live_health(conn, cfg, d["system"].get("disk_free_bytes"))
    ws = service.worker_state(conn, now=d["now"])
    h["worker"] = {"alive": d["worker_alive"], "paused": d["paused"], "stopping": ws["stopping"],
                   "explain": ("Arrêt demandé : le worker termine ses jobs en cours puis s'éteint." if ws["stopping"]
                               else "Le worker tourne et surveille les anime." if d["worker_alive"]
                               else "Le worker est arrêté : aucune surveillance ni publication tant qu'il n'est pas relancé.")}
    h["checks_list"] = [dict(c, name=n) for n, c in h["checks"].items()]
    return h


def notifications(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"items": [{"key": k, "label": NOTIF_TEXT[k][0], "help": NOTIF_TEXT[k][1],
                       "enabled": notifier.enabled(conn, k)} for k in notifier.KEYS]}
