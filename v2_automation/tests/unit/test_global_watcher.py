"""GLOBAL WATCHER: one cycle belongs to the watcher and visits EVERY active anime; new episodes of several
anime become jobs in the same cycle; retry / cleanup / parallelism are global, not tied to one anime.
Nothing here is specific to one title: the anime are A, B, C ... and the source is a scripted fake."""
import hashlib
import itertools
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from test_auto_worker import _run_worker, conn  # noqa: F401
from test_cleanup import _cfg as _cleanup_cfg, _mk as _mk_published
from test_discovery import BASE, Site, _cfg, _watch
from test_downloader import FakeExtraction, FakeTelegram, _build_deps, _cfg as _dl_cfg

from v2_automation import discovery, repo, worker
from v2_automation.discovery import DiscoveryScheduler
from v2_automation.downloader import DownloadManager
from v2_automation.timeutil import add_seconds, now_utc


def _wait_cycles(sched, n, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(sched.cycles) < n:
        time.sleep(0.02)
    assert len(sched.cycles) >= n, "the cycle did not finish"


def _next_cycle_is_due(conn):
    """Test clock: the production interval is 1800 s; here the previous cycle is simply aged past it."""
    conn.execute("UPDATE control SET cvalue=? WHERE ckey=?",
                 (add_seconds(now_utc(), -1801), discovery.CYCLE_KEY))
    conn.commit()


def _jobs_by_anime(conn):
    rows = conn.execute("SELECT q.anime_key, e.episode_number FROM queue_items q JOIN episodes e ON e.id=q.episode_id "
                        "ORDER BY q.anime_key, e.episode_number").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["anime_key"], []).append(r["episode_number"])
    return out


@pytest.fixture()
def five(conn):
    site = Site()
    for i, name in enumerate("ABCDE", start=1):
        site.set(name.lower(), i, [1, 2, 3], title=f"Anime {name}")
        _watch(conn, name.lower(), i, title=f"Anime {name}")
    return site


# ── 1. FULL CYCLE ────────────────────────────────────────────────────────────────

def test_a_cycle_visits_every_active_anime_and_only_new_episodes_become_jobs(conn, five, caplog):
    caplog.set_level(logging.INFO)
    sched = DiscoveryScheduler(conn, _cfg(), fetch=five, interval_s=1800)
    sched.tick()
    _wait_cycles(sched, 1)
    c = sched.cycles[0]                                                    # cycle N: bootstrap of all five
    assert c["anime_count"] == 5 and c["checked"] == 5 and c["new_episodes"] == 0 and c["errors"] == 0
    assert _jobs_by_anime(conn) == {}                                     # "nothing new" is normal, not an error
    assert len({u for u in five.calls}) == 5                               # every anime was fetched

    _next_cycle_is_due(conn)
    five.set("b", 2, [1, 2, 3, 4], title="Anime B")                       # B: E04 new
    five.set("e", 5, [1, 2, 3, 4, 5], title="Anime E")                    # E: E04 and E05 new
    sched.tick()
    _wait_cycles(sched, 2)
    c = sched.cycles[1]
    assert c["checked"] == 5 and c["new_episodes"] == 3 and c["jobs_created"] == 3
    assert _jobs_by_anime(conn) == {"postid:2": [4], "postid:5": [4, 5]}   # A, C, D: no job
    assert c["animes"]["postid:1"]["new"] == 0 and c["animes"]["postid:5"]["new"] == 2
    sched.shutdown()

    text = "\n".join(r.getMessage() for r in caplog.records)
    for line in ("[WATCHER] cycle_started", "[WATCHER] anime_count=5", "[WATCHER] checking anime=postid:1",
                 "[WATCHER] checking anime=postid:5", "discovered_count=4 new_count=1",
                 "[WATCHER] cycle_finished checked_animes=5 new_episodes=3 jobs_created=3 errors=0"):
        assert line in text
    assert "mode=bootstrap" in text and "mode=incremental" in text


def test_spec_example_a_b_c_new_episodes_in_the_same_cycle_and_none_for_a(conn):
    site = Site()
    for name, pid, eps in (("a", 1, [1]), ("b", 2, [5]), ("c", 3, [10])):
        site.set(name, pid, eps)
        _watch(conn, name, pid)
    sched = DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    sched.tick(); _wait_cycles(sched, 1)
    _next_cycle_is_due(conn)
    site.set("b", 2, [5, 6]); site.set("c", 3, [10, 11])
    sched.tick(); _wait_cycles(sched, 2)
    assert _jobs_by_anime(conn) == {"postid:2": [6], "postid:3": [11]}     # B E06 and C E11, nothing for A
    sched.shutdown()


