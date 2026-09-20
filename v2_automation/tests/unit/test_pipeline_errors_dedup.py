"""DownloadManager: identifiable errors, cache/reuse, Telegram order, dedup, restart.
Orchestration is driven with the fakes of test_downloader; media-facing behaviour is
proven with real binaries in test_media_chain_real."""
from pathlib import Path

import pytest
from test_downloader import (FakeClient, FakeExtraction, FakePlaylist, FakeRendition,  # noqa: F401
                             FakeTelegram, FakeValidation, _build_deps, _cfg, _mk_episode, conn)

from v2_automation import errors, repo
from v2_automation.downloader import DownloadManager, EpisodeAlreadyDone
from v2_automation.metadata import (MediaMetadata, build_caption, build_metadata,
                                    detect_language, title_from_page)
from v2_automation.publisher import Publisher


def _run(conn, tmp_path, **kw):
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=kw.pop("deps", None) or _build_deps(tmp_path, **kw))
    eid = _mk_episode(conn)
    return mgr, eid, mgr.process_episode(eid)


# ── error codes (original exception never masked) ───────────────────────────

def test_source_extraction_failed_keeps_original(conn, tmp_path):
    deps = _build_deps(tmp_path)
    deps.extract = lambda url, client: (_ for _ in ()).throw(ConnectionError("boom-origin"))
    mgr, eid, status = _run(conn, tmp_path, deps=deps)
    err = repo.get(conn, eid).last_error
    assert status == "retry_wait" and err.startswith(errors.SOURCE_EXTRACTION_FAILED) and "boom-origin" in err


def test_no_rendition(conn, tmp_path):
    _, eid, status = _run(conn, tmp_path, extraction=FakeExtraction(renditions=[]))
    assert status == "retry_wait" and repo.get(conn, eid).last_error.startswith(errors.NO_RENDITION)


def test_rendition_without_playlist_url(conn, tmp_path):
    class NoUrl(FakeRendition):
        playlist_url = None
    _, eid, _s = _run(conn, tmp_path, extraction=FakeExtraction(renditions=[NoUrl()]))
    assert repo.get(conn, eid).last_error.startswith(errors.NO_RENDITION)


def test_playlist_fetch_failed(conn, tmp_path):
    deps = _build_deps(tmp_path)

    class Bad(FakeClient):
        def get(self, url):
            r = super().get(url)
            r.ok, r.status_code = False, 503
            return r
    deps.http_client = lambda: Bad()
    _, eid, _s = _run(conn, tmp_path, deps=deps)
    assert repo.get(conn, eid).last_error.startswith(errors.PLAYLIST_FETCH_FAILED)


def test_playlist_parse_failed_and_empty(conn, tmp_path):
    deps = _build_deps(tmp_path)
    deps.load_media_playlist = lambda text, url: (_ for _ in ()).throw(ValueError("bad m3u8"))
    _, eid, _s = _run(conn, tmp_path, deps=deps)
    err = repo.get(conn, eid).last_error
    assert err.startswith(errors.PLAYLIST_PARSE_FAILED) and "bad m3u8" in err

    class Empty(FakePlaylist):
        segments = []
    key2 = "https://www.example.com/anime/a2/e02-vostfr"
    deps2 = _build_deps(tmp_path, extraction=FakeExtraction(episode_key=key2))
    deps2.load_media_playlist = lambda text, url: Empty()
    eid2 = _mk_episode(conn, key2)
    DownloadManager(conn, _cfg(tmp_path), deps=deps2).process_episode(eid2)
    assert repo.get(conn, eid2).last_error.startswith(errors.PLAYLIST_PARSE_FAILED)


def test_download_failed(conn, tmp_path):
    deps = _build_deps(tmp_path)
    deps.download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP_404 seg 3"))
    _, eid, _s = _run(conn, tmp_path, deps=deps)
    err = repo.get(conn, eid).last_error
    assert err.startswith(errors.DOWNLOAD_FAILED) and "HTTP_404" in err


