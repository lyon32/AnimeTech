"""The watcher never searches for a specific anime: it only detects what is NEW since its last scan.  A user search is
the opposite path.  Both must converge on the same media_key (one row, one job) whatever the order."""
import sqlite3
import threading

import pytest
from test_discovery import _jobs, _watch
from test_site_feed import FeedSite, _cfg_feed, _cycle
from test_site_feed import conn as feed_conn  # noqa: F401  (fixture)

from v2_automation import db, discovery, service
from v2_automation.catalog import SourceCatalog
from v2_automation.requests_mgr import NewRequest, RequestManager
from v2_automation.timeutil import now_utc
from v2support import BASE, Site, cfg


def is_search(url: str) -> bool:
    return "?s=" in url or "ajaxsearchpro" in url or "admin-ajax" in url


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _user_asks(conn, site, ep, user=1):
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    m.upsert_user(user)
    r = m.create(NewRequest(user_id=user, kind="episode", anime_key="postid:1", title="A", version="VOSTFR",
                            source_url=f"{BASE}/anime/a/", episode_number=ep))
    m.process(r["id"])
    return m, r


def test_a_watcher_check_never_issues_a_search_request(conn):
    site = Site(); site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), key, site)
    site.set("a", 1, [1, 2, 3])
    discovery.check_anime(conn, cfg(), key, site)
    assert site.calls and not any(is_search(u) for u in site.calls)
    assert all(u.startswith(f"{BASE}/anime/") for u in site.calls)                    # only pages it already knows


def test_a_full_global_cycle_with_the_feed_never_searches(feed_conn):
    _cycle.sched = None
    site = FeedSite()
    site.home([("iron-wok-jan", "Iron Wok Jan!", [(12, "1 minute ago")])])
    site.anime("iron-wok-jan", 501, {12: "1 minute ago", 11: "September 13, 2026"})
    _cycle(feed_conn, site, 1)
    _cycle(feed_conn, site, 2)
    assert site.calls and not any(is_search(u) for u in site.calls)
    assert _jobs(feed_conn, "postid:501") == [(12, "queued")]                         # detected once, never asked for


def test_baseline_then_incremental_only_new_content_becomes_a_job(conn):
    site = Site(); site.set("a", 1, [1, 2, 3])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), key, site)
    assert _jobs(conn, key) == []                                                     # nothing before the last scan
    site.set("a", 1, [1, 2, 3, 4, 5])
    discovery.check_anime(conn, cfg(), key, site)
    assert _jobs(conn, key) == [(4, "queued"), (5, "queued")]
    discovery.check_anime(conn, cfg(), key, site)
    assert len(_jobs(conn, key)) == 2                                                 # a known episode recreates nothing


def _count(conn, ep):
    return conn.execute("SELECT COUNT(*) FROM episodes WHERE anime_key='postid:1' AND episode_number=?", (ep,)).fetchone()[0]


def test_watcher_first_then_user_share_one_media_and_one_job(conn):
    site = Site(); site.set("a", 1, [1, 2]); _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), "postid:1", site)
    site.set("a", 1, [1, 2, 3])
    discovery.check_anime(conn, cfg(), "postid:1", site)
    m, r = _user_asks(conn, site, 3)
    assert _count(conn, 3) == 1 and len(_jobs(conn, "postid:1")) == 1
    row = conn.execute("SELECT origin, media_key FROM episodes WHERE episode_number=3").fetchone()
    assert row["origin"] == "watcher" and m.items(r["id"])[0]["media_key"] == row["media_key"]


def test_user_first_then_watcher_share_one_media_and_one_job(conn):
    site = Site(); site.set("a", 1, [1, 2]); _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), "postid:1", site)
    site.set("a", 1, [1, 2, 3])
    m, r = _user_asks(conn, site, 3)                                                  # the user is faster than the scan
    discovery.check_anime(conn, cfg(), "postid:1", site)                              # the watcher then sees E3
    assert _count(conn, 3) == 1 and len(_jobs(conn, "postid:1")) == 1
    row = conn.execute("SELECT origin, media_key FROM episodes WHERE episode_number=3").fetchone()
    assert row["origin"] == "user" and m.items(r["id"])[0]["media_key"] == row["media_key"]


def test_watcher_and_user_at_the_same_instant_one_media(tmp_path):
    path = str(tmp_path / "x.sqlite3")
    c0 = sqlite3.connect(path); c0.row_factory = sqlite3.Row; db.migrate(c0)
    _watch(c0, "a", 1)
    site = Site(); site.set("a", 1, [1, 2])
    discovery.check_anime(c0, cfg(), "postid:1", site)
    c0.close()
    site.set("a", 1, [1, 2, 3])
    barrier, errs = threading.Barrier(2), []

    def run(kind):
        c = sqlite3.connect(path, timeout=30); c.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=10)
            if kind == "watcher":
                discovery.check_anime(c, cfg(), "postid:1", site)
            else:
                _user_asks(c, site, 3)
        except Exception as exc:                                                      # pragma: no cover
            errs.append(repr(exc))
        finally:
            c.close()

    ts = [threading.Thread(target=run, args=(k,)) for k in ("watcher", "user")]
    [t.start() for t in ts]; [t.join(30) for t in ts]
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    assert not errs, errs
    assert c.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=3").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM queue_items q JOIN episodes e ON e.id=q.episode_id WHERE e.episode_number=3").fetchone()[0] == 1
    c.close()


def test_the_origin_is_visible_in_the_history_and_the_panel_query(conn):
    site = Site(); site.set("a", 1, [1, 2]); _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), "postid:1", site)
    site.set("a", 1, [1, 2, 3, 4])
    discovery.check_anime(conn, cfg(), "postid:1", site)
    _user_asks(conn, site, 4)                                                         # E4 already there: joined, origin stays
    origins = {r["episode_number"]: r["origin"] for r in conn.execute("SELECT episode_number, origin FROM episodes")}
    assert origins[3] == "watcher" and origins[4] == "watcher"
    items = service.episodes_query(conn, None, 50)
    assert items and all("origin" in i and "media_ref" in i for i in items)
    assert {i["origin"] for i in items} == {"watcher"}
