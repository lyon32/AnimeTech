"""Telegram publication via python-telegram-bot (real sendMessage / sendVideo)."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from telegram import Bot, Message
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.request import HTTPXRequest


class TelegramPublishError(RuntimeError):
    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def classify_error(exc: Exception) -> str:
    if isinstance(exc, TimedOut):
        return "TIMEOUT"
    if isinstance(exc, BadRequest):
        text = str(exc).lower()
        if "too large" in text or "too big" in text or ("file" in text and "size" in text):
            return "FILE_TOO_LARGE"
        if "chat not found" in text or "channel not found" in text:
            return "CHAT_NOT_FOUND"
        if "unauthorized" in text or "token" in text:
            return "UNAUTHORIZED"
        return "BAD_REQUEST"
    if isinstance(exc, Forbidden):
        return "FORBIDDEN"
    if isinstance(exc, NetworkError):
        return "NETWORK"
    if isinstance(exc, TelegramError):
        return "TELEGRAM_ERROR"
    return "OTHER"


@dataclass
class TelegramVerification:
    channel_title: str | None = None
    channel_type: str | None = None
    message_id: int | None = None
    chat_id: str | None = None
    caption: str | None = None
    video_file_id: str | None = None
    video_size: int | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_duration: float | None = None
    telegram_file_size: int | None = None
    upload_bytes_match_local: bool | None = None
    upload_sha256_match_local: bool | None = None


class TelegramClient:
    def __init__(
        self,
        token: str,
        channel_id: str,
        base_url: str | None = None,
        *,
        connect_timeout: float = 30.0,
        read_timeout: float = 900.0,
        write_timeout: float = 900.0,
    ) -> None:
        self.token = token
        self.channel_id = channel_id
        self.base_url = base_url
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self._cached_bot: Bot | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bot(self) -> Bot:
        if self._cached_bot is None:
            request = HTTPXRequest(
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout,
                write_timeout=self.write_timeout,
                pool_timeout=30.0,
                media_write_timeout=self.write_timeout,
            )
            kwargs = {"token": self.token, "request": request}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._cached_bot = Bot(**kwargs)
        return self._cached_bot

    def _run(self, coro):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        if self._cached_bot is not None:
            req = getattr(self._cached_bot, "request", None)
            if req is not None and self._loop is not None and not self._loop.is_closed():
                try:
                    self._loop.run_until_complete(req.shutdown())
                except Exception:
                    pass
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
        self._loop = None
        self._cached_bot = None

    def get_me(self) -> dict:
        async def _impl():
            me = await self._bot().get_me()
            return {"id": me.id, "username": me.username, "is_bot": me.is_bot}

        return self._run(_impl())

    def get_chat(self) -> dict:
        async def _impl():
            chat = await self._bot().get_chat(self.channel_id)
            return {"id": str(chat.id), "type": chat.type, "title": chat.title}

        return self._run(_impl())

    def send_message(self, text: str) -> Message:
        async def _impl():
            return await self._bot().send_message(chat_id=self.channel_id, text=text)

        try:
            return self._run(_impl())
        except Exception as exc:  # classified below
            raise TelegramPublishError(f"sendMessage failed: {exc}", classify_error(exc)) from exc

    def send_video(self, path: Path, caption: str | None = None) -> Message:
        async def _impl():
            kwargs = {"chat_id": self.channel_id, "video": open(path, "rb"), "supports_streaming": True}
            if caption:
                kwargs["caption"] = caption
            fh = kwargs["video"]
            try:
                return await self._bot().send_video(**kwargs)
            finally:
                fh.close()

        try:
            return self._run(_impl())
        except Exception as exc:  # classified below
            raise TelegramPublishError(f"sendVideo failed: {exc}", classify_error(exc)) from exc

    def verify_upload(
        self,
        message: Message,
        local_size: int,
        local_sha256: str,
        *,
        full_byte_check: bool = True,
    ) -> TelegramVerification:
        """Recovers the placed message and, when possible, re-downloads the file
        from Telegram's servers to compare size/hash with the local validated file."""
        video = getattr(message, "video", None)
        chat = getattr(message, "chat", None)

        v = TelegramVerification(
            message_id=message.message_id,
            chat_id=str(chat.id) if chat else None,
            caption=message.caption,
            video_file_id=video.file_id if video else None,
            video_size=video.file_size if video else None,
            video_width=video.width if video else None,
            video_height=video.height if video else None,
            video_duration=video.duration if video else None,
        )

        if not video:
            return v

        async def _impl():
            try:
                chat_obj = await self._bot().get_chat(self.channel_id)
                v.channel_title = getattr(chat_obj, "title", None)
                v.channel_type = getattr(chat_obj, "type", None)
            except TelegramError:
                pass
            f = await self._bot().get_file(video.file_id)
            v.telegram_file_size = f.file_size
            v.upload_bytes_match_local = (local_size == f.file_size) if f.file_size is not None else None
            if full_byte_check and f.file_size is not None:
                data = await f.download_as_bytearray()
                dig = hashlib.sha256(data).hexdigest()
                v.upload_sha256_match_local = (dig == local_sha256)
            return v

        return self._run(_impl())


def caption_for(anime: str, episode: str, language: str, resolution: str) -> str:
    return (
        f"🎬 {anime}\n"
        f"📺 Épisode {episode}\n"
        f"🎙️ {language}\n"
        f"🎞️ {resolution}\n"
        "━━━━━━━━━━━━━━\n"
        "📥 Disponible maintenant"
    )