"""Multi-channel publication + configuration (channels, required channels and options come from config/env, never code)."""
import sqlite3

import pytest
from test_delivery import FakeTransport
from v1_poc.telegram_client import TelegramPublishError

from v2_automation import app_config, db, repo
from v2_automation.channels import extra_channels, fanout, recover_fanout
from v2_automation.models import Episode
from v2_automation.telegram_publisher import TelegramPublisher
from v2support import cfg


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    yield c
    c.close()


def _published(conn, n=1, publish_channel=1):
    ep = Episode(anime_key="postid:1", episode_key=f"k{n}", canonical_episode_url=f"k{n}/", language="vf", episode_number=n,
                 episode_url=f"k{n}", publish_channel=publish_channel)
    eid, _ = repo.upsert_episode(conn, ep)
    conn.execute("UPDATE episodes SET status='cleanup_pending', video_message_id=?, thumbnail_message_id=? WHERE id=?",
                 (200 + n, 100 + n, eid))
    conn.execute("INSERT INTO publications(episode_id, publication_type, status, chat_id, message_id) VALUES (?, 'first_publication', 'sent', '-100PRIMARY', ?)",
                 (eid, 200 + n))
    conn.commit()
    return eid


def C(**kw):
    return cfg(channel_id="-100PRIMARY", channels=["-100PRIMARY", "@chan_b", "@chan_c"], **kw)


def test_extra_channels_come_from_config_and_exclude_the_primary():
    assert extra_channels(C()) == ["@chan_b", "@chan_c"]
    assert extra_channels(cfg(channel_id="-100X", channels=["-100X"])) == []
    assert extra_channels(cfg(channel_id="", channels=[])) == []


def test_media_is_copied_to_every_extra_channel_thumbnail_then_video_exactly_once(conn):
    eid = _published(conn)
    tr = FakeTransport()
    pub = TelegramPublisher(tr)
    out = fanout(conn, pub, C())
    assert out["copied"] == 4
    assert tr.calls == [("copy", "@chan_b", "-100PRIMARY", 101), ("copy", "@chan_b", "-100PRIMARY", 201),
                        ("copy", "@chan_c", "-100PRIMARY", 101), ("copy", "@chan_c", "-100PRIMARY", 201)]
    assert fanout(conn, pub, C())["copied"] == 0 and len(tr.calls) == 4                     # never twice
    rows = conn.execute("SELECT publication_type, status FROM publications WHERE episode_id=? AND publication_type LIKE 'copy_%'", (eid,)).fetchall()
    assert len(rows) == 4 and {r["status"] for r in rows} == {"sent"}


def test_a_failing_channel_does_not_block_the_others_and_is_retried_later(conn):
    _published(conn)
    tr = FakeTransport()
    tr.fail_next = [TelegramPublishError("Forbidden: bot is not a member of the channel", "FORBIDDEN")]   # chan_b thumbnail fails
    pub = TelegramPublisher(tr)
    out = fanout(conn, pub, C())
    assert out["failed"] == 1 and out["copied"] == 2                                          # chan_b nothing, chan_c both
    assert ("copy", "@chan_c", "-100PRIMARY", 201) in tr.calls and ("copy", "@chan_b", "-100PRIMARY", 201) not in tr.calls
    assert fanout(conn, pub, C())["copied"] == 2                                             # chan_b succeeds on the next pass


def test_private_only_media_never_reaches_a_channel(conn):
    _published(conn, publish_channel=0)
    tr = FakeTransport()
    assert fanout(conn, TelegramPublisher(tr), C())["copied"] == 0 and tr.calls == []


def test_crash_mid_copy_is_uncertain_and_never_copied_again(conn):
    eid = _published(conn)
    conn.execute("INSERT INTO publications(episode_id, publication_type, status, chat_id) VALUES (?, 'copy_video:@chan_b', 'sending', '@chan_b')", (eid,))
    conn.commit()
    assert recover_fanout(conn) == [eid]
    tr = FakeTransport()
    fanout(conn, TelegramPublisher(tr), C())
    assert ("copy", "@chan_b", "-100PRIMARY", 201) not in tr.calls                           # uncertain: left to an operator
    assert conn.execute("SELECT status FROM publications WHERE publication_type='copy_video:@chan_b'").fetchone()[0] == "uncertain"


def test_two_channels_a_and_b_are_both_served_without_any_hardcoded_channel(conn):
    _published(conn)
    tr = FakeTransport()
    fanout(conn, TelegramPublisher(tr), cfg(channel_id="-1001", channels=["-1001", "@other_channel"]))
    assert {c[1] for c in tr.calls} == {"@other_channel"}


# -- configuration -----------------------------------------------------------------------

@pytest.fixture()
def loader(monkeypatch):
    monkeypatch.setattr(app_config, "_probe_local_bot_api", lambda base: {"enabled": False})
    for k in ("TELEGRAM_CHANNELS", "REQUIRED_CHANNELS", "USER_BOT_TOKEN", "REQUEST_WAIT_TIMEOUT", "WATCH_INTERVAL",
              "CLEANUP_AFTER_DAYS", "MAX_CONCURRENT_DOWNLOADS", "TELEGRAM_LOCAL_BOT_API_URL", "TELEGRAM_API_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    return app_config.load_config


def test_config_defaults_come_from_yaml(loader):
    c = loader()
    assert c.required_channels == ["@spy_family_2025"] and c.requests["wait_timeout_seconds"] == 1200
    assert c.channels and c.channels[0] == c.channel_id or not c.channel_id


def test_env_overrides_channels_and_options(loader, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNELS", "@a, @b ,@c")
    monkeypatch.setenv("REQUIRED_CHANNELS", "@channel_1,@channel_2,@channel_3")
    monkeypatch.setenv("REQUEST_WAIT_TIMEOUT", "600")
    monkeypatch.setenv("WATCH_INTERVAL", "900")
    monkeypatch.setenv("CLEANUP_AFTER_DAYS", "7")
    monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "5")
    monkeypatch.setenv("USER_BOT_TOKEN", "1:tok")
    monkeypatch.setenv("TELEGRAM_LOCAL_BOT_API_URL", "http://10.0.0.5:8081")
    c = loader()
    assert c.channels == ["@a", "@b", "@c"] and c.required_channels == ["@channel_1", "@channel_2", "@channel_3"]
    assert c.requests["wait_timeout_seconds"] == 600 and c.source["poll_interval_seconds"] == 900
    assert c.publication["cleanup_after_days"] == 7 and c.queues["max_concurrent_downloads"] == 5
    assert c.user_bot_token == "1:tok" and c.telegram["api_base_url"] == "http://10.0.0.5:8081"
