"""CORE MEDIA ENGINE: media identity, shared jobs (watcher + users), request model, dedup, expiry, cancel, history.

Real SQLite database and the real source parsers; only the source's HTTP layer is faked (a page string).
"""
import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from v2_automation import db, discovery, media, repo
from v2_automation.catalog import SourceCatalog
from v2_automation.requests_mgr import (ActiveRequestExists, NewRequest, RequestManager, RequestState as RS,
                                        can_transition)
from v2_automation.timeutil import now_utc
from v2support import BASE, Clock, Site, cfg


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def site():
    return Site()


def mgr(conn, site, clock=None, **kw):
    return RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=clock or now_utc, **kw)


def new(user, slug="a", post=1, ep=1, version="VOSTFR", kind="episode", **kw):
    return NewRequest(user_id=user, kind=kind, anime_key=f"postid:{post}", title="Anime Test", version=version,
                      source_url=f"{BASE}/anime/{slug}/", episode_number=ep, **kw)


def ask(m, user, **kw):
    m.upsert_user(user)
    req = m.create(new(user, **kw))
    return m.process(req["id"])


def n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


# -- A. media_key -------------------------------------------------------------------

def test_media_key_deterministic_and_case_insensitive():
    a = media.compute_media_key("postid:1", None, 1150, "vf")
    assert a == media.compute_media_key("postid:1", 0, 1150, "VF") == media.compute_media_key("postid:1", None, 1150, " Vf ")
    assert a.startswith("m_") and len(a) == 34


def test_media_key_differs_by_version_episode_anime_season():
    base = media.compute_media_key("postid:1", None, 1150, "VF")
    assert base != media.compute_media_key("postid:1", None, 1150, "VOSTFR")          # VF != VOSTFR
    assert base != media.compute_media_key("postid:1", None, 1151, "VF")
    assert base != media.compute_media_key("postid:2", None, 1150, "VF")
    assert base != media.compute_media_key("postid:1", 2, 1150, "VF")


def test_media_key_unnumbered_uses_url_and_requires_one():
    a = media.compute_media_key("postid:1", None, None, "VF", "https://x/film-1")
    assert a != media.compute_media_key("postid:1", None, None, "VF", "https://x/film-2")
    with pytest.raises(ValueError):
        media.compute_media_key("postid:1", None, None, "VF")


def test_media_state_view_and_expired():
    assert media.media_state("validated") is media.MediaState.READY
    assert media.media_state("cleaned") is media.MediaState.PUBLISHED
    assert media.media_state("thumbnail_published") is media.MediaState.PUBLISHING
    assert media.media_state("failed", "NOT_AVAILABLE_YET: fenetre") is media.MediaState.EXPIRED
    assert media.media_state("failed", "DOWNLOAD_FAILED: x") is media.MediaState.FAILED
    assert {s.value for s in media.MediaState} >= {"DISCOVERED", "QUEUED", "DOWNLOADING", "DOWNLOADED", "VALIDATING",
                                                   "READY", "PUBLISHING", "PUBLISHED", "FAILED", "RETRY_WAIT", "EXPIRED"}


def test_request_transitions():
    assert can_transition(RS.PENDING, RS.SEARCHING) and can_transition(RS.WAITING_FOR_MEDIA, RS.EXPIRED)
    assert not can_transition(RS.COMPLETED, RS.PENDING) and not can_transition(RS.PENDING, RS.DELIVERING)


def test_backfill_gives_legacy_rows_a_key(conn):
    conn.execute("INSERT INTO episodes(anime_key, episode_key, canonical_episode_url, language, episode_number, media_key) "
                 "VALUES ('postid:9','k9','k9/','vf',3, NULL)")
    conn.commit()
    assert media.backfill_media_keys(conn) == 1
    assert n(conn, "SELECT media_key FROM episodes") == media.compute_media_key("postid:9", None, 3, "vf")
    assert media.backfill_media_keys(conn) == 0                                        # idempotent


# -- C. dedup: 3 users, one media -> ONE download -------------------------------------

def test_three_users_one_media_one_download(conn, site, caplog):
    site.set("a", 1, [1, 2, 3, 4, 5])
    m = mgr(conn, site)
    with caplog.at_level(logging.INFO, logger="v2_automation.media"):
        reqs = [ask(m, u, ep=5) for u in (101, 102, 103)]
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 1                               # one media row
    assert n(conn, "SELECT COUNT(*) FROM queue_items") == 1                            # one job in the queue
    assert n(conn, "SELECT COUNT(DISTINCT episode_id) FROM request_items") == 1
    assert n(conn, "SELECT COUNT(*) FROM request_items") == 3                          # three requests reference it
    assert n(conn, "SELECT COUNT(*) FROM requests") == 3
    actions = [r.getMessage().split("action=")[1].split()[0] for r in caplog.records if "[MEDIA]" in r.getMessage()]
    assert actions == ["created", "joined", "joined"]                                  # events: 1 job created, 2 joins
    assert all(r["state"] == "QUEUED" for r in reqs)