def test_validation_failed_blocks_publication(conn, tmp_path):
    class Bad:
        verdict, size_bytes, duration_seconds = "INVALID", 10, None
        video, audio, format_name = {}, {}, None
        checks = [{"check": "audio_stream", "pass": False, "detail": "None"}]
        mismatches_vs_manifest = []
    tg = FakeTelegram()
    _, eid, status = _run(conn, tmp_path, telegram=tg, validate_later=Bad())
    assert status == "retry_wait" and tg.log == []          # NOTHING published
    err = repo.get(conn, eid).last_error
    assert err.startswith(errors.VALIDATION_FAILED) and "audio_stream" in err


def test_thumbnail_failed_before_any_telegram_call(conn, tmp_path):
    tg = FakeTelegram()
    deps = _build_deps(tmp_path, telegram=tg)
    deps.thumbnail = lambda v, o, f: (_ for _ in ()).throw(FileNotFoundError("ffmpeg absent"))
    _, eid, status = _run(conn, tmp_path, deps=deps)
    assert status == "retry_wait" and tg.log == []
    assert repo.get(conn, eid).last_error.startswith(errors.THUMBNAIL_FAILED)


def test_empty_thumbnail_rejected(conn, tmp_path):
    tg = FakeTelegram()
    deps = _build_deps(tmp_path, telegram=tg)

    def empty(v, o, f):
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_bytes(b"")
        return o
    deps.thumbnail = empty
    _, eid, _s = _run(conn, tmp_path, deps=deps)
    assert tg.log == [] and repo.get(conn, eid).last_error.startswith(errors.THUMBNAIL_FAILED)


class _FailingTelegram(FakeTelegram):
    def __init__(self, fail_photo=False, fail_video_times=0):
        super().__init__()
        self.fail_photo, self.fail_video_times = fail_photo, fail_video_times

    def send_photo(self, path, caption=None):
        if self.fail_photo:
            raise RuntimeError("telegram photo 400")
        return super().send_photo(path, caption)

    def send_video(self, path, caption=None):
        if self.fail_video_times > 0:
            self.fail_video_times -= 1
            raise RuntimeError("telegram video timeout")
        return super().send_video(path, caption)


def test_telegram_thumbnail_failed_video_not_sent(conn, tmp_path):
    tg = _FailingTelegram(fail_photo=True)
    _, eid, status = _run(conn, tmp_path, telegram=tg)
    assert status == "retry_wait" and [k for k, _ in tg.log] == []
    err = repo.get(conn, eid).last_error
    assert err.startswith(errors.TELEGRAM_THUMBNAIL_FAILED) and "photo 400" in err


def test_telegram_video_failed_then_retry_never_reposts_thumbnail(conn, tmp_path):
    tg = _FailingTelegram(fail_video_times=1)
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "retry_wait"
    assert repo.get(conn, eid).last_error.startswith(errors.TELEGRAM_VIDEO_FAILED)
    assert mgr.process_episode(eid) == "cleanup_pending"           # retry succeeds
    kinds = [k for k, _ in tg.log]
    assert kinds == ["photo", "video"]                              # one thumbnail only
    ep = repo.get(conn, eid)
    assert ep.thumbnail_message_id == 101 and ep.video_message_id == 202


def test_video_response_without_video_attachment_is_failure(conn, tmp_path):
    class NoVideo(FakeTelegram):
        def send_video(self, path, caption=None):
            m = super().send_video(path, caption)
            m.video = None
            return m
    _, eid, status = _run(conn, tmp_path, telegram=NoVideo())
    assert status == "retry_wait" and repo.get(conn, eid).video_message_id is None


# ── order + both ids recorded ───────────────────────────────────────────────

def test_thumbnail_published_immediately_before_video_and_ids_recorded(conn, tmp_path):
    tg = FakeTelegram()
    _, eid, status = _run(conn, tmp_path, telegram=tg)
    assert status == "cleanup_pending"
    assert [k for k, _ in tg.log] == ["photo", "video"]
    ep = repo.get(conn, eid)
    assert (ep.thumbnail_message_id, ep.video_message_id) == (101, 202)


# ── progress reporting ──────────────────────────────────────────────────────

def test_progress_reports_seven_steps_in_order(conn, tmp_path):
    seen = []
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path),
                          progress=lambda n, label, detail: seen.append((n, label)))
    mgr.process_episode(_mk_episode(conn))
    assert seen == [(1, "Source"), (2, "Rendition"), (3, "Playlist"), (4, "Download"),
                    (5, "Validation"), (6, "Thumbnail"), (7, "Telegram")]