def test_a_failing_anime_does_not_stop_the_others_and_is_recorded(conn, five):
    real = five.__call__

    def flaky(url):
        if url.endswith("/c/"):
            raise discovery.DiscoveryError("HTTP 503 on the anime page")
        return real(url)
    sched = DiscoveryScheduler(conn, _cfg(), fetch=flaky, interval_s=1800)
    sched.tick(); _wait_cycles(sched, 1)
    c = sched.cycles[0]
    assert c["checked"] == 5 and c["errors"] == 1 and c["animes"]["postid:3"]["error"]
    assert conn.execute("SELECT COUNT(*) FROM animes WHERE last_successful_check_at IS NOT NULL").fetchone()[0] == 4
    sched.shutdown()


def test_active_animes_are_read_from_the_database_not_hardcoded(conn, five):
    conn.execute("UPDATE animes SET enabled=0 WHERE anime_key='postid:2'")
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:9', 'Sans URL', 1, NULL)")
    conn.commit()
    assert discovery.active_animes(conn) == ["postid:1", "postid:3", "postid:4", "postid:5"]
    site = Site(); site.set("zzz", 77, [1], title="Nouveau")
    site.pages.update(five.pages)
    _watch(conn, "zzz", 77, title="Nouveau")                              # a brand new anime joins the next cycle
    assert "postid:77" in discovery.active_animes(conn)


# ── 6. THE 30-MINUTE CYCLE ───────────────────────────────────────────────────────

def test_one_cycle_per_interval_even_with_two_schedulers_and_after_a_restart(conn, five, monkeypatch):
    monkeypatch.delenv("V2_TEST_MODE", raising=False)
    assert discovery.poll_interval_s(_cfg()) == 1800                        # production value
    assert discovery.poll_interval_s(_cfg(poll_interval_seconds=1800)) == 1800
    s1 = DiscoveryScheduler(conn, _cfg(), fetch=five)
    s1.tick(); _wait_cycles(s1, 1)
    calls = len(five.calls)
    s2 = DiscoveryScheduler(conn, _cfg(), fetch=five)                       # "restart" / second process
    for s in (s1, s2):
        assert s.tick() == []                                               # interval not elapsed: no cycle
    assert len(five.calls) == calls and s2.cycles == []
    monkeypatch.setenv("V2_TEST_MODE", "1")
    assert discovery.poll_interval_s(_cfg(test_poll_interval_seconds=10)) == 10    # same code, short interval
    s1.shutdown(); s2.shutdown()


def test_two_processes_never_run_two_cycles_for_the_same_interval(conn):
    assert discovery.claim_cycle(conn, 1800) is True
    assert discovery.claim_cycle(conn, 1800) is False
    _next_cycle_is_due(conn)
    assert discovery.claim_cycle(conn, 1800) is True


def test_restart_creates_no_duplicate_jobs_and_last_cycle_is_persisted(conn, five):
    s1 = DiscoveryScheduler(conn, _cfg(), fetch=five, interval_s=1800)
    s1.tick(); _wait_cycles(s1, 1)
    five.set("d", 4, [1, 2, 3, 4], title="Anime D")
    _next_cycle_is_due(conn)
    s1.tick(); _wait_cycles(s1, 2)
    s1.shutdown()
    before = _jobs_by_anime(conn)
    s2 = DiscoveryScheduler(conn, _cfg(), fetch=five, interval_s=1800)     # process restarted, same source state
    _next_cycle_is_due(conn)
    s2.tick(); _wait_cycles(s2, 1)
    s2.shutdown()
    assert _jobs_by_anime(conn) == before == {"postid:4": [4]}             # no second job for D E04
    assert discovery.last_cycle(conn)["checked"] == 5


# ── 5 + TEST 4: PARALLELISM ACROSS ANIME, FIFO INSIDE ONE ────────────────────────

