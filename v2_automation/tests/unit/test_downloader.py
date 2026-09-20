"""DownloadManager orchestration tests — everything faked, no network."""
import sqlite3
from pathlib import Path

import pytest

from v2_automation import db, evidence, repo
from v2_automation.downloader import DownloadManager, RetryWindowExceeded
from v2_automation.models import Episode
from v2_automation.states import State


class FakeClient:
    def get(self, url):
        class R:
            ok = True
            status_code = 200
            text = url
        return R()

    def close(self):
        pass


class FakeExtraction:
    def __init__(self, episode_key=None, renditions=None):
        self.episode_key = episode_key or "https://www.example.com/anime/a1/e01-vostfr"
        self.renditions = renditions if renditions is not None else [FakeRendition()]

    @property
    def episode_url(self):
        return self.episode_key


class FakeRendition:
    resolution = "1920x1080"
    bandwidth_bps = 4_000_000
    fps = "24"
    video_codec = "h264"
    audio_codec = "aac"
    playlist_url = "https://cdn.example.com/pl.m3u8"


class FakePlaylist:
    segments = [object()]
    total_duration_seconds = 1200.0
    encrypted = False


class FakeDownloadResult:
    class M:
        average_download_rate_bps = 5_000_000
        download_started_at = "t0"
        download_finished_at = "t1"
        download_duration_seconds = 12.0
        bytes_downloaded = 1_000_000
        segment_count = 10

    measurements = M()

    def __init__(self, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00" * 64)


class FakeValidation:
    verdict = "VALID"
    size_bytes = 64
    duration_seconds = 1200.0
    format_name = "mp4"
    video = {"codec_name": "h264", "width": 1920, "height": 1080}
    audio = {"codec_name": "aac"}
    checks = [{"check": "x", "pass": True}]
    mismatches_vs_manifest = []


class FakeTelegram:
    class _Chat:
        id = "-100x"

    class _Video:
        file_id = "vid1"
        file_size = 64
        duration = 1200.0

    class _Msg:
        def __init__(self, message_id, kind="video"):
            self.message_id = message_id
            self.chat = FakeTelegram._Chat()
            self.caption = None
            self.video = FakeTelegram._Video() if kind == "video" else None
            self.photo = None

    def __init__(self):
        self.log = []

    def send_photo(self, path, caption=None):
        self.log.append(("photo", Path(path).name))
        m = self._Msg(101, kind="photo")
        m.photo = [type("P", (), {"file_id": "ph1"})()]
        return m

    def send_video(self, path, caption=None):
        self.log.append(("video", Path(path).name))
        return self._Msg(202, kind="video")

    def close(self):
        pass


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite3.connect(tmp_path / "t.sqlite3")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _cfg(tmp_path):
    from v2_automation.app_config import AppConfig, BotCapacity
    return AppConfig(
        source={}, queues={},
        downloads={"retry_window_hours": 24},
        telegram={"api_base_url": "http://127.0.0.1:8081", "thumbnail_before_video": True},
        publication={}, limits={}, monitoring={}, logging={},
        bot_token="t", channel_id="-100x", admin_telegram_ids=[],
        bot_capacity=BotCapacity(True, True, None, None, None, None, None, None),
    )


def _mk_episode(conn, key="https://www.example.com/anime/a1/e01-vostfr") -> int:
    ep = Episode(anime_key="a1", episode_key=key,
                 canonical_episode_url=key + "/", language="vostfr",
                 episode_number=1, episode_url=key)
    eid, _ = repo.upsert_episode(conn, ep)
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    conn.commit()
    return eid


def _build_deps(ttmp, *, telegram=None, structure="ok",
                extraction=None, validate_later=None):
    import v2_automation.downloader as dm

    def structure_check(url):
        if structure == "changed":
            return False, "structure_mismatch"
        return True, "ok"

    def download(playlist, output_path, ffmpeg, **kw):
        return FakeDownloadResult(output_path)

    def validate(path, ffprobe, expected=None):
        if validate_later is not None:
            return validate_later
        return FakeValidation()

    def thumb(video_path, out, ffmpeg, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return out

    return dm.DownloadDeps(
        extract=lambda url, client: extraction or FakeExtraction(),
        load_media_playlist=lambda text, url: FakePlaylist(),
        download=download,
        validate=validate,
        sha256=lambda path: "sha" + "0" * 60,
        http_client=lambda: FakeClient(),
        ffmpeg=lambda: Path("ffmpeg"),
        ffprobe=lambda: Path("ffprobe"),
        telegram=lambda: (telegram if telegram is not None else FakeTelegram()),
        structure_check=structure_check,
        thumbnail=thumb,
    )


def test_happy_path_full_durable_chain(conn, tmp_path):
    cfg = _cfg(tmp_path)
    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "cleanup_pending"
    ep = repo.get(conn, eid)
    assert ep.status == "cleanup_pending"
    assert ep.video_message_id == 202
    assert ep.thumbnail_message_id == 101
    assert ep.file_path and Path(ep.file_path).exists()
    pubs = conn.execute("SELECT publication_type, status, message_id FROM publications").fetchall()
    types = sorted((r["publication_type"], r["message_id"]) for r in pubs)
    assert types == [("first_publication", 202), ("thumbnail", 101)]
    # publishing was sequential thumbnail->video
    assert Path(ep.file_path).stat().st_size == 64


def test_no_thumbnail_message_when_disabled(conn, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.telegram["thumbnail_before_video"] = False
    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "cleanup_pending"
    ep = repo.get(conn, eid)
    assert ep.thumbnail_message_id is None
    assert ep.video_message_id == 202
    assert conn.execute("SELECT COUNT(*) AS n FROM publications").fetchone()["n"] == 1


def test_structure_change_blocks(conn, tmp_path):
    cfg = _cfg(tmp_path)
    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path, structure="changed"))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "structure_changed"
    assert repo.get(conn, eid).status == "structure_changed"


def test_failure_routes_to_retry_wait_then_failed(conn, tmp_path):
    import v2_automation.downloader as dm
    cfg = _cfg(tmp_path)

    class BoomValidation:
        verdict = "INVALID"
        size_bytes = 0
        mismatch = ["truncated"]

    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path, validate_later=BoomValidation()))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "retry_wait"
    ep = repo.get(conn, eid)
    assert ep.retry_count == 1
    assert ep.retry_until_at is not None

    # second run while still inside the window -> still retry_wait (higher count)
    assert mgr.process_episode(eid) == "retry_wait"
    assert repo.get(conn, eid).retry_count == 2

    # force exhaustion: expire the retry window
    from datetime import datetime, timedelta, timezone
    expired = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    repo.set_retry_until(conn, eid, expired, retry_count=9)
    conn.commit()
    assert mgr.process_episode(eid) == "failed"
    assert repo.get(conn, eid).status == "failed"
    assert repo.get(conn, eid).last_error is not None


