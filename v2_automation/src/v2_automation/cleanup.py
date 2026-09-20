"""Phase 10 — nettoyage 14 jours.

Retention rule (master prompt): the local copy of a published file may be
deleted once it is older than `publication.cleanup_after_days`; the Telegram
messages (thumbnail + video) are retained forever.  Only episodes whose
publication is already durable to a message id are cleaned; a partial
publication is NEVER cleaned by this module.

Each deletion writes an evidence file (path must stay masked elsewhere; no
secret content) and transitions PUBLISHED/CLEANUP_PENDING -> CLEANED.

`published_at` alone drives age: a published episode with a provable
publication (video message id) but NO `published_at` can never be age-checked —
it is routed to CLEANUP_BLOCKED and surfaced to the operator instead of being
silently age-estimated from `updated_at` (closure gap fix).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import alerts, app_config, evidence, repo
from .timeutil import ago_seconds, now_utc

CLEANABLE = ("published", "cleanup_pending", "cleanup_blocked")
BLOCKED = "cleanup_blocked"


def run_cleanup(conn: sqlite3.Connection, cfg: app_config.AppConfig,
                *, retention_days: float | None = None,
                dispatch=None) -> dict[str, Any]:
    """`dispatch` is the optional alert push callback (see alerts.raise_alert).
    A BLOCKED episode always raises a cleanup_blocked alert with its own akey,
    so the operator can triage — never a silent skip."""
    from .alerts import KIND_CLEANUP_BLOCKED, raise_alert
    days = retention_days if retention_days is not None \
        else float(cfg.publication.get("cleanup_after_days", 14))
    window_s = days * 86400
    rows = conn.execute(
        "SELECT id, anime_key, file_path, file_size, video_message_id, published_at, updated_at, status "
        "FROM episodes "
        "WHERE status IN ('published', 'cleanup_pending', 'cleanup_blocked') AND file_path IS NOT NULL "
        "ORDER BY id").fetchall()

    cleaned: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    skipped, errors = 0, 0
    for r in rows:
        # only touch episodes whose video is provably published (message id set)
        if not r["video_message_id"]:
            skipped += 1
            continue
        # NO fallback anchor: without published_at the age cannot be proven.
        if not r["published_at"]:
            try:
                repo.transition(conn, r["id"], BLOCKED)
                conn.commit()
            except ValueError:
                pass  # already blocked; keep counting
            blocked.append({
                "episode_id": r["id"],
                "status": r["status"],
                "reason": "published_at manquant — age de retention non prouvable",
                "blocked_at": now_utc(),
            })
            try:
                raise_alert(conn, KIND_CLEANUP_BLOCKED, f"ep:{r['id']}",
                            f"nettoyage bloqué — épisode {r['id']}",
                            f"{r['anime_key']} publié sans published_at : retention "
                            f"non prouvable, decision manuelle requise.", dispatch=dispatch)
            except Exception:
                pass  # an alert problem must never break the cleanup sweep
            continue
        anchor = r["published_at"]
        if ago_seconds(anchor) < window_s:
            continue                                            # still in retention
        ep = repo.get(conn, r["id"])
        try:
            removed = _delete_file(Path(ep.file_path))
            if ep.thumbnail_path:
                _delete_file(Path(ep.thumbnail_path))
            if ep.status == "published":
                repo.transition(conn, ep.id, "cleanup_pending")
            if not ep.cleanup_at:
                repo.set_cleanup_at(conn, ep.id, now_utc())
            repo.transition(conn, ep.id, "cleaned")
            conn.commit()
            cleaned.append({
                "episode_id": ep.id,
                "anime_key": ep.anime_key,
                "file_path": ep.file_path,
                "thumbnail_path": ep.thumbnail_path,
                "video_message_id": ep.video_message_id,
                "file_size": ep.file_size,
                "deleted": removed,
                "cleaned_at": now_utc(),
            })
        except Exception as exc:
            errors += 1
            conn.rollback()

    evidence_path: str | None = None
    if cleaned or blocked:
        out = evidence.evidence_dir("cleanup") / f"cleanup_{now_utc().replace(':', '-')}.json"
        out.write_text(json.dumps({
            "stage": "cleanup_days",
            "retention_days": days,
            "cleaned_count": len(cleaned),
            "blocked_count": len(blocked),
            "skipped": skipped,
            "errors": errors,
            "items": cleaned,
            "blocked": blocked,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        evidence_path = str(out)
    return {"stage": "cleanup_days", "retention_days": days, "cleaned": len(cleaned),
            "blocked": blocked, "blocked_count": len(blocked),
            "skipped": skipped, "errors": errors, "evidence_path": evidence_path}


def _delete_file(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False