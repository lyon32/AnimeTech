"""Channel membership gate for the user bot.

The user must belong to AT LEAST ONE of `user_bot.required_channels` (config / REQUIRED_CHANNELS — never hard-coded).
Membership is read from Telegram at EVERY interaction that needs access (nothing is cached): someone who left the
channel is refused on their next message, and is let back in as soon as the check says they joined again.

A check that cannot be done (the bot is not an admin of the channel, Telegram unreachable) is NOT a yes: access is
refused with a distinct reason so the operator can fix it.  An empty list of required channels means "open access".
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from .telegram_publisher import scrub
from .timeutil import now_utc

logger = logging.getLogger(__name__)

MEMBER_STATUSES = ("creator", "administrator", "member")


@dataclass
class Access:
    ok: bool
    status: str                                  # granted | not_member | check_failed | open
    channels: dict[str, str] = field(default_factory=dict)      # channel -> telegram status or "error"
    missing: list[str] = field(default_factory=list)
    error: str | None = None


class MembershipService:
    def __init__(self, conn: sqlite3.Connection, member_status: Callable[[str, int], str], required: list[str], *,
                 now: Callable[[], str] = now_utc):
        self.conn, self._status, self.required, self.now = conn, member_status, list(required), now

    def check(self, user_id: int) -> Access:
        if not self.required:
            return self._record(user_id, Access(True, "open"))
        statuses: dict[str, str] = {}
        errors: list[str] = []
        for chan in self.required:
            try:
                statuses[chan] = self._status(chan, user_id)
            except Exception as exc:                                  # cannot tell: never a silent "yes"
                statuses[chan] = "error"
                errors.append(f"{chan}: {scrub(exc)}"[:200])
                logger.warning("[MEMBERSHIP] user=%s channel=%s vérification impossible: %s", user_id, chan, scrub(exc))
        if any(s in MEMBER_STATUSES for s in statuses.values()):
            return self._record(user_id, Access(True, "granted", statuses))
        if errors and all(s == "error" for s in statuses.values()):
            return self._record(user_id, Access(False, "check_failed", statuses, list(self.required), "; ".join(errors)))
        return self._record(user_id, Access(False, "not_member", statuses, list(self.required)))

    def _record(self, user_id: int, a: Access) -> Access:
        self.conn.execute("UPDATE users SET access_status=?, access_checked_at=? WHERE telegram_id=?",
                          (a.status, self.now(), user_id))
        self.conn.commit()
        logger.info("[MEMBERSHIP] user=%s access=%s channels=%s", user_id, a.status, a.channels)
        return a


def join_url(channel: str) -> str | None:
    """https://t.me/name for a public @name channel; None for a numeric id (no public link)."""
    c = channel.strip()
    if c.startswith("@"):
        return f"https://t.me/{c[1:]}"
    if c.startswith("https://t.me/"):
        return c
    return None