def test_second_request_for_same_user_refused_and_season_counts_one(conn, site):
    site.set("a", 1, [1, 2, 3])
    m = mgr(conn, site)
    first = ask(m, 7, kind="season", ep=None)
    assert first["state"] == "QUEUED" and n(conn, "SELECT COUNT(*) FROM requests") == 1
    with pytest.raises(ActiveRequestExists) as ei:
        m.create(new(7, ep=1))
    assert ei.value.request["id"] == first["id"]
    assert n(conn, "SELECT COUNT(*) FROM requests WHERE user_id=7") == 1              # a season = ONE active request, not 3


def test_database_itself_refuses_a_second_active_request(conn, site):
    site.set("a", 1, [1])
    m = mgr(conn, site)
    m.upsert_user(5)
    m.create(new(5))
    with pytest.raises(sqlite3.IntegrityError):                                        # bypass the manager: raw insert
        conn.execute("INSERT INTO requests(user_id,kind,anime_key,version,state,created_at,updated_at) "
                     "VALUES (5,'episode','postid:1','VF','PENDING','t','t')")


def test_terminal_request_frees_the_user(conn, site):
    site.set("a", 1, [1, 2])
    m = mgr(conn, site)
    r = ask(m, 8, ep=1)
    m.cancel(r["id"])
    assert m.active_request(8) is None
    assert ask(m, 8, ep=2)["state"] == "QUEUED"


# -- D. season = parent request + child jobs -------------------------------------------

def test_season_is_one_request_with_one_job_per_episode(conn, site):
    site.set("bleach", 3, [1, 2, 3], title="Bleach")
    m = mgr(conn, site)
    r = ask(m, 1, slug="bleach", post=3, kind="season", ep=None)
    assert n(conn, "SELECT COUNT(*) FROM requests") == 1                               # request_count = 1
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 3                               # media_jobs = 3
    assert n(conn, "SELECT COUNT(*) FROM request_items WHERE request_id=?", r["id"]) == 3
    assert [i["episode_number"] for i in m.items(r["id"])] == [1, 2, 3]
    heads = repo.next_heads(conn, 10)
    assert len(heads) == 1 and repo.get(conn, heads[0]).episode_number == 1           # FIFO per anime: E01 first


# -- E. VF and VOSTFR are two media, shared between users of one version --------------

def test_two_versions_are_two_media_but_users_of_one_version_share(conn, site):
    site.set("x-vf", 10, [1], lang="vf", title="X")
    site.set("x-vostfr", 11, [1], lang="vostfr", title="X")
    m = mgr(conn, site)
    ask(m, 1, slug="x-vf", post=10, ep=1, version="VF")
    ask(m, 2, slug="x-vostfr", post=11, ep=1, version="VOSTFR")
    keys = [r[0] for r in conn.execute("SELECT media_key FROM episodes").fetchall()]
    assert len(keys) == 2 and len(set(keys)) == 2                                      # 2 records, 2 keys
    for u in (3, 4, 5, 6, 7):                                                          # 5 more users on E01 VF
        ask(m, u, slug="x-vf", post=10, ep=1, version="VF")
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 2                               # still 2: no new download
    assert n(conn, "SELECT COUNT(*) FROM queue_items") == 2
    assert n(conn, "SELECT COUNT(*) FROM request_items i JOIN episodes e ON e.id=i.episode_id WHERE e.language='vf'") == 6


# -- F. waiting + expiry with a controllable clock -------------------------------------

def test_missing_episode_waits_then_expires_after_20_minutes(conn, site):
    site.set("a", 1, [1, 2, 3])
    clock = Clock()
    m = mgr(conn, site, clock)
    m.upsert_user(1)
    r = m.create(new(1, ep=4))
    assert r["state"] == "PENDING"
    r = m.process(r["id"])
    assert r["state"] == "WAITING_FOR_MEDIA" and r["error_code"] == "EPISODE_NOT_AVAILABLE"
    clock.advance(19 * 60)
    m.tick()
    assert m.get(r["id"])["state"] == "WAITING_FOR_MEDIA"                             # 19 min: still waiting
    clock.advance(2 * 60)
    out = m.tick()
    assert m.get(r["id"])["state"] == "EXPIRED" and r["id"] in out["expired"]         # 21 min: expired
    assert m.active_request(1) is None                                                 # the user is free again