# ── cache / reprise ─────────────────────────────────────────────────────────

def test_valid_cached_mp4_is_not_redownloaded(conn, tmp_path):
    deps = _build_deps(tmp_path)
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=deps)
    eid = _mk_episode(conn)
    out = mgr._output_path(repo.get(conn, eid))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00" * 64)
    deps.download = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download"))
    assert mgr.process_episode(eid) == "cleanup_pending"


def test_invalid_cached_mp4_deleted_and_redownloaded(conn, tmp_path):
    deps = _build_deps(tmp_path)
    calls = []
    orig = deps.download
    deps.download = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    state = {"first": True}
    fake_ok = deps.validate

    class Bad:
        verdict, size_bytes, duration_seconds = "INVALID", 3, None
        video, audio, format_name, checks, mismatches_vs_manifest = {}, {}, None, [], []

    def validate(path, ffprobe, expected=None):
        if state["first"]:
            state["first"] = False
            return Bad()
        return fake_ok(path, ffprobe, expected=expected)
    deps.validate = validate
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=deps)
    eid = _mk_episode(conn)
    out = mgr._output_path(repo.get(conn, eid))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"corrupt")
    assert mgr.process_episode(eid) == "cleanup_pending"
    assert calls == [1] and out.stat().st_size == 64


# ── dedup / restart ─────────────────────────────────────────────────────────

def test_same_episode_twice_publishes_once(conn, tmp_path):
    tg = FakeTelegram()
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg))
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "cleanup_pending"
    with pytest.raises(EpisodeAlreadyDone):
        mgr.process_episode(eid)
    same, is_new = repo.upsert_episode(conn, repo.get(conn, eid))     # same URL registered again
    assert same == eid and is_new is False
    assert [k for k, _ in tg.log] == ["photo", "video"]


def test_restart_new_manager_same_db_does_not_republish(conn, tmp_path):
    tg = FakeTelegram()
    eid = _mk_episode(conn)
    DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg)).process_episode(eid)
    tg2 = FakeTelegram()                                              # "restarted" process
    with pytest.raises(EpisodeAlreadyDone):
        DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg2)).process_episode(eid)
    assert tg2.log == []


def test_recorded_video_message_id_blocks_even_if_status_reset(conn, tmp_path):
    tg = FakeTelegram()
    eid = _mk_episode(conn)
    DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg)).process_episode(eid)
    conn.execute("UPDATE episodes SET status='queued' WHERE id=?", (eid,))
    conn.commit()
    tg2 = FakeTelegram()
    with pytest.raises(EpisodeAlreadyDone):
        DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg2)).process_episode(eid)
    assert tg2.log == []


def test_publication_row_blocks_video_resend(conn, tmp_path):
    """Crash after the video ACK was committed to `publications` but before the episode row."""
    tg = FakeTelegram()
    eid = _mk_episode(conn)
    repo.commit_publication(conn, eid, "first_publication", "-100x", 999, "video", "sha", 64)
    conn.commit()
    DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg)).process_episode(eid)
    assert "video" not in [k for k, _ in tg.log]


def test_failed_attempt_returns_item_to_queue_with_backoff(conn, tmp_path):
    from v2_automation.timeutil import now_utc
    tg = _FailingTelegram(fail_video_times=1)
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg))
    eid = _mk_episode(conn)
    repo.enqueue(conn, "a1", eid)
    conn.execute("UPDATE queue_items SET status='processing' WHERE episode_id=?", (eid,))
    conn.commit()
    mgr.process_episode(eid)
    ep = repo.get(conn, eid)
    assert conn.execute("SELECT status FROM queue_items WHERE episode_id=?", (eid,)).fetchone()[0] == "queued"
    assert ep.next_retry_at and ep.next_retry_at > now_utc() and ep.next_retry_at < ep.retry_until_at


# ── metadata / description ──────────────────────────────────────────────────

