"""Publisher glue tests — no network, no real token."""
from __future__ import annotations

import pytest

from v2_automation.publisher import local_bot_base_url, V2TelegramClient

FAKE_TOKEN = "1234567890:AAtest_token_abcdefghijklmnoXX"


def test_bot_glue_keeps_token_out_of_authority():
    base = local_bot_base_url("http://127.0.0.1:8081/", FAKE_TOKEN)
    assert base == "http://127.0.0.1:8081/bot"
    client = V2TelegramClient(FAKE_TOKEN, "-100x", base_url=base)
    # token never lands in the URL authority (host:port)
    assert FAKE_TOKEN not in base
    assert ":8081/bot" in base


def test_local_bot_base_trailing_slash_normalized():
    assert local_bot_base_url("http://127.0.0.1:8081", FAKE_TOKEN).endswith("/bot")
    assert local_bot_base_url("http://127.0.0.1:8081/", FAKE_TOKEN).endswith("/bot")


def test_client_keeps_raw_token_separate():
    client = V2TelegramClient(FAKE_TOKEN, "-100x", base_url="http://127.0.0.1:8081/bot")
    assert client.token == FAKE_TOKEN and client.base_url == "http://127.0.0.1:8081/bot"

def _local_client(monkeypatch, sent, fail=None):
    from v2_automation import publisher as pb
    calls = []
    monkeypatch.setattr(pb.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or None)

    class Bot:
        async def send_video(self, **kw):
            sent.update(kw)
            if fail:
                raise fail
            return "MSG"

    c = pb.V2TelegramClient("SECRET:TOKEN", "-100", base_url="http://127.0.0.1:8081/bot")
    c.local_container = "srv"
    c._cached_bot = Bot()
    return c, calls


def test_local_send_uses_file_uri_and_removes_the_copy_on_success(monkeypatch):
    from pathlib import Path
    sent = {}
    c, calls = _local_client(monkeypatch, sent)
    assert c.send_video(Path("/x/a.mp4"), caption="🎬 t") == "MSG"
    assert sent["video"] == "file:///data/v2_upload/a.mp4" and sent["caption"] == "🎬 t"
    assert any(cmd[:3] == ["docker", "cp", "/x/a.mp4"] or cmd[1] == "cp" for cmd in calls)
    assert calls[-1][:4] == ["docker", "exec", "srv", "rm"]           # copy removed after success


def test_local_send_keeps_the_copy_when_the_connection_drops(monkeypatch):
    import httpx
    from pathlib import Path
    from v1_poc.telegram_client import TelegramPublishError
    sent = {}
    c, calls = _local_client(monkeypatch, sent, fail=httpx.RemoteProtocolError("Server disconnected"))
    with pytest.raises(TelegramPublishError):
        c.send_video(Path("/x/a.mp4"), caption="c")
    assert not any(cmd[:4] == ["docker", "exec", "srv", "rm"] for cmd in calls)   # server may still be uploading


def test_local_send_removes_the_copy_on_a_real_rejection(monkeypatch):
    from pathlib import Path
    from v1_poc.telegram_client import TelegramPublishError
    sent = {}
    c, calls = _local_client(monkeypatch, sent, fail=ValueError("bad request"))
    with pytest.raises(TelegramPublishError):
        c.send_video(Path("/x/a.mp4"))
    assert calls[-1][:4] == ["docker", "exec", "srv", "rm"]


def test_connection_lost_detection_walks_the_cause_chain():
    import httpx
    from v2_automation.publisher import connection_lost
    inner = httpx.RemoteProtocolError("x")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert connection_lost(outer) and not connection_lost(ValueError("plain"))
    loop = RuntimeError("a")
    loop.__cause__ = loop                       # cyclic chain must not hang
    assert not connection_lost(loop)
