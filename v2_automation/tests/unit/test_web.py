"""Web admin panel tests (FastAPI TestClient, in-memory DB, no network)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from v2_automation import db, repo
from v2_automation.models import Episode
from v2_automation.web import create_app

TOKEN_SENTINEL = "123456:ADMIN_TEST_SENTINEL_TOKEN"


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "v2.sqlite3"
    c = sqlite3.connect(str(path), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    repo.save_capacity(c, {"documented_upload_limit_mib": "2000",
                          "tested_upload_limit_mib": "800",
                          "first_failed_upload_mib": "900",
                          "free_disk_bytes": "107374182400",
                          "http_server_version": "Bot API 10.3"})
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def client(conn):
    os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN_SENTINEL
    app = create_app(connect=lambda: conn)
    with TestClient(app) as tc:
        yield tc
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)


def _insert(conn: sqlite3.Connection, status: str, anime_key: str = "anime-a",
            episode_number: int = 1) -> int:
    ep = Episode(anime_key=anime_key, episode_key=f"{anime_key}-e{episode_number:02d}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime_key}/e{episode_number}",
                 language="vostfr", season=None, episode_number=episode_number,
                 label=f"{anime_key} épisode {episode_number}", episode_url="",
                 status=status)
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    return eid


def _no_secret(body: str) -> None:
    assert TOKEN_SENTINEL not in body


def test_healthz_readyz(client):
    assert client.get("/healthz").json()["ok"] is True
    ready = client.get("/readyz").json()
    assert ready["ok"] is True and ready["schema_version"] >= 1


def test_dashboard_html(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # the panel is now static files under /ui (the inline page was replaced): the shell links them, no JSON links
    assert "Panneau admin" in body and "/ui/app.js" in body and "/ui/app.css" in body
    assert "/api/overview" not in body
    _no_secret(body)


def test_overview_no_secret(client, conn):
    _insert(conn, "published", episode_number=1)
    _insert(conn, "retry_wait", episode_number=2)
    o = client.get("/api/overview").json()
    assert o["episodes"]["total"] == 2
    assert o["episodes"]["by_status"]["published"] == 1
    assert o["capacity"]["tested_upload_limit_mib"] == "800"
    assert o["limits_mib"]["tested"] == 800
    assert o["queue"]["depth"] == 0
    _no_secret(client.get("/api/overview").text)


def test_episodes_filter_and_detail(client, conn):
    e1 = _insert(conn, "published", "anime-b")
    _insert(conn, "retry_wait")
    items = client.get("/api/episodes?status=published").json()["items"]
    assert len(items) == 1 and items[0]["anime_key"] == "anime-b"
    detail = client.get(f"/api/episodes/{e1}").json()
    assert detail["episode"]["status"] == "published"
    assert detail["publications"] == []
    _no_secret(client.get(f"/api/episodes/{e1}").text)


def test_error_and_history_endpoints(client, conn):
    eid = _insert(conn, "failed")
    repo.mark_failed(conn, eid, "test error")
    conn.commit()
    errs = client.get("/api/errors").json()["items"]
    assert any(e["status"] == "failed" for e in errs)
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/capacity").json()["items"]["tested_upload_limit_mib"] == "800"


def test_requeue_retry_wait(client, conn):
    eid = _insert(conn, "retry_wait")
    repo.set_retry_until(conn, eid, "2099-01-01T00:00:00Z", retry_count=3, error="boom")
    conn.commit()
    d = client.post(f"/api/episodes/{eid}/requeue").json()
    assert d["status"] == "queued"
    ep = repo.get(conn, eid)
    assert ep.retry_until_at is None and ep.retry_count == 0
    assert repo.queue_depth(conn) == 1


def test_requeue_reject_non_recoverable(client, conn):
    eid = _insert(conn, "published")
    assert client.post(f"/api/episodes/{eid}/requeue").status_code == 409
    eid2 = _insert(conn, "skipped_dup")
    assert client.post(f"/api/episodes/{eid2}/requeue").status_code == 409


def test_fail_retry_wait(client, conn):
    eid = _insert(conn, "retry_wait")
    d = client.post(f"/api/episodes/{eid}/fail").json()
    assert d["status"] == "failed"
    assert "forcé manuellement" in repo.get(conn, eid).last_error


def test_fail_reject_non_retry_wait(client, conn):
    eid = _insert(conn, "queued")
    assert client.post(f"/api/episodes/{eid}/fail").status_code == 409


def test_unknown_episode_404(client):
    assert client.get("/api/episodes/99999").status_code == 404
    assert client.post("/api/episodes/99999/requeue").status_code == 404


def test_health_endpoint_no_network(conn):
    from v2_automation.app_config import AppConfig, BotCapacity
    cap = BotCapacity(enabled=True, getme_ok=True, http_server_version="test",
                      free_disk_bytes=10**12, documented_limit_bytes=None,
                      tested_limit_bytes=None, status_code=200, error=None)
    fake = AppConfig(source={}, queues={}, downloads={"min_free_disk_bytes": 0}, telegram={},
                     publication={}, limits={}, monitoring={}, logging={}, bot_token="",
                     channel_id="", admin_telegram_ids=[], bot_capacity=cap)
    app = create_app(connect=lambda: conn, cfg_factory=lambda: fake)
    with TestClient(app) as tc:
        h = tc.get("/api/health").json()
    assert h["ok"] is True and {"db", "disk", "queue", "retry", "errors"} <= set(h["checks"])


# ── operational control endpoints ───────────────────────────────────────────────

def test_control_pause_resume(client, conn):
    c = client.get("/api/control").json()
    assert c["paused"] is False
    client.post("/api/control/pause")
    assert client.get("/api/control").json()["paused"] is True
    client.post("/api/control/resume")
    assert client.get("/api/control").json()["paused"] is False


def test_control_animes_toggle_and_upsert(client, conn):
    d = client.post("/api/animes/anime-a/enable").json()
    assert d["ok"] and d["enabled"] is True
    client.post("/api/animes/anime-a/disable")
    items = client.get("/api/control").json()["animes"]
    assert any(a["anime_key"] == "anime-a" and a["enabled"] == 0 for a in items)
    r = client.post("/api/animes/anime-a?title=Zerobase&enabled=true")
    assert r.status_code == 200
    first = client.get("/api/control").json()["animes"][0]
    assert first["title"] == "Zerobase" and first["enabled"] == 1


def test_control_alerts_ack(client, conn):
    d = client.post("/api/alerts/1/ack")
    assert d.status_code == 404                          # aucune alerte → 404
    from v2_automation import alerts
    alerts.raise_alert(conn, "low_disk", "disk", "espace bas")
    conn.commit()
    assert client.get("/api/control").json()["alerts_open"] == 1
    items = client.get("/api/alerts").json()["items"]
    assert items[0]["kind"] == "low_disk"
    ac = client.post(f"/api/alerts/{items[0]['id']}/ack").json()
    assert ac["ok"] is True
    assert client.get("/api/control").json()["alerts_open"] == 0


def test_control_cancel_episode(client, conn):
    import v2_automation.repo as repo_mod
    eid = _insert(conn, "queued")
    repo_mod.enqueue(conn, "anime-a", eid)
    conn.commit()
    d = client.post(f"/api/episodes/{eid}/cancel").json()
    assert d["ok"] and d["status"] == "failed"
    assert repo_mod.queue_depth(conn) == 0
    assert client.post("/api/episodes/99999/cancel").status_code == 404

# ── automatic mode: animes with source URL, force-check, jobs, host state ────────

from test_discovery import BASE, Site, _cfg as _disc_cfg   # noqa: E402


@pytest.fixture()
def auto_client(conn):
    site = Site()
    site.set("nouveau", 77, [1, 2], title="Nouveau <b>Titre</b>")
    app = create_app(connect=lambda: conn, cfg_factory=lambda: _disc_cfg(), fetch=site)
    with TestClient(app) as tc:
        yield tc


def test_add_anime_by_url_then_list_edit_force_check(auto_client, conn):
    r = auto_client.post("/api/animes", params={"source_url": f"{BASE}/anime/nouveau/"})
    assert r.status_code == 200 and r.json()["anime_key"] == "postid:77"
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0        # no job from a URL alone
    items = auto_client.get("/api/animes").json()["items"]
    assert items[0]["source_url"].endswith("/anime/nouveau/") and items[0]["enabled"] == 1
    assert auto_client.post("/api/animes/postid:77/edit", params={"title": "Autre"}).status_code == 200
    assert auto_client.post("/api/animes/postid:77/force-check").json()["ok"]
    assert conn.execute("SELECT force_check FROM animes").fetchone()[0] == 1
    assert auto_client.post("/api/animes/postid:77/disable").status_code == 200
    assert auto_client.get("/api/animes").json()["items"][0]["enabled"] == 0


def test_add_anime_rejects_foreign_url_and_unknown_actions(auto_client):
    assert auto_client.post("/api/animes", params={"source_url": "https://evil.example/anime/x/"}).status_code == 400
    assert auto_client.post("/api/animes/inconnu/force-check").status_code == 404
    assert auto_client.post("/api/animes/inconnu/edit", params={"title": "x"}).status_code == 404
    assert auto_client.post("/api/animes/postid:1/edit", params={"source_url": "https://evil.example/x"}).status_code in (400, 404)


def test_jobs_show_progress_and_only_active_ones(auto_client, conn):
    _insert(conn, "downloading", "a", 1)
    _insert(conn, "publishing_video", "a", 2)
    _insert(conn, "cleanup_pending", "a", 3)
    items = auto_client.get("/api/jobs").json()["items"]
    by_ep = {i["episode_number"]: i for i in items}
    assert set(by_ep) == {1, 2}                                   # finished episodes are not jobs
    assert by_ep[1]["progress"] == {"step": 3, "of": 7, "percent": 43}
    assert by_ep[2]["progress"]["step"] == 7


def test_system_status_reports_host_and_worker_without_secrets(auto_client, conn):
    os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN_SENTINEL
    try:
        body = auto_client.get("/api/system").text
        data = auto_client.get("/api/system").json()
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    _no_secret(body)
    assert data["poll_interval_seconds"] == 1800 and data["worker"] is None and data["paused"] is False
    assert "cpu_percent" in data and "disk_free_bytes" in data and "net_bytes_recv" in data


def test_dashboard_javascript_is_valid_and_escapes_source_text(client):
    import shutil
    import subprocess
    import tempfile
    js = client.get("/ui/app.js").text                     # the inline page was replaced by static files
    assert "const esc = " in js and "const html = " in js          # every dynamic value goes through html``
    assert "${a.title" in js                                       # source titles are interpolated, hence escaped
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
        os.unlink(f.name)
        assert r.returncode == 0, r.stderr


def test_requeue_starts_now_not_at_the_old_backoff_time(client, conn):
    eid = _insert(conn, "retry_wait")
    conn.execute("UPDATE episodes SET next_retry_at='2099-01-01T00:00:00Z' WHERE id=?", (eid,))
    conn.commit()
    assert client.post(f"/api/episodes/{eid}/requeue").status_code == 200
    assert conn.execute("SELECT next_retry_at FROM episodes WHERE id=?", (eid,)).fetchone()[0] is None
