"""Rule 8: at most 3 downloads at once, in order of detection, and the next anime starts the moment ONE download
finishes (not when its send / publication ends, not when all three are done)."""
import hashlib
import threading
import time

from test_auto_worker import _run_worker, conn  # noqa: F401
from test_discovery import _cfg
from test_downloader import FakeExtraction, _build_deps, _cfg as _dl_cfg
from test_global_watcher import _queued
from test_publication_rules import SlowTelegram

from v2_automation import repo
from v2_automation.downloader import DownloadManager


def _manager(conn, tmp_path, tg, durations, spans):
    deps = _build_deps(tmp_path, telegram=tg)
    deps.extract = lambda url, client: FakeExtraction(episode_key=url)
    deps.sha256 = lambda path: hashlib.sha256(str(path).encode()).hexdigest()
    plain = deps.download

    def download(playlist, output_path, ffmpeg, **kw):
        anime = next(a for a in durations if f"_anime_{a}_" in str(output_path))
        t0 = time.monotonic()
        time.sleep(durations[anime])
        res = plain(playlist, output_path, ffmpeg, **kw)
        spans[anime] = (t0, time.monotonic())
        return res
    deps.download = download
    return DownloadManager(conn, _dl_cfg(tmp_path), deps=deps)


def test_the_next_anime_starts_when_one_download_finishes_not_when_its_send_ends(conn, tmp_path):
    tg = SlowTelegram()
    tg.video_s = 1.2                                                     # sending takes much longer than downloading
    durations = {"a": 0.3, "b": 0.6, "c": 0.9, "d": 0.2, "e": 0.2}
    ids = {a: _queued(conn, a, 1) for a in "abcde"}                       # detection order: a, b, c, d, e
    spans = {}
    mgr = _manager(conn, tmp_path, tg, durations, spans)
    _run_worker(conn, _cfg(), mgr.process_episode, timeout=40,
                until=lambda: all(repo.get(conn, i).status == "cleanup_pending" for i in ids.values()))
    start = {a: s for a, (s, _) in spans.items()}
    end = {a: e for a, (_, e) in spans.items()}
    first = min(start.values())
    assert max(start[a] for a in "abc") - first < 0.5                     # the first three start together
    assert start["d"] >= end["a"] - 0.05 and start["d"] < first + 1.6     # d starts as soon as A's DOWNLOAD is done ...
    a_published = next(t for k, n, t in tg.calls if k == "video_end" and "_a_" in n)
    assert start["d"] < a_published                                        # ... while A is still being sent
    assert start["e"] >= end["b"] - 0.05 and start["e"] < a_published + 0.5   # e follows the next finished download (b)
    # never more than 3 downloads at the same time
    events = sorted([(s, 1) for s, _ in spans.values()] + [(e, -1) for _, e in spans.values()])
    running = peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    assert peak <= 3


def test_detection_order_decides_who_starts_first(conn, tmp_path):
    tg = SlowTelegram()
    durations = {"a": 0.3, "b": 0.6, "c": 0.9, "d": 0.1, "e": 0.1}      # slots free up one after the other: d, then e
    ids = {a: _queued(conn, a, 1) for a in "abcde"}
    spans = {}
    mgr = _manager(conn, tmp_path, tg, durations, spans)
    _run_worker(conn, _cfg(), mgr.process_episode, timeout=40,
                until=lambda: all(repo.get(conn, i).status == "cleanup_pending" for i in ids.values()))
    order = [a for a, _ in sorted(spans.items(), key=lambda kv: kv[1][0])]
    assert set(order[:3]) == {"a", "b", "c"} and order[3:] == ["d", "e"]   # the first three DETECTED are served first
    assert max(spans[a][0] for a in "abc") <= min(spans[a][0] for a in "de")   # d and e wait for a free slot


def test_a_callback_free_process_function_still_works(conn, tmp_path):
    """Stand-ins used elsewhere take only the episode id: the slot is then freed at the end, as before."""
    done = []
    eid = _queued(conn, "a", 1)

    def plain(episode_id):
        done.append(episode_id)
        conn.execute("UPDATE episodes SET status='cleanup_pending' WHERE id=?", (episode_id,))
        repo.release_queue_item(conn, episode_id)
        conn.commit()
    _run_worker(conn, _cfg(), plain, until=lambda: bool(done))
    assert done == [eid]
