"""Private delivery + crash recovery (real SQLite, real files; the Telegram boundary is a recording fake — the real
Telegram round trip is NOT claimed here, see V2_TEST_REPORT.md).

Crash matrix (section 52): after download / before publication / after publication / before private delivery /
after private delivery.  Expected everywhere: no duplicate after the restart.
"""
import sqlite3
from pathlib import Path

import pytest
from test_downloader import FakeTelegram, _build_deps, _cfg, conn as dl_conn  # noqa: F401
from v1_poc.telegram_client import TelegramPublishError

from v2_automation import db, media, recovery, repo
from v2_automation.catalog import SourceCatalog
from v2_automation.delivery import DeliveryEngine
from v2_automation.downloader import DownloadManager
from v2_automation.requests_mgr import NewRequest, RequestManager
from v2_automation.telegram_publisher import (BotAPITransport, LocalBotAPITransport, Sent, TelegramPublisher, scrub)
from v2_automation.models import Episode
from v2_automation.timeutil import now_utc
from v2support import BASE, Clock, Site, cfg


class FakeTransport:
    """Records every call; failures can be scripted per call."""
    name = "fake"

    def __init__(self):
        self.calls, self.fail_next, self._mid = [], [], 1000

    def _next(self):
        self._mid += 1
        return self._mid

    def _maybe_fail(self):
        if self.fail_next:
            exc = self.fail_next.pop(0)
            if exc is not None:
                raise exc

    def send_video(self, chat_id, media, caption=None):
        self.calls.append(("send_video", chat_id, "file" if isinstance(media, Path) else "file_id"))
        self._maybe_fail()
        return Sent(self._next(), str(chat_id), file_id=f"FID{self._mid}", file_size=1)

    def copy_message(self, chat_id, from_chat_id, message_id):
        self.calls.append(("copy", chat_id, from_chat_id, message_id))
        self._maybe_fail()
        return Sent(self._next(), str(chat_id))

    def send_text(self, chat_id, text, keyboard=None, *, html=False):
        self.calls.append(("text", chat_id))
        return Sent(self._next(), str(chat_id))

    def member_status(self, chat_id, user_id):
        return "member"

    def get_me(self):
        return {"id": 1, "username": "fake"}

    def close(self):
        pass

    def kinds(self):
        return [c[0] + ":" + c[2] if c[0] == "send_video" else c[0] for c in self.calls]


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _mgr(conn, site):
    return RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)


def _ask(m, user, slug="u", post=2, ep=1):
    m.upsert_user(user)
    r = m.create(NewRequest(user_id=user, kind="episode", anime_key=f"postid:{post}", title="Anime Test", version="VOSTFR",
                            source_url=f"{BASE}/anime/{slug}/", episode_number=ep))
    return m.process(r["id"])


def _make_ready(conn, tmp_path, episode_id, size=2048):
    """What the download engine leaves behind for a private-only media (see test_private_media for the real path)."""
    f = tmp_path / f"e{episode_id}.mp4"
    f.write_bytes(b"x" * size)
    conn.execute("UPDATE episodes SET status='ready', file_path=?, file_size=?, video_sha256='s' WHERE id=?",
                 (str(f), size, episode_id))
    conn.execute("DELETE FROM queue_items WHERE episode_id=?", (episode_id,))
    conn.commit()
    return f


def _engine(conn, tr, **kw):
    return DeliveryEngine(conn, TelegramPublisher(tr), cfg(user_bot={"delivery_copy_from_channel": True}), **kw)


def n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


# -- N users, one media: 1 download, N deliveries, ONE upload --------------------------------------