class NumberedTelegram(FakeTelegram):
    """Distinct message ids per message, safe across threads."""
    _ids = itertools.count(1000)
    _lock = threading.Lock()

    def send_photo(self, path, caption=None):
        with self._lock:
            self.log.append(("photo", path.name, time.monotonic()))
            m = self._Msg(next(self._ids), kind="photo")
        m.photo = [type("P", (), {"file_id": "ph"})()]
        return m

    def send_video(self, path, caption=None):
        with self._lock:
            self.log.append(("video", path.name, time.monotonic()))
            return self._Msg(next(self._ids), kind="video")


def _queued(conn, anime, n):
    from v2_automation.models import Episode
    url = f"https://www.example.com/anime/{anime}/e{n:02d}-vostfr"
    eid, _ = repo.upsert_episode(conn, Episode(anime_key=anime, episode_key=url, canonical_episode_url=url + "/",
                                               language="vostfr", episode_number=n, episode_url=url))
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


def _manager(conn, tmp_path, telegram, *, work_s=0.4, gauge=None, fail_for=()):
    deps = _build_deps(tmp_path, telegram=telegram)
    deps.extract = lambda url, client: FakeExtraction(episode_key=url)
    deps.sha256 = lambda path: hashlib.sha256(str(path).encode()).hexdigest()   # distinct files, distinct hashes
    plain = deps.download

    def download(playlist, output_path, ffmpeg, **kw):
        if gauge is not None:
            with gauge["lock"]:
                gauge["now"] += 1
                gauge["max"] = max(gauge["max"], gauge["now"])
        time.sleep(work_s)
        try:
            return plain(playlist, output_path, ffmpeg, **kw)
        finally:
            if gauge is not None:
                with gauge["lock"]:
                    gauge["now"] -= 1
    deps.download = download
    if fail_for:
        good_validate = deps.validate
        deps.validate = lambda path, ffprobe, expected=None: (
            type("Bad", (), {"verdict": "INVALID", "size_bytes": 0, "mismatch": ["truncated"]})()
            if any(f in str(path) for f in fail_for) else good_validate(path, ffprobe, expected))
    return DownloadManager(conn, _dl_cfg(tmp_path), deps=deps)


def test_several_anime_download_in_parallel_and_all_are_published_to_telegram(conn, tmp_path):
    tg = NumberedTelegram()
    gauge = {"lock": threading.Lock(), "now": 0, "max": 0}
    ids = {("A", 1): _queued(conn, "a", 1), ("B", 10): _queued(conn, "b", 10), ("C", 5): _queued(conn, "c", 5),
           ("A", 2): _queued(conn, "a", 2)}
    mgr = _manager(conn, tmp_path, tg, gauge=gauge)
    out = _run_worker(conn, _cfg(), mgr.process_episode,
                      until=lambda: all(repo.get(conn, i).status == "cleanup_pending" for i in ids.values()))
    assert gauge["max"] >= 3 and out["active_max"] >= 3                     # A, B and C downloaded at the same time
    assert sorted(k for k, *_ in tg.log).count("photo") == 4 and [k for k, *_ in tg.log].count("video") == 4
    for eid in ids.values():                                                # thumbnail message, then video message
        pubs = {r["publication_type"] for r in conn.execute("SELECT publication_type FROM publications WHERE episode_id=?", (eid,))}
        assert pubs == {"thumbnail", "first_publication"}
    a1, a2 = repo.get(conn, ids[("A", 1)]), repo.get(conn, ids[("A", 2)])
    assert a1.published_at <= a2.published_at and a1.video_message_id < a2.video_message_id   # FIFO inside anime A


# ── TEST 2: RETRY WINDOW BELONGS TO THE EPISODE, IT BLOCKS NO OTHER ANIME ────────

