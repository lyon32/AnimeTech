"""Publication for V2 — thumbnail message THEN video message.

Subclasses the proven `v1_poc.telegram_client.TelegramClient` (never edited)
and adds the photo-path used for the thumbnail message.  Every send is wrapped
in TelegramPublishError with a classified kind so the state machine can decide
RETRY_WAIT vs FAILED.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot, Message
from telegram.request import HTTPXRequest

from v1_poc.telegram_client import TelegramClient, TelegramPublishError, classify_error

THUMB_DEFAULT_WIDTH = 640


def local_bot_base_url(api_base: str, token: str) -> str:
    """telegram 22.x concatène `base_url` + `token` (aucun `/` ajouté).

    Local Bot API : le bon « base_url » à passer au Bot est donc
    `{api_base}/bot` → requêtes sur `.../bot<TOKEN>/<method>`.
    Documenté ici pour éviter que le token ne glisse dans l'autorité
    (host:port) de l'URL et fuite dans une trace d'exception.
    """
    return f"{api_base.rstrip('/')}/bot"


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _duration_from_ffmpeg(video_path: Path, ffmpeg_bin: Path, timeout_seconds: int) -> float | None:
    """Duration read from `ffmpeg -i` stderr (no ffprobe needed); None if unreadable."""
    proc = subprocess.run([str(ffmpeg_bin), "-hide_banner", "-i", str(video_path)],
                          capture_output=True, text=True, timeout=timeout_seconds)
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return None
    h, mi, sec = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(sec) or None


def extract_thumbnail(
    video_path: Path,
    out_jpg: Path,
    ffmpeg_bin: Path,
    *,
    seek_frac: float = 0.1,
    width: int = THUMB_DEFAULT_WIDTH,
    timeout_seconds: int = 120,
) -> Path:
    """Extracts one representative frame (still image) from the video.

    Pure ffmpeg: `-ss t -i in -frames:v 1 -vf scale=w:-1 -q:v 4 out.jpg`.
    Returns out_jpg. Raises CalledProcessError on failure.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")
    duration = _duration_from_ffmpeg(video_path, ffmpeg_bin, timeout_seconds)
    seek = max(0.0, (duration or 60) * seek_frac) if duration is not None else seek_frac
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_bin), "-y", "-loglevel", "error",
        "-ss", f"{seek:.2f}", "-i", str(video_path),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
        str(out_jpg),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    if proc.returncode != 0 or not out_jpg.exists() or out_jpg.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg thumbnail failed rc={proc.returncode}: {proc.stderr[-500:]}")
    return out_jpg


def connection_lost(exc: BaseException) -> bool:
    """True when the connection dropped while a request was in flight (the outcome is unknown:
    the server may have finished the upload).  Walks the whole cause chain."""
    import httpx
    from telegram.error import TimedOut
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout, TimedOut)):
            return True
        exc = exc.__cause__
    return False


class V2TelegramClient(TelegramClient):
    # When set (name of the Local Bot API docker container), videos are copied into the
    # server's own disk and sent by `file://` path: the multipart path crashes the local
    # server on large files (see V4_FINAL_CAPACITY_REPORT.md; file:// proven to 1800 MiB).
    # The server image must carry the IDLE_TIMEOUT patch (v2_local_bot_api_poc/Dockerfile): upstream
    # closes the connection 500 s into a big upload; wait_for_message() is the fallback when it does.
    local_container: str | None = None
    UPLOAD_DIR = "/data/v2_upload"

    def wait_for_message(self, candidate_ids, caption: str, *, timeout_s: float = 1800,
                         poll_s: float = 20) -> int | None:
        """After a dropped connection the Bot API server keeps uploading and the message shows up
        minutes later.  Finds it WITHOUT re-sending: re-applying our exact caption to a candidate id
        answers "message is not modified" iff that message exists with that caption (non-destructive)."""
        import time
        deadline = time.monotonic() + timeout_s
        while True:
            for mid in candidate_ids:
                async def _probe(mid=mid):
                    try:
                        await self._bot().edit_message_caption(self.channel_id, mid, caption=caption)
                        return True                      # it existed and we (idempotently) set our caption
                    except Exception as exc:
                        return "not modified" in str(exc).lower()
                if self._run(_probe()):
                    return mid
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_s)

    def _bot(self) -> Bot:
        if self._cached_bot is None:
            request = HTTPXRequest(connect_timeout=self.connect_timeout, read_timeout=self.read_timeout,
                                   write_timeout=self.write_timeout, pool_timeout=30.0,
                                   media_write_timeout=self.write_timeout)
            kwargs = {"token": self.token, "request": request}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            if self.local_container:
                kwargs["local_mode"] = True      # required by PTB to pass file:// URIs
            self._cached_bot = Bot(**kwargs)
        return self._cached_bot

    def send_video(self, path: Path, caption: str | None = None) -> Message:
        if not self.local_container:
            return super().send_video(path, caption)
        remote = f"{self.UPLOAD_DIR}/{path.name}"
        try:
            for cmd in (["docker", "exec", self.local_container, "mkdir", "-p", self.UPLOAD_DIR],
                        ["docker", "cp", str(path), f"{self.local_container}:{remote}"]):
                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1800)

            async def _impl():
                kwargs = {"chat_id": self.channel_id, "video": f"file://{remote}",
                          "supports_streaming": True}
                if caption:
                    kwargs["caption"] = caption
                return await self._bot().send_video(**kwargs)

            msg = self._run(_impl())
        except Exception as exc:
            if not connection_lost(exc):      # on a lost connection keep the copy: the server may still be uploading
                self.cleanup_remote(path)
            raise TelegramPublishError(f"sendVideo failed: {exc}", classify_error(exc)) from exc
        self.cleanup_remote(path)
        return msg

    def cleanup_remote(self, path: Path) -> None:
        if self.local_container:
            subprocess.run(["docker", "exec", self.local_container, "rm", "-f",
                            f"{self.UPLOAD_DIR}/{Path(path).name}"], capture_output=True, timeout=120)

    def send_photo(self, path: Path, caption: str | None = None) -> Message:
        async def _impl():
            kwargs = {"chat_id": self.channel_id, "photo": open(path, "rb")}
            if caption:
                kwargs["caption"] = caption
            fh = kwargs["photo"]
            try:
                return await self._bot().send_photo(**kwargs)
            finally:
                fh.close()

        try:
            return self._run(_impl())
        except Exception as exc:
            raise TelegramPublishError(f"sendPhoto failed: {exc}", classify_error(exc)) from exc


@dataclass
class PublishedMediaResult:
    thumbnail_message_id: int | None
    thumbnail_file_id: str | None
    video_message_id: int | None
    video_file_id: str | None
    video_size: int | None


class Publisher:
    """Sequences thumbnail -> video and records both message ids."""

    def __init__(self, client: V2TelegramClient, *, thumbnail_caption: str | None = None,
                 video_caption_fn=None):
        self.client = client
        self.thumbnail_caption = thumbnail_caption
        self.video_caption_fn = video_caption_fn or (lambda: None)

    def publish_thumbnail(self, image_path: Path) -> tuple[int, str | None]:
        msg = self.client.send_photo(image_path, caption=self.thumbnail_caption)
        photo = getattr(msg, "photo", None)
        file_id = photo[-1].file_id if photo else None
        return msg.message_id, file_id

    def publish_video(self, video_path: Path, caption: str | None) -> Message:
        return self.client.send_video(video_path, caption=caption)

    def close(self) -> None:
        self.client.close()