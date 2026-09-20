"""Worker (closure) tests — lease acquire/renew/steal/refuse, and the worker
loop drives queued heads only (respecting pause) and releases its lease."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from v2_automation import db, queues, repo, worker
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode
from v2_automation.timeutil import add_seconds, now_utc

OWNER = "testhost:424242"


def _cfg() -> AppConfig:
    return AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={},
                     limits={}, monitoring={}, logging={}, bot_token="", channel_id="",
                     admin_telegram_ids=[],
                     bot_capacity=BotCapacity(True, True, "t", 10 ** 12, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _lease_row(conn, owner, expiry_iso, now=None):
    now = now or now_utc()
    conn.execute("INSERT INTO leases(name, owner, acquired_at, last_heartbeat, expires_at) "
                 "VALUES ('worker', ?, ?, ?, ?)", (owner, now, now, expiry_iso))
    conn.commit()


def _ep(conn, anime, epnum=1):
    ep = Episode(anime_key=anime, episode_key=f"{anime}-e{epnum:02d}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e{epnum}",
                 episode_number=epnum, status="queued")
    eid, _ = repo.upsert_episode(conn, ep)
    repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


# ── lease primitives ─────────────────────────────────────────────────────────────

def test_acquire_fresh_then_renewed(conn):
    r1 = worker.acquire_lease(conn, owner=OWNER, now=now_utc())
    assert r1["acquired"] and r1["reason"] == "fresh"
    r2 = worker.acquire_lease(conn, owner=OWNER, now=add_seconds(now_utc(), 10))
    assert r2["acquired"] and r2["reason"] == "renewed"


def test_refused_while_held_by_other_live_owner(conn):
    live = f"host:{os.getpid()}"  # un pid vraiment vivant (ce process pytest)
    _lease_row(conn, live, add_seconds(now_utc(), 200))
    r = worker.acquire_lease(conn, owner=OWNER, now=now_utc())
    assert r["acquired"] is False and r["reason"] == "held-by-live-worker"


def test_stale_expired_but_pid_alive_is_refused_safely(conn):
    live = f"host:{os.getpid()}"
    _lease_row(conn, live, add_seconds(now_utc(), -10))   # TTL expired mais process vivant
    r = worker.acquire_lease(conn, owner=OWNER, now=now_utc())
    assert r["acquired"] is False           # ne STEAL pas un propriétaire encore vivant


def test_stale_expired_dead_pid_is_stolen(conn):
    _lease_row(conn, "host:1", add_seconds(now_utc(), -10))   # pid 1 n'existe pas
    r = worker.acquire_lease(conn, owner=OWNER, now=now_utc(), is_live=lambda o: False)
    assert r["acquired"] and r["reason"] == "stolen" and r["owner"] == OWNER


def test_release_only_by_owner(conn):
    worker.acquire_lease(conn, owner=OWNER)
    assert worker.release_lease(conn, owner="autre:123") is False
    assert worker.release_lease(conn, owner=OWNER) is True
    assert worker.release_lease(conn, owner=OWNER) is False


def test_heartbeat_refreshes_expiry(conn):
    worker.acquire_lease(conn, owner=OWNER, now=now_utc())
    exp_before = conn.execute("SELECT expires_at FROM leases WHERE name='worker'").fetchone()[0]
    worker.heartbeat(conn, owner=OWNER, now=add_seconds(now_utc(), 10))
    exp_after = conn.execute("SELECT expires_at FROM leases WHERE name='worker'").fetchone()[0]
    assert exp_after > exp_before


# ── worker loop ──────────────────────────────────────────────────────────────────

def _run_worker_thread(conn, stop, processed_list, paused=False):
    from v2_automation import service
    if paused:
        service.set_paused(conn, True)

    def fake_process(eid: int) -> str:
        processed_list.append(eid)
        return "queued"

    stats = worker.run_worker(_cfg(), conn, stop=stop, process_episode=fake_process,
                              loop_delay_s=0.05, cleanup_every_s=1e9)
    return stats


def test_worker_refuses_second_instance(conn):
    """Le bail est refusé tant qu'un processus LIVE le tient → pas de
    double-traitement même si on lance un second worker."""
    live = f"host:{os.getpid()}"
    _lease_row(conn, live, add_seconds(now_utc(), 300))
    processed = []
    stats = worker.run_worker(_cfg(), conn, stop=threading.Event(),
                              process_episode=lambda eid: processed.append(eid),
                              loop_delay_s=0.01, cleanup_every_s=1e9)
    assert stats["processed"] == 0
    assert stats["stop_reason"].startswith("busy:")
    assert processed == []


def test_worker_processes_heads_and_releases_lease(conn):
    a = _ep(conn, "anime-a", 1)
    b = _ep(conn, "anime-b", 2)
    stop, done = threading.Event(), {}
    processed = []

    def run():
        done["stats"] = _run_worker_thread(conn, stop, processed)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.6)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert sorted(processed) == sorted([a, b])
    assert done["stats"]["processed"] == 2
    assert conn.execute("SELECT COUNT(*) FROM leases WHERE name='worker'").fetchone()[0] == 0


def test_worker_respects_paused_control(conn):
    _ep(conn, "anime-a", 1)
    stop, processed = threading.Event(), []

    def run():
        _run_worker_thread(conn, stop, processed, paused=True)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.6)
    stop.set()
    t.join(timeout=10)
    assert processed == []       # gating : zéro traitement pendant pause
    # l'épisode reste dans la file, intact
    assert repo.queue_depth(conn) == 1