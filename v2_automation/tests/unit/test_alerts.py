"""Alerts (closure) tests — DB-backed dedup + throttle + dispatch injection,
plus the cleanup/recovery hooks raising real alerts."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from v2_automation import alerts, db, recovery, repo
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode


def _cfg() -> AppConfig:
    return AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={},
                     limits={}, monitoring={}, logging={}, bot_token="", channel_id="",
                     admin_telegram_ids=[],
                     bot_capacity=BotCapacity(True, True, "t", 10 ** 12, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _insert(conn, status, anime="anime-a", epnum=1, **kw):
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{epnum}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e{epnum}",
                 episode_number=epnum, status=status, **kw)
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    return eid


def test_raise_new_alert_dispatches_once(conn):
    seen = []
    r = alerts.raise_alert(conn, "new_episode", "anime-a:e1", "nouvel épisode",
                           "corps", dispatch=lambda k, t, b: seen.append((k, t, b)))
    conn.commit()
    assert r["new"] is True and r["dispatch"] is True
    assert seen and seen[0][0] == "new_episode"
    assert alerts.open_count(conn) == 0          # informational: pushed, nothing to handle -> not left open


def test_problem_alerts_stay_open_until_resolved(conn):
    alerts.raise_alert(conn, "definitive_failure", "ep:9", "echec", "corps", dispatch=lambda *a: None)
    conn.commit()
    assert alerts.open_count(conn) == 1


def test_dedup_rolls_up_on_same_key(conn):
    alerts.raise_alert(conn, "cleanup_blocked", "ep:1", "x"); conn.commit()
    alerts.raise_alert(conn, "cleanup_blocked", "ep:1", "x"); conn.commit()
    alerts.raise_alert(conn, "cleanup_blocked", "ep:2", "x"); conn.commit()
    items = {i["akey"]: i for i in alerts.list_alerts(conn)}
    assert set(items) == {"ep:1", "ep:2"}
    assert items["ep:1"]["count"] == 2 and items["ep:2"]["count"] == 1


def test_throttle_suppresses_second_dispatch_within_window(conn):
    seen = []
    base = "2026-09-18T10:00:00Z"
    alerts.raise_alert(conn, "retry", "ep:1", "t", "", now=base,
                       dispatch=lambda k, t, b: seen.append(k)); conn.commit()
    alerts.raise_alert(conn, "retry", "ep:1", "t", "", now=base,
                       dispatch=lambda k, t, b: seen.append(k)); conn.commit()
    assert len(seen) == 1
    assert alerts.list_alerts(conn)[0]["count"] == 2


def test_dispatch_again_after_throttle_window(conn):
    seen = []
    t0, t1 = "2026-09-18T10:00:00Z", "2026-09-18T11:01:00Z"
    alerts.raise_alert(conn, "retry", "ep:1", "t", "", now=t0,
                       dispatch=lambda k, t, b: seen.append(k)); conn.commit()
    alerts.raise_alert(conn, "retry", "ep:1", "t", "", now=t1,
                       dispatch=lambda k, t, b: seen.append(k)); conn.commit()
    assert len(seen) == 2
    assert alerts.list_alerts(conn)[0]["count"] == 2   # same row, incremented


def test_ack_missing_is_false_and_clear_closes_open(conn):
    assert alerts.ack(conn, 999) is False
    alerts.raise_alert(conn, "low_disk", "disk", "espace bas"); conn.commit()
    assert alerts.ack(conn, 1) is True
    assert alerts.open_count(conn) == 0
    assert alerts.ack(conn, 1) is False
    alerts.raise_alert(conn, "worker_stopped", "loop", "arrêt"); conn.commit()
    alerts.raise_alert(conn, "low_disk", "disk", "espace bas"); conn.commit()
    assert alerts.clear_closed(conn) == 2


def test_list_alerts_orders_by_last_raised(conn):
    t0, t1 = "2026-09-18T10:00:00Z", "2026-09-18T10:05:00Z"
    alerts.raise_alert(conn, "a", "ep:1", "x", now=t0); conn.commit()
    alerts.raise_alert(conn, "b", "ep:2", "y", now=t1); conn.commit()
    items = alerts.list_alerts(conn, status=None)
    assert items[0]["akey"] == "ep:2"


def test_cleanup_blocked_episode_raises_alert(conn):
    eid = _insert(conn, "published", file_path=str(Path("tmp/x.mp4")),
                  video_message_id=101)   # pas de published_at → retention non prouvable
    hooked = []
    from v2_automation import cleanup
    res = cleanup.run_cleanup(conn, _cfg(),
                              dispatch=lambda k, t, b: hooked.append((k, t, b)))
    assert res["blocked_count"] == 1
    assert repo.get(conn, eid).status == "cleanup_blocked"
    assert hooked and hooked[0][0] == "cleanup_blocked"
    assert alerts.open_count(conn) == 1
    assert alerts.list_alerts(conn)[0]["akey"] == f"ep:{eid}"


def test_recovery_risky_publication_raises_alert(conn):
    eid = _insert(conn, "publishing_video", epnum=7)
    hooked = []
    res = recovery.run_recovery(conn, _cfg(),
                                dispatch=lambda k, t, b: hooked.append((k, t, b)))
    assert res["publishing_to_failed"] and hooked
    assert hooked[0][0] == "recovery_after_crash"
    assert alerts.open_count(conn) == 1

def test_default_dispatcher_never_targets_the_public_channel(monkeypatch):
    """Alerts go to the administrators' private chats, never to the content channel."""
    from v2_automation import alerts, publisher
    from v2_automation.app_config import AppConfig, BotCapacity
    sent = []

    class FakeClient:
        def __init__(self, token, chat_id, base_url=None, **kw):
            self.chat_id = chat_id

        def send_message(self, text):
            sent.append((self.chat_id, text))

    monkeypatch.setattr(publisher, "V2TelegramClient", FakeClient)

    def cfg(admins):
        return AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={}, limits={},
                         monitoring={}, logging={}, bot_token="t", channel_id="-100CHANNEL",
                         admin_telegram_ids=admins, bot_capacity=BotCapacity(True, True, "t", 1, None, None, 200, None))
    assert alerts.default_dispatcher(cfg([])) is None                 # no admin: DB-only, nothing published
    dispatch = alerts.default_dispatcher(cfg([111, 222]))
    dispatch("retry", "titre", "corps")
    assert sorted(c for c, _ in sent) == ["111", "222"] and all("-100CHANNEL" != c for c, _ in sent)
