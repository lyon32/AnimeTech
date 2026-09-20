"""Notifications: on/off switches, filtered dispatch, daily summary (once per day), publication message."""
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from test_downloader import FakeTelegram, _build_deps, _cfg as _dl_cfg, _mk_episode, conn as dl_conn  # noqa: F401

from v2_automation import alerts, db, notifier, repo
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.downloader import DownloadManager

TZ = timezone(timedelta(hours=1))


def _cfg() -> AppConfig:
    return AppConfig(source={"poll_interval_seconds": 1800}, queues={}, downloads={}, telegram={},
                     publication={}, limits={}, monitoring={}, logging={}, bot_token="", channel_id="",
                     admin_telegram_ids=[], bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    yield c
    c.close()


def test_all_switches_are_on_by_default_and_toggle(conn):
    assert all(notifier.enabled(conn, k) for k in notifier.KEYS)
    assert notifier.toggle(conn, "published") is False and notifier.enabled(conn, "published") is False
    assert notifier.toggle(conn, "published") is True
    with pytest.raises(ValueError):
        notifier.set_enabled(conn, "inconnu", True)


def test_kinds_map_to_the_right_group():
    assert notifier.group_of(alerts.KIND_PUBLISHED) == "published"
    assert notifier.group_of(alerts.KIND_NEW_EPISODE) == "new_episode"
    for kind in (alerts.KIND_RETRY, alerts.KIND_DEFINITIVE_FAILURE, alerts.KIND_LOW_DISK,
                 alerts.KIND_RECOVERY_AFTER_CRASH, alerts.KIND_DISCOVERY_ERROR, alerts.KIND_WORKER_STOPPED):
        assert notifier.group_of(kind) == "problems"


def test_filtered_dispatcher_honours_switches_but_the_alert_is_still_recorded(conn):
    pushed = []
    d = notifier.filtered(conn, lambda k, t, b: pushed.append(k))
    notifier.set_enabled(conn, "problems", False)
    alerts.raise_alert(conn, "definitive_failure", "ep:1", "échec", dispatch=d)
    alerts.raise_alert(conn, "published", "ep:2", "publié", dispatch=d)
    conn.commit()
    assert pushed == ["published"]                                         # problems muted, published still pushed
    assert alerts.open_count(conn) == 1                                    # ... and the failure is still recorded
    assert notifier.filtered(conn, None) is None                           # no admin: nothing to filter


def test_daily_summary_is_sent_once_per_day_from_the_chosen_hour(conn):
    sent = []
    send = sent.append
    early, late, next_day = "2026-09-20T07:30:00Z", "2026-09-20T09:00:00Z", "2026-09-21T08:30:00Z"     # local 08:30 / 10:00 / next 09:30
    assert notifier.maybe_send_daily(conn, _cfg(), send, hour=9, now=early, tz=TZ) is False            # before 09:00 local
    assert notifier.maybe_send_daily(conn, _cfg(), send, hour=9, now=late, tz=TZ) is True
    assert notifier.maybe_send_daily(conn, _cfg(), send, hour=9, now="2026-09-20T15:00:00Z", tz=TZ) is False   # same day
    assert notifier.maybe_send_daily(conn, _cfg(), send, hour=9, now=next_day, tz=TZ) is True                    # next day
    assert len(sent) == 2 and "Résumé de la journée" in sent[0]


def test_daily_summary_respects_switch_restart_and_send_failures(conn):
    sent = []
    now = "2026-09-20T09:00:00Z"
    notifier.set_enabled(conn, "daily", False)
    assert notifier.maybe_send_daily(conn, _cfg(), sent.append, hour=9, now=now, tz=TZ) is False
    notifier.set_enabled(conn, "daily", True)

    def broken(text):
        raise RuntimeError("telegram down")
    assert notifier.maybe_send_daily(conn, _cfg(), broken, hour=9, now=now, tz=TZ) is False     # not marked as sent
    assert notifier.maybe_send_daily(conn, _cfg(), sent.append, hour=9, now=now, tz=TZ) is True   # retried next loop
    assert notifier.maybe_send_daily(conn, _cfg(), sent.append, hour=9, now=now, tz=TZ) is False  # a "restart" never doubles
    assert notifier.maybe_send_daily(conn, _cfg(), None, hour=9, now="2026-09-22T09:00:00Z", tz=TZ) is False   # no admin


def test_daily_summary_content(conn):
    conn.execute("INSERT INTO animes (anime_key, title) VALUES ('a', 'Bleach')")
    conn.execute("INSERT INTO episodes (anime_key, episode_key, source, canonical_episode_url, language, episode_url, status,"
                 " episode_number, published_at) VALUES ('a','k1','s','u1','vostfr','u1','cleanup_pending', 36, '2026-09-20T06:00:00Z')")
    conn.execute("INSERT INTO episodes (anime_key, episode_key, source, canonical_episode_url, language, episode_url, status,"
                 " episode_number, published_at) VALUES ('a','k0','s','u0','vostfr','u0','cleanup_pending', 1, '2026-09-10T06:00:00Z')")
    conn.commit()
    text = notifier.daily_summary_text(conn, _cfg(), now="2026-09-20T09:00:00Z", tz=TZ)
    assert "1 épisode(s) publié(s) sur 24 h" in text and "Bleach E36" in text and "E1\n" not in text
    assert "à traiter" in text and "alerte" in text


# ── publication message + alerts resolved by publication ─────────────────────────

def test_publication_notifies_and_closes_the_episodes_old_alerts(dl_conn, tmp_path):
    seen = []
    conn = dl_conn
    conn.execute("INSERT INTO animes (anime_key, title, enabled) VALUES ('a1', 'Anime Un', 1)")
    eid = _mk_episode(conn)
    alerts.raise_alert(conn, "retry", f"ep:{eid}", "nouvelle tentative")
    alerts.raise_alert(conn, "definitive_failure", f"ep:{eid}", "échec")
    conn.commit()
    mgr = DownloadManager(conn, _dl_cfg(tmp_path), deps=_build_deps(tmp_path, telegram=FakeTelegram()),
                          alerter=lambda kind, akey, title, body="": seen.append((kind, akey, title)))
    assert mgr.process_episode(eid) == "cleanup_pending"
    published = [s for s in seen if s[0] == "published"]
    assert len(published) == 1 and published[0][1] == f"ep:{eid}"
    assert "Anime Un E1 publié" in published[0][2] and " Mo" in published[0][2]
    assert alerts.open_count(conn) == 0                                    # its retry / failure alerts are moot now


def test_failed_processing_sends_no_publication_message(dl_conn, tmp_path):
    seen = []
    conn = dl_conn
    deps = _build_deps(tmp_path)
    deps.extract = lambda url, client: (_ for _ in ()).throw(ConnectionError("boom"))
    eid = _mk_episode(conn)
    DownloadManager(conn, _dl_cfg(tmp_path), deps=deps,
                    alerter=lambda kind, akey, title, body="": seen.append(kind)).process_episode(eid)
    # contract changed on purpose: a failing video now retries SILENTLY; the "vidéo inaccessible" alert only comes
    # after `persistent_error_alert_minutes` (see test_persistent_errors.py), never at the first failure
    assert "published" not in seen and "retry" not in seen


def test_push_icons_are_right_for_information_and_problems(monkeypatch):
    from v2_automation import publisher
    sent = []

    class C:
        def __init__(self, token, chat_id, base_url=None, **kw):
            pass

        def send_message(self, text):
            sent.append(text)
    monkeypatch.setattr(publisher, "V2TelegramClient", C)
    cfg = AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={}, limits={}, monitoring={},
                    logging={}, bot_token="t", channel_id="-100", admin_telegram_ids=[1],
                    bot_capacity=BotCapacity(True, True, "t", 1, None, None, 200, None))
    d = alerts.default_dispatcher(cfg)
    d("published", "Bleach E36 publié", "")
    d("new_episode", "Nouvel épisode", "")
    d("definitive_failure", "Échec", "")
    assert [t.split(" ", 1)[0] for t in sent] == ["✅", "🆕", "⚠️"]


def test_pushes_from_several_threads_never_share_the_telegram_loop_at_the_same_time(monkeypatch):
    import threading
    import time as _time
    from v2_automation import publisher
    inside, worst = [0], [0]
    lock = threading.Lock()

    class C:
        def __init__(self, token, chat_id, base_url=None, **kw):
            pass

        def send_message(self, text):
            with lock:
                inside[0] += 1
                worst[0] = max(worst[0], inside[0])
            _time.sleep(0.05)
            with lock:
                inside[0] -= 1
    monkeypatch.setattr(publisher, "V2TelegramClient", C)
    cfg = AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={}, limits={}, monitoring={}, logging={},
                    bot_token="t", channel_id="-100", admin_telegram_ids=[1],
                    bot_capacity=BotCapacity(True, True, "t", 1, None, None, 200, None))
    d = alerts.default_dispatcher(cfg)
    threads = [threading.Thread(target=d, args=("new_episode", f"x{i}", "")) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert worst[0] == 1                                            # never two sends at once (was: "event loop is already running")
