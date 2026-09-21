"""TelegramPublisher — what the media engine talks to.  The engine never knows which Bot API it is using.

    TelegramPublisher
        |-- BotAPITransport        standard Bot API (api.telegram.org)        uploads <= 50 MB
        `-- LocalBotAPITransport   Local Bot API server (`telegram.api_base_url`) big files, `file://` path

`send_video(chat_id, media)` is the same call for both; the transport decides how the bytes travel.
`media` is a local Path, or a Telegram `file_id` string (reuse of an already-uploaded media by the SAME bot).

EXISTING BEHAVIOR  `publisher.V2TelegramClient` sends to ONE channel and is welded to the Local Bot API container.
GAP                no private send, no copy between chats, no membership check, no transport switch, one channel.
CHANGE             this module.  The proven channel publication (`DownloadManager._publish`) is left untouched; the
                   user side and any additional channel go through here.

Secrets: the token only lives in the `Bot` object; every error message is scrubbed of it before it is raised/logged.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from telegram import Bot
from telegram.request import HTTPXRequest

from v1_poc.telegram_client import TelegramPublishError, classify_error

logger = logging.getLogger(__name__)

STANDARD_UPLOAD_LIMIT = 50 * 1024 * 1024          # documented Bot API upload limit (standard server)
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def scrub(text: str) -> str:
    """Never let a bot token appear in an error message or a log line."""
    return _TOKEN_RE.sub("<token>", str(text))


@dataclass
class Sent:
    message_id: int
    chat_id: str
    file_id: str | None = None
    file_size: int | None = None


class Transport(Protocol):
    name: str

    def send_text(self, chat_id: int | str, text: str, keyboard: Any = None, *, html: bool = False) -> Sent: ...
    def send_video(self, chat_id: int | str, media: Path | str, caption: str | None = None) -> Sent: ...
    def copy_message(self, chat_id: int | str, from_chat_id: int | str, message_id: int) -> Sent: ...
    def member_status(self, chat_id: int | str, user_id: int) -> str: ...
    def get_me(self) -> dict: ...
    def close(self) -> None: ...


class BotAPITransport:
    """Standard Bot API."""
    name = "bot_api"
    max_upload_bytes: int | None = STANDARD_UPLOAD_LIMIT

    def __init__(self, token: str, *, base_url: str | None = None, read_timeout: float = 900.0,
                 write_timeout: float = 900.0, connect_timeout: float = 30.0):
        self._token, self._base_url = token, base_url
        self._timeouts = (connect_timeout, read_timeout, write_timeout)
        self._bot_obj: Bot | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()                  # one loop, one call at a time (the engine and the bot share it)

    # -- plumbing ------------------------------------------------------------------
    def _make_bot(self) -> Bot:
        c, r, w = self._timeouts
        request = HTTPXRequest(connect_timeout=c, read_timeout=r, write_timeout=w, pool_timeout=30.0,
                               media_write_timeout=w)
        kwargs: dict[str, Any] = {"token": self._token, "request": request}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if getattr(self, "_local_mode", False):
            kwargs["local_mode"] = True
        return Bot(**kwargs)

    def bot(self) -> Bot:
        with self._lock:
            if self._bot_obj is None:
                self._bot_obj = self._make_bot()
            return self._bot_obj

    def run(self, coro):
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            return self._loop.run_until_complete(coro)

    def close(self) -> None:
        with self._lock:
            if self._bot_obj is not None and self._loop is not None and not self._loop.is_closed():
                try:
                    self._loop.run_until_complete(self._bot_obj.request.shutdown())
                except Exception:
                    pass
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
            self._loop, self._bot_obj = None, None

    def _wrap(self, what: str, exc: Exception) -> TelegramPublishError:
        return TelegramPublishError(f"{what} failed: {scrub(exc)}", classify_error(exc))

    # -- operations ----------------------------------------------------------------
    def get_me(self) -> dict:
        async def _impl():
            me = await self.bot().get_me()
            return {"id": me.id, "username": me.username}
        try:
            return self.run(_impl())
        except Exception as exc:
            raise self._wrap("getMe", exc) from None

    def send_text(self, chat_id, text, keyboard=None, *, html: bool = False) -> Sent:
        async def _impl():
            return await self.bot().send_message(chat_id=chat_id, text=text, reply_markup=keyboard,
                                                 parse_mode="HTML" if html else None)
        try:
            m = self.run(_impl())
        except Exception as exc:
            raise self._wrap("sendMessage", exc) from None
        return Sent(m.message_id, str(m.chat.id))

    def send_video(self, chat_id, media, caption=None) -> Sent:
        if isinstance(media, Path):
            size = media.stat().st_size
            if self.max_upload_bytes is not None and size > self.max_upload_bytes:
                raise TelegramPublishError(
                    f"sendVideo refused: {size} bytes > {self.max_upload_bytes} (limite de l'API standard)", "FILE_TOO_LARGE")
        try:
            msg = self.run(self._send_video(chat_id, media, caption))
        except Exception as exc:
            self._after_failed_upload(media, exc)
            raise self._wrap("sendVideo", exc) from None
        self._after_upload(media)
        video = getattr(msg, "video", None)
        return Sent(msg.message_id, str(msg.chat.id), getattr(video, "file_id", None), getattr(video, "file_size", None))

    async def _send_video(self, chat_id, media, caption):
        kwargs: dict[str, Any] = {"chat_id": chat_id, "supports_streaming": True}
        if caption:
            kwargs["caption"] = caption
        if isinstance(media, Path):
            with media.open("rb") as fh:
                return await self.bot().send_video(video=fh, **kwargs)
        return await self.bot().send_video(video=media, **kwargs)          # a file_id

    def _after_upload(self, media) -> None: ...
    def _after_failed_upload(self, media, exc) -> None: ...

    def copy_message(self, chat_id, from_chat_id, message_id) -> Sent:
        async def _impl():
            return await self.bot().copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)
        try:
            m = self.run(_impl())
        except Exception as exc:
            raise self._wrap("copyMessage", exc) from None
        return Sent(m.message_id, str(chat_id))

    def member_status(self, chat_id, user_id) -> str:
        """creator | administrator | member | left | kicked  (raises if the bot cannot see the chat).  A restricted user
        counts as a member only while still in the chat."""
        async def _impl():
            return await self.bot().get_chat_member(chat_id=chat_id, user_id=user_id)
        try:
            m = self.run(_impl())
        except Exception as exc:
            raise self._wrap("getChatMember", exc) from None
        status = str(m.status)
        if status == "restricted":
            return "member" if getattr(m, "is_member", False) else "left"
        return status


class LocalBotAPITransport(BotAPITransport):
    """Local Bot API server: no 50 MB ceiling; when `upload_container` is set the file is copied into the server's
    own disk and sent by `file://` path (the multipart path crashes the server on big files — see the V4/V5 reports)."""
    name = "local_bot_api"
    max_upload_bytes = None
    UPLOAD_DIR = "/data/v2_upload"

    def __init__(self, token: str, api_base_url: str, *, upload_container: str | None = None, **kw):
        super().__init__(token, base_url=f"{api_base_url.rstrip('/')}/bot", **kw)
        self.upload_container = upload_container or None
        self._local_mode = True

    def _remote(self, media: Path) -> str:
        return f"{self.UPLOAD_DIR}/{media.name}"

    async def _send_video(self, chat_id, media, caption):
        if isinstance(media, Path) and self.upload_container:
            for cmd in (["docker", "exec", self.upload_container, "mkdir", "-p", self.UPLOAD_DIR],
                        ["docker", "cp", str(media), f"{self.upload_container}:{self._remote(media)}"]):
                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1800)
            kwargs: dict[str, Any] = {"chat_id": chat_id, "supports_streaming": True}
            if caption:
                kwargs["caption"] = caption
            return await self.bot().send_video(video=f"file://{self._remote(media)}", **kwargs)
        return await super()._send_video(chat_id, media, caption)

    def _after_upload(self, media) -> None:
        self._cleanup_remote(media)

    def _after_failed_upload(self, media, exc) -> None:
        from .publisher import connection_lost
        if not connection_lost(exc):               # on a lost connection keep the copy: the server may still be uploading
            self._cleanup_remote(media)

    def _cleanup_remote(self, media) -> None:
        if isinstance(media, Path) and self.upload_container:
            subprocess.run(["docker", "exec", self.upload_container, "rm", "-f", self._remote(media)],
                           capture_output=True, timeout=120)


class TelegramPublisher:
    """The single Telegram entry point of the user side and of extra channels."""

    def __init__(self, transport: Transport, *, channels: list[str] | None = None):
        self.transport = transport
        self.channels = list(channels or [])

    @property
    def transport_name(self) -> str:
        return getattr(self.transport, "name", type(self.transport).__name__)

    def send_video(self, chat_id, media: Path | str, caption: str | None = None) -> Sent:
        """Same call for both Bot APIs: `send_video(media)`."""
        return self.transport.send_video(chat_id, media, caption)

    def copy(self, chat_id, from_chat_id, message_id) -> Sent:
        return self.transport.copy_message(chat_id, from_chat_id, message_id)

    def send_text(self, chat_id, text, keyboard=None, *, html: bool = False) -> Sent:
        return self.transport.send_text(chat_id, text, keyboard, html=html)

    def membership(self, user_id: int, channels: list[str]) -> dict[str, str]:
        return {c: self.transport.member_status(c, user_id) for c in channels}

    def close(self) -> None:
        self.transport.close()


def transport_from_config(cfg, token: str) -> Transport:
    """Local Bot API when `telegram.api_base_url` is configured, the standard Bot API otherwise."""
    base = (cfg.telegram or {}).get("api_base_url") or ""
    timeout = float((cfg.telegram or {}).get("upload_timeout_seconds", 600))
    if base:
        return LocalBotAPITransport(token, base, upload_container=(cfg.telegram or {}).get("local_upload_container"),
                                    read_timeout=timeout, write_timeout=timeout)
    return BotAPITransport(token, read_timeout=timeout, write_timeout=timeout)
