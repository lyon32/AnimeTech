"""Canonical media identity (source | anime_id | season | episode | version) and the atomic claim.

  * the key is the same in search, request, queue, download, storage, publication, delivery and history (one trace from a key);
  * "is it free?" and "take it" are ONE database statement: threads AND separate processes racing for the same media produce
    exactly one owner — checking that a file exists is never the lock.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_delivery import FakeTransport
from test_downloader import FakeExtraction, FakeTelegram, _build_deps, _cfg as dl_cfg

from v2_automation import db, discovery, media, repo, schema, service
from v2_automation.catalog import SourceCatalog
from v2_automation.delivery import DeliveryEngine
from v2_automation.downloader import DownloadManager, EpisodeBusy, MediaFileLock
from v2_automation.models import Episode
from v2_automation.requests_mgr import NewRequest, RequestManager
from v2_automation.telegram_publisher import TelegramPublisher
from v2_automation.timeutil import now_utc
from v2support import BASE, Site, cfg

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


# -- the identity ----------------------------------------------------------------------------------------

def test_the_readable_identity_and_the_key_include_the_source():
    ref = media.media_ref("postid:53702", None, 1150, "vostfr")
    assert ref == "voir-anime.to|postid:53702|S00|E1150|VOSTFR"                    # source + anime_id + season + episode + version
    assert media.media_ref("postid:1", 2, 5, "VF") == "voir-anime.to|postid:1|S02|E5|VF"
    a = media.compute_media_key("postid:1", None, 5, "VF")
    assert a == "m_" + hashlib.sha256(b"media2|voir-anime.to|postid:1|S00|E5|VF").hexdigest()[:32]
    assert a != media.compute_media_key("postid:1", None, 5, "VF", source="other-source.example")      # same anime id, other source
    assert a != media.compute_media_key("postid:1", None, 5, "VOSTFR") and a != media.compute_media_key("postid:1", 1, 5, "VF")


def test_every_new_row_carries_key_and_ref_and_they_agree(conn):
    res = media.ensure_media(conn, anime_key="postid:9", episode_number=3, version="vf", episode_url="u/3", episode_key="k3")
    ep = repo.get(conn, res.episode_id)
    assert ep.media_ref == "voir-anime.to|postid:9|S00|E3|VF" and ep.media_key == media.compute_media_key("postid:9", None, 3, "VF")


def test_the_watcher_computes_the_same_identity_as_a_user_request(conn):
    site = Site()
    site.set("a", 1, [1, 2])
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:1','A',1,?)", (f"{BASE}/anime/a/",))
    conn.commit()
    discovery.check_anime(conn, cfg(), "postid:1", site)
    site.set("a", 1, [1, 2, 3])
    discovery.check_anime(conn, cfg(), "postid:1", site)                              # the WATCHER records E3
    watcher_ref = n(conn, "SELECT media_ref FROM episodes WHERE episode_number=3")
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    m.upsert_user(1)
    r = m.create(NewRequest(user_id=1, kind="episode", anime_key="postid:1", title="A", version="VOSTFR",
                            source_url=f"{BASE}/anime/a/", episode_number=3))
    m.process(r["id"])                                                                # the USER asks for E3
    assert n(conn, "SELECT COUNT(*) FROM episodes WHERE episode_number=3") == 1
    assert m.items(r["id"])[0]["media_key"] == n(conn, "SELECT media_key FROM episodes WHERE episode_number=3")
    assert watcher_ref == "voir-anime.to|postid:1|S00|E3|VOSTFR"


# -- migration v5 ------------------------------------------------------------------------------------------

def _v4_database(path):
    c = sqlite3.connect(str(path), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    for v in (1, 2, 3, 4):
        c.executescript(schema.MIGRATIONS[v])
        c.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, 't')", (v,))
    for num in (1, 2, 3):                                                             # rows keyed the schema-v4 way (no source)
        old = "m_" + hashlib.sha256(f"media1|postid:7|s0|n{num}|VF".encode()).hexdigest()[:32]
        c.execute("INSERT INTO episodes (anime_key, episode_key, canonical_episode_url, language, episode_number, media_key, status) "
                  "VALUES ('postid:7', ?, ?, 'vf', ?, ?, 'published')", (f"k{num}", f"k{num}/", num, old))
        c.execute("INSERT INTO publications (episode_id, publication_type, status, chat_id, message_id) VALUES (?, 'first_publication', 'sent', '-1', ?)", (num, num))
    c.execute("INSERT INTO users VALUES (5, 'u', 't', 't', 'granted', NULL, 0)")
    c.execute("INSERT INTO requests (user_id, kind, anime_key, version, state, created_at, updated_at) VALUES (5,'episode','postid:7','VF','COMPLETED','t','t')")
    c.execute("INSERT INTO request_items (request_id, media_key, episode_id, episode_number, state, created_at, updated_at) "
              "SELECT 1, media_key, id, episode_number, 'COMPLETED', 't', 't' FROM episodes")
    c.commit()
    return c


def test_migration_v5_rekeys_every_row_and_follows_requests_and_publications(tmp_path):
    c = _v4_database(tmp_path / "old.sqlite3")
    old_keys = {r[0] for r in c.execute("SELECT media_key FROM episodes")}
    assert db.migrate(c) == [5]
    rows = c.execute("SELECT media_key, media_ref, episode_number FROM episodes ORDER BY id").fetchall()
    assert [r["media_ref"] for r in rows] == [f"voir-anime.to|postid:7|S00|E{i}|VF" for i in (1, 2, 3)]
    assert not ({r["media_key"] for r in rows} & old_keys)                            # the source is now in the hash
    assert [r["media_key"] for r in rows] == [media.compute_media_key("postid:7", None, i, "VF") for i in (1, 2, 3)]
    assert c.execute("SELECT COUNT(DISTINCT media_key) FROM episodes").fetchone()[0] == 3        # still unique
    items = c.execute("SELECT media_key FROM request_items ORDER BY episode_id").fetchall()
    assert [i[0] for i in items] == [r["media_key"] for r in rows]                     # requests follow their media
    pubs = c.execute("SELECT p.media_key, e.media_key FROM publications p JOIN episodes e ON e.id=p.episode_id").fetchall()
    assert all(a == b for a, b in pubs)                                                # publications name their media
    assert db.migrate(c) == [] and media.backfill_media_keys(c) == 0                   # idempotent
    c.close()


def test_a_publication_inserted_later_gets_its_media_key_from_the_trigger(conn):
    res = media.ensure_media(conn, anime_key="postid:9", episode_number=1, version="VF", episode_url="u", episode_key="k")
    repo.commit_publication(conn, res.episode_id, "first_publication", "-100", 5, "video", "sha", 10)
    assert n(conn, "SELECT media_key FROM publications") == res.media_key


# -- one trace from a key ------------------------------------------------------------------------------------

def test_from_one_media_key_the_whole_chain_can_be_reconstructed(conn, tmp_path):
    site = Site()
    site.set("a", 1, [1, 2])
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    for u in (1, 2):
        m.upsert_user(u)
        r = m.create(NewRequest(user_id=u, kind="episode", anime_key="postid:1", title="A", version="VOSTFR",
                                source_url=f"{BASE}/anime/a/", episode_number=2))
        m.process(r["id"])
    key = n(conn, "SELECT media_key FROM episodes")
    tg = FakeTelegram()
    conn.execute("UPDATE episodes SET publish_channel=1 WHERE media_key=?", (key,))
    conn.commit()
    eid = repo.next_heads(conn, 3)[0]
    from v2_automation.queues import QueueManager
    assert QueueManager(conn).dequeue_episode(eid)
    ekey = repo.get(conn, eid).episode_key
    mgr = DownloadManager(conn, dl_cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg, extraction=FakeExtraction(episode_key=ekey)))
    assert mgr.process_episode(eid) in ("published", "cleanup_pending")
    DeliveryEngine(conn, TelegramPublisher(FakeTransport()), cfg(user_bot={"delivery_copy_from_channel": True})).run()
    t = service.trace_media(conn, key)
    assert t["media_ref"] == "voir-anime.to|postid:1|S00|E2|VOSTFR" and t["origin"] == "user"
    assert [r["user_id"] for r in t["requests"]] == [1, 2]                              # search -> request
    assert t["job"]["status"] in ("published", "cleanup_pending") and t["job"]["claimed_by"] is None      # job, claim released
    assert t["download"]["file_path"] and t["download"]["sha256"]                       # download + validation
    assert [p["publication_type"] for p in t["publications"]] == ["thumbnail", "first_publication"]
    assert all(p["media_key"] == key for p in t["publications"])                        # publication
    assert [d["status"] for d in t["deliveries"]] == ["sent", "sent"]                   # private deliveries
    assert service.trace_media(conn, "m_unknown") is None


def test_the_media_key_is_in_the_download_logs_and_in_the_file_name(conn, tmp_path, caplog):
    import logging
    res = media.ensure_media(conn, anime_key="postid:1", episode_number=1, version="VF", episode_url="https://x/e1", episode_key="https://x/e1")
    eid = res.episode_id
    from v2_automation.queues import QueueManager
    QueueManager(conn).dequeue_episode(eid)
    mgr = DownloadManager(conn, dl_cfg(tmp_path), deps=_build_deps(tmp_path, telegram=FakeTelegram(),
                                                                    extraction=FakeExtraction(episode_key="https://x/e1")))
    with caplog.at_level(logging.INFO, logger="v2_automation.downloader"):
        mgr.process_episode(eid)
    lines = [r.getMessage() for r in caplog.records if "job=" in r.getMessage()]
    assert lines and all(f"media={res.media_key}" in l for l in lines), lines[:3]
    assert Path(repo.get(conn, eid).file_path).name.endswith(f"__{res.media_key[2:10]}.mp4")


# -- the atomic claim -----------------------------------------------------------------------------------------

def test_claim_is_a_compare_and_set_with_a_single_winner_among_many_threads(tmp_path):
    path = str(tmp_path / "race.sqlite3")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    res = media.ensure_media(c, anime_key="postid:1", episode_number=1, version="VF", episode_url="u", episode_key="k")
    c.close()
    wins, barrier = [], threading.Barrier(16)

    def worker(i):
        cc = sqlite3.connect(path, timeout=30)
        cc.row_factory = sqlite3.Row
        barrier.wait()
        if repo.claim_media(cc, res.episode_id, f"thread-{i}"):
            wins.append(i)
        cc.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1


_CLAIM_SCRIPT = """
import sqlite3, sys, time, os
sys.path.insert(0, {src!r})
from v2_automation import repo
c = sqlite3.connect({path!r}, timeout=30); c.row_factory = sqlite3.Row
while not os.path.exists({go!r}): time.sleep(0.001)
print("WIN" if repo.claim_media(c, {eid}, "proc-" + str(os.getpid())) else "LOSE")
"""


def test_claim_has_a_single_winner_among_separate_processes(tmp_path):
    path, go = str(tmp_path / "proc.sqlite3"), str(tmp_path / "go.flag")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    eid = media.ensure_media(c, anime_key="postid:1", episode_number=1, version="VF", episode_url="u", episode_key="k").episode_id
    c.close()
    code = _CLAIM_SCRIPT.format(src=SRC, path=path, go=go, eid=eid)
    procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) for _ in range(5)]
    time.sleep(1.5)                                                                     # all five are spinning on the flag
    Path(go).write_text("go")
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert sorted(outs) == ["LOSE"] * 4 + ["WIN"]


def test_a_claim_of_a_dead_owner_is_recovered_by_ttl_and_cleared_at_boot(conn):
    eid = media.ensure_media(conn, anime_key="postid:1", episode_number=1, version="VF", episode_url="u", episode_key="k").episode_id
    assert repo.claim_media(conn, eid, "A") and not repo.claim_media(conn, eid, "B")
    assert repo.claim_media(conn, eid, "A")                                              # the owner may renew
    conn.execute("UPDATE episodes SET claimed_at='2000-01-01T00:00:00Z' WHERE id=?", (eid,))
    assert repo.claim_media(conn, eid, "B")                                              # ancient claim: dead owner, taken over
    from v2_automation import recovery
    rec = recovery.run_recovery(conn, cfg(publication={"cleanup_after_days": 14}))
    assert rec["claims_cleared"] == 1 and n(conn, "SELECT claimed_by FROM episodes") is None


def test_two_workers_processing_the_same_episode_download_it_once(conn, tmp_path):
    counter = {"n": 0}
    deps = _build_deps(tmp_path, telegram=FakeTelegram(), extraction=FakeExtraction(episode_key="https://x/e1"))
    real = deps.download

    def slow(*a, **k):
        counter["n"] += 1
        time.sleep(0.6)
        return real(*a, **k)
    deps.download = slow
    res = media.ensure_media(conn, anime_key="postid:1", episode_number=1, version="VF", episode_url="https://x/e1", episode_key="https://x/e1")
    from v2_automation.queues import QueueManager
    QueueManager(conn).dequeue_episode(res.episode_id)
    outcomes = []

    def run():
        try:
            outcomes.append(DownloadManager(conn, dl_cfg(tmp_path), deps=deps).process_episode(res.episode_id))
        except EpisodeBusy:
            outcomes.append("busy")
        except Exception as exc:                                                        # EpisodeAlreadyDone if the winner is done
            outcomes.append(type(exc).__name__)

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert counter["n"] == 1                                                            # the download ran ONCE
    assert sum(1 for o in outcomes if o in ("published", "cleanup_pending", "retry_wait", "ready")) == 1     # exactly one real run
    assert n(conn, "SELECT claimed_by FROM episodes WHERE id=?", res.episode_id) is None            # and the claim is released


def test_media_file_lock_is_exclusive_and_recovers_from_a_crash(tmp_path):
    f = tmp_path / "ep.mp4"
    with MediaFileLock(f):
        with pytest.raises(EpisodeBusy):
            MediaFileLock(f).__enter__()                                                # a second writer is refused
    assert not (tmp_path / "ep.mp4.lock").exists()                                      # released
    (tmp_path / "ep.mp4.lock").write_text("999999\n")                                   # left by a crashed process (no such pid)
    with MediaFileLock(f):
        assert (tmp_path / "ep.mp4.lock").read_text().split()[0] == str(os.getpid())    # taken over


# -- "A checks: absent, B checks: absent" -----------------------------------------------------------------------

def test_two_requesters_who_both_saw_the_media_absent_still_create_one_row(tmp_path, monkeypatch):
    path = str(tmp_path / "abs.sqlite3")
    c0 = sqlite3.connect(path)
    c0.row_factory = sqlite3.Row
    db.migrate(c0)
    c0.close()
    barrier, results, seen = threading.Barrier(2), [], threading.local()
    real_find = media.find_media

    def find_then_wait(conn, key):                                                      # both threads look BEFORE either inserts
        found = real_find(conn, key)
        if not getattr(seen, "waited", False):                                           # only the first look is synchronised
            seen.waited = True
            barrier.wait(timeout=10)
        return found
    monkeypatch.setattr(media, "find_media", find_then_wait)

    def ask(user):
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            r = media.ensure_media(c, anime_key="postid:1", episode_number=7, version="VF", episode_url="u", episode_key="k", origin="user")
            results.append(r.action)
        finally:
            c.close()

    ts = [threading.Thread(target=ask, args=(u,)) for u in (1, 2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1 and c.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0] == 1
    assert sorted(results) == ["created", "joined"]                                     # one created it, the other joined it


def test_three_users_and_the_watcher_at_the_same_instant_one_media(tmp_path):
    path = str(tmp_path / "mix.sqlite3")
    c0 = sqlite3.connect(path)
    c0.row_factory = sqlite3.Row
    db.migrate(c0)
    c0.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:1','A',1,?)", (f"{BASE}/anime/a/",))
    c0.commit()
    site = Site()
    site.set("a", 1, [1, 2])
    discovery.check_anime(c0, cfg(), "postid:1", site)                                  # baseline
    c0.close()
    site.set("a", 1, [1, 2, 3])
    barrier, errs = threading.Barrier(4), []

    def user(u):
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            m = RequestManager(c, SourceCatalog(cfg(), fetch=site), now=now_utc)
            m.upsert_user(u)
            r = m.create(NewRequest(user_id=u, kind="episode", anime_key="postid:1", title="A", version="VOSTFR",
                                    source_url=f"{BASE}/anime/a/", episode_number=3))
            barrier.wait(timeout=10)
            m.process(r["id"])
        except Exception as exc:                                                        # pragma: no cover
            errs.append(repr(exc))
        finally:
            c.close()

    def watcher():
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=10)
            discovery.check_anime(c, cfg(), "postid:1", site, now=None)
        except Exception as exc:                                                        # pragma: no cover
            errs.append(repr(exc))
        finally:
            c.close()

    ts = [threading.Thread(target=user, args=(u,)) for u in (11, 12, 13)] + [threading.Thread(target=watcher)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = sqlite3.connect(path)
    assert not errs, errs
    assert c.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=3").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM queue_items WHERE episode_id=(SELECT id FROM episodes WHERE episode_number=3)").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM request_items").fetchone()[0] == 3
