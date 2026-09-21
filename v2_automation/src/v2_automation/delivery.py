"""Private delivery: media READY/PUBLISHED -> the user who asked, persisted delivery by delivery.

EXISTING BEHAVIOR  nothing is ever sent to a user.
DESIRED BEHAVIOR   one persisted delivery per request item; N users waiting for one media = 1 download, N deliveries;
                   never twice to the same user after a restart.
CHANGE             this module + table `deliveries` (schema v4, UNIQUE(request_item_id)).

How a media reaches a user (first that applies; the caller never sees the difference):
  file_id  a previous delivery of this media by this bot stored its Telegram file_id  -> sendVideo(file_id), no upload
  copy     the media is already in a channel (video_message_id)                       -> copyMessage, no upload
  upload   the validated file is still on disk                                        -> sendVideo(file) through the
           configured transport (standard Bot API or Local Bot API), the returned file_id is stored for the next user

Crash safety (the outcome of a Telegram call cannot be read back for a private chat):
  * `sending` is committed BEFORE the call and `sent` (+ message id, file id) right after it.
  * A delivery found `sending` at boot is `uncertain`: it is NEVER re-sent automatically (no accidental duplicate).  Its
    request is failed with an explicit message and the user can ask again — the media is then served from the reference
    already stored (file_id / channel), with no new download.
  * A definitive Telegram refusal is `failed` and retried with a backoff up to `max_attempts`; a blocked bot is final.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable

from . import errors, media, repo
from .requests_mgr import ACTIVE as ACTIVE_REQUEST_STATES
from .telegram_publisher import Sent, TelegramPublisher, scrub
from .timeutil import add_seconds, now_utc, utc_diff_seconds

logger = logging.getLogger(__name__)

UNCERTAIN_KINDS = ("TIMEOUT", "NETWORK")           # the request may have reached Telegram
FINAL_KINDS = ("FORBIDDEN", "CHAT_NOT_FOUND", "UNAUTHORIZED")
BACKOFF_S = 30.0


class DeliveryEngine:
    def __init__(self, conn: sqlite3.Connection, publisher: TelegramPublisher, cfg=None, *,
                 now: Callable[[], str] = now_utc, max_attempts: int | None = None):
        self.conn, self.pub, self.now = conn, publisher, now
        ub = getattr(cfg, "user_bot", None) or {}
        self.max_attempts = int(max_attempts or ub.get("max_delivery_attempts", 3))
        self.copy_from_channel = bool(ub.get("delivery_copy_from_channel", True))
        self.cleanup_days = float((getattr(cfg, "publication", None) or {}).get("cleanup_after_days", 14))

    # -- planning -----------------------------------------------------------------------
    def plan(self) -> list[int]:
        """Create the delivery of every request item whose media can be served now.  Idempotent (UNIQUE per item)."""
        marks = ",".join("?" * len(ACTIVE_REQUEST_STATES))
        rows = self.conn.execute(
            f"SELECT i.id AS item_id, i.request_id, r.user_id, i.episode_id FROM request_items i "
            f"JOIN requests r ON r.id=i.request_id WHERE r.state IN ({marks}) "
            f"AND i.state NOT IN ('COMPLETED','CANCELLED','FAILED') "
            f"AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.request_item_id=i.id) "
            f"ORDER BY i.request_id, i.episode_number, i.id", ACTIVE_REQUEST_STATES).fetchall()
        created = []
        for r in rows:
            ep = repo.get(self.conn, r["episode_id"])
            if ep is None or not media.is_deliverable(ep):
                continue
            now = self.now()
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO deliveries (request_id, request_item_id, user_id, media_id, status, created_at, "
                "updated_at) VALUES (?,?,?,?, 'pending', ?, ?)",
                (r["request_id"], r["item_id"], r["user_id"], r["episode_id"], now, now))
            if cur.rowcount:
                created.append(cur.lastrowid)
                logger.info("[DELIVERY] delivery=%s request=%s media=%s user=%s planned", cur.lastrowid,
                            r["request_id"], ep.media_key, r["user_id"])
        self.conn.commit()
        return created

    # -- sending ------------------------------------------------------------------------
    def _due(self, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM deliveries WHERE status IN ('pending','failed') AND attempt_count < ? ORDER BY id LIMIT ?",
            (self.max_attempts, limit * 4)).fetchall()
        due = []
        for r in rows:
            d = dict(r)
            if d["status"] == "failed" and utc_diff_seconds(self.now(), d["updated_at"]) < BACKOFF_S * 2 ** (d["attempt_count"] - 1):
                continue
            due.append(d)
        return due[:limit]

    def run(self, limit: int = 10) -> dict[str, Any]:
        """Plan, then send what is due.  Returns counts and the ids of the requests whose state may have changed."""
        self.plan()
        out: dict[str, Any] = {"sent": 0, "failed": 0, "uncertain": 0, "skipped": 0, "requests": set()}
        for d in self._due(limit):
            res = self._send_one(d)
            out[res] += 1
            if d["request_id"] is not None:
                out["requests"].add(d["request_id"])
        out["requests"] = sorted(out["requests"])
        return out

    def _claim(self, d: dict) -> bool:
        cur = self.conn.execute(
            "UPDATE deliveries SET status='sending', attempt_count=attempt_count+1, updated_at=? "
            "WHERE id=? AND status IN ('pending','failed')", (self.now(), d["id"]))
        self.conn.commit()
        return cur.rowcount == 1

    def _send_one(self, d: dict) -> str:
        if not self._claim(d):
            return "skipped"                            # another worker owns it: not our outcome to count
        ep = repo.get(self.conn, d["media_id"])
        caption = self._caption(ep, d["request_id"])
        try:
            sent, method = self._transmit(d, ep, caption)
        except Exception as exc:
            return self._on_error(d, exc)
        self._on_sent(d, ep, sent, method)
        return "sent"

    def _transmit(self, d: dict, ep, caption: str) -> tuple[Sent, str]:
        user = d["user_id"]
        ref = self.conn.execute(
            "SELECT telegram_file_id FROM deliveries WHERE media_id=? AND status='sent' AND telegram_file_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (ep.id,)).fetchone()
        if ref is not None:
            return self.pub.send_video(user, ref["telegram_file_id"], caption), "file_id"
        if self.copy_from_channel and ep.video_message_id:
            pub = self.conn.execute("SELECT chat_id FROM publications WHERE episode_id=? AND publication_type="
                                    "'first_publication' AND status='sent'", (ep.id,)).fetchone()
            if pub is not None and pub["chat_id"]:
                try:
                    return self.pub.copy(user, pub["chat_id"], ep.video_message_id), "copy"
                except Exception as exc:
                    has_file = bool(ep.file_path and Path(ep.file_path).exists())
                    if getattr(exc, "kind", "") == "FORBIDDEN" or not has_file:
                        raise                          # blocked by the user, or nothing to upload instead
                    logger.warning("[DELIVERY] copy impossible (%s): envoi du fichier local", scrub(exc))
        path = Path(ep.file_path) if ep.file_path else None
        if path is None or not path.exists():
            raise errors.PipelineError(errors.STORAGE_ERROR,
                                       "fichier local supprimé et aucune référence Telegram réutilisable")
        if ep.file_size and path.stat().st_size != ep.file_size:
            raise errors.PipelineError(errors.STORAGE_ERROR, "fichier local altéré (taille différente de la validation)")
        return self.pub.send_video(user, path, caption), "upload"

    def _caption(self, ep, request_id: int) -> str:
        row = self.conn.execute("SELECT title, version FROM requests WHERE id=?", (request_id,)).fetchone()             if request_id is not None else None
        if row is None:                                   # sent by an administrator: title from the watched list
            a = self.conn.execute("SELECT title FROM animes WHERE anime_key=?", (ep.anime_key,)).fetchone()
            row = {"title": a["title"] if a else None, "version": ep.language}
        title = (row["title"] if row and row["title"] else ep.anime_key)
        ver = media.normalize_version(row["version"] if row else ep.language)
        n = f"Épisode {ep.episode_number}" if ep.episode_number is not None else (ep.label or "Film")
        return f"{title}\n{n} · {ver}"

    def _on_sent(self, d: dict, ep, sent: Sent, method: str) -> None:
        now = self.now()
        self.conn.execute(
            "UPDATE deliveries SET status='sent', telegram_message_id=?, telegram_file_id=COALESCE(?, telegram_file_id), "
            "method=?, last_error=NULL, completed_at=?, updated_at=? WHERE id=?",
            (sent.message_id, sent.file_id, method, now, now, d["id"]))
        if ep.status == "ready":                          # private-only media: the first delivery is its "publication"
            repo.transition(self.conn, ep.id, "published")
            self.conn.execute("UPDATE episodes SET published_at=COALESCE(published_at, ?), cleanup_at=COALESCE(cleanup_at, ?), "
                              "updated_at=? WHERE id=?", (now, add_seconds(now, self.cleanup_days * 86400), now, ep.id))
        self.conn.execute("UPDATE request_items SET state='COMPLETED', updated_at=? WHERE id=?", (now, d["request_item_id"]))
        self.conn.commit()
        logger.info("[DELIVERY] delivery=%s request=%s media=%s user=%s sent via %s message=%s", d["id"], d["request_id"],
                    ep.media_key, d["user_id"], method, sent.message_id)

    def _on_error(self, d: dict, exc: Exception) -> str:
        kind = getattr(exc, "kind", "") or ""
        code = errors.classify(exc) if not kind else errors.TELEGRAM_ERROR
        msg = f"{code}/{kind}: {scrub(exc)}"[:500] if kind else f"{code}: {scrub(exc)}"[:500]
        now = self.now()
        if kind in UNCERTAIN_KINDS:
            status, outcome = "uncertain", "uncertain"
        else:
            status, outcome = "failed", "failed"
        attempts = self.max_attempts if kind in FINAL_KINDS or isinstance(exc, errors.PipelineError) else None
        self.conn.execute("UPDATE deliveries SET status=?, last_error=?, updated_at=?, "
                          "attempt_count=COALESCE(?, attempt_count) WHERE id=?", (status, msg, now, attempts, d["id"]))
        exhausted = d["attempt_count"] + 1 >= self.max_attempts          # d holds the count from before the claim
        if status == "uncertain" or attempts is not None or exhausted:
            self.conn.execute("UPDATE request_items SET state='FAILED', updated_at=? WHERE id=?", (now, d["request_item_id"]))
        self.conn.commit()
        logger.warning("[DELIVERY] delivery=%s request=%s user=%s %s: %s", d["id"], d["request_id"], d["user_id"], status, msg)
        return outcome

    # -- crash recovery -----------------------------------------------------------------
    def recover(self) -> dict[str, list[int]]:
        return recover_deliveries(self.conn, now=self.now())


def recover_deliveries(conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, list[int]]:
    """Boot: a delivery left `sending` may or may not have reached the user — it becomes `uncertain`, never re-sent
    automatically; its request is failed with a clear message so the user can simply ask again."""
    now = now or now_utc()
    rows = conn.execute("SELECT id, request_id, request_item_id FROM deliveries WHERE status='sending'").fetchall()
    for r in rows:
        conn.execute("UPDATE deliveries SET status='uncertain', updated_at=?, last_error=? WHERE id=?",
                     (now, "arrêt pendant l'envoi : résultat inconnu, pas de renvoi automatique", r["id"]))
        conn.execute("UPDATE request_items SET state='FAILED', updated_at=? WHERE id=?", (now, r["request_item_id"]))
    conn.commit()
    return {"uncertain": [r["id"] for r in rows], "requests": sorted({r["request_id"] for r in rows if r["request_id"] is not None})}
