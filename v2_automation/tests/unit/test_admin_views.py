"""Telegram admin screens: pure views (no network), deterministic time, no jargon, safe HTML."""
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from v2_automation import admin_views as v
from v2_automation import alerts, db, notifier, recovery, repo, service
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode

NOW = "2026-09-20T07:08:42Z"
TZ = timezone(timedelta(hours=1))                     # local time for the tests: 08:08:42


def _cfg(**limits) -> AppConfig:
    return AppConfig(source={"base_url": "https://voir-anime.to/", "poll_interval_seconds": 1800}, queues={},
                     downloads={}, telegram={}, publication={"cleanup_after_days": 14}, limits=limits,
                     monitoring={}, logging={}, bot_token="", channel_id="", admin_telegram_ids=[],
                     bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    c.execute("INSERT INTO animes (anime_key, title, enabled, source_url, last_checked_at, last_successful_check_at) "
              "VALUES ('postid:1', 'Bleach <b>&</b>', 1, 'https://voir-anime.to/anime/b/', "
              "'2026-09-20T07:08:12Z', '2026-09-20T07:08:12Z')")
    c.commit()
    yield c
    c.close()


def _ep(conn, n, status, **kw):
    ep = Episode(anime_key="postid:1", episode_key=f"k{n}", episode_number=n, status="discovered",
                 canonical_episode_url=f"https://x/{n}", episode_url=f"https://x/{n}")
    eid, _ = repo.upsert_episode(conn, ep)
    sets = ", ".join(f"{k}=?" for k in ["status", *kw])
    conn.execute(f"UPDATE episodes SET {sets} WHERE id=?", (status, *kw.values(), eid))
    conn.commit()
    return eid


def _alive(conn, secs=60):
    conn.execute("INSERT OR REPLACE INTO leases VALUES ('worker','host:1','2026-09-20T07:00:00Z',"
                 "'2026-09-20T07:08:00Z', ?)", (service.add_seconds(NOW, secs),))
    conn.commit()


# ── helpers ──────────────────────────────────────────────────────────────────────

def test_time_helpers_use_local_time_and_relative_words():
    assert v.hm(NOW, TZ) == "08:08" and v.hms(NOW, TZ) == "08:08:42" and v.day(NOW, TZ) == "20/09"
    assert v.ago("2026-09-20T07:08:00Z", NOW) == "à l'instant"
    assert v.ago("2026-09-20T06:48:42Z", NOW) == "il y a 20 min"
    assert v.ago("2026-09-20T05:00:00Z", NOW) == "il y a 2 h 08"
    assert v.ago(None, NOW) == "jamais"
    assert v.bar(43) == "▓▓▓░░░░" and v.bar(0) == "░░░░░░░" and v.bar(100) == "▓▓▓▓▓▓▓"
    assert v.size_label(364724237) == "348 Mo" and v.size_label(2 * 1073741824) == "2.0 Go"


def test_esc_neutralises_source_markup():
    assert v.esc("<b>x</b> & <script>") == "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;"


# ── home ─────────────────────────────────────────────────────────────────────────

def test_home_shows_real_state_without_jargon_and_without_baseline_in_counts(conn):
    _alive(conn)
    for n in range(1, 11):
        _ep(conn, n, "discovered")                                    # baseline: known, not jobs
    _ep(conn, 36, "cleanup_pending", published_at="2026-09-20T06:48:42Z", file_size=364724237)
    run = _ep(conn, 37, "downloading")
    wait = _ep(conn, 49, "retry_wait", last_error="NOT_AVAILABLE_YET: player is a youtube embed")
    repo.enqueue(conn, "postid:1", run)
    repo.enqueue(conn, "postid:1", wait)
    conn.commit()
    view = v.home(conn, _cfg(), now=NOW, tz=TZ)
    t = str(view.text)
    assert "Tableau de bord" in t and "mis à jour 08:08:42" in t
    assert "🟢 Worker actif" in t and "file active" in t
    # wording changed on purpose: the cycle belongs to the watcher ("Prochain cycle"), plus a "today" line
    assert "Prochain cycle 08:38" in t and "dernier contrôle 08:08 ✅" in t and "Aujourd'hui :" in t
    assert "EN COURS (1)" in t and "Téléchargement 3/7" in t and "▓▓▓" in t and "43 %" in t
    assert "EN ATTENTE (1)" in t and "source pas encore prête" in t
    assert "DERNIERS PUBLIÉS" in t and "E36" in t and "348 Mo" in t and "il y a 20 min" in t
    assert "1 anime · 1 publiés" in t                                # the 10 baseline episodes are NOT counted
    for jargon in ("cleanup_pending", "retry_wait", "cleanup pending", "discovered", "NOT_AVAILABLE"):
        assert jargon not in t
    assert "Bleach &lt;b&gt;&amp;&lt;/b&gt;" in t                    # source title escaped
    assert len(t) < 1500
    flat = [b for row in view.rows for b in row]
    assert {d for _, d in flat} == {"nav:jobs", "nav:anime", "nav:alerts", "nav:system", "nav:notif", "nav:home", "nav:cycles"}   # + Cycles screen


def test_home_when_idle_and_worker_stopped(conn):
    t = str(v.home(conn, _cfg(), now=NOW, tz=TZ).text)
    assert "🔴 Worker arrêté" in t and "😴 Rien en cours" in t and "Surveillance à l'arrêt" in t
    assert "✨ Rien à traiter" in t
    assert "EN ATTENTE" not in t and "DERNIERS PUBLIÉS" not in t        # empty sections are hidden


def test_home_flags_what_needs_handling(conn):
    _alive(conn)
    _ep(conn, 5, "failed", last_error="DOWNLOAD_FAILED: HTTP_404")
    alerts.raise_alert(conn, "definitive_failure", "ep:1", "échec")
    conn.commit()
    t = str(v.home(conn, _cfg(), now=NOW, tz=TZ).text)
    assert "⚠️ 1 à traiter · 1 alerte" in t


def test_every_callback_is_short_enough_for_telegram(conn):
    _alive(conn)
    _ep(conn, 1, "queued")
    _ep(conn, 2, "failed")
    alerts.raise_alert(conn, "retry", "ep:1", "x")
    conn.commit()
    for view in (v.home(conn, _cfg(), now=NOW, tz=TZ), v.jobs_view(conn, _cfg(), now=NOW, tz=TZ),
                 v.anime_view(conn, _cfg(), now=NOW, tz=TZ), v.alerts_view(conn, _cfg(), now=NOW, tz=TZ),
                 v.system_view(conn, _cfg(), now=NOW, tz=TZ), v.notifications_view(conn, _cfg(), now=NOW, tz=TZ),
                 v.anime_detail(conn, _cfg(), "postid:1", now=NOW, tz=TZ)):
        assert len(str(view.text)) < 4096
        for row in view.rows:
            for label, data in row:
                assert len(data.encode()) <= v.MAX_CALLBACK_BYTES and label


# ── other screens ────────────────────────────────────────────────────────────────

def test_jobs_screen_lists_actions_and_retry_all_only_when_needed(conn):
    _alive(conn)
    r = _ep(conn, 1, "downloading")
    q = _ep(conn, 2, "queued")
    repo.enqueue(conn, "postid:1", r)
    repo.enqueue(conn, "postid:1", q)
    conn.commit()
    view = v.jobs_view(conn, _cfg(), now=NOW, tz=TZ)
    data = [d for row in view.rows for _, d in row]
    assert f"act:cancel:{r}" in data and f"act:cancel:{q}" in data and f"act:retry:{q}" in data
    assert "act:retryall" not in data                                   # nothing failed: no bulk button
    _ep(conn, 3, "failed", last_error="DOWNLOAD_FAILED: x")
    data2 = [d for row in v.jobs_view(conn, _cfg(), now=NOW, tz=TZ).rows for _, d in row]
    assert "act:retryall" in data2 and "nav:home" in data2


def test_anime_screen_and_detail(conn):
    _ep(conn, 1, "discovered")
    _ep(conn, 36, "cleanup_pending", published_at="2026-09-20T06:48:42Z")
    view = v.anime_view(conn, _cfg(), now=NOW, tz=TZ)
    t = str(view.text)
    assert "Anime surveillés (1)" in t and "1 publiés" in t
    data = [d for row in view.rows for _, d in row]
    assert {"act:check:postid:1", "act:toggle:postid:1", "nav:animedetail:postid:1", "act:add"} <= set(data)
    det = str(v.anime_detail(conn, _cfg(), "postid:1", now=NOW, tz=TZ).text)
    assert "E36" in det and "publié" in det and "1 épisodes déjà connus" in det
    assert "introuvable" in str(v.anime_detail(conn, _cfg(), "nope", now=NOW).text).lower()


def test_alerts_screen_is_readable_and_actionable(conn):
    e = _ep(conn, 13, "failed")
    alerts.raise_alert(conn, "recovery_after_crash", f"ep:{e}", "x")
    conn.commit()
    view = v.alerts_view(conn, _cfg(), now=NOW, tz=TZ)
    t = str(view.text)
    assert "💥 Publication interrompue" in t and "Bleach &lt;b&gt;&amp;&lt;/b&gt; E13" in t and "ep:" not in t
    data = [d for row in view.rows for _, d in row]
    assert any(d.startswith("act:ack:") for d in data) and "act:ackall" in data


def test_alerts_screen_when_empty(conn):
    t = str(v.alerts_view(conn, _cfg(), now=NOW, tz=TZ).text)
    assert "Aucune alerte" in t


def test_system_screen(conn):
    _alive(conn)
    t = str(v.system_view(conn, _cfg(max_safe_publish_mib=1800), now=NOW, tz=TZ, telegram_ok=True).text)
    assert "Système" in t and "CPU" in t and "RAM" in t and "Disque" in t and "Réseau" in t
    assert "Worker : 🟢 actif" in t and "toutes les 30 min" in t and "🟢 joignable" in t and "1800 Mio" in t
    assert "🔴 injoignable" in str(v.system_view(conn, _cfg(), now=NOW, tz=TZ, telegram_ok=False).text)


def test_notifications_screen_reflects_switches(conn):
    assert str(v.notifications_view(conn, _cfg(), now=NOW, tz=TZ).text).count("🟢") == 4     # all on by default
    notifier.set_enabled(conn, "daily", False)
    t = str(v.notifications_view(conn, _cfg(), now=NOW, tz=TZ).text)
    assert t.count("🟢") == 3 and "⚪ 🗓 Résumé quotidien" in t


# ── alerts close themselves ──────────────────────────────────────────────────────

def test_alerts_close_when_the_episode_is_published_and_stale_ones_are_swept(conn):
    e1 = _ep(conn, 1, "cleanup_pending", published_at=NOW)
    e2 = _ep(conn, 2, "retry_wait", last_error="NOT_AVAILABLE_YET: x")
    e3 = _ep(conn, 3, "failed", last_error="boom")
    for k, ak in (("definitive_failure", f"ep:{e1}"), ("recovery_after_crash", f"ep:{e1}"), ("retry", f"ep:{e2}"),
                  ("definitive_failure", f"ep:{e3}"), ("cleanup_blocked", f"ep:{e1}")):
        alerts.raise_alert(conn, k, ak, "t")
    conn.commit()
    assert alerts.open_count(conn) == 5
    assert alerts.sweep_stale(conn) == 3                                 # e1 x2 (published) + e2 (waiting for source)
    conn.commit()
    left = {(a["kind"], a["akey"]) for a in alerts.list_alerts(conn)}
    assert left == {("definitive_failure", f"ep:{e3}"), ("cleanup_blocked", f"ep:{e1}")}     # real problems remain


def test_recovery_sweeps_stale_alerts_at_boot(conn):
    e1 = _ep(conn, 1, "cleanup_pending", published_at=NOW)
    alerts.raise_alert(conn, "retry", f"ep:{e1}", "t")
    conn.commit()
    res = recovery.run_recovery(conn, _cfg())
    assert res["alerts_closed"] == 1 and alerts.open_count(conn) == 0
