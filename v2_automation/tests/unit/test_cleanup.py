"""Cleanup (Phase 10) tests — retention window, provenance, no partial cleanup."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from v2_automation import cleanup, db, evidence, repo
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.models import Episode


def _cfg(retention_days: float = 14.0) -> AppConfig:
    return AppConfig(source={}, queues={}, downloads={}, telegram={},
                     publication={"cleanup_after_days": retention_days}, limits={},
                     monitoring={}, logging={}, bot_token="", channel_id="",
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


def _mk(conn, tmp_path, status="published", *, age_days=None, with_msg=True, anime="anime-a") -> tuple[Path, int]:
    if with_msg:
        file_path = tmp_path / "files" / f"{anime}-{getattr(_mk, 'counter', 0)}.mp4"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"x" * 64)
        _mk.counter = getattr(_mk, "counter", 0) + 1
        video_message_id = 100 + _mk.counter
    else:
        file_path = None
        video_message_id = None
    published_at = (datetime.now(timezone.utc) - timedelta(days=age_days or 0)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ") if age_days is not None else None
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{age_days}-{len(list(tmp_path.glob('*')))}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e1",
                 episode_number=1, status=status, file_path=str(file_path) if file_path else None,
                 file_size=file_path.stat().st_size if file_path else None,
                 video_message_id=video_message_id, published_at=published_at)
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    return file_path, eid


def test_cleans_expired_published(conn, tmp_path):
    fpath, eid = _mk(conn, tmp_path, age_days=20)
    res = cleanup.run_cleanup(conn, _cfg(retention_days=14))
    assert res["cleaned"] == 1
    assert not fpath.exists()
    assert repo.get(conn, eid).status == "cleaned"
    payload = Path(res["evidence_path"]).read_text(encoding="utf-8")
    assert f'"episode_id": {eid}' in payload and fpath.name in payload


def test_skips_recently_published(conn, tmp_path):
    fpath, eid = _mk(conn, tmp_path, age_days=1)
    res = cleanup.run_cleanup(conn, _cfg(retention_days=14))
    assert res["cleaned"] == 0 and fpath.exists()
    assert repo.get(conn, eid).status == "published"


def test_never_cleans_unpublished_partial(conn, tmp_path):
    fpath, eid = _mk(conn, tmp_path, age_days=20, with_msg=False)
    res = cleanup.run_cleanup(conn, _cfg(retention_days=14))
    assert res["cleaned"] == 0
    assert repo.get(conn, eid).status != "cleaned"


def test_cleans_cleanup_pending_state(conn, tmp_path):
    fpath, eid = _mk(conn, tmp_path, status="cleanup_pending", age_days=30)
    res = cleanup.run_cleanup(conn, _cfg(retention_days=14))
    assert res["cleaned"] == 1 and not fpath.exists()
    assert repo.get(conn, eid).status == "cleaned"


def test_missing_file_is_cleanable(conn, tmp_path):
    fake = tmp_path / "ghost" / "gone.mp4"
    ep = Episode(anime_key="anime-a", episode_key="ghost-e1",
                 canonical_episode_url="https://voir-anime.to/anime/anime-a/e1",
                 episode_number=1, status="published", file_path=str(fake),
                 video_message_id=99,
                 published_at=(datetime.now(timezone.utc) - timedelta(days=20)
                               ).strftime("%Y-%m-%dT%H:%M:%SZ"))
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    res = cleanup.run_cleanup(conn, _cfg(14))
    assert res["cleaned"] == 1 and repo.get(conn, eid).status == "cleaned"


def test_published_without_anchor_goes_cleanup_blocked(conn, tmp_path):
    """Closure gap fix: NO fallback to updated_at — a published episode without
    published_at cannot have a provable age and is surfaced, not silently cleaned."""
    fpath, eid = _mk(conn, tmp_path, age_days=None, with_msg=True)   # published_at=None
    assert fpath.exists() and fpath.stat().st_size > 0
    res = cleanup.run_cleanup(conn, _cfg(retention_days=14))
    assert res["cleaned"] == 0
    assert res["blocked_count"] == 1
    assert repo.get(conn, eid).status == "cleanup_blocked"
    assert fpath.exists()                       # file kept — no destructive guess
    assert res["evidence_path"] is not None
    payload = Path(res["evidence_path"]).read_text(encoding="utf-8")
    assert '"blocked_count": 1' in payload and "published_at" in payload


def test_cleanup_blocked_episode_resolvable_manually(conn, tmp_path):
    """Closure gap fix: once the operator backfills published_at, the next
    cleanup pass re-evaluates and cleans (CLEANUP_BLOCKED stays under the
    single deletion owner — the cleanup module)."""
    file_path = tmp_path / "files" / "resolved.mp4"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * 64)
    ep = Episode(anime_key="anime-a", episode_key="resolved-1",
                 canonical_episode_url="https://voir-anime.to/anime/anime-a/e1",
                 episode_number=1, status="cleanup_blocked",
                 file_path=str(file_path), video_message_id=77,
                 published_at=(datetime.now(timezone.utc) - timedelta(days=30)
                               ).strftime("%Y-%m-%dT%H:%M:%SZ"))
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    res = cleanup.run_cleanup(conn, _cfg(retention_days=14))
    assert res["cleaned"] == 1
    ep = repo.get(conn, eid)
    assert ep.status == "cleaned"
    assert ep.cleanup_at is not None

# ── J+14 with REAL files: only the local file goes, never the messages ────────────

def test_real_files_are_deleted_only_after_14_days_from_published_at(tmp_path):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from v2_automation import cleanup, db, repo
    from v2_automation.app_config import AppConfig, BotCapacity
    from v2_automation.models import Episode
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    cfg = AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={"cleanup_after_days": 14},
                    limits={}, monitoring={}, logging={}, bot_token="", channel_id="", admin_telegram_ids=[],
                    bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))

    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def make(n, published_days_ago, updated_days_ago=0):
        f = tmp_path / f"e{n}.mp4"
        f.write_bytes(b"\x00" * 2048)
        t = tmp_path / f"e{n}.jpg"
        t.write_bytes(b"\xff\xd8" + b"\x00" * 64)
        ep = Episode(anime_key="a", episode_key=f"a-{n}", canonical_episode_url=f"https://x/a/{n}", episode_number=n,
                     episode_url=f"https://x/a/{n}", status="cleanup_pending", file_path=str(f), thumbnail_path=str(t),
                     video_message_id=1000 + n, thumbnail_message_id=900 + n,
                     published_at=None if published_days_ago is None else ago(published_days_ago))
        eid, _ = repo.upsert_episode(c, ep)
        c.execute("UPDATE episodes SET updated_at=?, status='cleanup_pending' WHERE id=?", (ago(updated_days_ago), eid))
        c.commit()
        return eid, f, t

    old, old_f, old_t = make(1, 15)                     # published 15 days ago  -> cleaned
    new, new_f, new_t = make(2, 13)                     # 13 days ago            -> kept
    stale, st_f, st_t = make(3, 1, updated_days_ago=30)  # updated_at is old but published yesterday -> kept
    nopub, np_f, np_t = make(4, None)                   # no published_at        -> CLEANUP_BLOCKED
    res = cleanup.run_cleanup(c, cfg)
    assert res["cleaned"] == 1 and res["blocked_count"] == 1
    assert not old_f.exists() and not old_t.exists()                       # local files gone
    assert new_f.exists() and st_f.exists() and np_f.exists()               # others untouched
    kept = repo.get(c, old)
    assert kept.status == "cleaned" and kept.video_message_id == 1001 and kept.thumbnail_message_id == 901
    assert kept.published_at is not None and kept.anime_key == "a" and kept.episode_number == 1   # history kept
    assert repo.get(c, stale).status == "cleanup_pending"                   # never judged on updated_at
    assert repo.get(c, nopub).status == "cleanup_blocked"
    assert c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 4      # no episode row is ever deleted


def test_recovery_schedules_cleanup_at_from_published_at(tmp_path):
    import sqlite3
    from v2_automation import db, recovery, repo
    from v2_automation.app_config import AppConfig, BotCapacity
    from v2_automation.models import Episode
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.migrate(c)
    cfg = AppConfig(source={}, queues={}, downloads={}, telegram={}, publication={"cleanup_after_days": 14},
                    limits={}, monitoring={}, logging={}, bot_token="", channel_id="", admin_telegram_ids=[],
                    bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))
    ep = Episode(anime_key="a", episode_key="a-1", canonical_episode_url="https://x/a/1", episode_number=1,
                 episode_url="https://x/a/1", status="cleanup_pending", video_message_id=5,
                 published_at="2026-09-01T00:00:00Z")
    eid, _ = repo.upsert_episode(c, ep)
    c.commit()
    res = recovery.run_recovery(c, cfg)
    assert res["cleanup_scheduled"] == 1
    assert repo.get(c, eid).cleanup_at == "2026-09-15T00:00:00Z"            # published_at + 14 days
