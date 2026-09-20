"""Monitoring / healthchecks tests (pure DB + fake config, no network)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from v2_automation import db, monitoring, repo
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    c.commit()
    yield c
    c.close()


def _cfg(free_bytes: int = 10**12, max_stall: int = 3600, min_free: int = 0) -> AppConfig:
    cap = BotCapacity(enabled=True, getme_ok=True, http_server_version="test",
                      free_disk_bytes=free_bytes, documented_limit_bytes=None,
                      tested_limit_bytes=None, status_code=200, error=None)
    return AppConfig(source={}, queues={}, downloads={"min_free_disk_bytes": min_free},
                     telegram={}, publication={}, limits={}, monitoring={"max_stall_seconds": max_stall},
                     logging={}, bot_token="", channel_id="", admin_telegram_ids=[], bot_capacity=cap)


@pytest.fixture()
def healthy_cfg():
    return _cfg()


def test_healthy_pipeline(conn, healthy_cfg):
    h = monitoring.pipeline_health(conn, healthy_cfg)
    assert h["ok"] is True
    assert h["checks"]["db"]["ok"] and h["checks"]["queue"]["ok"]
    assert h["checks"]["disk"]["detail"].endswith("GiB libre")


def test_disk_gate_low(conn):
    h = monitoring.pipeline_health(conn, _cfg(free_bytes=100, min_free=5 * 2**30))
    assert h["ok"] is False and h["checks"]["disk"]["ok"] is False


def test_retry_window_expired_detected(conn):
    eid = _insert(conn, "retry_wait")
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo.set_retry_until(conn, eid, past, retry_count=1, error="retry expired")
    conn.commit()
    h = monitoring.pipeline_health(conn, _cfg())
    assert h["checks"]["retry"]["ok"] is False
    assert "expirée" in h["checks"]["retry"]["detail"]


def test_stalled_queue_head_detected(conn):
    _insert(conn, "queued")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "UPDATE queue_items SET created_at=?, status='queued' "
        "WHERE episode_id=(SELECT id FROM episodes LIMIT 1)", (old,))
    conn.commit()
    h = monitoring.pipeline_health(conn, _cfg(max_stall=3600))
    assert h["checks"]["queue"]["ok"] is False
    assert "min" in h["checks"]["queue"]["detail"]


def test_error_ratio_no_warning_below_half(conn):
    _insert(conn, "published", epnum=1)
    _insert(conn, "published", epnum=2)
    f = _insert(conn, "failed", epnum=3)
    repo.mark_failed(conn, f, "boom")
    conn.commit()
    h = monitoring.pipeline_health(conn, _cfg())
    assert h["checks"]["errors"]["ok"] is True
    assert "1 échec(s) / 2 publication(s)" in h["checks"]["errors"]["detail"]


def test_error_ratio_warns_above_half(conn):
    _insert(conn, "published", epnum=1)
    f = _insert(conn, "failed", epnum=2)
    repo.mark_failed(conn, f, "boom")
    conn.commit()
    h = monitoring.pipeline_health(conn, _cfg())
    assert h["checks"]["errors"]["ok"] is False


def _insert(conn, status, anime="anime-a", epnum=1) -> int:
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{epnum}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e{epnum}",
                 episode_number=epnum, status=status)
    eid, _ = repo.upsert_episode(conn, ep)
    repo.enqueue(conn, anime, eid) if status == "queued" else None
    conn.commit()
    return eid