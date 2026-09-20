"""Players fallback (myTV -> Stape direct MP4) and the "video not ready" state (404: silent retries every 3 min)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from test_downloader import FakeTelegram, _build_deps, _cfg as _dl_cfg, _mk_episode, conn as dl_conn  # noqa: F401
from v1_poc.source_client import SourceAccessError

from v2_automation import alerts, errors, players, repo
from v2_automation.downloader import DownloadManager

EPISODE = "https://voir-anime.to/anime/black-torch/black-torch-12-vostfr/"
PAGE = ('<select class="host-select"><option value="LECTEUR myTV"></option><option value="LECTEUR MOON"></option>'
        '<option value="LECTEUR VOE"></option><option value="LECTEUR Stape"></option></select>'
        '<select class="host-select"><option value="LECTEUR myTV"></option></select><title>Black Torch - 12</title>')
STAPE_EMBED = ("<script>document.getElementById('robotlink').innerHTML = '//streamt'+ "
               "('xcdape.com/get_video?id=GMk&expires=1&ip=IP&token=TOK').substring(2).substring(1);</script>")
STAPE_EMBED = STAPE_EMBED.replace("getElementById('robotlink')", "getElementById('robotlink')").replace(
    "document.getElementById('robotlink').innerHTML", "document.getElementById('robotlink').innerHTML")


class Resp:
    def __init__(self, text="", status=200, headers=None):
        self.text, self.status_code, self.headers = text, status, headers or {}


class FakeHttp:
    """Answers the few URLs the fallback touches."""
    def __init__(self, stape_ok=True):
        self.stape_ok, self.calls = stape_ok, []

    def get(self, url, headers=None):
        self.calls.append(url)
        if url == EPISODE:
            return Resp(PAGE)
        if "host=" in url:
            host = url.split("host=")[1]
            frame = {"LECTEUR%20MOON": "https://mfw09.org/e/abc", "LECTEUR%20VOE": "https://voe.sx/e/abc",
                     "LECTEUR%20Stape": "https://streamtape.com/e/GMk"}[host]
            return Resp(f'<iframe src="{frame}"></iframe>')
        if url == "https://streamtape.com/e/GMk":
            return Resp(STAPE_EMBED.replace("robotlink')", "robotlink')"))
        if "get_video" in url:
            if not self.stape_ok:
                return Resp("", 404, {"content-type": "text/html"})
            return Resp("", 206, {"content-type": "video/mp4", "content-range": "bytes 0-0/324867418"})
        return Resp("", 404)

    def close(self):
        pass


def _primary_404(url, client):
    raise SourceAccessError("master manifest fetch failed: status=404 error=HTTP_404")


def test_streamtape_address_is_decoded_and_the_players_are_listed():
    html = "x.robotlink').innerHTML = '//streamt'+ ('xcdape.com/get_video?id=A&token=B').substring(2).substring(1);"
    assert players.streamtape_direct_url(html) == "https://streamtape.com/get_video?id=A&token=B"
    assert players.streamtape_direct_url("nothing here") is None
    assert players.host_names(PAGE) == ["LECTEUR myTV", "LECTEUR MOON", "LECTEUR VOE", "LECTEUR Stape"]


def test_the_default_player_is_used_alone_when_it_works():
    http = FakeHttp()
    sentinel = SimpleNamespace(renditions=[1], direct_url=None)
    assert players.extract_with_fallback(EPISODE, None, primary=lambda u, c: sentinel, http=http) is sentinel
    assert http.calls == []                                             # no needless request


def test_when_mytv_fails_stape_serves_the_episode_as_a_direct_mp4(monkeypatch):
    http = FakeHttp()
    fake_record = SimpleNamespace(episode_key="k", anime_key="a", anime_post_id="1", episode_number=12,
                                  page_title_raw="Black Torch - 12", language=None)
    monkeypatch.setattr("source_audit.analysis.episode.parse_episode_page", lambda page, url: fake_record)
    ext = players.extract_with_fallback(EPISODE, None, primary=_primary_404, http=http)
    assert isinstance(ext, players.DirectExtraction) and ext.player_name == "Stape"
    assert ext.direct_url == "https://streamtape.com/get_video?id=GMk&expires=1&ip=IP&token=TOK"
    assert ext.direct_size == 324867418 and ext.episode_number == 12 and ext.renditions == []


def test_every_player_failing_lists_what_was_tried(monkeypatch):
    fake_record = SimpleNamespace(episode_key="k", anime_key="a", anime_post_id="1", episode_number=12,
                                  page_title_raw="t", language=None)
    monkeypatch.setattr("source_audit.analysis.episode.parse_episode_page", lambda page, url: fake_record)
    with pytest.raises(SourceAccessError) as ei:
        players.extract_with_fallback(EPISODE, None, primary=_primary_404, http=FakeHttp(stape_ok=False))
    msg = str(ei.value)
    assert "manifest fetch failed: status=404" in msg and "MOON : non pris en charge" in msg \
        and "VOE : non pris en charge" in msg and "Stape : vidéo indisponible" in msg


# ── the pipeline: direct MP4 path + "video not ready" state ──────────────────────

def _manager(conn, tmp_path, extract):
    deps = _build_deps(tmp_path, telegram=FakeTelegram())
    deps.extract = extract
    return DownloadManager(conn, _dl_cfg(tmp_path), deps=deps)


def test_a_direct_mp4_episode_is_downloaded_validated_published_and_the_player_is_recorded(dl_conn, tmp_path, monkeypatch):
    eid = _mk_episode(dl_conn)
    ep = repo.get(dl_conn, eid)
    ext = players.DirectExtraction(episode_url=ep.episode_url, episode_key=ep.episode_key, anime_key="a1", anime_post_id="1",
                                   episode_number=1, player_name="Stape", direct_url="https://streamtape.com/get_video?x",
                                   direct_size=64, direct_referer="https://streamtape.com/e/x")
    mgr = _manager(dl_conn, tmp_path, lambda url, client: ext)
    written = {}

    def fake_direct(extraction, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00" * 64)
        written["path"] = output_path
        return SimpleNamespace(measurements=SimpleNamespace(download_duration_seconds=1.0, average_download_rate_bps=1000))
    monkeypatch.setattr(mgr, "_download_direct", fake_direct)
    assert mgr.process_episode(eid) == "cleanup_pending"
    assert written["path"].exists() and repo.get(dl_conn, eid).video_message_id
    assert dl_conn.execute("SELECT cvalue FROM control WHERE ckey=?", (f"player:{eid}",)).fetchone()[0] == "Stape"


def test_the_direct_download_refuses_an_incomplete_file_and_leaves_nothing_behind(tmp_path, monkeypatch):
    class Half(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, headers={"content-length": "100"}, content=b"x" * 40)      # stops short

    real = httpx.Client
    monkeypatch.setattr("httpx.Client", lambda **kw: real(transport=Half(), **{k: v for k, v in kw.items() if k != "transport"}))
    mgr = DownloadManager.__new__(DownloadManager)
    ext = SimpleNamespace(direct_url="https://s/x", direct_referer="https://s/", direct_size=100)
    out = tmp_path / "video.mp4"
    with pytest.raises(RuntimeError, match="incomplet"):
        mgr._download_direct(ext, out)
    assert not out.exists() and not out.with_suffix(".part").exists()


def _flaky_extract(fails):
    """Video 404 for the first `fails` attempts, then a normal extraction."""
    calls = {"n": 0}
    good = _build_deps(None, telegram=FakeTelegram()).extract if False else None

    def extract(url, client):
        calls["n"] += 1
        if calls["n"] <= fails:
            raise SourceAccessError("master manifest fetch failed: status=404 error=HTTP_404")
        from test_downloader import FakeExtraction
        return FakeExtraction()
    return extract, calls


def test_a_404_is_a_silent_retry_every_3_minutes_and_publishes_as_soon_as_the_video_is_there(dl_conn, tmp_path):
    extract, calls = _flaky_extract(2)
    mgr = _manager(dl_conn, tmp_path, extract)
    eid = _mk_episode(dl_conn)
    assert mgr.process_episode(eid) == "retry_wait"
    ep = repo.get(dl_conn, eid)
    assert ep.retry_count == 1 and errors.SOURCE_VIDEO_PROCESSING in (ep.last_error or "") or "404" in ep.last_error
    delay = (datetime.fromisoformat(ep.next_retry_at.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    assert 150 < delay <= 190                                                   # ~3 minutes, not 1-2-4-8...
    until1 = ep.retry_until_at
    assert mgr.process_episode(eid) == "retry_wait" and repo.get(dl_conn, eid).retry_count == 2
    assert repo.get(dl_conn, eid).retry_until_at == until1                      # the 24 h window is not refreshed
    assert mgr.process_episode(eid) == "cleanup_pending"                        # video there: published at once
    assert calls["n"] == 3 and FakeTelegram is not None


def test_nothing_is_posted_and_no_alert_before_20_minutes_then_one_alert(dl_conn, tmp_path):
    seen = []
    extract, _ = _flaky_extract(99)
    tg = FakeTelegram()
    deps = _build_deps(tmp_path, telegram=tg)
    deps.extract = extract
    mgr = DownloadManager(dl_conn, _dl_cfg(tmp_path), deps=deps, alerter=lambda *a, **k: seen.append(a))
    eid = _mk_episode(dl_conn)
    mgr.process_episode(eid)
    assert tg.log == [] and seen == []                                          # no thumbnail, no video, no alert
    assert alerts.raise_persistent_errors(dl_conn, minutes=20) == 0             # 0 min in: silence
    dl_conn.execute("UPDATE episodes SET retry_until_at=? WHERE id=?",
                    ((datetime.now(timezone.utc) + timedelta(hours=24) - timedelta(minutes=21)).isoformat(), eid))
    dl_conn.commit()
    assert alerts.raise_persistent_errors(dl_conn, minutes=20) == 1             # 21 min: one alert
    assert alerts.raise_persistent_errors(dl_conn, minutes=20) == 0             # never repeated


def test_a_harmless_chapter_track_warning_does_not_fail_the_integrity_check(tmp_path, monkeypatch):
    """Direct MP4s from the Stape player carry a QuickTime chapter reference ffprobe grumbles about (rc=0): the video
    packets are fine, so this warning must not make a good episode INVALID (it did, for Black Torch E12)."""
    import json
    import subprocess
    from v1_poc import validator
    ok = json.dumps({"streams": [{"nb_frames": "100", "nb_read_packets": "100"}]})

    def run(stderr, rc=0):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout=ok, stderr=stderr)
    monkeypatch.setattr(subprocess, "run", run("[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] Referenced QT chapter track not found\n"))
    assert validator.check_packet_integrity(tmp_path / "x.mp4", tmp_path / "ffprobe")[0] is True
    monkeypatch.setattr(subprocess, "run", run("[mov,mp4 @ 0x1] moov atom not found\n"))
    assert validator.check_packet_integrity(tmp_path / "x.mp4", tmp_path / "ffprobe")[0] is False       # real errors still fail
    monkeypatch.setattr(subprocess, "run", run("", rc=1))
    assert validator.check_packet_integrity(tmp_path / "x.mp4", tmp_path / "ffprobe")[0] is False