def test_duplicate_guard_skips(conn, tmp_path):
    # extraction reports a DIFFERENT canonicalization than the queued episode -> dedup
    other = FakeExtraction(episode_key="https://www.example.com/anime/a1/e01-vostfr/")
    other.episode_key = "https://www.example.com/anime/a1/e01-vostfr" + "-dup"
    cfg = _cfg(tmp_path)
    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path, extraction=other))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "skipped_dup"
    assert repo.get(conn, eid).status == "skipped_dup"


def test_evidence_dir_writes(tmp_path):
    p = evidence.evidence_dir("media", "downloads")
    assert p.exists()
    el = evidence.write_json(p / "x.json", {"a": 1})
    assert el.read_text(encoding="utf-8") == '{\n  "a": 1\n}'


def test_episode_already_done_prevents_rerun(conn, tmp_path):
    from v2_automation.downloader import EpisodeAlreadyDone
    cfg = _cfg(tmp_path)
    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "cleanup_pending"
    with pytest.raises(EpisodeAlreadyDone):
        mgr.process_episode(eid)


def test_over_ceiling_rendition_refused_before_download(conn, tmp_path):
    """Phase 6: an episode estimated above the proven upload cap goes to
    RETRY_WAIT (with a clear ceiling error) BEFORE any disk is consumed."""
    class HugeRendition(FakeRendition):
        bandwidth_bps = 30_000_000          # ~3.75 MB/s at 1200s => 4.3 GiB
        resolution = "3840x2160"

    huge = FakeExtraction(renditions=[HugeRendition()])
    cfg = _cfg(tmp_path)
    mgr = DownloadManager(conn, cfg, deps=_build_deps(tmp_path, extraction=huge),
                          max_publish_bytes=768 * 1024 * 1024)
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "retry_wait"
    ep = repo.get(conn, eid)
    assert "plafond" in (ep.last_error or "") or ep.last_error
    assert ep.file_path is None and ep.video_message_id is None

# ── regressions: PNG poster ignored (frame used instead) / truncated cached MP4 reused ──────────────────────────────
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@pytest.mark.parametrize("payload,ext", [(PNG, ".png"), (JPG, ".jpg"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
                                         (b"<html>", None)])
def test_image_ext_by_magic_bytes(payload, ext):
    from v2_automation.downloader import _image_ext
    assert _image_ext(payload) == ext


@pytest.mark.parametrize("payload,ext", [(PNG, ".png"), (JPG, ".jpg")])
def test_source_poster_used_whatever_its_format(conn, tmp_path, monkeypatch, payload, ext):
    import httpx
    monkeypatch.setattr(evidence, "evidence_dir", lambda name: (tmp_path / name).resolve() if (tmp_path / name).mkdir(parents=True, exist_ok=True) is None else None)

    class R:
        status_code = 200
        content = payload
        headers = {"content-type": "image/x"}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path))
    eid = _mk_episode(conn)
    out = mgr._make_thumbnail(repo.get(conn, eid), tmp_path / "v.mp4", "https://x.example/p")
    assert out.name == f"thumb_{eid}_source{ext}" and out.read_bytes() == payload   # the poster, not a video frame


def test_reuse_cached_rejects_truncated_mp4(conn, tmp_path):
    v = _build_deps(tmp_path)
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=v)
    f = tmp_path / "ep.mp4"
    f.write_bytes(b"x" * 10)

    class Bad(FakeValidation):
        verdict = "INVALID"
        checks = [{"check": "vs_manifest_duration", "pass": False}]
    mgr.deps.validate = lambda path, ffprobe, expected=None: Bad()
    assert mgr._reuse_cached(f, {"duration_seconds": 1430}) is None
    assert not f.exists()                                   # partial file removed -> re-download


def test_direct_mp4_truncation_detected_from_sibling_durations(conn, tmp_path):
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path))
    sib = [_mk_episode(conn, key=f"https://www.example.com/anime/a1/e0{i}-vostfr") for i in (2, 3)]
    for i, e in enumerate(sib):
        conn.execute("UPDATE episodes SET video_message_id=? WHERE id=?", (900 + i, e))
        mgr._remember_duration(e, 1430.0 + 10 * i)
    cur = _mk_episode(conn, key="https://www.example.com/anime/a1/e09-vostfr")
    assert mgr._reference_duration(repo.get(conn, cur)) == 1435.0          # median of the siblings
    other = _mk_episode(conn, key="https://www.example.com/anime/zz/e01-vostfr")
    conn.execute("UPDATE episodes SET anime_key='zz' WHERE id=?", (other,))
    assert mgr._reference_duration(repo.get(conn, other)) is None           # no history: no reference