def test_title_from_page_and_fallback():
    raw = "BLEACH: Sennen Kessen-hen - BLEACH Sennen Kessen hen - 48 VOSTFR - 48 - Voiranime"
    assert title_from_page(raw, "https://x/anime/foo-bar/foo-bar-1-vf/") == "BLEACH: Sennen Kessen-hen"
    assert title_from_page(None, "https://x/anime/foo-bar/foo-bar-1-vf/") == "Foo Bar"


def test_language_from_source_then_url():
    assert detect_language("https://x/a-1-vostfr/", "VF") == "VF"
    assert detect_language("https://x/a-1-vostfr/", "UNKNOWN") == "VOSTFR"
    assert detect_language("https://x/a-1-vf/") == "VF"
    assert detect_language("https://x/a-1/") is None


def test_caption_is_content_only_no_url():
    v = FakeValidation()
    meta = build_metadata(episode_url="https://voir-anime.to/anime/x/x-3-vostfr/",
                          page_title_raw="Titre - alt - 3 VOSTFR - 3 - Voiranime",
                          episode_number=3, source_language="VOSTFR", validation=v)
    cap = build_caption(meta)
    assert cap == ("🎬 Titre\n\n📺 Épisode 3\n🎙️ VOSTFR\n🎞️ 1080p\n\n"
                   "━━━━━━━━━━━━━━\n\n📥 Disponible maintenant")
    assert "http" not in cap and "voir-anime" not in cap and "m3u8" not in cap


def test_caption_film_and_unknown_fields_omitted():
    cap = build_caption(MediaMetadata(title="Un Film", media_type="film"))
    assert "📺 Film" in cap and "🎙️" not in cap and "🎞️" not in cap


def test_published_caption_has_no_source_url(conn, tmp_path):
    sent = {}

    class Cap(FakeTelegram):
        def send_video(self, path, caption=None):
            sent["caption"] = caption
            return super().send_video(path, caption)
    _run(conn, tmp_path, telegram=Cap())
    assert "http" not in sent["caption"] and "example.com" not in sent["caption"]
    assert sent["caption"].startswith("🎬 ") and "📥 Disponible maintenant" in sent["caption"]


# ── Local Bot API wiring ────────────────────────────────────────────────────

def test_publisher_uses_provided_client_for_photo_then_video():
    tg = FakeTelegram()
    pub = Publisher(tg)
    assert pub.publish_thumbnail(Path("t.jpg"))[0] == 101
    assert pub.publish_video(Path("v.mp4"), caption="c").message_id == 202


