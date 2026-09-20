"""Web panel: readable views (French wording, no raw URL, no secret), live health/capacity, same-origin guard."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from v2_automation import alerts, db, repo, web_data
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode
from v2_automation.web import create_app

RAW_URL = "https://voir-anime.to/anime/secret-page/episode-49"


def _cfg(free: int = 10**12) -> AppConfig:
    return AppConfig(source={"poll_interval_seconds": 1800}, queues={"max_concurrent_downloads": 3},
                     downloads={"min_free_disk_bytes": 5 * 2**30},
                     telegram={"api_base_url": "http://127.0.0.1:8081", "local_upload_container": "c"}, publication={},
                     limits={"max_safe_publish_mib": 1800, "documented_upload_limit_mb": 2000}, monitoring={}, logging={},
                     bot_token="", channel_id="", admin_telegram_ids=[],
                     bot_capacity=BotCapacity(True, True, "t", free, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    c.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('a', 'Bleach', 1, ?)", (RAW_URL,))
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def client(conn):
    with TestClient(create_app(connect=lambda: conn, cfg_factory=_cfg)) as tc:
        yield tc


def _ep(conn, status, n, anime="a", error=None):
    e = Episode(anime_key=anime, episode_key=f"{anime}-{n}", canonical_episode_url=f"{RAW_URL}-{n}", language="vostfr",
                season=None, episode_number=n, label=f"{anime} {n}", episode_url=RAW_URL, status=status)
    eid, _ = repo.upsert_episode(conn, e)
    if error:
        repo.mark_failed(conn, eid, error)
        conn.execute("UPDATE episodes SET status=? WHERE id=?", (status, eid))
    if status in ("queued", "retry_wait"):
        repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


def test_dashboard_is_humanised_and_hides_raw_links(client, conn):
    _ep(conn, "published", 36)
    _ep(conn, "retry_wait", 49, error=f"NOT_AVAILABLE_YET: page {RAW_URL} is not ready")
    d = client.get("/api/dashboard").json()
    assert d["banner"]["level"] in ("warn", "info", "ok") and d["banner"]["text"]
    assert d["waiting"][0]["status_label"] == "en attente" and d["waiting"][0]["reason"] == "source pas encore prête"
    assert RAW_URL not in json.dumps(d) and "source_url" not in d["animes"][0]


def test_banner_levels(conn):
    d = web_data.dashboard(conn, _cfg())
    assert any("worker" in i["text"].lower() for i in d["banner"]["issues"]) and d["banner"]["level"] == "warn"
    _ep(conn, "failed", 5, error="boom")
    assert web_data.dashboard(conn, _cfg())["banner"]["level"] == "bad"


def test_episodes_view_default_hides_baseline_and_filters(client, conn):
    _ep(conn, "discovered", 1)
    _ep(conn, "published", 2)
    _ep(conn, "cleanup_pending", 3)
    _ep(conn, "failed", 4, error="x")
    d = client.get("/api/episodes-view").json()
    assert d["total"] == 3 and d["groups"] == {"published": 2, "running": 0, "waiting": 0, "attention": 1, "discovered": 1}
    assert client.get("/api/episodes-view?status=published").json()["total"] == 2
    assert client.get("/api/episodes-view?status=discovered").json()["total"] == 1
    assert client.get("/api/episodes-view?q=Bleach").json()["total"] == 3
    assert client.get("/api/episodes-view?q=E4").json()["total"] == 1                # "E4" = episode number 4
    first = client.get("/api/episodes-view?status=published").json()["items"][0]
    assert first["anime_title"] == "Bleach" and first["status_label"] == "publié" and first["tone"] == "ok"


def test_episode_detail_and_unknown(client, conn):
    eid = _ep(conn, "failed", 7, error=f"Erreur sur {RAW_URL}")
    d = client.get(f"/api/episodes-view/{eid}").json()
    assert d["actionable"] is True and RAW_URL not in d["last_error"] and "[lien]" in d["last_error"]
    assert client.get("/api/episodes-view/9999").status_code == 404


def test_queue_lanes_are_per_anime_and_ordered_by_episode(client, conn):
    _ep(conn, "queued", 12)
    _ep(conn, "queued", 10)
    lanes = client.get("/api/queue/lanes").json()
    assert lanes["total"] == 2 and lanes["paused"] is False
    assert [i["episode_number"] for i in lanes["lanes"][0]["items"]] == [10, 12]
    assert lanes["lanes"][0]["anime_title"] == "Bleach"


def test_problems_lists_failures_and_open_alerts_without_links(client, conn):
    eid = _ep(conn, "failed", 8, error=f"HTTP 500 {RAW_URL}")
    alerts.raise_alert(conn, "definitive_failure", f"ep:{eid}", "échec", "détail")
    conn.commit()
    p = client.get("/api/problems").json()
    assert p["total"] == 2 and p["episodes"][0]["subject"] == "Bleach E8" and p["alerts"][0]["title"] == "Échec définitif"
    assert p["alerts"][0]["subject"] == "Bleach E8" and RAW_URL not in json.dumps(p)
    assert client.post(f"/api/alerts/{p['alerts'][0]['id']}/ack").status_code == 200
    assert client.get("/api/problems").json()["alerts"] == []


def test_health_ignores_an_episode_waiting_for_its_source_or_a_paused_queue(client, conn):
    eid = _ep(conn, "retry_wait", 49)
    conn.execute("UPDATE queue_items SET status='queued', created_at='2020-01-01T00:00:00Z' WHERE episode_id=?", (eid,))
    conn.commit()
    h = client.get("/api/health/report").json()
    assert h["checks"]["queue"]["ok"] is True                                  # waiting for the source is not a stall
    assert all(c["label"] and c["explain"] for c in h["checks_list"]) and "worker" in h
    q = _ep(conn, "queued", 50)
    conn.execute("UPDATE queue_items SET created_at='2020-01-01T00:00:00Z' WHERE episode_id=?", (q,))
    conn.commit()
    assert client.get("/api/health/report").json()["checks"]["queue"]["ok"] is False   # a really queued one is
    client.post("/api/control/pause")
    assert client.get("/api/health/report").json()["checks"]["queue"]["ok"] is True    # ... unless paused on purpose


def test_capacity_live_uses_current_values_and_evidence(conn, tmp_path):
    (tmp_path / "size_1800.json").write_text(json.dumps({"target_mib": 1800, "result": "PASS", "size_mib": 1804.8,
                                                         "upload_s": 1765.9}))
    (tmp_path / "summary.json").write_text(json.dumps({"rows": [{"target_mib": 1000, "result": "PASS", "size_mib": 996.0,
                                                                 "upload_s": 961.4}]}))
    proven = web_data.proven_uploads(tmp_path)
    assert [u["target_mib"] for u in proven] == [1000, 1800] and proven[1]["throughput_mibs"] == pytest.approx(1.02, abs=0.01)
    cap = web_data.capacity_live(conn, _cfg(), probe=lambda cfg: True, uploads=proven)
    assert cap["server"]["reachable"] is True and cap["server"]["upload_by_file_path"] is True
    assert cap["limits"]["max_safe_publish_mib"] == 1800 and cap["limits"]["largest_proven_mib"] == 1804.8
    assert web_data.capacity_live(conn, _cfg(), probe=lambda cfg: False, uploads=[])["server"]["reachable"] is False


def test_probe_reports_unreachable_server_quickly():
    cfg = _cfg()
    cfg.telegram["api_base_url"] = "http://127.0.0.1:1"
    assert web_data.probe_local_server(cfg, timeout=0.5) is False
    cfg.telegram["api_base_url"] = ""
    assert web_data.probe_local_server(cfg) is None


def test_notifications_toggle(client):
    items = client.get("/api/notifications").json()["items"]
    assert [i["key"] for i in items] == ["published", "new_episode", "problems", "daily"] and all(i["enabled"] for i in items)
    assert client.post("/api/notifications/daily/toggle").json() == {"ok": True, "key": "daily", "enabled": False}
    assert client.post("/api/notifications/inconnu/toggle").status_code == 404


def test_actions_from_a_foreign_origin_are_refused(client, conn):
    eid = _ep(conn, "failed", 9, error="x")
    evil = {"Origin": "https://evil.example"}
    assert client.post(f"/api/episodes/{eid}/cancel", headers=evil).status_code == 403
    assert client.post("/api/control/pause", headers=evil).status_code == 403
    assert client.post("/api/control/pause", headers={"Origin": "http://127.0.0.1:8085"}).status_code == 200
    assert client.post("/api/control/resume", headers={"Origin": "http://localhost:8085"}).status_code == 200
    assert client.get("/api/dashboard", headers=evil).status_code == 200            # reading stays possible


def test_static_files_are_served_uncached_without_external_assets(client):
    for path, kind in (("/", "text/html"), ("/ui/app.css", "text/css"), ("/ui/app.js", "javascript")):
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"] and r.headers["cache-control"] == "no-store"
    assert client.get("/ui/../web.py").status_code in (400, 404)
    page = client.get("/").text
    assert 'src="http' not in page and 'href="http' not in page


def test_dashboard_and_cycles_endpoints_show_today_and_the_next_cycle(client, conn):
    _ep(conn, "published", 36)
    conn.execute("UPDATE episodes SET published_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE episode_number=36")
    conn.commit()
    d = client.get("/api/dashboard").json()
    assert d["today"]["published"] == 1 and d["today"]["detected"] >= 1 and d["animes"][0]["today"] >= 1
    c = client.get("/api/cycles").json()
    assert c["items"] == [] and c["interval_seconds"] == 1800 and c["today"]["published"] == 1 and "next_cycle" in c
    js = client.get("/ui/app.js").text
    assert "Cycles de surveillance" in js and "Prochain cycle" in js and "Aujourd’hui" in js      # panel texts present
