"""Phase 12 — token-safe logging + publication evidence."""
from __future__ import annotations

import io
import logging
from pathlib import Path

from v2_automation import db, evidence, repo
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.logsetup import configure
from v2_automation.models import Episode

TOKEN = "1234567890:AAtest_token_abcdefghijklmno"
_FMT = {'bot_capacity': BotCapacity(True, True, "t", 10**12, None, None, 200, None)}


def _cfg(tmp_path, bot_token=TOKEN):
    return AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={},
                     limits={}, monitoring={}, logging={"log_file": {"dir": str(tmp_path)}},
                     bot_token=bot_token, channel_id="", admin_telegram_ids=[], **_FMT)


def _reset_logger():
    root = logging.getLogger("v2_automation")
    root.handlers.clear()
    root.filters.clear()
    root.setLevel(logging.NOTSET)
    if hasattr(root, "_v2_configured"):
        del root._v2_configured


def test_log_file_created(tmp_path):
    _reset_logger()
    configure(_cfg(tmp_path))
    assert (tmp_path / "v2_automation.log").exists()


def test_token_never_lands_in_log(tmp_path):
    _reset_logger()
    configure(_cfg(tmp_path))
    root = logging.getLogger("v2_automation")
    root.info("connect with token %s and channel drop", TOKEN)
    for h in root.handlers:
        h.flush()
    text = (tmp_path / "v2_automation.log").read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "***TOKEN***" in text


def test_record_publication_writes_no_secret(tmp_path, monkeypatch):
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    ep = Episode(anime_key="anime-a", episode_key="anime-a-1",
                 canonical_episode_url="https://voir-anime.to/anime/a/e1",
                 episode_number=1, status="published")
    eid, _ = repo.upsert_episode(c, ep)
    c.commit()
    payload = {"episode_id": eid, "anime_key": "anime-a", "episode_number": 1,
               "label": "anime-a-1", "thumbnail_message_id": 55, "video_message_id": 77,
               "video_sha256": "a" * 64, "video_file_size": 123, "channel_id": "chan"}
    out = evidence.record_publication(payload)
    assert out is not None and out.exists()
    txt = out.read_text(encoding="utf-8")
    assert '"video_message_id": 77' in txt and '"video_sha256": "aaaa' in txt
    assert "bot_token" not in txt