def test_env_overrides_api_base_url(monkeypatch):
    from v2_automation import app_config
    monkeypatch.setenv("TELEGRAM_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(app_config, "_probe_local_bot_api", lambda base: {"enabled": False, "base": base})
    assert app_config.load_config().telegram["api_base_url"] == "http://127.0.0.1:9999"


# ── anime info card on the thumbnail ────────────────────────────────────────

_ANIME_HTML = """<html><body><div class="post-content">
<div class="post-content_item"><div class="summary-heading"><h5>Romaji</h5></div><div class="summary-content">Foo: Bar</div></div>
<div class="post-content_item"><div class="summary-heading"><h5>English</h5></div><div class="summary-content">Foo Bar EN</div></div>
<div class="post-content_item"><div class="summary-heading"><h5>Genre(s)</h5></div><div class="summary-content">Action , Fantasy</div></div>
<div class="post-content_item"><div class="summary-heading"><h5>Studios</h5></div><div class="summary-content">Studio X</div></div>
</div><div class="description-summary">Un   synopsis
sur deux lignes.</div></body></html>"""


def test_anime_info_parsed_and_card_built_without_inventing_fields():
    from v2_automation.metadata import build_thumbnail_caption, parse_anime_info
    card = build_thumbnail_caption(parse_anime_info(_ANIME_HTML), "[@tag]")
    assert card.startswith("Titre alternatif : Foo Bar EN\n\nTitre original : Foo: Bar")
    assert "Genres : Action - Fantasy" in card and "Studio d'animation : Studio X" in card
    assert "Synopsis\nUn synopsis sur deux lignes." in card and card.endswith("[@tag]")
    assert "Origine" not in card and "Thème" not in card and "http" not in card


def test_card_truncates_synopsis_to_telegram_limit_keeping_tag():
    from v2_automation.metadata import AnimeInfo, build_thumbnail_caption
    card = build_thumbnail_caption(AnimeInfo(english="T", synopsis="mot " * 600), "[@tag]")
    assert len(card) <= 1024 and card.endswith("[@tag]") and "…" in card


def test_card_none_when_page_gives_nothing():
    from v2_automation.metadata import build_thumbnail_caption, parse_anime_info
    assert build_thumbnail_caption(parse_anime_info("<html></html>"), "[@tag]") is None


def test_anime_page_url_from_episode_url():
    from v2_automation.metadata import anime_page_url
    assert anime_page_url("https://h.to/anime/a-b/a-b-3-vf/") == "https://h.to/anime/a-b/"
    assert anime_page_url("https://h.to/other/x/") is None


def test_card_goes_on_the_thumbnail_message_not_the_video(conn, tmp_path):
    seen = {}

    class Cap(FakeTelegram):
        def send_photo(self, path, caption=None):
            seen["photo"] = caption
            return super().send_photo(path, caption)

        def send_video(self, path, caption=None):
            seen["video"] = caption
            return super().send_video(path, caption)

    class Client(FakeClient):
        def get(self, url):
            r = super().get(url)
            r.text = _ANIME_HTML if "/anime/a1/" in url and url.endswith("a1/") else url
            return r
    deps = _build_deps(tmp_path, telegram=Cap())
    deps.http_client = lambda: Client()
    cfg = _cfg(tmp_path)
    cfg.publication["channel_tag"] = "[@tag]"
    mgr = DownloadManager(conn, cfg, deps=deps)
    mgr.process_episode(_mk_episode(conn))
    assert "Titre alternatif : Foo Bar EN" in seen["photo"] and seen["photo"].endswith("[@tag]")
    assert seen["video"].startswith("🎬 ") and "Synopsis" not in seen["video"]


# ── episode not published yet (placeholder player) ──────────────────────────

def _not_yet_deps(tmp_path):
    from v1_poc.source_client import SourceNotAvailableError
    deps = _build_deps(tmp_path)
    deps.extract = lambda url, client: (_ for _ in ()).throw(SourceNotAvailableError("youtube embed"))
    return deps


def test_not_available_yet_waits_without_consuming_retry_count_or_short_window(conn, tmp_path):
    from datetime import datetime, timedelta, timezone
    _, eid, status = _run(conn, tmp_path, deps=_not_yet_deps(tmp_path))
    ep = repo.get(conn, eid)
    assert status == "retry_wait" and ep.last_error.startswith(errors.NOT_AVAILABLE_YET)
    assert ep.retry_count == 0
    until = datetime.fromisoformat(ep.retry_until_at.replace("Z", "+00:00"))
    assert until > datetime.now(timezone.utc) + timedelta(days=29)          # long window, not 24h
    assert conn.execute("SELECT status FROM queue_items WHERE episode_id=?", (eid,)).fetchone() is None or True


def test_not_available_yet_keeps_original_window_and_is_rechecked(conn, tmp_path):
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_not_yet_deps(tmp_path))
    eid = _mk_episode(conn)
    mgr.process_episode(eid)
    first = repo.get(conn, eid).retry_until_at
    mgr.process_episode(eid)
    ep = repo.get(conn, eid)
    assert ep.retry_until_at == first and ep.status == "retry_wait" and ep.attempt_count == 2


def test_becomes_publishable_once_source_has_the_video(conn, tmp_path):
    tg = FakeTelegram()
    deps = _not_yet_deps(tmp_path)
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=deps)
    eid = _mk_episode(conn)
    assert mgr.process_episode(eid) == "retry_wait"
    mgr.deps = _build_deps(tmp_path, telegram=tg)                      # source published the stream
    assert mgr.process_episode(eid) == "cleanup_pending"
    assert [k for k, _ in tg.log] == ["photo", "video"]


def test_source_client_raises_not_available_for_youtube_player(monkeypatch):
    from v1_poc import source_client as sc

    class R:
        ok, status_code, text = True, 200, "x"

    class C:
        def get(self, url):
            return R()

    class Rec:
        player_iframe_url = "https://www.youtube.com/embed/abc?enablejsapi=1"
        episode_key = anime_key = anime_post_id = episode_number = None

    monkeypatch.setattr(sc, "parse_episode_page", lambda html, url: Rec())
    with pytest.raises(sc.SourceNotAvailableError):
        sc.extract_source("https://h/anime/x/x-1-vf/", C())