def test_an_episode_in_retry_blocks_no_other_anime_and_fails_after_24h(conn, tmp_path):
    tg = NumberedTelegram()
    bad_id = _queued(conn, "a", 1)
    later_a = _queued(conn, "a", 2)                                          # same anime: waits behind E01 (FIFO)
    b_id = _queued(conn, "b", 1)
    bad = _manager(conn, tmp_path, tg, work_s=0.05, fail_for=("a",))
    good = _manager(conn, tmp_path, tg, work_s=0.05)

    def route(eid):
        return (bad if eid == bad_id else good).process_episode(eid)

    site = Site(); site.set("c", 3, [1], title="Anime C")
    _watch(conn, "c", 3, title="Anime C")
    sched = DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    sched.tick(); _wait_cycles(sched, 1)                                     # baseline of C
    _next_cycle_is_due(conn)
    site.set("c", 3, [1, 2], title="Anime C")                                # C E02 appears while A E01 is in retry

    def c_done():
        row = conn.execute("SELECT id FROM episodes WHERE anime_key='postid:3' AND episode_number=2").fetchone()
        return row is not None and repo.get(conn, row["id"]).status == "cleanup_pending"
    _run_worker(conn, _cfg(), route, discovery=sched,
                until=lambda: repo.get(conn, b_id).status == "cleanup_pending" and c_done())
    a = repo.get(conn, bad_id)
    assert a.status == "retry_wait" and a.retry_count >= 1 and a.retry_until_at   # still inside its 24 h window
    assert repo.get(conn, b_id).status == "cleanup_pending" and c_done()          # B and the newly detected C went on
    assert repo.get(conn, later_a).status == "queued"                              # only A's own next episode waits

    expired = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    repo.set_retry_until(conn, bad_id, expired, retry_count=9)
    conn.commit()
    assert bad.process_episode(bad_id) == "failed"                                # 24 h elapsed: FAILED
    assert repo.get(conn, b_id).status == "cleanup_pending"                        # nobody else is affected
    sched.shutdown()


# ── TEST 3: CLEANUP IS GLOBAL, D+14 FROM published_at, FILES ONLY ────────────────

def test_cleanup_walks_every_published_episode_of_every_anime_and_never_touches_telegram(conn, tmp_path):
    from v2_automation import cleanup
    old = {a: _mk_published(conn, tmp_path, age_days=15, anime=a) for a in ("anime-a", "anime-b", "anime-c")}
    young = {a: _mk_published(conn, tmp_path, age_days=3, anime=a) for a in ("anime-d", "anime-e")}
    before = {eid: conn.execute("SELECT video_message_id FROM episodes WHERE id=?", (eid,)).fetchone()[0]
              for _, eid in [*old.values(), *young.values()]}
    res = cleanup.run_cleanup(conn, _cleanup_cfg(retention_days=14))
    assert res["cleaned"] == 3
    assert all(not path.exists() for path, _ in old.values()) and all(path.exists() for path, _ in young.values())
    for _, eid in old.values():
        assert repo.get(conn, eid).status == "cleaned"
    after = {eid: conn.execute("SELECT video_message_id FROM episodes WHERE id=?", (eid,)).fetchone()[0] for eid in before}
    assert after == before                                                  # Telegram message ids untouched
    assert not hasattr(cleanup, "delete_message")                          # the module cannot delete a message


# ── the shared connection under parallel jobs (found while proving parallelism) ──────

def test_parallel_jobs_never_read_a_torn_or_stale_row_on_the_shared_connection(conn):
    """Parallel real jobs once lost a state change (repo.get returned nothing / an older status for a moment).
    Every job now reads its own row back exactly as it wrote it, while other threads hammer the connection."""
    ids = [_queued(conn, f"x{i}", 1) for i in range(6)]
    bad = []

    def hammer(eid):
        for n in range(150):
            new = "downloading" if n % 2 == 0 else "queued"
            conn.execute("UPDATE episodes SET status=? WHERE id=?", (new, eid))
            conn.commit()
            got = repo.get(conn, eid)
            if got is None or got.status != new:
                bad.append((eid, new, None if got is None else got.status))
    threads = [threading.Thread(target=hammer, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bad == []


def test_parallel_downloads_never_share_a_segment_folder(conn, tmp_path):
    """Two anime downloading at once used to write the same segments/seg_00123.ts (mixed video, then
    FileNotFoundError when the first job cleaned up).  Every episode now has its own work folder."""
    seen = {}
    lock = threading.Lock()
    tg = NumberedTelegram()
    ids = [_queued(conn, a, 1) for a in ("a", "b", "c")]
    mgr = _manager(conn, tmp_path, tg, work_s=0.3)
    plain = mgr.deps.download

    def spy(playlist, output_path, ffmpeg, **kw):
        with lock:
            seen[str(output_path)] = kw["work_root"]
        return plain(playlist, output_path, ffmpeg, **kw)
    mgr.deps.download = spy
    _run_worker(conn, _cfg(), mgr.process_episode,
                until=lambda: all(repo.get(conn, i).status == "cleanup_pending" for i in ids))
    roots = list(seen.values())
    assert len(roots) == 3 and len({str(r) for r in roots}) == 3                 # three different folders
    assert all(r.name.startswith("ep_") for r in roots)
