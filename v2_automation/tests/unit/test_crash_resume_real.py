"""Crash / resume with REAL processes, real HTTP and a real database.  The child process is killed (TerminateProcess) at a
precise, observed point — never at a guessed delay — and a fresh process resumes from the persistent state only.

  * download killed at ~60 % (segments counted) -> restart -> recovery requeues -> completes -> published ONCE;
  * killed after the Bot API server accepted the video but before the answer was recorded -> restart -> never re-sent;
  * killed after publication -> restart -> nothing re-sent.
All state is read back by media_key.
"""
import json
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_downloader import _cfg, _mk_episode

from v2_automation import db, recovery, repo

HERE = Path(__file__).resolve().parent
CHILD = str(HERE / "crash_child.py")
SEGMENTS = 20


class FakeBotApi:
    """A real HTTP server speaking the Bot API shape PTB expects; records every accepted upload."""

    def __init__(self, hang_video_after_accept: bool = False):
        self.events, self.hang = [], hang_video_after_accept
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if self.headers.get("Transfer-Encoding") == "chunked":
                    while True:
                        size = int(self.rfile.readline().strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        self.rfile.read(size)
                        self.rfile.readline()
                else:
                    self.rfile.read(n)
                method = self.path.rsplit("/", 1)[-1]
                mid = 100 + len(outer.events) + 1
                msg = {"message_id": mid, "date": int(time.time()), "chat": {"id": -100, "type": "channel", "title": "c"}}
                if method == "sendPhoto":
                    msg["photo"] = [{"file_id": "ph", "file_unique_id": "u1", "width": 10, "height": 10}]
                elif method == "sendVideo":
                    msg["video"] = {"file_id": "vid", "file_unique_id": "u2", "width": 10, "height": 10, "duration": 5}
                outer.events.append(method)
                if method == "sendVideo" and outer.hang:
                    outer.hang = False                                     # accepted... and the client is killed before the answer
                    time.sleep(60)
                body = json.dumps({"ok": True, "result": msg}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def count(self, method):
        return self.events.count(method)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _open(path):
    c = sqlite3.connect(str(path), check_same_thread=False, timeout=30, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def spawn(work, api, eid, delay, reuse="no"):
    return subprocess.Popen([sys.executable, CHILD, str(work / "t.sqlite3"), str(work), api.url, str(eid), str(delay), reuse],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(cond, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def served(work):
    p = work / "segments_served.log"
    return len(p.read_text().split()) if p.exists() else 0


@pytest.fixture()
def world(tmp_path):
    conn = _open(tmp_path / "t.sqlite3")
    db.migrate(conn)
    eid = _mk_episode(conn)
    conn.commit()
    api = FakeBotApi()
    yield conn, eid, api, tmp_path
    api.close()
    conn.close()


def state(conn, eid):
    ep = repo.get(conn, eid)
    return ep, ep.media_key


def finish(proc, timeout=90):
    out, _ = proc.communicate(timeout=timeout)
    return out


def test_download_killed_at_60_percent_resumes_completes_and_publishes_once(world):
    conn, eid, api, work = world
    key0 = state(conn, eid)[1]
    p = spawn(work, api, eid, 0.15)
    assert wait_for(lambda: served(work) >= int(SEGMENTS * 0.6)), "the child never reached 60 %"
    p.kill()
    p.wait()
    at_kill = served(work)
    assert 12 <= at_kill < SEGMENTS
    assert api.count("sendVideo") == 0 and api.count("sendPhoto") == 0            # nothing published yet
    ep, key = state(conn, eid)
    assert ep.status == "downloading" and key == key0                             # persistent state, readable by media_key
    recovery.run_recovery(conn, _cfg(work))
    ep = repo.get(conn, eid)
    assert ep.status == "queued" and ep.claimed_by is None                        # requeued; the dead process' claim is gone
    out = finish(spawn(work, api, eid, 0.0))
    assert "RESULT cleanup_pending" in out, out
    ep, key = state(conn, eid)
    assert key == key0 and ep.status == "cleanup_pending" and ep.video_message_id
    assert api.count("sendVideo") == 1 and api.count("sendPhoto") == 1            # published exactly once
    print(f"MEASURE default engine: served before kill={at_kill}, total={served(work)} of {SEGMENTS}")


def test_complete_segments_are_not_downloaded_again_after_the_restart(world):
    """A segment is complete or absent (write to .part then rename): a restart only fetches what is missing."""
    conn, eid, api, work = world
    p = spawn(work, api, eid, 0.15, reuse="yes")
    assert wait_for(lambda: served(work) >= 12)
    p.kill()
    p.wait()
    before = served(work)
    recovery.run_recovery(conn, _cfg(work))
    out = finish(spawn(work, api, eid, 0.0, reuse="yes"))
    assert "RESULT cleanup_pending" in out, out
    assert served(work) <= SEGMENTS + 1                                           # each segment fetched once (+ the one in flight)
    assert served(work) - before <= SEGMENTS - before + 1
    assert api.count("sendVideo") == 1


def test_crash_after_the_server_accepted_the_video_never_publishes_it_again(tmp_path):
    conn = _open(tmp_path / "t.sqlite3")
    db.migrate(conn)
    eid = _mk_episode(conn)
    conn.commit()
    api = FakeBotApi(hang_video_after_accept=True)
    try:
        p = spawn(tmp_path, api, eid, 0.0)
        assert wait_for(lambda: api.count("sendVideo") == 1), "the server never received the video"
        p.kill()
        p.wait()                                                                   # killed before the answer reached the process
        ep, key = state(conn, eid)
        assert ep.status == "publishing_video"                                     # the DB never learned about the success
        report = recovery.run_recovery(conn, _cfg(tmp_path))
        ep, key2 = state(conn, eid)
        assert key2 == key and ep.status == "failed"                               # manual decision, not a silent requeue
        assert report["publishing_to_failed"]
        time.sleep(0.5)
        assert repo.get(conn, eid).status == "failed" and api.count("sendVideo") == 1
    finally:
        api.close()
        conn.close()


def test_a_restart_after_full_publication_resends_nothing(world):
    conn, eid, api, work = world
    out = finish(spawn(work, api, eid, 0.0))
    assert "RESULT cleanup_pending" in out, out
    assert api.count("sendVideo") == 1
    recovery.run_recovery(conn, _cfg(work))                                        # a "restart" after the crash
    assert repo.get(conn, eid).status == "cleanup_pending"
    finish(spawn(work, api, eid, 0.0))                                             # the worker retries the same job
    assert api.count("sendVideo") == 1 and api.count("sendPhoto") == 1
