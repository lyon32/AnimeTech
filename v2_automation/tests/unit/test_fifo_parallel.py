"""Per-anime FIFO (strict, one active episode per anime) and cross-anime parallelism."""
import sqlite3
from pathlib import Path

import pytest

from v2_automation import db, recovery, repo
from v2_automation.models import Episode
from v2_automation.queues import QueueManager
from v2_automation.timeutil import add_seconds, now_utc


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _ep(conn, anime, n, status="queued"):
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{n}", episode_number=n,
                 canonical_episode_url=f"https://x/anime/{anime}/{n}", episode_url=f"https://x/anime/{anime}/{n}",
                 status="discovered")
    eid, _ = repo.upsert_episode(conn, ep)
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


def test_one_head_per_anime_in_order(conn):
    a1, a2, a3 = (_ep(conn, "A", n) for n in (1, 2, 3))
    assert repo.next_heads(conn, 10) == [a1]


def test_next_episode_waits_while_the_previous_is_processing(conn):
    a1, a2 = _ep(conn, "A", 1), _ep(conn, "A", 2)
    assert QueueManager(conn).dequeue_episode(a1)                 # E01 claimed (downloading/publishing)
    repo.transition(conn, a1, "downloading")
    conn.commit()
    assert repo.next_heads(conn, 10) == []                        # E02 must NOT start in parallel


def test_next_episode_does_not_overtake_a_retrying_predecessor(conn):
    a1, a2 = _ep(conn, "A", 1), _ep(conn, "A", 2)
    repo.transition(conn, a1, "retry_wait")
    conn.execute("UPDATE episodes SET next_retry_at=? WHERE id=?", (add_seconds(now_utc(), 600), a1))
    conn.commit()
    assert repo.next_heads(conn, 10) == []                        # E01 is waiting: E02 waits too
    conn.execute("UPDATE episodes SET next_retry_at=? WHERE id=?", (add_seconds(now_utc(), -5), a1))
    conn.commit()
    assert repo.next_heads(conn, 10) == [a1]                      # due again: E01 first


def test_next_episode_starts_once_the_previous_is_finished(conn):
    a1, a2 = _ep(conn, "A", 1), _ep(conn, "A", 2)
    QueueManager(conn).dequeue_episode(a1)
    repo.release_queue_item(conn, a1)                             # E01 published -> leaves the queue
    conn.commit()
    assert repo.next_heads(conn, 10) == [a2]


def test_animes_run_in_parallel(conn):
    heads = {a: _ep(conn, a, 1) for a in ("A", "B", "C")}
    _ep(conn, "A", 2)
    got = repo.next_heads(conn, 10)
    assert sorted(got) == sorted(heads.values())                  # one head each, A's E02 held back
    QueueManager(conn).dequeue_episode(heads["A"])
    assert sorted(repo.next_heads(conn, 10)) == sorted([heads["B"], heads["C"]])   # A busy, B and C free


def test_limit_caps_the_number_of_heads(conn):
    for a in ("A", "B", "C"):
        _ep(conn, a, 1)
    assert len(repo.next_heads(conn, 2)) == 2


def test_recovery_drops_queue_items_of_finished_episodes(conn):
    a1, a2 = _ep(conn, "A", 1), _ep(conn, "A", 2)
    conn.execute("UPDATE episodes SET status='cleanup_pending' WHERE id=?", (a1,))   # left by an old run
    conn.execute("UPDATE queue_items SET status='processing' WHERE episode_id=?", (a1,))   # never released
    conn.commit()
    res = _recover(conn)
    assert res["finished_items_dropped"] == 1
    assert repo.next_heads(conn, 10) == [a2]


def _recover(conn):
    from v2_automation.app_config import AppConfig, BotCapacity
    cfg = AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={}, limits={}, monitoring={},
                    logging={}, bot_token="", channel_id="", admin_telegram_ids=[],
                    bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))
    return recovery.run_recovery(conn, cfg)


def test_an_older_episode_detected_late_goes_before_a_waiting_newer_one(conn):
    """E49 is waiting for its source (retry); E36 is detected afterwards: E36 must not be stuck behind E49."""
    e49 = _ep(conn, "A", 49)
    repo.transition(conn, e49, "retry_wait")
    conn.execute("UPDATE episodes SET next_retry_at=? WHERE id=?", (add_seconds(now_utc(), 1800), e49))
    e36 = _ep(conn, "A", 36)                                      # enqueued later => higher position
    conn.commit()
    assert repo.next_heads(conn, 10) == [e36]
    QueueManager(conn).dequeue_episode(e36)
    assert repo.next_heads(conn, 10) == []                        # E49 still waits: E36 is active
    repo.release_queue_item(conn, e36)
    conn.commit()
    assert repo.next_heads(conn, 10) == []                        # E49 not due yet
