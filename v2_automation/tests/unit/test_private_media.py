"""Private-only media (a user asked for an anime outside the watched list): the ONE download engine produces the
file, validates it, then stops at READY — nothing is posted to the channel, the queue moves on."""
from test_downloader import FakeTelegram, _build_deps, _cfg, conn  # noqa: F401

from v2_automation import media, repo
from v2_automation.downloader import DownloadManager
from v2_automation.models import Episode
from v2_automation.states import State, can_transition


def _mk(conn, number, *, publish_channel):
    key = f"https://www.example.com/anime/a1/e{number:02d}-vostfr"
    ep = Episode(anime_key="a1", episode_key=key, canonical_episode_url=key + "/", language="vostfr",
                 episode_number=number, episode_url=key, publish_channel=publish_channel,
                 origin="watcher" if publish_channel else "user")
    eid, _ = repo.upsert_episode(conn, ep)
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    repo.enqueue(conn, "a1", eid)
    conn.commit()
    return eid


def test_private_media_stops_at_ready_and_never_touches_the_channel(conn, tmp_path):
    tg = FakeTelegram()
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg))
    eid = _mk(conn, 1, publish_channel=0)
    assert mgr.process_episode(eid) == "ready"
    ep = repo.get(conn, eid)
    assert tg.log == []                                                   # nothing sent to any channel
    assert ep.file_path and ep.file_size and ep.video_sha256              # the validated file is recorded
    assert media.media_state(ep.status) is media.MediaState.READY and media.is_deliverable(ep)
    assert conn.execute("SELECT COUNT(*) FROM queue_items WHERE episode_id=?", (eid,)).fetchone()[0] == 0


def test_channel_media_still_publishes_exactly_as_before(conn, tmp_path):
    tg = FakeTelegram()
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=tg))
    eid = _mk(conn, 1, publish_channel=1)
    assert mgr.process_episode(eid) in ("published", "cleanup_pending")
    assert [k for k, _ in tg.log] == ["photo", "video"]                    # thumbnail then video, as V1 does


def test_ready_releases_the_anime_fifo_so_the_next_episode_starts(conn, tmp_path):
    mgr = DownloadManager(conn, _cfg(tmp_path), deps=_build_deps(tmp_path, telegram=FakeTelegram()))
    e1 = _mk(conn, 1, publish_channel=0)
    e2 = _mk(conn, 2, publish_channel=0)
    assert repo.next_heads(conn, 5) == [e1]                                # E02 waits behind E01
    mgr.process_episode(e1)
    assert repo.next_heads(conn, 5) == [e2]                                # READY freed the head


def test_ready_state_machine():
    assert can_transition(State.VALIDATED, State.READY)
    assert can_transition(State.READY, State.PUBLISHED) and can_transition(State.READY, State.QUEUED)
    assert not can_transition(State.DOWNLOADING, State.READY)
