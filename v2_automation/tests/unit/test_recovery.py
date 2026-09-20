"""Recovery (Phase 11) tests — mid-flight reset, expired retry promotion, FIFO repair."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from v2_automation import db, recovery, repo
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode


def _cfg() -> AppConfig:
    return AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={},
                     limits={}, monitoring={}, logging={}, bot_token="", channel_id="",
                     admin_telegram_ids=[],
                     bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _insert(conn, status, anime="anime-a", epnum=1, retry_until=None):
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{epnum}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e{epnum}",
                 episode_number=epnum, status=status, retry_until_at=retry_until)
    eid, _ = repo.upsert_episode(conn, ep)
    if status in ("queued", "retry_wait"):
        repo.enqueue(conn, anime, eid)
    conn.commit()
    return eid


def _past_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_safe_midflight_states_reset_to_queued(conn):
    ids = {s: _insert(conn, s, epnum=i) for i, s in enumerate(recovery.SAFE_MIDFLIGHT, start=1)}
    res = recovery.run_recovery(conn, _cfg())
    assert {r["was"] for r in res["reset_to_queued"]} == set(recovery.SAFE_MIDFLIGHT)
    for eid in ids.values():
        assert repo.get(conn, eid).status == "queued"
    assert repo.queue_depth(conn) == len(recovery.SAFE_MIDFLIGHT)


def test_risky_publishing_states_fail_without_requeue(conn):
    """Closure gap fix: a crash during thumbnail/video publish may already have
    sent a Telegram message — auto-requeuing risks DOUBLE publication."""
    ids = {s: _insert(conn, s, epnum=i + 50) for i, s in enumerate(recovery.RISKY_MIDFLIGHT)}
    res = recovery.run_recovery(conn, _cfg())
    assert {r["was"] for r in res["publishing_to_failed"]} == set(recovery.RISKY_MIDFLIGHT)
    for eid in ids.values():
        ep = repo.get(conn, eid)
        assert ep.status == "failed"
        assert "double publication" in (ep.last_error or "")
        assert ep.retry_until_at is None      # nex jamais re-planifie automatiquement
    assert repo.queue_depth(conn) == 0
    assert res["reset_to_queued"] == []


def test_cleaned_and_published_untouched(conn):
    _insert(conn, "cleaned", epnum=1)
    _insert(conn, "published", epnum=2)
    res = recovery.run_recovery(conn, _cfg())
    assert res["reset_to_queued"] == []
    assert repo.get(conn, 1).status == "cleaned" and repo.get(conn, 2).status == "published"


def test_expired_retry_promoted_to_failed(conn):
    eid = _insert(conn, "retry_wait", epnum=1, retry_until=_past_iso(25))
    _insert(conn, "retry_wait", epnum=2, retry_until=_past_iso(-1))   # future window -> kept
    res = recovery.run_recovery(conn, _cfg())
    assert [r["episode_id"] for r in res["expired_to_failed"]] == [eid]
    assert repo.get(conn, eid).status == "failed"
    assert repo.get(conn, 2).status == "retry_wait"


def test_dangling_queue_items_repaired(conn):
    eid = _insert(conn, "downloading", epnum=1)   # enqueued? no: only queued/retry_wait enqueue
    repo.enqueue(conn, "anime-a", eid)
    conn.commit()
    res = recovery.run_recovery(conn, _cfg())
    assert res["reset_to_queued"] and res["dangling_queue_repaired"] == 0
    assert repo.queue_depth(conn) == 1


def test_evidence_written_on_repair(conn):
    _insert(conn, "downloading", epnum=1)
    res = recovery.run_recovery(conn, _cfg())
    assert res["evidence_path"] and Path(res["evidence_path"]).exists()
    assert "reset_to_queued" in Path(res["evidence_path"]).read_text(encoding="utf-8")

def test_killed_process_claim_is_released_so_the_episode_can_be_claimed_again(conn):
    """Real kill test finding: the item stayed 'processing' after the reset, so nobody could claim it."""
    from v2_automation.queues import QueueManager
    eid = _insert(conn, "queued", epnum=1)
    qm = QueueManager(conn)
    assert qm.dequeue_episode(eid)                      # a process claims it ...
    repo.transition(conn, eid, "downloading")           # ... starts, then is killed mid-download
    conn.commit()
    recovery.run_recovery(conn, _cfg())                 # restart
    assert repo.get(conn, eid).status == "queued"
    assert conn.execute("SELECT status FROM queue_items WHERE episode_id=?", (eid,)).fetchone()[0] == "queued"
    assert eid in repo.next_heads(conn, 5)              # the worker would pick it up
    assert qm.dequeue_episode(eid)                      # claimable again


def test_orphan_processing_claim_of_a_waiting_episode_is_released(conn):
    """State left by an earlier restart: episode already 'queued' again but the item still 'processing'."""
    from v2_automation.queues import QueueManager
    eid = _insert(conn, "queued", epnum=1)
    conn.execute("UPDATE queue_items SET status='processing' WHERE episode_id=?", (eid,))
    conn.commit()
    res = recovery.run_recovery(conn, _cfg())
    assert res["orphan_claims_released"] == 1
    assert QueueManager(conn).dequeue_episode(eid)


# ── reconcile: the server published the video after the crash ─────────────────

class _FakeTg:
    def __init__(self, found):
        self.found, self.asked, self.cleaned, self.closed = found, None, 0, 0

    def wait_for_message(self, ids, caption, *, timeout_s, poll_s):
        self.asked = (list(ids), caption)
        return self.found

    def cleanup_remote(self, path):
        self.cleaned += 1

    def close(self):
        self.closed += 1


def _uncertain_episode(conn, thumb_id=41, error="arrêt pendant publication — re-publication MANUELLE requise"):
    eid = _insert(conn, "queued", epnum=7)
    conn.execute("UPDATE episodes SET status='failed', last_error=?, file_path='x.mp4', file_size=10 WHERE id=?",
                 (error, eid))
    repo.commit_publication(conn, eid, "thumbnail", "-100", thumb_id, "photo", None, 5)
    conn.commit()
    return eid


def test_reconcile_records_the_video_the_server_published_without_sending(conn, monkeypatch):
    monkeypatch.setattr(recovery, "_video_caption", lambda c, ep: "CAPTION")
    eid = _uncertain_episode(conn)
    tg = _FakeTg(found=42)
    done = recovery.reconcile_uncertain(_cfg_with_token(), telegram_factory=lambda: tg) if False else \
        recovery.reconcile_uncertain(conn, _cfg_with_token(), telegram_factory=lambda: tg)
    assert done == [{"episode_id": eid, "video_message_id": 42, "thumbnail_message_id": 41}]
    ep = repo.get(conn, eid)
    assert ep.status == "cleanup_pending" and ep.video_message_id == 42 and ep.last_error is None
    assert tg.asked == ([42, 43, 44], "CAPTION") and tg.cleaned == 1 and tg.closed == 1
    assert conn.execute("SELECT message_id FROM publications WHERE episode_id=? AND publication_type='first_publication'",
                        (eid,)).fetchone()[0] == 42
    assert conn.execute("SELECT COUNT(*) FROM queue_items WHERE episode_id=?", (eid,)).fetchone()[0] == 0


def test_reconcile_leaves_the_episode_failed_when_the_video_is_not_there(conn, monkeypatch):
    monkeypatch.setattr(recovery, "_video_caption", lambda c, ep: "CAPTION")
    eid = _uncertain_episode(conn)
    assert recovery.reconcile_uncertain(conn, _cfg_with_token(), telegram_factory=lambda: _FakeTg(None)) == []
    assert repo.get(conn, eid).status == "failed"
    assert conn.execute("SELECT COUNT(*) FROM publications WHERE episode_id=? AND publication_type='first_publication'",
                        (eid,)).fetchone()[0] == 0


def test_reconcile_ignores_other_failures_and_missing_caption(conn, monkeypatch):
    monkeypatch.setattr(recovery, "_video_caption", lambda c, ep: None)       # no file / title: cannot search
    _uncertain_episode(conn)
    tg = _FakeTg(found=99)
    assert recovery.reconcile_uncertain(conn, _cfg_with_token(), telegram_factory=lambda: tg) == []
    assert tg.asked is None
    monkeypatch.setattr(recovery, "_video_caption", lambda c, ep: "C")
    other = _insert(conn, "queued", epnum=8)
    conn.execute("UPDATE episodes SET status='failed', last_error='DOWNLOAD_FAILED: x' WHERE id=?", (other,))
    repo.commit_publication(conn, other, "thumbnail", "-100", 60, "photo", None, 5)
    conn.commit()
    tg2 = _FakeTg(found=99)
    done = recovery.reconcile_uncertain(conn, _cfg_with_token(), telegram_factory=lambda: tg2)
    assert [d["episode_id"] for d in done] != [other]                          # a DOWNLOAD_FAILED row is not touched
    assert repo.get(conn, other).status == "failed"


def test_run_recovery_without_telegram_credentials_skips_reconcile(conn):
    _uncertain_episode(conn)
    assert recovery.run_recovery(conn, _cfg())["reconciled"] == []


def test_a_telegram_error_never_breaks_the_recovery_sweep(conn, monkeypatch):
    monkeypatch.setattr(recovery, "_video_caption", lambda c, ep: "C")
    _uncertain_episode(conn)

    def boom():
        raise RuntimeError("telegram down")
    assert recovery.reconcile_uncertain(conn, _cfg_with_token(), telegram_factory=boom) == []


def _cfg_with_token() -> AppConfig:
    c = _cfg()
    return AppConfig(**{**c.__dict__, "bot_token": "t", "channel_id": "-100"})