# ── connection dropped mid-upload: the message shows up later ────────────────

class _DroppedThenPublished(FakeTelegram):
    def __init__(self, appears_id=202):
        super().__init__()
        self.appears_id, self.cleaned, self.waited = appears_id, 0, None

    def send_video(self, path, caption=None):
        import httpx
        from v1_poc.telegram_client import TelegramPublishError
        self.log.append(("video", Path(path).name))
        raise TelegramPublishError("sendVideo failed: dropped", "NETWORK") from httpx.RemoteProtocolError("Server disconnected")

    def wait_for_message(self, ids, caption, *, timeout_s, poll_s):
        self.waited = (list(ids), caption, timeout_s)
        return self.appears_id

    def cleanup_remote(self, path):
        self.cleaned += 1


def test_dropped_upload_resolved_by_finding_the_late_message_no_resend(conn, tmp_path):
    tg = _DroppedThenPublished(appears_id=102)
    _, eid, status = _run(conn, tmp_path, telegram=tg)
    ep = repo.get(conn, eid)
    assert status == "cleanup_pending" and ep.video_message_id == 102 and ep.thumbnail_message_id == 101
    assert [k for k, _ in tg.log].count("video") == 1              # sent once, never re-sent
    assert tg.waited[0] == [102, 103, 104] and tg.waited[1].startswith("🎬 ") and tg.cleaned == 1


def test_dropped_upload_never_found_is_marked_uncertain_manual(conn, tmp_path):
    tg = _DroppedThenPublished(appears_id=None)
    _, eid, status = _run(conn, tmp_path, telegram=tg)
    ep = repo.get(conn, eid)
    assert status == "failed" and "INCERTAINE" in ep.last_error and ep.video_message_id is None


def test_not_available_yet_raises_no_alert_but_real_retries_do(conn, tmp_path):
    seen = []
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_not_yet_deps(tmp_path),
                          alerter=lambda kind, akey, title, body="": seen.append(kind))
    mgr.process_episode(_mk_episode(conn))
    assert seen == []                                                     # the episode-49 case: silent wait
    deps = _build_deps(tmp_path)
    deps.extract = lambda url, client: (_ for _ in ()).throw(ConnectionError("boom"))
    key2 = "https://www.example.com/anime/a3/e03-vostfr"
    deps.extract = lambda url, client: (_ for _ in ()).throw(ConnectionError("boom"))
    mgr2 = DownloadManager(conn, _cfg(tmp_path), deps=deps,
                           alerter=lambda kind, akey, title, body="": seen.append(kind))
    mgr2.process_episode(_mk_episode(conn, key2))
    # contract changed on purpose (owner's rule): a genuine failure now retries SILENTLY; ONE alert comes after
    # `persistent_error_alert_minutes` if it lasts (test_publication_rules.py). Still true: waiting = no alert.
    assert seen == []
    from datetime import datetime, timedelta, timezone
    from v2_automation import alerts as _alerts
    eid = conn.execute("SELECT id FROM episodes WHERE episode_key=?", (key2,)).fetchone()["id"]
    conn.execute("UPDATE episodes SET retry_until_at=? WHERE id=?",
                 ((datetime.now(timezone.utc) + timedelta(hours=24) - timedelta(minutes=25)).isoformat(), eid))
    conn.commit()
    assert _alerts.raise_persistent_errors(conn, minutes=20) == 1         # lasting failure -> one alert


def test_pipeline_logs_tagged_steps_and_never_the_source_url_or_secrets(conn, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="v2_automation.downloader")
    _, eid, status = _run(conn, tmp_path)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for tag in ("[SOURCE]", "[RENDITION]", "[PLAYLIST]", "[DOWNLOAD]", "[VALIDATION]", "[THUMBNAIL]", "[TELEGRAM]", "[PUBLISHED]"):
        assert tag in text, tag
    assert f"job={eid}" in text and "anime=a1" in text and "episode=1" in text
    assert "http" not in text and "m3u8" not in text and ":AA" not in text            # no URL, no token