def test_episode_appearing_while_waiting_continues_automatically(conn, site):
    site.set("a", 1, [1, 2, 3])
    clock = Clock()
    m = mgr(conn, site, clock)
    r = ask(m, 1, ep=4)
    assert r["state"] == "WAITING_FOR_MEDIA"
    site.set("a", 1, [1, 2, 3, 4])                                                     # the source publishes E04
    clock.advance(10 * 60)
    m.tick()
    r = m.get(r["id"])
    assert r["state"] == "QUEUED" and n(conn, "SELECT COUNT(*) FROM queue_items") == 1


def test_wait_timeout_is_configurable(conn, site):
    site.set("a", 1, [1])
    clock = Clock()
    m = mgr(conn, site, clock, wait_timeout_s=60)
    r = ask(m, 1, ep=9)
    clock.advance(61)
    m.tick()
    assert m.get(r["id"])["state"] == "EXPIRED"


def test_transient_source_error_keeps_searching_then_fails_with_a_classified_code(conn, site):
    site.set("a", 1, [1])
    clock = Clock()
    m = mgr(conn, site, clock)
    m.upsert_user(1)
    r = m.create(new(1, ep=1))

    def boom(url):
        raise RuntimeError("HTTP 503 timed out")
    m.catalog._fetch = boom
    r1 = m.process(r["id"])
    assert r1["state"] == "SEARCHING" and r1["error_code"] == "TIMEOUT"
    clock.advance(21 * 60)
    r2 = m.process(r["id"])
    assert r2["state"] == "FAILED" and r2["error_code"] == "TIMEOUT"


def test_latest_episode_is_the_highest_really_listed(conn, site):
    site.set("a", 1, [1, 2, 3, 7])
    m = mgr(conn, site)
    m.upsert_user(1)
    r = m.create(new(1, ep=None, latest=True))
    r = m.process(r["id"])
    assert r["episode_number"] == 7 and n(conn, "SELECT episode_number FROM episodes") == 7


# -- G. watcher and users converge on ONE job -------------------------------------------

def _watch(conn, slug, post, title="Anime Test"):
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES (?,?,1,?)",
                 (f"postid:{post}", title, f"{BASE}/anime/{slug}/"))
    conn.commit()
    return f"postid:{post}"


def test_watcher_detects_then_user_requests_same_media(conn, site):
    site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), key, site)                                      # baseline: E1, E2 known
    site.set("a", 1, [1, 2, 3])
    rep = discovery.check_anime(conn, cfg(), key, site)                                # watcher queues E3
    assert len(rep["new"]) == 1
    m = mgr(conn, site)
    r = ask(m, 1, ep=3)
    assert n(conn, "SELECT COUNT(*) FROM episodes WHERE episode_number=3") == 1        # ONE media job
    assert n(conn, "SELECT COUNT(*) FROM queue_items") == 1
    it = m.items(r["id"])[0]
    assert repo.get(conn, it["episode_id"]).origin == "watcher" and repo.get(conn, it["episode_id"]).publish_channel == 1


def test_user_requests_then_watcher_detects_same_media(conn, site):
    site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), key, site)                                      # baseline
    site.set("a", 1, [1, 2, 3])
    m = mgr(conn, site)
    ask(m, 1, ep=3)                                                                    # user first: creates E3
    rep = discovery.check_anime(conn, cfg(), key, site)                                # watcher then finds it known
    assert rep["new"] == [] and rep["known"] >= 3
    assert n(conn, "SELECT COUNT(*) FROM episodes WHERE episode_number=3") == 1
    assert n(conn, "SELECT COUNT(*) FROM queue_items WHERE anime_key=?", key) == 1


def test_baseline_episode_requested_by_a_user_becomes_a_job(conn, site):
    site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, cfg(), key, site)                                      # E1, E2 = discovered (never queued)
    assert n(conn, "SELECT COUNT(*) FROM queue_items") == 0
    m = mgr(conn, site)
    r = ask(m, 1, ep=2)
    assert n(conn, "SELECT status FROM episodes WHERE episode_number=2") == "queued" and r["state"] == "QUEUED"
    assert n(conn, "SELECT publish_channel FROM episodes WHERE episode_number=2") == 0      # an old episode never lands in the channel


