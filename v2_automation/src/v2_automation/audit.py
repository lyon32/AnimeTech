"""Audit log of every administrative action (Telegram admin and web panel).

One chokepoint: the mutating functions of `service.py` are wrapped with `audited(...)`.  The surface (Telegram / web) sets
WHO acts through `acting_as(...)`; a call made by the system itself (the worker, recovery) has no actor and is not logged
as an admin action.  Nothing secret is ever written: the metadata is the action's own result message.

    ADMIN 12345 · ACTION retry · TARGET media_abc · RESULT success
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable

from .timeutil import now_utc

logger = logging.getLogger(__name__)

_actor: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("audit_actor", default=None)


@contextmanager
def acting_as(surface: str, admin_id: str | int):
    tok = _actor.set((surface, str(admin_id)))
    try:
        yield
    finally:
        _actor.reset(tok)


def current_actor() -> tuple[str, str] | None:
    return _actor.get()


def record(conn: sqlite3.Connection, *, admin_id: str | int, surface: str, action: str, target: Any = None,
           result: str = "success", metadata: dict | None = None) -> int:
    meta = json.dumps(metadata, ensure_ascii=False, default=str)[:2000] if metadata else None
    cur = conn.execute("INSERT INTO audit_log (ts, admin_user_id, surface, action, target, result, metadata) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (now_utc(), str(admin_id), surface, action, None if target is None else str(target)[:200], result, meta))
    conn.commit()
    logger.info("[AUDIT] ADMIN %s ACTION %s TARGET %s RESULT %s (%s)", admin_id, action, target, result, surface)
    return cur.lastrowid


def recent(conn: sqlite3.Connection, limit: int = 100, *, action: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM audit_log", []
    if action:
        q += " WHERE action=?"
        args.append(action)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def audited(action: str) -> Callable:
    """Wrap a `service` function `fn(conn, target, ...) -> dict`.  Logged only when an admin surface set the actor."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(conn, *a, **kw):
            try:
                res = fn(conn, *a, **kw)
            except Exception as exc:
                who = _actor.get()
                if who:
                    record(conn, admin_id=who[1], surface=who[0], action=action, target=a[0] if a else None,
                           result=f"error: {type(exc).__name__}", metadata={"error": str(exc)[:200]})
                raise
            who = _actor.get()
            if who:
                ok = res.get("ok", True) if isinstance(res, dict) else True
                record(conn, admin_id=who[1], surface=who[0], action=action,
                       target=a[0] if a and not isinstance(a[0], (dict, list)) else kw.get("anime_key") or kw.get("episode_id"),
                       result=("unconfirmed" if isinstance(res, dict) and res.get("needs_confirmation")
                               else "success" if ok else "failure"),
                       metadata={k: v for k, v in (res or {}).items() if k in ("message", "status", "episode_id", "request_id")}
                       if isinstance(res, dict) else None)
            return res
        wrapper.__wrapped_unaudited__ = fn
        return wrapper
    return deco
