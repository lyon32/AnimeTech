"""Operational control (closure) tests — pause/anime gates on the two single
chokepoints (repo.next_heads + queue dequeue), plus the service manual actions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from v2_automation import db, queues, repo, service
from v2_automation.models import Episode


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _ep(conn, anime="anime-a", epnum=1, status="queued"):
    ep = Episode(anime_key=anime, episode_key=f"{anime}-e{epnum:02d}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e{epnum}",
                 episode_number=epnum, status=status)
    eid, _ = repo.upsert_episode(conn, ep)
    if status == "queued":
        repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


def test_pause_blocks_next_heads_and_dequeue(conn):
    a1, a2 = _ep(conn, "anime-a", 1), _ep(conn, "anime-b", 1)
    assert len(repo.next_heads(conn, 10)) == 2
    service.set_paused(conn, True)
    assert repo.next_heads(conn, 10) == []
    qm = queues.QueueManager(conn)
    assert qm.dequeue_episode(a1) is False
    assert qm.dequeue_head_of("anime-a") is None
    service.set_paused(conn, False)
    assert sorted(repo.next_heads(conn, 10)) == sorted([a1, a2])
    assert qm.dequeue_episode(a1) is True


def test_disabled_anime_excluded_from_heads(conn):
    a1, e1b = _ep(conn, "anime-a", 1), _ep(conn, "anime-b", 1)
    service.upsert_anime(conn, "anime-a", enabled=False)
    heads = repo.next_heads(conn, 10)
    assert heads == [e1b]                      # anime-a hors-jeu
    service.set_anime_enabled(conn, "anime-a", True)
    assert sorted(repo.next_heads(conn, 10)) == sorted([a1, e1b])
    service.set_anime_enabled(conn, "anime-a", False)
    qm = queues.QueueManager(conn)
    assert qm.dequeue_episode(a1) is False     # gate aussi au dequeue
    assert repo.queue_depth(conn) == 2


def test_unknown_anime_treated_as_enabled_legacy(conn):
    eid = _ep(conn, "anime-legacy", 1)         # jamais déclaré dans animes
    assert repo.next_heads(conn, 10) == [eid]
    assert queues.QueueManager(conn).dequeue_episode(eid) is True


def test_upsert_anime_to_pause_and_reactivate(conn):
    service.upsert_anime(conn, "anime-a", title="Un Anime")
    assert service.anime_list(conn)[0]["enabled"] == 1
    service.upsert_anime(conn, "anime-a", enabled=False)
    assert service.anime_list(conn)[0]["enabled"] == 0
    service.upsert_anime(conn, "anime-a", enabled=True, title="Renommé")
    row = service.anime_list(conn)[0]
    assert row["enabled"] == 1 and row["title"] == "Renommé"


def test_is_paused_roundtrip(conn):
    assert service.is_paused(conn) is False
    service.set_paused(conn, True)
    assert service.is_paused(conn) is True
    service.set_paused(conn, False)
    assert service.is_paused(conn) is False


def test_next_heads_respects_retry_time_even_with_control(conn):
    """Gate composé : enabled + non-paused n'interfèrent pas avec next_retry_at."""
    from v2_automation.repo import set_retry_until
    eid = _ep(conn, "anime-a", 1)
    set_retry_until(conn, eid, "2099-01-01T00:00:00Z", 1, "wait", "2099-01-01T00:00:00Z")
    conn.commit()
    assert repo.next_heads(conn, 10) == []
    assert repo.next_heads(conn, 10, now="2099-01-01T00:00:01Z") == [eid]


def test_cancel_episode_removes_from_queue(conn):
    eid = _ep(conn, "anime-a", 1, status="queued")
    res = service.cancel_episode(conn, eid)
    assert res["ok"] is True
    assert repo.queue_depth(conn) == 0
    ep = repo.get(conn, eid)
    assert ep.status == "failed"
    assert "manuellement" in (ep.last_error or "")
    # irréversible côté auto : un requeue reste possible ensuite
    rq = service.requeue_episode(conn, eid)
    assert rq["ok"] is True and repo.queue_depth(conn) == 1


def test_cancel_not_cancellable_from_published(conn):
    eid = _ep(conn, "anime-a", 1, status="published")
    res = service.cancel_episode(conn, eid)
    assert res["ok"] is False
    assert repo.get(conn, eid).status == "published"