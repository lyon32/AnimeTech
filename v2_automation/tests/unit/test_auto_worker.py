"""The automatic worker loop end to end (real DB, real worker loop, real scheduler + discovery;
only the episode processing is a timed stand-in): parallel animes, strict FIFO per anime,
host-pressure throttling, detection -> queue -> processing, restart without duplicates."""
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from test_discovery import BASE, Site, _cfg, _watch

from v2_automation import db, discovery, repo, worker
from v2_automation.models import Episode
from v2_automation.scheduler import DynamicScheduler


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _queue(conn, anime, n):
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{n}", episode_number=n, status="discovered",
                 canonical_episode_url=f"https://x/anime/{anime}/{n}", episode_url=f"https://x/anime/{anime}/{n}")
    eid, _ = repo.upsert_episode(conn, ep)
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


class Recorder:
    """Stand-in for DownloadManager.process_episode: takes `work_s`, records overlap, finishes the episode."""
    def __init__(self, conn, work_s=0.3):
        self.conn, self.work_s, self.lock = conn, work_s, threading.Lock()
        self.running, self.max_running, self.order, self.by_anime_running, self.max_by_anime = 0, 0, [], {}, {}

    def __call__(self, eid):
        ep = repo.get(self.conn, eid)
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.order.append((ep.anime_key, ep.episode_number))
            n = self.by_anime_running.get(ep.anime_key, 0) + 1
            self.by_anime_running[ep.anime_key] = n
            self.max_by_anime[ep.anime_key] = max(self.max_by_anime.get(ep.anime_key, 0), n)
        time.sleep(self.work_s)
        with self.lock:
            self.conn.execute("UPDATE episodes SET status='cleanup_pending' WHERE id=?", (eid,))
            repo.release_queue_item(self.conn, eid)          # what DownloadManager does at the end state
            self.conn.commit()
            self.running -= 1
            self.by_anime_running[ep.anime_key] -= 1


def _run_worker(conn, cfg, proc, *, until, scheduler=None, discovery=False, system_fn=None, timeout=20):
    stop = threading.Event()
    out = {}

    def target():
        out.update(worker.run_worker(
            cfg, conn, stop=stop, process_episode=proc, loop_delay_s=0.02, discovery=discovery,
            scheduler=scheduler or DynamicScheduler({"max_concurrent_downloads": 3, "min_free_disk_bytes": 0}),
            system_fn=system_fn or (lambda: {"cpu_percent": 10.0, "ram_percent": 30.0, "free_disk_bytes": 10 ** 12}),
            reconcile_every_s=10 ** 6, cleanup_every_s=10 ** 6))
    t = threading.Thread(target=target)
    t.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        time.sleep(0.02)
    stop.set()
    t.join(15)
    assert not t.is_alive()
    return out


def test_several_animes_download_in_parallel(conn):
    for a in ("A", "B", "C"):
        _queue(conn, a, 1)
    rec = Recorder(conn, work_s=0.5)
    out = _run_worker(conn, _cfg(), rec, until=lambda: len(rec.order) == 3 and rec.running == 0)
    assert rec.max_running == 3 and out["active_max"] == 3               # A, B, C at the same time


def test_fifo_per_anime_while_other_animes_run(conn):
    for n in (1, 2, 3):
        _queue(conn, "A", n)
    for n in (1, 2):
        _queue(conn, "B", n)
    rec = Recorder(conn, work_s=0.15)
    _run_worker(conn, _cfg(), rec, until=lambda: len(rec.order) == 5 and rec.running == 0)
    a_order = [n for a, n in rec.order if a == "A"]
    b_order = [n for a, n in rec.order if a == "B"]
    assert a_order == [1, 2, 3] and b_order == [1, 2]                   # E01 before E02 before E03
    assert rec.max_by_anime["A"] == 1 and rec.max_by_anime["B"] == 1    # never two at once for one anime
    assert rec.max_running == 2                                          # but A and B overlap


def test_host_pressure_throttles_parallelism(conn):
    for a in ("A", "B", "C"):
        _queue(conn, a, 1)
    rec = Recorder(conn, work_s=0.15)
    _run_worker(conn, _cfg(), rec, until=lambda: len(rec.order) == 3 and rec.running == 0,
                system_fn=lambda: {"cpu_percent": 99.0, "ram_percent": 30.0, "free_disk_bytes": 10 ** 12})
    assert rec.max_running == 1                                          # CPU saturated: one at a time


def test_low_disk_pauses_new_downloads(conn):
    _queue(conn, "A", 1)
    rec = Recorder(conn, work_s=0.05)
    sched = DynamicScheduler({"max_concurrent_downloads": 3, "min_free_disk_bytes": 5 * 10 ** 9})
    _run_worker(conn, _cfg(), rec, until=lambda: False, scheduler=sched, timeout=0.6,
                system_fn=lambda: {"cpu_percent": 5.0, "ram_percent": 5.0, "free_disk_bytes": 10 ** 9})
    assert rec.order == []                                               # nothing starts below the disk floor


def test_new_episode_is_detected_queued_and_processed_automatically(conn):
    site = Site(); site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)                       # the anime was added earlier: baseline done
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=0.25)
    rec = Recorder(conn, work_s=0.05)

    def publish_e03_later():
        time.sleep(0.3)
        site.set("a", 1, [1, 2, 3])
    threading.Thread(target=publish_e03_later, daemon=True).start()
    _run_worker(conn, _cfg(), rec, discovery=sch, until=lambda: len(rec.order) == 1 and rec.running == 0)
    assert rec.order == [(key, 3)]                                       # baseline E01/E02 never processed
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=3").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM episodes WHERE episode_number=3").fetchone()[0] == "cleanup_pending"


def test_restart_does_not_reprocess_or_duplicate(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)                       # baseline
    site.set("a", 1, [1, 2])
    rec1 = Recorder(conn, work_s=0.05)
    _run_worker(conn, _cfg(), rec1, discovery=discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=0.1),
                until=lambda: len(rec1.order) == 1 and rec1.running == 0)
    rec2 = Recorder(conn, work_s=0.05)                                   # restart: new worker, same database
    _run_worker(conn, _cfg(), rec2, discovery=discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=0.1),
                until=lambda: False, timeout=0.8)
    assert rec1.order == [(key, 2)] and rec2.order == []
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=2").fetchone()[0] == 1


def test_a_discovery_crash_does_not_stop_downloads(conn):
    _queue(conn, "A", 1)

    class Broken:
        def tick(self):
            raise RuntimeError("source scheduler exploded")

        def shutdown(self, wait=True):
            pass
    rec = Recorder(conn, work_s=0.05)
    _run_worker(conn, _cfg(), rec, discovery=Broken(), until=lambda: len(rec.order) == 1 and rec.running == 0)
    assert rec.order == [("A", 1)]
