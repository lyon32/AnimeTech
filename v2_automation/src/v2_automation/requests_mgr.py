"""User requests — a different concept from a media.

EXISTING BEHAVIOR  no users, no requests: only the watcher creates jobs.
DESIRED BEHAVIOR   a user asks for an episode (or a season); the request never owns a download: it finds or
                   creates THE media through `media.ensure_media` (shared with the watcher), waits for it, and is
                   then delivered.  Several users / the watcher asking for the same media = one job.
GAP                request model, one-active-request limit, parent/child season, waiting + expiry, cancel, history.
CHANGE             this module + tables `users`, `requests`, `request_items` (schema v4).

Request states   PENDING > SEARCHING > FOUND > QUEUED > PROCESSING > DELIVERING > COMPLETED
                 side states: WAITING_FOR_MEDIA (episode not listed / video not ready yet), CANCELLED, EXPIRED, FAILED.
A season is ONE request (parent) with one item (child) per listed episode; items are processed progressively.
The wait deadline (`request.wait_timeout_seconds`, 20 min) is configurable and read from an injectable clock.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from . import errors, media, repo
from .catalog import SourceCatalog
from .timeutil import add_seconds, now_utc, utc_diff_seconds

logger = logging.getLogger(__name__)

DEFAULT_WAIT_TIMEOUT_S = 20 * 60
DEFAULT_RECHECK_S = 60


class RequestState(str, Enum):
    PENDING = "PENDING"
    SEARCHING = "SEARCHING"
    FOUND = "FOUND"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    WAITING_FOR_MEDIA = "WAITING_FOR_MEDIA"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


S = RequestState
TERMINAL = (S.COMPLETED.value, S.CANCELLED.value, S.EXPIRED.value, S.FAILED.value)     # keep in step with ux_requests_one_active
ACTIVE = tuple(s.value for s in S if s.value not in TERMINAL)

_ACTIVE_NOW = {S.PENDING, S.SEARCHING, S.FOUND, S.QUEUED, S.PROCESSING, S.WAITING_FOR_MEDIA, S.DELIVERING}
_ALLOWED: dict[RequestState, set[RequestState]] = {
    S.PENDING: {S.SEARCHING, S.CANCELLED, S.FAILED},
    S.SEARCHING: {S.FOUND, S.WAITING_FOR_MEDIA, S.CANCELLED, S.EXPIRED, S.FAILED},
    S.FOUND: {S.QUEUED, S.PROCESSING, S.DELIVERING, S.WAITING_FOR_MEDIA, S.CANCELLED, S.FAILED},
    S.QUEUED: {S.PROCESSING, S.WAITING_FOR_MEDIA, S.DELIVERING, S.COMPLETED, S.CANCELLED, S.FAILED},
    S.PROCESSING: {S.QUEUED, S.WAITING_FOR_MEDIA, S.DELIVERING, S.COMPLETED, S.CANCELLED, S.FAILED},
    S.WAITING_FOR_MEDIA: {S.FOUND, S.QUEUED, S.PROCESSING, S.DELIVERING, S.CANCELLED, S.EXPIRED, S.FAILED},
    S.DELIVERING: {S.PROCESSING, S.QUEUED, S.COMPLETED, S.CANCELLED, S.FAILED},
    S.COMPLETED: set(), S.CANCELLED: set(), S.EXPIRED: set(), S.FAILED: set(),
}


def can_transition(cur: RequestState, new: RequestState) -> bool:
    return cur == new or new in _ALLOWED.get(cur, set())


class ActiveRequestExists(Exception):
    """The user already has one active request (a season counts as ONE)."""

    def __init__(self, request: dict[str, Any]):
        super().__init__(f"active request #{request['id']}")
        self.request = request


class RequestError(ValueError):
    pass


@dataclass
class NewRequest:
    user_id: int
    kind: str                       # episode | season
    anime_key: str
    title: str
    version: str
    source_url: str
    season: int | None = None
    episode_number: int | None = None
    latest: bool = False            # "dernier épisode": resolved from the listing, never guessed


class RequestManager:
    def __init__(self, conn: sqlite3.Connection, catalog: SourceCatalog, *, now: Callable[[], str] = now_utc,
                 wait_timeout_s: float = DEFAULT_WAIT_TIMEOUT_S, recheck_s: float = DEFAULT_RECHECK_S):
        self.conn, self.catalog, self.now = conn, catalog, now
        self.wait_timeout_s, self.recheck_s = wait_timeout_s, recheck_s

    @classmethod
    def from_config(cls, conn, cfg, catalog: SourceCatalog | None = None, **kw) -> "RequestManager":
        r = getattr(cfg, "requests", None) or {}
        return cls(conn, catalog or SourceCatalog(cfg),
                   wait_timeout_s=float(r.get("wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_S)),
                   recheck_s=float(r.get("recheck_seconds", DEFAULT_RECHECK_S)), **kw)

    # ── users ────────────────────────────────────────────────────────────────────
    def upsert_user(self, telegram_id: int, username: str | None = None) -> None:
        now = self.now()
        self.conn.execute(
            "INSERT INTO users (telegram_id, username, first_seen_at, last_seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
            "username=COALESCE(excluded.username, users.username)", (telegram_id, username, now, now))
        self.conn.commit()

    # ── reads ────────────────────────────────────────────────────────────────────
    def get(self, request_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def active_request(self, user_id: int) -> dict[str, Any] | None:
        marks = ",".join("?" * len(TERMINAL))
        row = self.conn.execute(f"SELECT * FROM requests WHERE user_id=? AND state NOT IN ({marks}) "
                                "ORDER BY id DESC LIMIT 1", (user_id, *TERMINAL)).fetchone()
        return dict(row) if row else None

    def items(self, request_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM request_items WHERE request_id=? ORDER BY episode_number, id", (request_id,)).fetchall()]

    def history(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """/history — straight from the database."""
        rows = self.conn.execute("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                 (user_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            its = self.items(d["id"])
            d["items_total"] = len(its)
            d["items_done"] = sum(1 for i in its if i["state"] == S.COMPLETED.value)
            out.append(d)
        return out

    # ── create (one active request per user) ─────────────────────────────────────
    def create(self, req: NewRequest) -> dict[str, Any]:
        if req.kind not in ("episode", "season"):
            raise RequestError(f"kind inconnu: {req.kind}")
        if req.kind == "episode" and req.episode_number is None and not req.latest:
            raise RequestError("numéro d'épisode ou « dernier épisode » requis")
        existing = self.active_request(req.user_id)
        if existing is not None:
            raise ActiveRequestExists(existing)
        now = self.now()
        try:
            cur = self.conn.execute(
                "INSERT INTO requests (user_id, kind, anime_key, title, season, episode_number, version, source_url, "
                "state, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (req.user_id, req.kind, req.anime_key, req.title, req.season, req.episode_number,
                 media.normalize_version(req.version), req.source_url, S.PENDING.value, now, now,
                 add_seconds(now, self.wait_timeout_s)))
            self.conn.commit()
        except sqlite3.IntegrityError:                     # lost a race: the database enforces the limit
            self.conn.rollback()
            existing = self.active_request(req.user_id)
            if existing is None:
                raise
            raise ActiveRequestExists(existing) from None
        rid = cur.lastrowid
        logger.info("[REQUEST] request=%s user=%s kind=%s anime=%s season=%s episode=%s version=%s",
                    rid, req.user_id, req.kind, req.anime_key, req.season,
                    "latest" if req.latest else req.episode_number, req.version)
        return self.get(rid)

    # ── state helpers ────────────────────────────────────────────────────────────
    def _set(self, rid: int, new: RequestState, *, error_code: str | None = None, error: str | None = None) -> None:
        cur = RequestState(self.get(rid)["state"])
        if not can_transition(cur, new):
            raise RequestError(f"transition interdite: {cur.value} -> {new.value}")
        now = self.now()
        self.conn.execute(
            "UPDATE requests SET state=?, updated_at=?, error_code=COALESCE(?, error_code), "
            "last_error=COALESCE(?, last_error), completed_at=CASE WHEN ? IN ('COMPLETED','CANCELLED','EXPIRED','FAILED') "
            "THEN ? ELSE completed_at END WHERE id=?",
            (new.value, now, error_code, error, new.value, now, rid))
        self.conn.commit()
        if new != cur:
            logger.info("[REQUEST] request=%s %s -> %s%s", rid, cur.value, new.value,
                        f" ({error_code})" if error_code else "")

    def _touch(self, rid: int) -> None:
        self.conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (self.now(), rid))
        self.conn.commit()

    # ── advancing a request ──────────────────────────────────────────────────────
    def process(self, rid: int) -> dict[str, Any]:
        """PENDING -> SEARCHING -> (FOUND -> QUEUED ...) | WAITING_FOR_MEDIA.  Safe to call again."""
        req = self.get(rid)
        if req is None or req["state"] in TERMINAL:
            return req or {}
        if req["state"] == S.PENDING.value:
            self._set(rid, S.SEARCHING)
            req = self.get(rid)
        try:
            listing = self.catalog.episodes(req["source_url"])
        except Exception as exc:                            # transient source problem: stay SEARCHING until the deadline
            code = errors.classify(exc)
            self.conn.execute("UPDATE requests SET error_code=?, last_error=?, updated_at=? WHERE id=?",
                              (code, f"{type(exc).__name__}: {exc}"[:500], self.now(), rid))
            self.conn.commit()
            logger.warning("[REQUEST] request=%s source illisible (%s): %s", rid, code, exc)
            if self._past_deadline(req):
                self._set(rid, S.FAILED, error_code=code)
            return self.get(rid)

        wanted = self._wanted(req, listing)
        if not wanted:
            if req["state"] != S.WAITING_FOR_MEDIA.value:
                self._set(rid, S.WAITING_FOR_MEDIA, error_code=errors.EPISODE_NOT_AVAILABLE,
                          error="épisode pas encore listé par la source")
            elif self._past_deadline(req):
                self._set(rid, S.EXPIRED, error_code=errors.EPISODE_NOT_AVAILABLE)
            else:
                self._touch(rid)
            return self.get(rid)

        if req["state"] in (S.SEARCHING.value, S.WAITING_FOR_MEDIA.value):
            self._set(rid, S.FOUND)
        for ep in wanted:
            self._attach(req, ep)
        self.sync(rid)
        return self.get(rid)

    def _wanted(self, req: dict, listing) -> list:
        if req["kind"] == "season":
            return [e for e in listing if e.number is not None]
        number = req["episode_number"]
        if number is None:                                  # "dernier épisode": the highest number really listed
            numbered = [e for e in listing if e.number is not None]
            if not numbered:
                return []
            latest = max(numbered, key=lambda e: e.number)
            self.conn.execute("UPDATE requests SET episode_number=? WHERE id=?", (latest.number, req["id"]))
            self.conn.commit()
            return [latest]
        return [e for e in listing if e.number == number]

    def _attach(self, req: dict, ep) -> None:
        """Find or create the media and link it to the request (one item per media)."""
        res = media.ensure_media(self.conn, anime_key=req["anime_key"], episode_number=ep.number,
                                 version=req["version"], episode_url=ep.url, episode_key=ep.key, label=ep.label,
                                 origin="user")
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO request_items (request_id, media_key, episode_id, episode_number, state, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (req["id"], res.media_key, res.episode_id, ep.number, S.PENDING.value, now, now))
        self.conn.commit()

    def _past_deadline(self, req: dict) -> bool:
        return bool(req["expires_at"]) and utc_diff_seconds(self.now(), req["expires_at"]) >= 0

    # ── item / request state from the media ──────────────────────────────────────
    def _item_state(self, item: dict) -> RequestState:
        if item["state"] in (S.COMPLETED.value, S.CANCELLED.value):
            return RequestState(item["state"])
        d = self.conn.execute("SELECT status FROM deliveries WHERE request_item_id=?", (item["id"],)).fetchone()
        if d is not None and d["status"] == "sent":
            return S.COMPLETED
        if d is not None and item["state"] == S.FAILED.value:        # the delivery itself failed for good / is uncertain
            return S.FAILED
        ep = repo.get(self.conn, item["episode_id"])
        if ep is None:
            return S.FAILED
        if media.is_deliverable(ep):
            return S.DELIVERING
        ms = media.media_state(ep.status, ep.last_error)
        if ms in (media.MediaState.FAILED, media.MediaState.EXPIRED):
            return S.FAILED
        if ms is media.MediaState.RETRY_WAIT and (ep.last_error or "").startswith(
                (errors.NOT_AVAILABLE_YET, errors.SOURCE_VIDEO_PROCESSING)):
            return S.WAITING_FOR_MEDIA
        if ms in (media.MediaState.DISCOVERED, media.MediaState.QUEUED, media.MediaState.RETRY_WAIT):
            return S.QUEUED
        return S.PROCESSING

    def sync(self, rid: int) -> dict[str, Any]:
        """Recompute item states from their media and derive the parent request's state."""
        req = self.get(rid)
        if req is None or req["state"] in TERMINAL:
            return req or {}
        states: list[RequestState] = []
        for it in self.items(rid):
            st = self._item_state(it)
            if st.value != it["state"]:
                self.conn.execute("UPDATE request_items SET state=?, updated_at=? WHERE id=?",
                                  (st.value, self.now(), it["id"]))
            states.append(st)
        self.conn.commit()
        if not states:
            return self.get(rid)
        vals = set(states)
        if vals <= {S.COMPLETED, S.FAILED, S.CANCELLED}:                  # everything settled
            if S.COMPLETED in vals:
                self._set(rid, S.COMPLETED)
            else:
                code, msg = self._failure_reason(rid)
                self._set(rid, S.FAILED, error_code=code, error=msg)
        elif S.DELIVERING in vals:
            new = S.DELIVERING
            self._set(rid, new)
        elif vals & {S.PROCESSING}:
            self._set(rid, S.PROCESSING)
        elif vals == {S.WAITING_FOR_MEDIA}:
            if self._past_deadline(req):
                self._set(rid, S.EXPIRED, error_code=errors.EPISODE_NOT_AVAILABLE, error="la vidéo n'est pas apparue à temps")
            else:
                self._set(rid, S.WAITING_FOR_MEDIA)
        else:
            self._set(rid, S.QUEUED)
        return self.get(rid)

    def _failure_reason(self, rid: int) -> tuple[str, str]:
        d = self.conn.execute("SELECT status, last_error FROM deliveries WHERE request_id=? AND status IN "
                              "('failed','uncertain') ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        if d is not None and d["status"] == "uncertain":
            return errors.TELEGRAM_ERROR, "livraison incertaine (arrêt pendant l'envoi) — redemandez : pas de nouveau téléchargement"
        if d is not None:
            code = (d["last_error"] or "").split(":", 1)[0].split("/", 1)[0]
            return (code if code in errors.ALL_CODES else errors.TELEGRAM_ERROR), (d["last_error"] or "livraison impossible")
        ep = self.conn.execute("SELECT e.last_error FROM request_items i JOIN episodes e ON e.id=i.episode_id "
                               "WHERE i.request_id=? AND e.last_error IS NOT NULL LIMIT 1", (rid,)).fetchone()
        if ep is not None:
            code = ep["last_error"].split(":", 1)[0]
            return (code if code in errors.ALL_CODES else errors.UNKNOWN_ERROR), ep["last_error"][:300]
        return errors.UNKNOWN_ERROR, "média indisponible"

    # ── the periodic tick (worker loop) ──────────────────────────────────────────
    def tick(self) -> dict[str, list[int]]:
        """Advance every active request.  A waiting request is re-checked at most every `recheck_s` and expires at its deadline."""
        out: dict[str, list[int]] = {"advanced": [], "expired": [], "failed": []}
        marks = ",".join("?" * len(ACTIVE))
        for row in self.conn.execute(f"SELECT id FROM requests WHERE state IN ({marks}) ORDER BY id", ACTIVE).fetchall():
            rid = row["id"]
            req = self.get(rid)
            try:
                if req["state"] in (S.PENDING.value, S.SEARCHING.value):
                    after = self.process(rid)
                elif req["state"] == S.WAITING_FOR_MEDIA.value:
                    if self._past_deadline(req):
                        self._set(rid, S.EXPIRED, error_code=errors.EPISODE_NOT_AVAILABLE)
                        after = self.get(rid)
                    elif self.items(rid):
                        after = self.sync(rid)                    # media exists (video not ready): follow it
                    elif utc_diff_seconds(self.now(), req["updated_at"]) >= self.recheck_s:
                        after = self.process(rid)                 # not listed yet: look again
                    else:
                        continue
                else:
                    after = self.sync(rid)
            except Exception as exc:
                logger.exception("[REQUEST] request=%s tick en erreur: %s", rid, exc)
                out["failed"].append(rid)
                continue
            if after and after["state"] == S.EXPIRED.value:
                out["expired"].append(rid)
            out["advanced"].append(rid)
        return out

    # ── cancel ───────────────────────────────────────────────────────────────────
    def cancel(self, rid: int, *, user_id: int | None = None) -> dict[str, Any]:
        """Cancel a request.  Its media job is cancelled only if it is still waiting in the queue, was created by a
        user request, is not published to a channel, and NO other active request waits for it.  A download already
        running is left to finish (the file stays in the cache; nobody is delivered from this request)."""
        req = self.get(rid)
        if req is None or (user_id is not None and req["user_id"] != user_id):
            return {"ok": False, "message": "demande introuvable"}
        if req["state"] in TERMINAL:
            fr = {"COMPLETED": "terminée", "CANCELLED": "annulée", "EXPIRED": "expirée", "FAILED": "échouée"}
            return {"ok": False, "message": f"Cette demande est déjà {fr.get(req['state'], req['state'].lower())}."}
        items = self.items(rid)
        self._set(rid, S.CANCELLED)
        self.conn.execute("UPDATE request_items SET state=?, updated_at=? WHERE request_id=? AND state<>?",
                          (S.CANCELLED.value, self.now(), rid, S.COMPLETED.value))
        self.conn.commit()
        released, kept = [], []
        for it in items:
            others = self.conn.execute(
                "SELECT COUNT(*) FROM request_items i JOIN requests r ON r.id=i.request_id "
                "WHERE i.episode_id=? AND i.request_id<>? AND r.state NOT IN ('COMPLETED','CANCELLED','EXPIRED','FAILED')",
                (it["episode_id"], rid)).fetchone()[0]
            ep = repo.get(self.conn, it["episode_id"])
            if others == 0 and ep is not None and media.discard_unstarted(self.conn, ep.id):
                released.append(ep.id)
            else:
                kept.append(it["episode_id"])
        logger.info("[REQUEST] request=%s cancelled jobs_cancelled=%s jobs_kept=%s", rid, released, kept)
        return {"ok": True, "request_id": rid, "jobs_cancelled": released, "jobs_kept": kept}
