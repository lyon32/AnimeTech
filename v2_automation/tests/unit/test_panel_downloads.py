"""Panel downloads: one episode / a selection / a whole season / the last N of an anime page, published to the channel.
Same identity as the watcher and the bot (one media_key, one job) and the same worker order (ascending per anime)."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_discovery import Site, _watch, _jobs
from bot_harness import World

from v2_automation import db, discovery, panel_downloads, repo
from v2_automation.search import SourceSearch
from v2_automation.web import create_app
from v2support import BASE, cfg


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def site():
    s = Site()
    s.set("re-zero-s4", 777, [1, 2, 3, 4, 5], title="Re:Zero S4")
    return s


@pytest.fixture()
def client(conn, site):
    app = create_app(connect=lambda: conn, cfg_factory=lambda: cfg(), fetch=site, auth=None)
    with TestClient(app) as tc:
        yield tc


URL = f"{BASE}/anime/re-zero-s4/"


def post(client, **body):
    return client.post("/api/downloads", json={"source_url": URL, "version": "VOSTFR", **body})


def test_the_episode_list_shows_the_real_episodes_and_what_is_known(client, conn):
    r = client.get("/api/anime-episodes", params={"url": URL}).json()
    assert [e["number"] for e in r["items"]] == [1, 2, 3, 4, 5] and all(e["known"] is None for e in r["items"])
    post(client, mode="episode", numbers=[2])
    r = client.get("/api/anime-episodes", params={"url": URL}).json()
    assert r["items"][1]["known"]["status"] == "queued" and r["items"][0]["known"] is None


def test_one_episode_is_queued_for_the_channel_with_a_readable_identity(client, conn):
    r = post(client, mode="episode", numbers=[3]).json()
    assert r["ok"] and len(r["created"]) == 1
    ep = conn.execute("SELECT * FROM episodes WHERE episode_number=3").fetchone()
    assert ep["origin"] == "panel" and ep["publish_channel"] == 1 and ep["status"] == "queued"
    assert ep["media_ref"] == "voir-anime.to|postid:777|S00|E3|VOSTFR"
    assert conn.execute("SELECT COUNT(*) FROM animes").fetchone()[0] == 0        # not watched: no side effect


def test_a_whole_season_is_queued_and_processed_in_ascending_order(client, conn):
    r = post(client, mode="season").json()
    assert [c["number"] for c in r["created"]] == [1, 2, 3, 4, 5]
    assert repo.next_heads(conn, 10) == [conn.execute("SELECT id FROM episodes WHERE episode_number=1").fetchone()[0]]
    assert _jobs(conn, "postid:777") == [(n, "queued") for n in (1, 2, 3, 4, 5)]


def test_the_last_n_episodes(client, conn):
    r = post(client, mode="last_n", n=2).json()
    assert [c["number"] for c in r["created"]] == [4, 5]


def test_asking_twice_or_after_the_watcher_creates_no_second_job(client, conn, site):
    post(client, mode="selection", numbers=[1, 2])
    again = post(client, mode="selection", numbers=[1, 2, 3]).json()
    assert [c["number"] for c in again["created"]] == [3] and len(again["already_queued"]) == 2
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3


def test_a_baseline_episode_of_a_watched_anime_is_published_when_the_admin_asks(client, conn, site):
    key = _watch(conn, "re-zero-s4", 777, "Re:Zero S4")
    discovery.check_anime(conn, cfg(), key, site)                                 # baseline: known, never published
    assert _jobs(conn, key) == []
    r = post(client, mode="episode", numbers=[5]).json()
    assert len(r["created"]) == 1
    assert conn.execute("SELECT publish_channel FROM episodes WHERE episode_number=5").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=5").fetchone()[0] == 1


def test_published_episodes_are_reported_not_requeued(client, conn):
    post(client, mode="episode", numbers=[1])
    conn.execute("UPDATE episodes SET status='published' WHERE episode_number=1")
    conn.commit()
    r = post(client, mode="episode", numbers=[1]).json()
    assert len(r["already_published"]) == 1 and not r["created"]


def test_bad_requests_are_refused_in_french(client):
    assert post(client, mode="episode", numbers=[]).status_code == 400
    assert post(client, mode="episode", numbers=[99]).status_code == 400
    assert post(client, mode="nope").status_code == 400
    assert client.post("/api/downloads", json={"source_url": "https://evil.example/anime/x/", "mode": "season"}).status_code == 400


def test_watch_option_adds_the_anime_to_the_watched_list(client, conn):
    r = post(client, mode="episode", numbers=[1], watch=True).json()
    assert r["watched"] and conn.execute("SELECT enabled FROM animes WHERE anime_key='postid:777'").fetchone()[0] == 1


def test_the_action_is_audited(client, conn):
    from v2_automation import audit
    post(client, mode="season")
    assert audit.recent(conn)[0]["action"] == "panel_download"


def test_search_groups_series_seasons_and_versions_like_the_bot(conn):
    w = World()
    s = SourceSearch(cfg(), conn, fetch=w, post=w.post)
    r = panel_downloads.search(conn, cfg(), "wakfu", searcher=s)
    assert r["ok"] and r["items"] and r["items"][0]["name"] == "Wakfu"
    assert r["items"][0]["versions"] == ["VF"] and len(r["items"][0]["seasons"]) == 4
    assert panel_downloads.search(conn, cfg(), "a")["ok"] is False
