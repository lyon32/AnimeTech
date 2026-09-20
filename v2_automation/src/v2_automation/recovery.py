"""Phase 11 — restart/recovery (resume after crash).

Called at every daemon boot (and manually via `v2_automation recover`):

1. Mid-flight episodes are interrupted by definition (no durable checkpoint).
   Two classes are handled differently (closure gap fix — double-publication risk):

   - SAFE_MIDFLIGHT (DOWNLOADING, DOWNLOADED, VALIDATING, VALIDATED): no external
     side effect happened yet, so each is routed FAILED -> QUEUED and re-enqueued.
   - RISKY_MIDFLIGHT (PUBLISHING_THUMBNAIL, THUMBNAIL_PUBLISHED, PUBLISHING_VIDEO):
     a message MAY already be live because the Telegram ACK and the DB commit are
     not atomic.  Re-running would DOUBLE-PUBLISH.  These are routed to FAILED and
     marked `decision: manual` — NOT auto-requeued.

2. RETRY_WAIT episodes whose 24h window expired are promoted to FAILED (the
   scheduler's own job, enforced here at boot as a safety net).

3. Dangling queue items (episode no longer queued/retry_wait) are repaired so
   the FIFO invariant holds.

A recovery evidence file is written only when something was repaired.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import alerts, app_config, evidence, repo
from .timeutil import add_seconds, now_utc

SAFE_MIDFLIGHT = ("downloading", "downloaded", "validating", "validated")
RISKY_MIDFLIGHT = ("publishing_thumbnail", "thumbnail_published", "publishing_video")
MIDFLIGHT = SAFE_MIDFLIGHT + RISKY_MIDFLIGHT  # backward-compat aggregate


def run_recovery(conn: sqlite3.Connection, cfg: app_config.AppConfig,
                 *, now: str | None = None, dispatch=None) -> dict[str, Any]:
    """`dispatch` = optional alert push (see alerts.raise_alert).  A risky
    mid-flight episode always raises a recovery_after_crash alert so the human
    decides the re-publication — never silently requeued."""
    from .alerts import KIND_RECOVERY_AFTER_CRASH, raise_alert
    now = now or now_utc()
    report: dict[str, Any] = {"reset_to_queued": [], "publishing_to_failed": [],
                              "expired_to_failed": [], "dangling_queue_repaired": 0,
                              "evidence_path": None}

    # 1a) safe mid-flight -> failed -> queued (no external side effect yet)
    rows = conn.execute(
        "SELECT id, status FROM episodes WHERE status IN (%s)"
        % ",".join("?" * len(SAFE_MIDFLIGHT)), SAFE_MIDFLIGHT).fetchall()
    for r in rows:
        ep = repo.get(conn, r["id"])
        repo.transition(conn, ep.id, "failed")
        repo.mark_failed(conn, ep.id, f"repris après arrêt (statut {r['status']})")
        repo.transition(conn, ep.id, "queued")
        repo.set_retry_until(conn, ep.id, None, 0)
        repo.enqueue(conn, ep.anime_key, ep.id)
        # the killed process had claimed the item ('processing'): hand it back so it can be claimed again
        conn.execute("UPDATE queue_items SET status='queued' WHERE episode_id=?", (ep.id,))
        report["reset_to_queued"].append({"episode_id": ep.id, "was": r["status"]})

    # 1b) risky mid-flight -> FAILED, explicit manual decision, never auto-requeued
    rows = conn.execute(
        "SELECT id, status FROM episodes WHERE status IN (%s)"
        % ",".join("?" * len(RISKY_MIDFLIGHT)), RISKY_MIDFLIGHT).fetchall()
    for r in rows:
        repo.transition(conn, r["id"], "failed")
        repo.mark_failed(conn, r["id"],
                         "arrêt pendant publication (vignette/vidéo): risque de double "
                         "publication — re-publication MANUELLE requise")
        report["publishing_to_failed"].append({"episode_id": r["id"], "was": r["status"]})
        try:
            raise_alert(conn, KIND_RECOVERY_AFTER_CRASH, f"ep:{r['id']}",
                        f"publication interrompue — épisode {r['id']}",
                        "arrêt pendant vignette/vidéo : re-publication MANUELLE "
                        "requise (risque de doublon)", dispatch=dispatch)
        except Exception:
            pass  # an alert problem must never break the recovery sweep

    # 2) expired retry windows -> failed
    expired = conn.execute(
        "SELECT id FROM episodes WHERE status='retry_wait' "
        "AND retry_until_at IS NOT NULL AND retry_until_at <= ?", (now,)).fetchall()
    for r in expired:
        repo.mark_failed(conn, r["id"], "fenêtre de retry (24h) expirée")
        report["expired_to_failed"].append({"episode_id": r["id"]})

    # 3) dangling queue items -> back to a consistent FIFO state
    dang = conn.execute("""
        SELECT q.id AS qid, e.status AS estatus
        FROM queue_items q JOIN episodes e ON e.id = q.episode_id
        WHERE q.status = 'queued' AND e.status NOT IN ('queued', 'retry_wait')""").fetchall()
    for d in dang:
        conn.execute("DELETE FROM queue_items WHERE id=?", (d["qid"],))
        report["dangling_queue_repaired"] += 1

    # 3b) orphan claims: an item left 'processing' by a killed process while its episode waits again
    cur = conn.execute("""
        UPDATE queue_items SET status='queued'
        WHERE status='processing'
          AND episode_id IN (SELECT id FROM episodes WHERE status IN ('queued', 'retry_wait'))""")
    report["orphan_claims_released"] = cur.rowcount

    # 3c) items of episodes that already reached an end state must not block their anime's FIFO
    cur = conn.execute("""
        DELETE FROM queue_items WHERE episode_id IN
          (SELECT id FROM episodes WHERE status IN ('published', 'cleanup_pending', 'cleaned', 'failed', 'skipped_dup'))""")
    report["finished_items_dropped"] = cur.rowcount

    # 3d) published episodes get their scheduled cleanup: cleanup_at = published_at + retention
    days = float((cfg.publication or {}).get("cleanup_after_days", 14))
    scheduled = 0
    for r in conn.execute("SELECT id, published_at FROM episodes WHERE published_at IS NOT NULL AND cleanup_at IS NULL "
                          "AND status IN ('published', 'cleanup_pending')").fetchall():
        conn.execute("UPDATE episodes SET cleanup_at=? WHERE id=?", (add_seconds(r["published_at"], days * 86400), r["id"]))
        scheduled += 1
    report["cleanup_scheduled"] = scheduled

    report["alerts_closed"] = alerts.sweep_stale(conn)

    conn.commit()
    report["reconciled"] = reconcile_uncertain(conn, cfg)

    repaired = len(report["reset_to_queued"]) + len(report["publishing_to_failed"]) \
        + len(report["expired_to_failed"]) \
        + report["dangling_queue_repaired"] + report["orphan_claims_released"] \
        + report["finished_items_dropped"] \
        + len(report["reconciled"]) + report["cleanup_scheduled"] + report["alerts_closed"]
    if repaired:
        out = evidence.evidence_dir("recovery") / f"recovery_{now.replace(':', '-')}.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        report["evidence_path"] = str(out)
    return report


# ── uncertain publications: find the video the server published on its own ──────

def _video_caption(conn, ep) -> str | None:
    """Rebuild the exact caption the video was sent with (title from `animes`, quality from the file)."""
    from pathlib import Path
    from .metadata import MediaMetadata, build_caption, detect_language, detect_media_type
    title_row = conn.execute("SELECT title FROM animes WHERE anime_key=?", (ep.anime_key,)).fetchone()
    title = (title_row["title"] if title_row else "") or ""
    if not title or not ep.file_path or not Path(ep.file_path).exists():
        return None
    from v1_poc.media_tools import ffprobe_bin
    from v1_poc.validator import run_ffprobe
    probe = run_ffprobe(Path(ep.file_path), ffprobe_bin())
    stream = next((st for st in ((probe.probe or {}).get("streams") or [])
                   if st.get("codec_type") == "video"), None)
    height = (stream or {}).get("height")
    return build_caption(MediaMetadata(
        title=title, episode=ep.episode_number,
        media_type=detect_media_type(ep.episode_url, ep.episode_number),
        language=detect_language(ep.episode_url, ep.language),
        quality=f"{height}p" if height else None))


def reconcile_uncertain(conn: sqlite3.Connection, cfg: app_config.AppConfig, *,
                        telegram_factory=None, wait_s: float = 20.0) -> list[dict[str, Any]]:
    """For episodes left FAILED with an uncertain publication (crash / dropped connection during the
    video upload) whose thumbnail was published: look for the video message right after the thumbnail
    (exact caption; non-destructive).  If it exists, record it and finish the episode WITHOUT sending
    anything.  Best effort: a Telegram problem never breaks the recovery sweep."""
    if telegram_factory is None:
        if not (cfg.bot_token and cfg.channel_id):
            return []
        from .downloader import make_telegram_client
        telegram_factory = lambda: make_telegram_client(cfg)  # noqa: E731
    rows = conn.execute("""
        SELECT e.id, p.message_id AS thumb_id FROM episodes e
        JOIN publications p ON p.episode_id = e.id AND p.publication_type='thumbnail' AND p.status='sent'
        WHERE e.status='failed' AND e.video_message_id IS NULL AND e.last_error LIKE '%MANUELLE%'
          AND NOT EXISTS (SELECT 1 FROM publications v WHERE v.episode_id=e.id
                          AND v.publication_type='first_publication')""").fetchall()
    done: list[dict[str, Any]] = []
    for r in rows:
        client = None
        try:
            ep = repo.get(conn, r["id"])
            caption = _video_caption(conn, ep)
            if caption is None:
                continue
            client = telegram_factory()
            found = client.wait_for_message(range(r["thumb_id"] + 1, r["thumb_id"] + 4), caption,
                                            timeout_s=wait_s, poll_s=5)
            if found is None:
                continue
            size = ep.file_size
            repo.commit_publication(conn, ep.id, "first_publication", cfg.channel_id, found,
                                    "video", ep.video_sha256, size)
            days = float((cfg.publication or {}).get("cleanup_after_days", 14))
            conn.execute("UPDATE episodes SET video_message_id=?, thumbnail_message_id=?, status='cleanup_pending', "
                         "last_error=NULL, published_at=COALESCE(published_at, ?), "
                         "cleanup_at=COALESCE(cleanup_at, ?), updated_at=? WHERE id=?",
                         (found, r["thumb_id"], now_utc(), add_seconds(now_utc(), days * 86400), now_utc(), ep.id))
            conn.execute("DELETE FROM queue_items WHERE episode_id=?", (ep.id,))
            alerts.resolve_for_episode(conn, ep.id)
            conn.commit()
            if ep.file_path:
                client.cleanup_remote(Path(ep.file_path))
            done.append({"episode_id": ep.id, "video_message_id": found, "thumbnail_message_id": r["thumb_id"]})
        except Exception as exc:  # never break the recovery sweep
            import logging
            logging.getLogger(__name__).warning("reconcile épisode %s impossible: %s", r["id"], exc)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
    return done