def test_three_users_one_download_three_deliveries_one_upload(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    reqs = [_ask(m, u) for u in (11, 12, 13)]
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 1                                  # download_count = 1
    eid = n(conn, "SELECT id FROM episodes")
    _make_ready(conn, tmp_path, eid)
    tr = FakeTransport()
    out = _engine(conn, tr).run()
    assert out["sent"] == 3 and n(conn, "SELECT COUNT(*) FROM deliveries WHERE status='sent'") == 3   # delivery_count = 3
    assert tr.kinds() == ["send_video:file", "send_video:file_id", "send_video:file_id"]  # bytes uploaded ONCE
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 1
    for r in reqs:
        m.sync(r["id"])
        assert m.get(r["id"])["state"] == "COMPLETED"
    row = conn.execute("SELECT status, published_at, cleanup_at FROM episodes").fetchone()
    assert row["status"] == "published" and row["published_at"] and row["cleanup_at"]     # cleanup anchor set


def test_restart_does_not_resend_a_sent_delivery(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    _ask(m, 11)
    _make_ready(conn, tmp_path, n(conn, "SELECT id FROM episodes"))
    tr = FakeTransport()
    _engine(conn, tr).run()
    tr2 = FakeTransport()                                                                 # "restart": new engine, same DB
    recovery.run_recovery(conn, cfg())
    out = _engine(conn, tr2).run()
    assert out["sent"] == 0 and tr2.calls == [] and n(conn, "SELECT COUNT(*) FROM deliveries") == 1


def test_channel_media_is_copied_not_uploaded(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    _ask(m, 21)
    eid = n(conn, "SELECT id FROM episodes")
    conn.execute("UPDATE episodes SET status='published', video_message_id=77, publish_channel=1 WHERE id=?", (eid,))
    conn.execute("INSERT INTO publications(episode_id, publication_type, status, chat_id, message_id) "
                 "VALUES (?, 'first_publication', 'sent', '-100123', 77)", (eid,))
    conn.commit()
    tr = FakeTransport()
    out = _engine(conn, tr).run()
    assert out["sent"] == 1 and tr.calls == [("copy", 21, "-100123", 77)]                # no download, no upload


def test_cleaned_local_file_is_served_from_the_stored_file_id(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    _ask(m, 31)
    eid = n(conn, "SELECT id FROM episodes")
    f = _make_ready(conn, tmp_path, eid)
    tr = FakeTransport()
    _engine(conn, tr).run()                                                               # 1st user: upload, file_id stored
    f.unlink()                                                                            # the 14-day cleanup removed the file
    conn.execute("UPDATE episodes SET status='cleaned' WHERE id=?", (eid,))
    conn.commit()
    r2 = _ask(m, 32)
    out = _engine(conn, tr).run()
    assert out["sent"] == 1 and tr.kinds() == ["send_video:file", "send_video:file_id"]  # no re-download, no re-upload
    assert n(conn, "SELECT COUNT(*) FROM episodes") == 1 and r2["state"] in ("DELIVERING", "QUEUED", "PROCESSING")


# -- crash matrix -----------------------------------------------------------------------------

def _dl_episode(dl_conn, *, publish_channel=1):
    key = "https://www.example.com/anime/a1/e01-vostfr"
    ep = Episode(anime_key="a1", episode_key=key, canonical_episode_url=key + "/", language="vostfr", episode_number=1,
                 episode_url=key, publish_channel=publish_channel)
    eid, _ = repo.upsert_episode(dl_conn, ep)
    repo.transition(dl_conn, eid, "identified")
    repo.transition(dl_conn, eid, "queued")
    repo.enqueue(dl_conn, "a1", eid)
    dl_conn.commit()
    return eid


@pytest.mark.parametrize("crashed_at", ["downloaded", "validated"])
def test_crash_after_download_or_before_publication_reuses_the_file(dl_conn, tmp_path, crashed_at):
    calls = {"download": 0}
    tg = FakeTelegram()
    deps = _build_deps(tmp_path, telegram=tg)
    real_dl = deps.download

    def counting(*a, **k):
        calls["download"] += 1
        return real_dl(*a, **k)
    deps.download = counting
    mgr = DownloadManager(dl_conn, _cfg(tmp_path), deps=deps)
    eid = _dl_episode(dl_conn)
    out = mgr._output_path(repo.get(dl_conn, eid))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"v" * 1000)                                                          # the file the dead process left
    dl_conn.execute("UPDATE episodes SET status=? WHERE id=?", (crashed_at, eid))
    dl_conn.execute("UPDATE queue_items SET status='processing' WHERE episode_id=?", (eid,))
    dl_conn.commit()
    rec = recovery.run_recovery(dl_conn, _cfg(tmp_path))                                  # restart
    assert [r["episode_id"] for r in rec["reset_to_queued"]] == [eid]
    assert mgr.process_episode(eid) in ("published", "cleanup_pending")
    assert calls["download"] == 0                                                         # existing valid file reused
    assert [k for k, _ in tg.log] == ["photo", "video"]                                   # published exactly once


def test_crash_after_publication_completes_from_the_record_without_resending(dl_conn, tmp_path):
    tg = FakeTelegram()
    mgr = DownloadManager(dl_conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg))
    eid = _dl_episode(dl_conn)
    dl_conn.execute("UPDATE episodes SET status='publishing_video' WHERE id=?", (eid,))
    repo.commit_publication(dl_conn, eid, "thumbnail", "-100", 10, "photo", None, 1)
    repo.commit_publication(dl_conn, eid, "first_publication", "-100", 11, "video", "sha", 100)   # Telegram ACK recorded...
    dl_conn.commit()                                                                                # ...crash before the episode row
    rec = recovery.run_recovery(dl_conn, _cfg(tmp_path))
    ep = repo.get(dl_conn, eid)
    assert rec["publishing_completed_from_record"] == [{"episode_id": eid, "was": "publishing_video"}]
    assert ep.status == "cleanup_pending" and ep.video_message_id == 11 and ep.published_at and ep.cleanup_at
    assert ep.status != "failed" and tg.log == []                                          # nothing re-published, no manual step
    with pytest.raises(Exception):
        mgr.process_episode(eid)                                                           # EpisodeAlreadyDone: never re-sent


def test_crash_publishing_without_a_record_still_goes_to_manual_never_auto_resent(dl_conn, tmp_path):
    eid = _dl_episode(dl_conn)
    dl_conn.execute("UPDATE episodes SET status='publishing_video' WHERE id=?", (eid,))
    dl_conn.commit()
    rec = recovery.run_recovery(dl_conn, _cfg(tmp_path))
    assert rec["publishing_to_failed"] and repo.get(dl_conn, eid).status == "failed"


def test_crash_before_private_delivery_sends_once_after_restart(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    _ask(m, 41)
    _make_ready(conn, tmp_path, n(conn, "SELECT id FROM episodes"))
    _engine(conn, FakeTransport()).plan()                                                  # delivery planned (pending) ... crash
    recovery.run_recovery(conn, cfg())
    tr = FakeTransport()
    assert _engine(conn, tr).run()["sent"] == 1 and len(tr.calls) == 1
    assert _engine(conn, tr).run()["sent"] == 0 and len(tr.calls) == 1                     # and never again


def test_crash_during_private_delivery_is_uncertain_and_never_auto_resent(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    r = _ask(m, 51)
    eid = n(conn, "SELECT id FROM episodes")
    _make_ready(conn, tmp_path, eid)
    eng = _engine(conn, FakeTransport())
    eng.plan()
    d = dict(conn.execute("SELECT * FROM deliveries").fetchone())
    assert eng._claim(d)                                                                   # 'sending' committed, then the process dies
    rec = recovery.run_recovery(conn, cfg())
    assert rec["deliveries_uncertain"] == [d["id"]]
    tr = FakeTransport()
    assert _engine(conn, tr).run()["sent"] == 0 and tr.calls == []                         # NO automatic duplicate
    m.sync(r["id"])
    req = m.get(r["id"])
    assert req["state"] == "FAILED" and req["error_code"] == "TELEGRAM_ERROR" and "incertaine" in req["last_error"]
    assert m.active_request(51) is None                                                     # the user may ask again
    r2 = _ask(m, 51)                                                                        # ...and is served from the file: no new download
    assert _engine(conn, tr).run()["sent"] == 1 and n(conn, "SELECT COUNT(*) FROM episodes") == 1
    assert r2["id"] != r["id"]


# -- failures ---------------------------------------------------------------------------------

def test_blocked_bot_is_a_final_classified_failure(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    r = _ask(m, 61)
    _make_ready(conn, tmp_path, n(conn, "SELECT id FROM episodes"))
    tr = FakeTransport()
    tr.fail_next = [TelegramPublishError("Forbidden: bot was blocked by the user", "FORBIDDEN")]
    out = _engine(conn, tr).run()
    m.sync(r["id"])
    assert out["failed"] == 1 and _engine(conn, tr).run()["sent"] == 0                     # no retry loop
    assert m.get(r["id"])["state"] == "FAILED" and m.get(r["id"])["error_code"] == "TELEGRAM_ERROR"


def test_transient_error_is_retried_with_backoff_then_exhausted(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    r = _ask(m, 71)
    _make_ready(conn, tmp_path, n(conn, "SELECT id FROM episodes"))
    clock = Clock(now_utc())
    tr = FakeTransport()
    tr.fail_next = [TelegramPublishError("Bad Request: x", "BAD_REQUEST")] * 3
    eng = _engine(conn, tr, now=clock)
    assert eng.run()["failed"] == 1
    assert eng.run()["failed"] == 0                                                        # backoff: not yet
    clock.advance(31)
    assert eng.run()["failed"] == 1
    clock.advance(61)
    assert eng.run()["failed"] == 1                                                        # 3rd attempt = max
    clock.advance(3600)
    assert eng.run()["failed"] == 0 and len(tr.calls) == 3                                 # bounded: no infinite loop
    m.sync(r["id"])
    assert m.get(r["id"])["state"] == "FAILED"


def test_timeout_during_send_is_uncertain_not_retried(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    _ask(m, 81)
    _make_ready(conn, tmp_path, n(conn, "SELECT id FROM episodes"))
    tr = FakeTransport()
    tr.fail_next = [TelegramPublishError("timed out", "TIMEOUT")]
    eng = _engine(conn, tr)
    assert eng.run()["uncertain"] == 1
    assert eng.run()["sent"] == 0 and len(tr.calls) == 1


def test_altered_local_file_is_refused(conn, tmp_path):
    site = Site()
    site.set("u", 2, [1])
    m = _mgr(conn, site)
    _ask(m, 91)
    eid = n(conn, "SELECT id FROM episodes")
    f = _make_ready(conn, tmp_path, eid)
    f.write_bytes(b"short")
    tr = FakeTransport()
    assert _engine(conn, tr).run()["failed"] == 1 and tr.calls == []
    assert "STORAGE_ERROR" in n(conn, "SELECT last_error FROM deliveries")


# -- transports -------------------------------------------------------------------------------

def test_standard_api_refuses_a_file_over_50mb_the_local_api_does_not(tmp_path):
    big = tmp_path / "big.mp4"
    with big.open("wb") as fh:
        fh.truncate(51 * 1024 * 1024)                                                      # sparse, no real 51 MB written
    std = BotAPITransport("123456:" + "A" * 30)
    with pytest.raises(TelegramPublishError) as ei:
        std.send_video(1, big)
    assert ei.value.kind == "FILE_TOO_LARGE"
    local = LocalBotAPITransport("123456:" + "A" * 30, "http://127.0.0.1:8081", upload_container="c")
    assert local.max_upload_bytes is None and local.name == "local_bot_api" and std.name == "bot_api"
    assert local._base_url == "http://127.0.0.1:8081/bot" and local._local_mode is True


def test_publisher_is_transport_agnostic():
    tr = FakeTransport()
    pub = TelegramPublisher(tr, channels=["@a", "@b"])
    pub.send_video(5, Path(__file__))
    assert pub.transport_name == "fake" and tr.calls[0][0] == "send_video" and pub.channels == ["@a", "@b"]


def test_bot_token_is_scrubbed_from_errors():
    tok = "123456789" + ":" + "AAH-abcdefghijklmnopqrstuvwxyz012345"
    assert tok not in scrub(f"Failed to connect to https://x/bot{tok}/sendVideo") and "<token>" in scrub(tok)
