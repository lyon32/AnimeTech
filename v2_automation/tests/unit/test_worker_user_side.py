"""The real worker loop serving user requests: independent jobs per anime, parallel downloads, one anime failing never blocks the
others, requests delivered from the same loop (real DB, real worker, real scheduler; only the download is a timed stand-in and
Telegram a recording transport).  Plus the 14-day cleanup on a controllable clock."""
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from test_delivery import FakeTransport

from v2_automation import cleanup, db, repo
from v2_automation.catalog import SourceCatalog
from v2_automation.delivery import DeliveryEngine
from v2_automation.requests_mgr import NewRequest, RequestManager
from v2_automation.telegram_publisher import TelegramPublisher
from v2_automation.timeutil import add_seconds, now_utc
from v2_automation.user_side import UserSide
from v2support import BASE, Site, cfg


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


class ReadyMaker:
    """Stand-in for DownloadManager.process_episode on PRIVATE media: takes time, leaves a real file, ends at READY."""

    def __init__(self, conn, root: Path, work_s=0.4, fail_for=()):
        self.conn, self.root, self.work_s, self.fail_for = conn, root, work_s, set(fail_for)
        self.lock = threading.Lock()
        self.running = self.max_running = 0
        self.calls: list[tuple] = []

    def __call__(self, eid):
        ep = repo.get(self.conn, eid)
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.calls.append((ep.anime_key, ep.episode_number))
            repo.bump_attempt(self.conn, eid)              # what DownloadManager.process_episode does on every attempt
        time.sleep(self.work_s)
        with self.lock:
            if ep.anime_key in self.fail_for:
                repo.transition(self.conn, eid, "downloading")
                repo.transition(self.conn, eid, "retry_wait")
                self.conn.execute("UPDATE episodes SET last_error='DOWNLOAD_FAILED: boom', next_retry_at=? WHERE id=?",
                                  (add_seconds(now_utc(), 3600), eid))
                self.conn.execute("UPDATE queue_items SET status='queued' WHERE episode_id=?", (eid,))
            else:
                f = self.root / f"{eid}.mp4"
                f.write_bytes(b"v" * 300)
                for st in ("downloading", "downloaded", "validating", "validated", "ready"):
                    repo.transition(self.conn, eid, st)
                self.conn.execute("UPDATE episodes SET file_path=?, file_size=300 WHERE id=?", (str(f), eid))
                repo.release_queue_item(self.conn, eid)
            self.conn.commit()
            self.running -= 1


