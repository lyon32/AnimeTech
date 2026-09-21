"""Core media engine with REAL binaries end to end (no mocked download, validation or thumbnail):

  source page (fake HTTP) -> request / watcher -> ONE media job -> real HLS download from a local origin (real ffmpeg mux)
  -> real ffprobe validation -> [channel publication | READY] -> private deliveries

The content is a synthetic test pattern generated here by ffmpeg (testsrc / sine): no third-party media, redistribution
is not an issue.  Only the two network edges are stand-ins: the source website (a page string) and Telegram (a recording
transport).  A real Telegram round trip is therefore NOT claimed by this file — see V2_TEST_REPORT.md.
"""
import http.server
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_downloader import FakeTelegram, _cfg as dl_cfg
from test_delivery import FakeTransport

from v1_poc.media_tools import MediaToolsError, ffmpeg_bin, ffprobe_bin
from v1_poc.validator import validate_media_file
from v2_automation import db, repo
from v2_automation.catalog import SourceCatalog
from v2_automation.delivery import DeliveryEngine
from v2_automation.downloader import DownloadManager, default_deps
from v2_automation.requests_mgr import NewRequest, RequestManager
from v2_automation.telegram_publisher import TelegramPublisher
from v2_automation.timeutil import now_utc
from v2support import BASE, Site, cfg

try:
    FFMPEG, FFPROBE = ffmpeg_bin(), ffprobe_bin()
except MediaToolsError:  # pragma: no cover
    pytest.skip("ffmpeg/ffprobe not available", allow_module_level=True)


def _run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


@pytest.fixture(scope="module")
def hls_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("hls_core")
    src = d / "src.mp4"
    _run(FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6", "-f", "lavfi", "-i",
         "sine=frequency=800:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "48", "-keyint_min", "48",
         "-sc_threshold", "0", "-c:a", "aac", "-shortest", src)
    _run(FFMPEG, "-y", "-i", src, "-c", "copy", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
         "-hls_segment_filename", d / "seg_%d.ts", d / "index.m3u8")
    return d


class Origin:
    def __init__(self, root: Path):
        self.root, self.hits = root, {}
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(outer.root), **k)

            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.lstrip("/")
                outer.hits[name] = outer.hits.get(name, 0) + 1
                super().do_GET()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}/"

    def close(self):
        self.srv.shutdown()


@pytest.fixture()
def origin(hls_dir):
    o = Origin(hls_dir)
    yield o
    o.close()


class Rendition:
    resolution, bandwidth_bps, fps = "320x180", 500_000, "24"
    video_codec, audio_codec = "avc1.42c01e", "mp4a.40.2"

    def __init__(self, url):
        self.playlist_url = url


class Client:
    """Real HTTP against the local origin only; anything else (the anime info card) is 'unavailable', never the network."""

    def __init__(self, base):
        self.base = base

    def get(self, url):
        if not url.startswith(self.base):
            return SimpleNamespace(ok=False, status_code=503, text="")
        from source_audit.fetch.http_client import HttpClient
        with HttpClient(timeout_seconds=10, max_retries=1, retry_backoff_seconds=0.1, user_agent="t") as c:
            return c.get(url)

    def close(self):
        pass


def real_manager(conn, tmp_path, origin, channel_tg) -> DownloadManager:
    c = dl_cfg(tmp_path)
    deps = default_deps(c)                                    # REAL download (ffmpeg mux), REAL ffprobe validation, sha256
    from source_audit.analysis.identity import build_episode_key
    deps.extract = lambda url, client: SimpleNamespace(episode_key=build_episode_key(url),
                                                       renditions=[Rendition(origin.base + "index.m3u8")])
    deps.http_client = lambda: Client(origin.base)
    deps.structure_check = lambda url: (True, "ok")
    deps.telegram = lambda: channel_tg
    return DownloadManager(conn, c, deps=deps)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _request(m, user, slug, post, ep=1):
    m.upsert_user(user)
    r = m.create(NewRequest(user_id=user, kind="episode", anime_key=f"postid:{post}", title="Test Pattern", version="VOSTFR",
                            source_url=f"{BASE}/anime/{slug}/", episode_number=ep))
    return m.process(r["id"])