def test_concurrent_users_never_create_two_jobs(tmp_path, site):
    site.set("a", 1, [1, 2, 3])
    path = str(tmp_path / "race.sqlite3")
    c0 = sqlite3.connect(path)
    c0.row_factory = sqlite3.Row
    db.migrate(c0)
    c0.close()
    errs = []

    def worker(user):
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            m = mgr(c, site)
            ask(m, user, ep=3)
        except Exception as exc:                                                       # pragma: no cover
            errs.append(repr(exc))
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(u,)) for u in range(200, 208)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = sqlite3.connect(path)
    assert not errs, errs
    assert c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM request_items").fetchone()[0] == 8


def test_watched_anime_publishes_to_channel_unwatched_stays_private(conn, site):
    site.set("w", 1, [1])
    site.set("u", 2, [1])
    _watch(conn, "w", 1)
    m = mgr(conn, site)
    ask(m, 1, slug="w", post=1, ep=1)
    ask(m, 2, slug="u", post=2, ep=1)
    pc = {r[0]: r[1] for r in conn.execute("SELECT anime_key, publish_channel FROM episodes").fetchall()}
    assert pc == {"postid:1": 1, "postid:2": 0}
    assert n(conn, "SELECT COUNT(*) FROM animes WHERE anime_key='postid:2'") == 0     # NOT added to the watched list


# -- J. cancel ------------------------------------------------------------------------

def test_cancel_with_other_waiters_keeps_the_job(conn, site):
    site.set("u", 2, [1])
    m = mgr(conn, site)
    a, b, c = (ask(m, u, slug="u", post=2, ep=1) for u in (1, 2, 3))
    out = m.cancel(a["id"], user_id=1)
    assert out["ok"] and out["jobs_cancelled"] == []                                   # B and C still wait for it
    assert n(conn, "SELECT status FROM episodes") == "queued"
    assert m.get(b["id"])["state"] == "QUEUED" and m.get(a["id"])["state"] == "CANCELLED"


def test_cancel_last_waiter_cancels_a_queued_private_job(conn, site):
    site.set("u", 2, [1])
    m = mgr(conn, site)
    a = ask(m, 1, slug="u", post=2, ep=1)
    out = m.cancel(a["id"], user_id=1)
    assert len(out["jobs_cancelled"]) == 1
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 0 and n(conn, "SELECT COUNT(*) FROM queue_items") == 0   # forgotten, not "failed"
    assert m.history(1)[0]["state"] == "CANCELLED"                                     # the history still shows it


def test_cancelling_a_whole_season_leaves_no_error_anywhere(conn, site):
    site.set("bleach", 3, list(range(1, 49)), title="Bleach")
    m = mgr(conn, site)
    r = ask(m, 1, slug="bleach", post=3, kind="season", ep=None)
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 48
    m.cancel(r["id"], user_id=1)
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 0
    assert n(conn, "SELECT COUNT(*) FROM episodes WHERE status IN ('failed','retry_wait','structure_changed','blocked')") == 0
    from v2_automation import service, web_data
    assert service.dashboard(conn, cfg())["attention"] == [] and web_data.problems(conn) == {"items": []} or         not web_data.problems(conn).get("items")                                       # nothing for the panel to flag
    h = m.history(1)[0]
    assert h["state"] == "CANCELLED" and h["items_total"] == 48
    assert ask(m, 1, slug="bleach", post=3, kind="season", ep=None)["state"] == "QUEUED"   # and it can be asked again


def test_cancel_never_kills_a_watcher_job_and_only_the_owner_can_cancel(conn, site):
    site.set("w", 1, [1])
    _watch(conn, "w", 1)
    m = mgr(conn, site)
    a = ask(m, 1, slug="w", post=1, ep=1)
    assert m.cancel(a["id"], user_id=99)["ok"] is False                                # not the owner
    out = m.cancel(a["id"], user_id=1)
    assert out["jobs_cancelled"] == [] and n(conn, "SELECT status FROM episodes") == "queued"   # the channel still gets it


# -- K. history -----------------------------------------------------------------------

def test_history_reads_the_database(conn, site):
    site.set("a", 1, [1, 2, 3])
    m = mgr(conn, site)
    r1 = ask(m, 1, ep=1)
    m.cancel(r1["id"])
    ask(m, 1, ep=2)
    h = m.history(1)
    assert [x["episode_number"] for x in h] == [2, 1] and [x["state"] for x in h] == ["QUEUED", "CANCELLED"]
    assert m.history(2) == []
