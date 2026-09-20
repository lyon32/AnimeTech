"""Phase 9 — métiers healthchecks du pipeline V2.

Checks read-only (no state writes, no source contact):
  db          schema present + readable
  disk        free space above the configured guard gate
  queue       no head stuck beyond max_stall_seconds (stalled pipeline)
  retry       no episode whose 24h retry window expired without promotion
              (the scheduler must transition RETRY_WAIT -> FAILED itself)
  errors      failed/skipped_dup ratio vs published (never zero-divide)

Each check returns {"ok": bool, "detail": str}.  Overall ok = all ok.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import app_config, repo
from .schema import current_schema_version
from .timeutil import now_utc


CHECK_TEXT = {      # name -> (label, meaning when ok, what to do when not ok)
    "db": ("Base de données", "La base répond et son schéma est à jour.",
           "Vérifiez le fichier de base et relancez le worker : la migration se fait au démarrage."),
    "disk": ("Espace disque", "Assez de place pour télécharger les prochains épisodes.",
             "Libérez de l'espace : les téléchargements sont refusés sous la réserve minimale."),
    "queue": ("File d'attente", "Aucun épisode n'attend un worker depuis trop longtemps.",
              "Un épisode attend depuis trop longtemps : le worker est probablement arrêté, démarrez-le."),
    "retry": ("Nouvelles tentatives", "Toutes les fenêtres de nouvelle tentative sont à jour.",
              "Des fenêtres de nouvelle tentative ont expiré sans être traitées : démarrez le worker."),
    "errors": ("Taux d'échec", "Les échecs restent rares par rapport aux publications.",
               "Beaucoup d'échecs : consultez la page Erreurs pour connaître la cause."),
}


def pipeline_health(conn: sqlite3.Connection, cfg: app_config.AppConfig | None = None,
                    *, max_stall_seconds: int = 3600) -> dict[str, Any]:
    cfg = cfg or app_config.load_config()
    checks: dict[str, dict[str, Any]] = {}

    # db / schema
    try:
        ver = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()
        checks["db"] = {"ok": ver is not None and ver["version"] >= 1,
                        "detail": f"schema v{ver['version'] if ver else '?'}"}
    except Exception as exc:
        checks["db"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    # disk
    min_free = int(cfg.downloads.get("min_free_disk_bytes", 0))
    free = cfg.bot_capacity.free_disk_bytes if cfg.bot_capacity else None
    if free is None:
        checks["disk"] = {"ok": False, "detail": "disque inconnu"}
    else:
        checks["disk"] = {"ok": free >= min_free,
                          "detail": f"{free // (1024**3)} GiB libre"}

    # queue stall: only an episode that is really waiting for a worker counts.  One that waits for its source
    # (retry_wait / NOT_AVAILABLE_YET) or a paused queue is expected to sit still, not stalled.
    max_stall = int(cfg.monitoring.get("max_stall_seconds", 0)) or max_stall_seconds
    paused = conn.execute("SELECT cvalue FROM control WHERE ckey='paused'").fetchone()
    is_paused = paused is not None and paused["cvalue"] == "1"
    row = conn.execute(
        "SELECT MIN(q.created_at) AS oldest FROM queue_items q JOIN episodes e ON e.id=q.episode_id "
        "WHERE q.status='queued' AND e.status='queued'").fetchone()
    oldest = row["oldest"]
    stalled = False
    detail = "file vide"
    if is_paused:
        detail = "file en pause (volontaire)"
    elif oldest is not None:
        from .timeutil import ago_seconds
        age = ago_seconds(oldest)
        stalled = age > max_stall
        detail = f"tête depuis {int(age) // 60} min (max {max_stall // 60} min)"
    checks["queue"] = {"ok": not stalled, "detail": detail}

    # expired retry windows not yet promoted
    expired = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes WHERE status='retry_wait' "
        "AND retry_until_at IS NOT NULL AND retry_until_at <= ?",
        (now_utc(),)).fetchone()["n"]
    checks["retry"] = {"ok": expired == 0,
                       "detail": f"{expired} fenêtre(s) de retry expirée(s) non promue(s)"}

    # errors vs published
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status").fetchall()}
    published = counts.get("published", 0) + counts.get("cleanup_pending", 0) + counts.get("cleaned", 0)
    failed = counts.get("failed", 0) + counts.get("skipped_dup", 0)
    if published == 0:
        effort = failed > 0
        detail = f"{failed} échec(s), aucun succès"
    else:
        effort = failed / published > 0.5
        detail = f"{failed} échec(s) / {published} publication(s)"
    checks["errors"] = {"ok": not effort, "detail": detail}

    for name, chk in checks.items():                        # human wording for the web panel (the keys stay as before)
        label, good, advice = CHECK_TEXT.get(name, (name, "", ""))
        chk["label"] = label
        chk["explain"] = good if chk["ok"] else advice
    return {"ok": all(c["ok"] for c in checks.values()), "ts": now_utc(), "checks": checks}