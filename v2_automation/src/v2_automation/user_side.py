"""The user side of the worker: requests, private deliveries, notifications, extra channels, and the user bot's polling.

Runs INSIDE the existing single worker (same lease, same database, same download engine): the worker loop calls `tick()`,
and the bot's long-poll runs in a daemon thread of the same process.  Two Telegram transports are used on purpose — the
long-poll holds its own connection for up to 30 s and must never delay a delivery.
"""
from __future__ import annotations

import logging
import threading

from .channels import fanout, recover_fanout
from .delivery import DeliveryEngine
from .membership import MembershipService
from .requests_mgr import RequestManager
from .search import SourceSearch
from .telegram_publisher import TelegramPublisher, transport_from_config
from .user_bot import TelegramOutbox, UserBotRouter, run_user_bot

logger = logging.getLogger(__name__)


class UserSide:
    def __init__(self, cfg, conn, *, transport=None, channel_transport=None, catalog=None):
        self.cfg, self.conn = cfg, conn
        self.transport = transport or transport_from_config(cfg, cfg.user_bot_token)       # worker thread (deliveries, notices)
        self.mgr = RequestManager.from_config(conn, cfg, catalog)
        self.engine = DeliveryEngine(conn, TelegramPublisher(self.transport, channels=cfg.channels), cfg)
        self.notifier = UserBotRouter(conn, cfg, TelegramOutbox(self.transport), self.mgr, SourceSearch(cfg, conn),
                                      MembershipService(conn, self.transport.member_status, cfg.required_channels))
        # extra channels are published by the channel bot (the one that already posts to the primary channel)
        self.channel_pub = None
        if len(cfg.channels or []) > 1 or channel_transport is not None:
            ct = channel_transport or (transport_from_config(cfg, cfg.bot_token) if cfg.bot_token else None)
            self.channel_pub = TelegramPublisher(ct, channels=cfg.channels) if ct is not None else None
        self._thread: threading.Thread | None = None

    def recover(self) -> None:
        recover_fanout(self.conn)                                  # deliveries are recovered by recovery.run_recovery

    def tick(self) -> dict:
        """One pass: advance requests, deliver, sync, notify, fan out.  A failing part never stops the others."""
        out: dict = {}
        for name, step in (("requests", self.mgr.tick), ("delivery", self._deliver), ("notify", self.notifier.notify_changes),
                           ("channels", self._fanout)):
            try:
                out[name] = step()
            except Exception as exc:
                logger.exception("[USERSIDE] %s en erreur: %s", name, exc)
                out[name] = f"error: {type(exc).__name__}"
        return out

    def _deliver(self):
        res = self.engine.run()
        for rid in res["requests"]:
            self.mgr.sync(rid)
        return {k: v for k, v in res.items() if k != "requests"}

    def _fanout(self):
        return fanout(self.conn, self.channel_pub, self.cfg) if self.channel_pub is not None else {}

    def start_bot(self, stop: threading.Event) -> None:
        if not self.cfg.user_bot_token:
            return
        self._thread = threading.Thread(target=run_user_bot, args=(self.cfg, stop), kwargs={"conn": self.conn},
                                        name="user-bot", daemon=True)
        self._thread.start()

    def close(self) -> None:
        for t in (self.transport, getattr(self.channel_pub, "transport", None)):
            try:
                if t is not None:
                    t.close()
            except Exception:
                pass
