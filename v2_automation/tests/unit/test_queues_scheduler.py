"""Queue FIFO ordering + dynamic scheduler decisions (no network)."""
import sqlite3
from pathlib import Path

import pytest

from v2_automation import db, repo
from v2_automation.models import Episode
from v2_automation.queues import QueueManager
from v2_automation.scheduler import (Allowance, DownloadBandwidthEstimator,
                                     DynamicScheduler, schedule)


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite3.connect(tmp_path / "t.sqlite3")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _queued_episode(conn, key, anime, num=None):
    ep = Episode(anime_key=anime, episode_key=key,
                 canonical_episode_url=f"https://x/anime/{key}/",
                 language="vostfr", episode_number=num)
    eid, _ = repo.upsert_episode(conn, ep)
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    return eid


def test_fifo_strict_within_anime(conn):
    q = QueueManager(conn)
    a1 = _queued_episode(conn, "a-01", "animeA", num=1)
    a2 = _queued_episode(conn, "a-02", "animeA", num=2)
    a3 = _queued_episode(conn, "a-03", "animeA", num=3)
    q.enqueue("animeA", a1)
    q.enqueue("animeA", a2)
    q.enqueue("animeA", a3)
    assert q.heads(10) == [a1]                    # jamais a2 avant a1
    head = q.dequeue_head_of("animeA")
    assert head == a1
    q.unqueue("animeA", head)
    assert q.heads(10) == [a1]                    # re-queue -> toujours devant
    # dequeue all heads sequentially and verify order
    order = []
    while True:
        h = q.dequeue_head_of("animeA")
        if h is None:
            break
        order.append(h)
    assert order == [a1, a2, a3]


def test_cross_anime_independence(conn):
    q = QueueManager(conn)
    x1 = _queued_episode(conn, "x1", "animeX", num=1)
    y1 = _queued_episode(conn, "y1", "animeY", num=1)
    q.enqueue("animeX", x1)
    q.enqueue("animeY", y1)
    assert q.heads(10) == [x1, y1] or q.heads(10) == [y1, x1]   # tete par anime
    assert sorted(q.heads(10)) == sorted([x1, y1])


def test_schedule_disk_gate():
    a = schedule(base_downloads=2, max_publications=1, pending_animes=5,
                 free_disk_bytes=1_000, min_free_disk_bytes=5_000_000_000,
                 next_estimated_bytes=None, estimator=None)
    assert a.downloads == 0 and a.paused and "disk-bas" in a.reason


def test_schedule_size_gate():
    a = schedule(base_downloads=2, max_publications=1, pending_animes=3,
                 free_disk_bytes=10_000_000_000, min_free_disk_bytes=1_000_000_000,
                 next_estimated_bytes=20_000_000_000, estimator=None)
    assert a.downloads == 0 and a.paused


def test_schedule_capped_by_pending_animes():
    a = schedule(base_downloads=8, max_publications=1, pending_animes=2,
                 free_disk_bytes=10_000_000_000, min_free_disk_bytes=0,
                 next_estimated_bytes=None, estimator=None)
    assert a.downloads == 2 and a.reason == "borne-par-nb-animes"


def test_schedule_boost_after_healthy_burst():
    est = DownloadBandwidthEstimator()
    for i in range(4):
        est.add_sample(4_000_000)
    a = schedule(base_downloads=1, max_publications=1, pending_animes=9,
                 free_disk_bytes=100_000_000_000, min_free_disk_bytes=0,
                 next_estimated_bytes=None, estimator=est)
    assert a.downloads == 2                      # base*2 max
    assert "+boost" in a.reason


def test_schedule_no_boost_when_slow():
    est = DownloadBandwidthEstimator()
    for i in range(6):
        est.add_sample(100_000)                  # lent
    a = schedule(base_downloads=1, max_publications=1, pending_animes=3,
                 free_disk_bytes=100_000_000_000, min_free_disk_bytes=0,
                 next_estimated_bytes=None, estimator=est)
    assert a.downloads == 1 and "boost" not in a.reason


def test_dynamic_scheduler_observes_transfers():
    s = DynamicScheduler({"max_concurrent_downloads": 1,
                          "max_concurrent_publications": 1,
                          "min_free_disk_bytes": 0})
    s.observe_transfer(5_000_000)
    s.observe_transfer(5_500_000)
    s.observe_transfer(4_800_000)
    s.tick(pending_animes=4, free_disk_bytes=10_000_000_000)
    assert s.last is not None and s.last.downloads == 2