def test_private_request_real_download_validate_ready_deliver_to_three_users(conn, tmp_path, origin, monkeypatch):
    from v2_automation import evidence
    monkeypatch.setattr(evidence, "EVID_DIR", tmp_path / "evidence")
    site = Site()
    site.set("pattern", 77, [1], title="Test Pattern")                                   # anime NOT in the watched list
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    reqs = [_request(m, u, "pattern", 77) for u in (1, 2, 3)]                             # three users, one media
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    channel = FakeTelegram()
    mgr = real_manager(conn, tmp_path, origin, channel)
    eid = repo.next_heads(conn, 5)[0]
    from v2_automation.queues import QueueManager
    assert QueueManager(conn).dequeue_episode(eid)
    assert mgr.process_episode(eid) == "ready"                                            # real download + validation
    ep = repo.get(conn, eid)
    v = validate_media_file(Path(ep.file_path), FFPROBE)                                  # independent re-validation of the file
    assert v.verdict == "VALID" and v.video["width"] == 320 and ep.file_size == Path(ep.file_path).stat().st_size
    assert channel.log == []                                                              # private media: nothing on any channel
    assert origin.hits["seg_0.ts"] == 1 and origin.hits["index.m3u8"] >= 1               # the source was read ONCE

    tr = FakeTransport()
    out = DeliveryEngine(conn, TelegramPublisher(tr), cfg()).run()
    assert out["sent"] == 3
    uploaded = [c for c in tr.calls if c[0] == "send_video" and c[2] == "file"]
    assert len(uploaded) == 1 and origin.hits["seg_0.ts"] == 1                            # 1 download, 1 upload, 3 deliveries
    for r in reqs:
        m.sync(r["id"])
        assert m.get(r["id"])["state"] == "COMPLETED"
    assert conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent'").fetchone()[0] == 3


def test_watcher_and_user_share_one_real_download_channel_publish_then_private_copies(conn, tmp_path, origin, monkeypatch):
    from v2_automation import discovery, evidence
    monkeypatch.setattr(evidence, "EVID_DIR", tmp_path / "evidence")
    site = Site()
    site.set("pattern", 88, [1], title="Test Pattern")
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:88','Test Pattern',1,?)",
                 (f"{BASE}/anime/pattern/",))
    conn.commit()
    discovery.check_anime(conn, cfg(), "postid:88", site)                                 # baseline: E1 known, no job
    site.set("pattern", 88, [1, 2], title="Test Pattern")
    rep = discovery.check_anime(conn, cfg(), "postid:88", site)                           # V1 watcher detects E2
    assert len(rep["new"]) == 1
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    reqs = [_request(m, u, "pattern", 88, ep=2) for u in (11, 12)]                        # V2: users ask for the same E2
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=2").fetchone()[0] == 1   # ONE media job
    channel = FakeTelegram()
    mgr = real_manager(conn, tmp_path, origin, channel)
    eid = repo.next_heads(conn, 5)[0]
    from v2_automation.queues import QueueManager
    assert QueueManager(conn).dequeue_episode(eid)
    assert mgr.process_episode(eid) in ("published", "cleanup_pending")
    assert [k for k, _ in channel.log] == ["photo", "video"] and origin.hits["seg_0.ts"] == 1
    tr = FakeTransport()
    assert DeliveryEngine(conn, TelegramPublisher(tr), cfg(user_bot={"delivery_copy_from_channel": True})).run()["sent"] == 2
    assert [c[0] for c in tr.calls] == ["copy", "copy"]                                   # no upload: copied from the channel
    assert conn.execute("SELECT COUNT(*) FROM publications WHERE publication_type='first_publication'").fetchone()[0] == 1