def _run_worker(conn, cfg_, proc, *, user_side, until, timeout=25):
    from v2_automation import worker
    from v2_automation.scheduler import DynamicScheduler
    stop = threading.Event()
    out = {}

    def target():
        out.update(worker.run_worker(
            cfg_, conn, stop=stop, process_episode=proc, loop_delay_s=0.02, discovery=False, user_side=user_side,
            scheduler=DynamicScheduler({"max_concurrent_downloads": 3, "min_free_disk_bytes": 0}),
            system_fn=lambda: {"cpu_percent": 10.0, "ram_percent": 30.0, "free_disk_bytes": 10 ** 12},
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


def _requests(conn, site, spec):
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    for user, (slug, post, ep) in spec.items():
        m.upsert_user(user)
        r = m.create(NewRequest(user_id=user, kind="episode", anime_key=f"postid:{post}", title=slug.upper(), version="VOSTFR",
                                source_url=f"{BASE}/anime/{slug}/", episode_number=ep))
        m.process(r["id"])
    return m


def test_three_anime_download_in_parallel_and_are_delivered_by_the_worker_loop(conn, tmp_path):
    site = Site()
    for slug, post, n in (("a", 1, 1), ("b", 2, 5), ("c", 3, 8)):
        site.set(slug, post, list(range(1, n + 1)), title=slug.upper())
    m = _requests(conn, site, {101: ("a", 1, 1), 102: ("b", 2, 5), 103: ("c", 3, 8)})
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3                      # three independent jobs
    tr = FakeTransport()
    us = UserSide(cfg(), conn, transport=tr, catalog=SourceCatalog(cfg(), fetch=site))
    proc = ReadyMaker(conn, tmp_path, work_s=0.5)
    out = _run_worker(conn, cfg(), proc, user_side=us,
                      until=lambda: conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent'").fetchone()[0] == 3)
    assert proc.max_running == 3 and out["active_max"] == 3                                      # no global blocking worker
    assert sorted(proc.calls) == [("postid:1", 1), ("postid:2", 5), ("postid:3", 8)]              # each downloaded exactly once
    assert sorted(c[1] for c in tr.calls if c[0] == "send_video") == [101, 102, 103]              # each user got their own media
    assert {r["state"] for r in m.history(101) + m.history(102) + m.history(103)} <= {"COMPLETED", "DELIVERING"}


def test_one_failing_anime_never_blocks_the_others(conn, tmp_path):
    site = Site()
    for slug, post in (("a", 1), ("b", 2), ("c", 3)):
        site.set(slug, post, [1, 2], title=slug.upper())
    m = _requests(conn, site, {1: ("a", 1, 1), 2: ("b", 2, 1), 3: ("c", 3, 1)})
    tr = FakeTransport()
    us = UserSide(cfg(), conn, transport=tr, catalog=SourceCatalog(cfg(), fetch=site))
    proc = ReadyMaker(conn, tmp_path, work_s=0.2, fail_for=("postid:2",))
    _run_worker(conn, cfg(), proc, user_side=us,
                until=lambda: conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent'").fetchone()[0] == 2)
    assert {c[1] for c in tr.calls if c[0] == "send_video"} == {1, 3}                             # A and C served
    assert conn.execute("SELECT status FROM episodes WHERE anime_key='postid:2'").fetchone()[0] == "retry_wait"   # B retries later
    assert conn.execute("SELECT attempt_count FROM episodes WHERE anime_key='postid:2'").fetchone()[0] == 1       # bounded, not looping


def test_user_side_isolates_a_failing_part(conn):
    us = UserSide(cfg(), conn, transport=FakeTransport())
    us.engine.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down"))
    out = us.tick()
    assert out["delivery"].startswith("error") and "requests" in out and not str(out["requests"]).startswith("error")   # the rest still ran


# -- cleanup J+14 on a controllable clock ------------------------------------------------------

def _published(conn, tmp_path, published_at, *, private=False):
    f = tmp_path / "ep.mp4"
    f.write_bytes(b"v" * 100)
    ep_status = "published"
    eid, _ = repo.upsert_episode(conn, __import__("v2_automation.models", fromlist=["Episode"]).Episode(
        anime_key="postid:1", episode_key="k", canonical_episode_url="k/", episode_number=1, status=ep_status,
        publish_channel=0 if private else 1))
    conn.execute("UPDATE episodes SET file_path=?, file_size=100, published_at=?, video_message_id=? WHERE id=?",
                 (str(f), published_at, None if private else 77, eid))
    conn.execute("INSERT INTO publications(episode_id, publication_type, status, chat_id, message_id) VALUES (?, 'first_publication', 'sent', '-100', 77)", (eid,))
    conn.commit()
    return eid, f


def test_cleanup_deletes_only_the_local_file_after_14_days_and_keeps_everything_else(conn, tmp_path):
    T = add_seconds(now_utc(), -(13 * 86400 + 23 * 3600))                          # published 13 d 23 h ago
    eid, f = _published(conn, tmp_path, T)
    res = cleanup.run_cleanup(conn, cfg(publication={"cleanup_after_days": 14}))
    assert res["cleaned"] == 0 and f.exists()                                       # T + 13.96 d : still kept
    conn.execute("UPDATE episodes SET published_at=? WHERE id=?", (add_seconds(now_utc(), -(14 * 86400 + 60)), eid))
    conn.commit()                                                                   # "the clock reached T + 14 d"
    res = cleanup.run_cleanup(conn, cfg(publication={"cleanup_after_days": 14}))
    assert res["cleaned"] == 1 and not f.exists()                                   # local file deleted
    row = conn.execute("SELECT status, video_message_id, published_at, media_key FROM episodes WHERE id=?", (eid,)).fetchone()
    assert row["status"] == "cleaned" and row["video_message_id"] == 77 and row["published_at"] and row["media_key"]   # record + Telegram ref remain
    assert conn.execute("SELECT message_id FROM publications WHERE episode_id=?", (eid,)).fetchone()[0] == 77
    assert cleanup.run_cleanup(conn, cfg(publication={"cleanup_after_days": 14}))["cleaned"] == 0    # after a restart: nothing more, no error


def test_cleanup_window_is_configurable(conn, tmp_path):
    eid, f = _published(conn, tmp_path, add_seconds(now_utc(), -(8 * 86400)))
    assert cleanup.run_cleanup(conn, cfg(publication={"cleanup_after_days": 14}))["cleaned"] == 0
    assert cleanup.run_cleanup(conn, cfg(publication={"cleanup_after_days": 7}))["cleaned"] == 1 and not f.exists()


def test_private_media_is_cleaned_only_once_a_delivered_copy_exists_and_is_then_served_from_it(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _requests(conn, site, {1: ("u", 2, 1)})
    eid = conn.execute("SELECT id FROM episodes").fetchone()[0]
    f = tmp_path / "p.mp4"
    f.write_bytes(b"v" * 100)
    conn.execute("UPDATE episodes SET status='ready', file_path=?, file_size=100, publish_channel=0 WHERE id=?", (str(f), eid))
    conn.commit()
    old = add_seconds(now_utc(), -(15 * 86400))
    conn.execute("UPDATE episodes SET published_at=? WHERE id=?", (old, eid))                                # (not delivered yet)
    conn.commit()
    tr = FakeTransport()
    DeliveryEngine(conn, TelegramPublisher(tr), cfg()).run()                                                 # first delivery: file_id stored
    conn.execute("UPDATE episodes SET published_at=? WHERE id=?", (old, eid))
    conn.commit()
    assert cleanup.run_cleanup(conn, cfg(publication={"cleanup_after_days": 14}))["cleaned"] == 1 and not f.exists()
    _requests(conn, site, {2: ("u", 2, 1)})                                                                   # a new user, later
    DeliveryEngine(conn, TelegramPublisher(tr), cfg()).run()
    assert [c[2] for c in tr.calls if c[0] == "send_video"] == ["file", "file_id"]                            # no re-download, no re-upload
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
