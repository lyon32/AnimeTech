"""Unit tests: Telegram error classification, caption format, upload verification."""
from __future__ import annotations

from pathlib import Path

import pytest

from telegram import Bot
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

from v1_poc.telegram_client import TelegramClient, TelegramPublishError, caption_for, classify_error


def test_classify_error_kinds():
    assert classify_error(TimedOut()) == "TIMEOUT"
    assert classify_error(NetworkError("boom")) == "NETWORK"
    assert classify_error(Forbidden("Forbidden: bot was blocked by the user")) == "FORBIDDEN"
    assert classify_error(BadRequest("file is too big")) == "FILE_TOO_LARGE"
    assert classify_error(BadRequest("chat not found")) == "CHAT_NOT_FOUND"
    assert classify_error(BadRequest("Unauthorized")) == "UNAUTHORIZED"


def test_caption_format():
    caption = caption_for("Bleach Sennen Kessen-Hen", "48", "VOSTFR", "1920x1080")
    assert "Bleach Sennen Kessen-Hen" in caption
    assert "Épisode 48" in caption
    assert "VOSTFR" in caption
    assert "1920x1080" in caption
    assert "Disponible maintenant" in caption


class _FakeVideo:
    file_id = "FAKE_VIDEO_FILE_ID"
    file_size = 123456
    width = 1920
    height = 1080
    duration = 24.0


class _FakeChat:
    id = -1001234567890


class _FakeMessage:
    message_id = 777
    caption = "c"
    video = _FakeVideo()
    chat = _FakeChat()


def test_verify_upload_recovers_message_metadata(monkeypatch):
    token = "123:FAKE"
    client = TelegramClient(token, "@channel")

    class _F:
        file_size = 123456
        file_path = "/x/file.mp4"

    async def fake_get_file(self, file_id):
        return _F()

    class _C:
        id = -1001234567890
        type = "channel"
        title = "Test Channel"

    async def fake_get_chat(self, chat_id):
        return _C()

    monkeypatch.setattr(Bot, "get_file", fake_get_file)
    monkeypatch.setattr(Bot, "get_chat", fake_get_chat)

    ver = client.verify_upload(_FakeMessage(), local_size=123456, local_sha256="deadbeef" * 8, full_byte_check=False)
    assert ver.message_id == 777
    assert ver.chat_id == "-1001234567890"
    assert ver.video_file_id == "FAKE_VIDEO_FILE_ID"
    assert ver.video_size == 123456
    assert ver.video_width == 1920
    assert ver.channel_title == "Test Channel"
    assert ver.channel_type == "channel"
    assert ver.upload_bytes_match_local is True


def test_send_video_error_wraps_classified_kind(tmp_path, monkeypatch):
    token = "123:FAKE"
    client = TelegramClient(token, "@channel")

    from telegram import Bot

    async def fake_send_video(self, **kwargs):
        raise BadRequest("file is too big")

    monkeypatch.setattr(Bot, "send_video", fake_send_video)
    f = tmp_path / "x.mp4"
    f.write_bytes(b"AAAA")
    with pytest.raises(TelegramPublishError) as exc_info:
        client.send_video(f, caption="x")
    assert exc_info.value.kind == "FILE_TOO_LARGE"