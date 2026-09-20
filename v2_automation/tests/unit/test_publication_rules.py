"""Publication rules decided by the owner:
  2. no thumbnail without an accessible video; retries are silent; ONE alert after 20 min
  4. the cycle never stops: an episode detected during a download starts at once
  5. publish as soon as an episode is ready, ONE publication at a time, first finished = first posted,
     thumbnail + video of an episode never interleaved with another episode's messages
  6. a problem on one anime never blocks the others"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from test_auto_worker import _run_worker, conn  # noqa: F401
from test_discovery import Site, _cfg, _watch
from test_downloader import FakeExtraction, FakeTelegram, _build_deps, _cfg as _dl_cfg
from test_global_watcher import NumberedTelegram, _next_cycle_is_due, _queued

from v2_automation import alerts, errors, repo, service, web_data
from v2_automation.discovery import DiscoveryScheduler
from v2_automation.downloader import DownloadManager


class SlowTelegram(NumberedTelegram):
    """Sends take time, so that an interleaving would be possible if two publications ran together."""
    photo_s, video_s = 0.15, 0.25

    def __init__(self, fail_photo_for=()):
        super().__init__()
        self.fail_photo_for, self.calls = set(fail_photo_for), []

    def send_photo(self, path, caption=None):
        name = path.name
        self.calls.append(("photo_start", name, time.monotonic()))
        if any(f in name for f in self.fail_photo_for):
            self.fail_photo_for = {f for f in self.fail_photo_for if f not in name}      # fails once
            raise ConnectionError("telegram unreachable")
        time.sleep(self.photo_s)
        self.calls.append(("photo_end", name, time.monotonic()))
        return super().send_photo(path, caption)

    def send_video(self, path, caption=None):
        self.calls.append(("video_start", path.name, time.monotonic()))
        time.sleep(self.video_s)
        self.calls.append(("video_end", path.name, time.monotonic()))
        return super().send_video(path, caption)


def _mgr(conn, tmp_path, tg, durations, *, starts=None):
    """Real DownloadManager, fake I/O; each anime downloads for its own time so the finishing order is chosen."""
    deps = _build_deps(tmp_path, telegram=tg)
    deps.extract = lambda url, client: FakeExtraction(episode_key=url)
    import hashlib
    deps.sha256 = lambda path: hashlib.sha256(str(path).encode()).hexdigest()
    plain = deps.download

    def download(playlist, output_path, ffmpeg, **kw):
        name = str(output_path)
        if starts is not None:
            starts[name] = time.monotonic()
        time.sleep(next((s for k, s in durations.items() if f"/anime/{k}/" in name.replace("_", "/").replace("\\", "/")
                         or f"_anime_{k}_" in name), 0.1))
        return plain(playlist, output_path, ffmpeg, **kw)
    deps.download = download
    return DownloadManager(conn, _dl_cfg(tmp_path), deps=deps)


def _all_published(conn, ids):
    return lambda: all(repo.get(conn, i).status == "cleanup_pending" for i in ids)


# ── rule 5: first finished, first posted; never interleaved ──────────────────────

def test_first_finished_is_posted_first_one_at_a_time_and_never_interleaved(conn, tmp_path):
    tg = SlowTelegram()
    ids = {"a": _queued(conn, "a", 1), "b": _queued(conn, "b", 1), "c": _queued(conn, "c", 1)}
    mgr = _mgr(conn, tmp_path, tg, {"a": 0.9, "b": 0.1, "c": 0.5})          # finishing order: b, c, a
    _run_worker(conn, _cfg(), mgr.process_episode, until=_all_published(conn, ids.values()))
    events = [(k, n) for k, n, _ in tg.calls]
    kinds = [k for k, _ in events]
    assert kinds == ["photo_start", "photo_end", "video_start", "video_end"] * 3          # strictly one after the other
    videos = [n for k, n in events if k == "video_end"]
    assert [next(a for a in "abc" if f"_{a}_" in n) for n in videos] == ["b", "c", "a"]   # order of arrival
    photos = [n for k, n in events if k == "photo_end"]
    assert [next(a for a in "abc" if f"thumb_{ids[a]}" in n) for n in photos] == ["b", "c", "a"]


def test_an_episode_ready_while_another_still_downloads_is_posted_at_once(conn, tmp_path):
    tg = SlowTelegram()
    fast, slow = _queued(conn, "b", 1), _queued(conn, "a", 1)
    mgr = _mgr(conn, tmp_path, tg, {"b": 0.1, "a": 2.0})
    seen = {}

    def fast_done():
        if repo.get(conn, fast).status == "cleanup_pending":
            seen["slow_status"], seen["t"] = repo.get(conn, slow).status, time.monotonic()
            return True
        return False
    t0 = time.monotonic()
    _run_worker(conn, _cfg(), mgr.process_episode, until=fast_done)
    assert seen["slow_status"] in ("downloading", "queued") and seen["t"] - t0 < 2.0     # posted while the other still downloads
    assert [k for k, _, _ in tg.calls][:4] == ["photo_start", "photo_end", "video_start", "video_end"]


# ── rule 6: one problem never blocks the others ──────────────────────────────────

def test_a_failing_publication_releases_the_line_and_the_next_episode_is_posted(conn, tmp_path):
    tg = SlowTelegram()
    a, b, c = _queued(conn, "a", 1), _queued(conn, "b", 1), _queued(conn, "c", 1)
    tg.fail_photo_for = {f"thumb_{a}."}                                      # A's thumbnail fails, once
    mgr = _mgr(conn, tmp_path, tg, {"a": 0.1, "b": 0.4, "c": 0.7})            # A finishes (and fails) first
    _run_worker(conn, _cfg(), mgr.process_episode, until=_all_published(conn, [b, c]))
    assert repo.get(conn, b).status == "cleanup_pending" and repo.get(conn, c).status == "cleanup_pending"
    assert repo.get(conn, a).status == "retry_wait" and repo.get(conn, a).video_message_id is None   # A retries later, alone
    assert [k for k, *_ in tg.calls if k == "video_end"].__len__() == 2                              # B and C posted


def test_the_gate_is_released_when_an_episode_dies_in_line(conn, tmp_path):
    from v2_automation.downloader import PublishGate
    gate, order = PublishGate(), []

    def worker(name, boom):
        try:
            with gate.arrive() as ticket:
                ticket.wait_turn()
                order.append(name)
                time.sleep(0.05)
                if boom:
                    raise RuntimeError("crash while publishing")
        except RuntimeError:
            pass
    threads = []
    for n, boom in (("first", True), ("second", False), ("third", False)):
        t = threading.Thread(target=worker, args=(n, boom))
        t.start()
        threads.append(t)
        time.sleep(0.02)                                                       # arrival order is fixed
    for t in threads:
        t.join(5)
    assert order == ["first", "second", "third"] and not any(t.is_alive() for t in threads)


# ── rule 2: no thumbnail without a video; silent retries; ONE alert after 20 min ─

def test_a_404_posts_nothing_retries_alone_and_lets_the_others_through(conn, tmp_path):
    tg = SlowTelegram()
    bad, good = _queued(conn, "a", 1), _queued(conn, "b", 1)
    mgr = _mgr(conn, tmp_path, tg, {"b": 0.1})
    real_extract = mgr.deps.extract

    def extract(url, client):
        if "/a/" in url or "_a_" in url or "anime/a/" in url:
            raise errors.PipelineError(errors.SOURCE_EXTRACTION_FAILED, "master manifest fetch failed: status=404 error=HTTP_404")
        return real_extract(url, client)
    mgr.deps.extract = extract
    _run_worker(conn, _cfg(), mgr.process_episode, until=_all_published(conn, [good]))
    a = repo.get(conn, bad)
    assert a.status == "retry_wait" and a.retry_count >= 1 and "404" in a.last_error
    assert all("_a_" not in n and "thumb_%d" % bad not in n for _, n, _ in tg.calls)      # not even a thumbnail for A
    assert repo.get(conn, good).status == "cleanup_pending"


def _retry_wait(conn, first_failure_minutes_ago, error="SOURCE_EXTRACTION_FAILED: status=404"):
    eid = _queued(conn, "a", 1)
    repo.transition(conn, eid, "retry_wait")
    until = (datetime.now(timezone.utc) + timedelta(hours=24) - timedelta(minutes=first_failure_minutes_ago)).isoformat()
    repo.set_retry_until(conn, eid, until, retry_count=3, error=error)
    conn.commit()
    return eid


def test_persistent_error_alert_comes_after_20_minutes_only_once(conn):
    conn.execute("INSERT INTO animes (anime_key, title, enabled) VALUES ('a', 'Anime A', 1)")
    eid = _retry_wait(conn, 10)
    pushed = []
    d = lambda kind, title, body="": pushed.append((kind, title))                          # noqa: E731
    assert alerts.raise_persistent_errors(conn, minutes=20, dispatch=d) == 0                # 10 min: silence
    assert alerts.open_count(conn) == 0 and pushed == []
    repo.set_retry_until(conn, eid, (datetime.now(timezone.utc) + timedelta(hours=24) - timedelta(minutes=21)).isoformat(),
                         retry_count=5, error="SOURCE_EXTRACTION_FAILED: status=404")
    conn.commit()
    assert alerts.raise_persistent_errors(conn, minutes=20, dispatch=d) == 1                # 21 min: one signal
    assert len(pushed) == 1 and pushed[0][0] == "retry" and "Vidéo inaccessible depuis 21 min" in pushed[0][1] and "Anime A E1" in pushed[0][1]
    assert alerts.raise_persistent_errors(conn, minutes=20, dispatch=d) == 0                # never repeated
    conn.execute("UPDATE alerts SET status='ack'")                                          # acknowledged: not raised again
    conn.commit()
    assert alerts.raise_persistent_errors(conn, minutes=20, dispatch=d) == 0 and len(pushed) == 1


def test_persistent_alert_is_visible_in_both_panels_and_closes_on_publication(conn):
    from v2_automation import admin_views
    conn.execute("INSERT INTO animes (anime_key, title, enabled) VALUES ('a', 'Anime A', 1)")
    eid = _retry_wait(conn, 25)
    alerts.raise_persistent_errors(conn, minutes=20)
    web = web_data.problems(conn)["alerts"]
    assert len(web) == 1 and web[0]["title"] == "Vidéo inaccessible" and "Anime A E1" in web[0]["subject"]
    assert "Vidéo inaccessible" in str(admin_views.alerts_view(conn, _cfg()).text)
    conn.execute("UPDATE episodes SET status='cleanup_pending' WHERE id=?", (eid,))
    alerts.sweep_stale(conn)
    conn.commit()
    assert alerts.open_count(conn) == 0                                                     # closes by itself


def test_an_episode_waiting_for_its_official_release_is_never_signalled(conn):
    conn.execute("INSERT INTO animes (anime_key, title, enabled) VALUES ('a', 'Anime A', 1)")
    _retry_wait(conn, 600, error="NOT_AVAILABLE_YET: player is a youtube embed")
    assert alerts.raise_persistent_errors(conn, minutes=20) == 0 and alerts.open_count(conn) == 0


# ── rule 4: the cycle never stops, a new episode starts at once ──────────────────

def test_an_episode_detected_during_a_download_starts_immediately(conn, tmp_path):
    from test_catchup_today import DatedSite
    site = DatedSite()
    site.show("b", 2, {1: "September 1, 2026"})
    _watch(conn, "b", 2)
    tg = SlowTelegram()
    running = _queued(conn, "a", 1)                                           # a long download is under way
    starts = {}
    mgr = _mgr(conn, tmp_path, tg, {"a": 1.5, "b": 0.1}, starts=starts)
    sched = DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    sched.tick()                                                              # cycle 1 = baseline of B
    end = time.monotonic() + 10
    while time.monotonic() < end and not sched.cycles:
        time.sleep(0.02)

    def detect_during_download():
        while not any("_a_" in k for k in starts):
            time.sleep(0.02)
        _next_cycle_is_due(conn)
        site.show("b", 2, {2: "3 seconds ago", 1: "September 1, 2026"})       # B E02 appears now
    threading.Thread(target=detect_during_download, daemon=True).start()
    _run_worker(conn, _cfg(), mgr.process_episode, discovery=sched,
                until=lambda: len(sched.cycles) >= 2 and any("_b_" in k for k in starts))
    a_start = next(v for k, v in starts.items() if "_a_" in k)
    b_start = next(v for k, v in starts.items() if "_b_" in k)
    assert b_start - a_start < 1.4                                            # B started while A was still downloading
    assert repo.get(conn, running).status in ("downloading", "cleanup_pending", "queued") and len(sched.cycles) >= 2


# ── panels: today's counters, next cycle, cycle history (web + Telegram) ─────────

def test_panels_show_today_next_cycle_and_the_cycle_history(conn):
    from test_catchup_today import DatedSite
    from v2_automation import admin_views, discovery
    site = DatedSite()
    site.show("a", 1, {2: "3 seconds ago", 1: "September 1, 2026"}, title="Anime A")
    _watch(conn, "a", 1, title="Anime A")
    sched = DiscoveryScheduler(conn, _cfg(catchup_today=True), fetch=site, interval_s=1800)
    sched.tick()
    end = time.monotonic() + 10
    while time.monotonic() < end and not sched.cycles:
        time.sleep(0.02)
    sched.shutdown()
    cfg = _cfg(catchup_today=True)
    from v2_automation import worker
    worker.acquire_lease(conn)                                                                        # a worker is running
    d = service.dashboard(conn, cfg)
    assert d["today"]["detected"] == 1 and d["today"]["published"] == 0
    assert d["next_check"] > d["now"]                                                                 # cycle-based, in the future
    web = web_data.cycles(conn, cfg)
    assert web["interval_seconds"] == 1800 and web["today"]["detected"] == 1
    assert web["items"][0]["checked"] == 1 and web["items"][0]["new_episodes"] == 1
    assert web["items"][0]["animes"][0] == {"title": "Anime A", "discovered": 2, "new": 1, "catchup": 1, "error": ""}
    assert web_data.dashboard(conn, cfg)["animes"][0]["today"] == 1
    text = str(admin_views.cycles_view(conn, cfg).text)
    assert "Cycles de surveillance" in text and "1 nouveau" in text and "rattrapage du jour" in text and "Anime A" in text
    home = str(admin_views.home(conn, cfg).text)
    assert "Prochain cycle" in home and "Aujourd'hui : 1 détecté(s) · 0 publié(s)" in home
