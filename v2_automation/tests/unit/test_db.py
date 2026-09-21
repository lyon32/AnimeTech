"""DB bootstrap + migrations + constraints + queue FIFO primitives."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from v2_automation import db, repo, schema
from v2_automation.models import Episode


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite3.connect(tmp_path / "t.sqlite3")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


def test_migrations_run_and_are_idempotent(conn):
    applied = db.migrate(conn)
    assert schema.SCHEMA_VERSION in applied
    assert db.migrate(conn) == []                 # idempotent
    assert db.applied_versions(conn) == set(schema.MIGRATIONS)


def test_migrations_v1_to_v2_upgrade_path():
    """A v1 database upgrades in place: new tables + v2 columns with sane defaults."""
    import tempfile
    td = tempfile.mkdtemp(prefix="v2mig_")
    db_path = Path(td) / "old.sqlite3"
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(schema.SCHEMA_V1)          # apply ONLY v1, like a pre-closure DB
        c.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')")
        c.execute("INSERT INTO episodes(anime_key, episode_key, source, canonical_episode_url, "
                  "language, episode_url) VALUES ('legacy','legacy-e1','voir-anime.to',"
                  "'https://voir-anime.to/anime/legacy/e1','vostfr','https://voir-anime.to/anime/legacy/e1')")
        c.commit()
        eid = c.execute("SELECT id FROM episodes").fetchone()["id"]
        applied = db.migrate(c)                    # v2, v3 (automatic mode), v4 (core media engine), v5 (canonical identity) on a v1 db
        assert applied == [2, 3, 4, 5]
        acols = {r["name"] for r in c.execute("PRAGMA table_info(animes)").fetchall()}
        assert {"source_url", "language", "last_checked_at", "last_successful_check_at",
                "last_check_error", "force_check"} <= acols
        cols = {r["name"] for r in c.execute("PRAGMA table_info(episodes)").fetchall()}
        assert {"cleanup_at", "attempt_count", "first_attempt_at",
                "last_attempt_at", "next_retry_at"} <= cols
        tables = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"animes", "control", "alerts", "leases"} <= tables
        ep = repo.get(c, eid)                      # legacy row readable after upgrade
        assert ep is not None and ep.attempt_count == 0 and ep.next_retry_at is None
        assert ep.media_key and ep.media_key.startswith("m_") and ep.origin == "watcher"   # back-filled
        assert ep.media_ref == "voir-anime.to|legacy|S00|E0|VOSTFR" or ep.media_ref.startswith("voir-anime.to|legacy|S00|")
    finally:
        c.close()
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_migration_creates_all_tables(conn):
    db.migrate(conn)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"schema_migrations", "bot_capacity", "episodes",
            "publications", "queue_items", "animes", "control",
            "alerts", "leases"} <= tables


def test_attempt_bookkeeping(conn):
    from v2_automation.timeutil import now_utc
    db.migrate(conn)
    eid, _ = repo.upsert_episode(conn, _ep("e1"))
    assert repo.get(conn, eid).attempt_count == 0
    repo.bump_attempt(conn, eid)
    repo.bump_attempt(conn, eid)
    ep = repo.get(conn, eid)
    assert ep.attempt_count == 2
    assert ep.first_attempt_at is not None and ep.last_attempt_at is not None
    assert ep.first_attempt_at == ep.last_attempt_at
    repo.bump_attempt(conn, eid)
    assert repo.get(conn, eid).attempt_count == 3
    assert repo.get(conn, eid).first_attempt_at is not None


def test_set_retry_until_stores_next_retry_at(conn):
    db.migrate(conn)
    eid, _ = repo.upsert_episode(conn, _ep("e1"))
    repo.set_retry_until(conn, eid, "2026-09-19T00:00:00Z", retry_count=1,
                         error="boom", next_retry_at="2026-09-19T00:00:00Z")
    ep = repo.get(conn, eid)
    assert ep.retry_until_at == "2026-09-19T00:00:00Z"
    assert ep.next_retry_at == "2026-09-19T00:00:00Z"
    assert ep.retry_count == 1 and ep.last_error == "boom"


def test_next_heads_gates_on_next_retry_at(conn):
    from v2_automation.timeutil import now_utc
    db.migrate(conn)
    eid_a, _ = repo.upsert_episode(conn, _ep("a", anime_key="one"))
    repo.transition(conn, eid_a, "identified")
    repo.transition(conn, eid_a, "queued")
    repo.enqueue(conn, "one", eid_a)
    # retry_wait head with a future retry timestamp is NOT eligible
    eid_b, _ = repo.upsert_episode(conn, _ep("b", anime_key="two"))
    repo.transition(conn, eid_b, "identified")
    repo.transition(conn, eid_b, "queued")
    repo.enqueue(conn, "two", eid_b)
    conn.execute("UPDATE episodes SET status='retry_wait', next_retry_at=? WHERE id=?",
                 ("2999-01-01T00:00:00Z", eid_b))
    conn.commit()
    heads = repo.next_heads(conn, 10)
    assert heads == [eid_a]                      # b excluded while in retry hold
    conn.execute("UPDATE episodes SET next_retry_at=? WHERE id=?",
                 (now_utc(), eid_b))
    conn.commit()
    assert eid_b in repo.next_heads(conn, 10)


def _ep(episode_key: str, anime_key: str = "a1", num: int | None = None) -> Episode:
    return Episode(anime_key=anime_key, episode_key=episode_key,
                   canonical_episode_url=f"https://voir-anime.to/anime/{episode_key}/",
                   language="vostfr", episode_number=num,
                   episode_url=f"https://voir-anime.to/anime/{episode_key}/")


def test_upsert_dedup_episode_key(conn):
    db.migrate(conn)
    eid1, new1 = repo.upsert_episode(conn, _ep("e1"))
    eid2, new2 = repo.upsert_episode(conn, _ep("e1"))
    assert new1 and not new2 and eid1 == eid2
    assert conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"] == 1


def test_unique_canonical_url(conn):
    db.migrate(conn)
    repo.upsert_episode(conn, _ep("e1"))
    dup = _ep("different-key")
    dup.canonical_episode_url = "https://voir-anime.to/anime/e1/"
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_episode(conn, dup)


def test_transition_rules_enforced(conn):
    db.migrate(conn)
    eid, _ = repo.upsert_episode(conn, _ep("e1"))
    assert repo.transition(conn, eid, "identified")
    assert repo.transition(conn, eid, "queued")
    assert repo.transition(conn, eid, "downloading")        # chemin normal
    assert repo.transition(conn, eid, "downloaded")
    with pytest.raises(ValueError):
        repo.transition(conn, eid, "queued")                # regression interdit ce chemin
    ep = repo.get(conn, eid)
    assert ep.status == "downloaded"


def test_queue_fifo_positions_by_anime(conn):
    db.migrate(conn)
    # enqueue in anime "one": two episodes in order (status passes queued first, as in real flow)
    e1a, _ = repo.upsert_episode(conn, _ep("x01", anime_key="one"))
    e1b, _ = repo.upsert_episode(conn, _ep("x02", anime_key="one"))
    repo.transition(conn, e1a, "identified")
    repo.transition(conn, e1a, "queued")
    repo.transition(conn, e1b, "identified")
    repo.transition(conn, e1b, "queued")
    assert repo.enqueue(conn, "one", e1a) == 1
    assert repo.enqueue(conn, "one", e1b) == 2
    assert repo.enqueue(conn, "one", e1b) == 2     # no-op si deja dans la file
    # heads: one per anime
    heads = repo.next_heads(conn, limit=10)
    assert e1a in heads and e1b not in heads       # FIFO: a avant b


def test_publication_unique(conn):
    db.migrate(conn)
    eid, _ = repo.upsert_episode(conn, _ep("e1"))
    repo.commit_publication(conn, eid, "first_publication", "-100x", 11, "video", "aa", 10)
    repo.commit_publication(conn, eid, "first_publication", "-100x", 22, "video", "aa", 10)  # update, pas insert
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM publications WHERE episode_id=?", (eid,)).fetchone()["n"] == 1
    repo.commit_publication(conn, eid, "excerpt", "-100x", 33, "thumbnail", "bb", 5)
    assert conn.execute("SELECT COUNT(*) AS n FROM publications WHERE episode_id=?",
                        (eid,)).fetchone()["n"] == 2


def test_diagnostics_empty_db(conn):
    db.migrate(conn)
    assert repo.queue_depth(conn) == 0
    assert repo.queue_by_anime(conn) == []
    assert repo.errors_recent(conn) == []
    assert repo.history(conn) == []

def test_shared_connection_survives_racing_commits(tmp_path):
    """Regression (found by the automatic-worker test): concurrent commit() on the shared connection
    raised "cannot commit - no transaction is active" and killed the worker loop."""
    import threading
    c = sqlite3.connect(str(tmp_path / "race.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.execute("CREATE TABLE t (n INTEGER)")
    errors = []

    def hammer():
        try:
            for i in range(300):
                c.execute("INSERT INTO t VALUES (?)", (i,))
                c.execute("SELECT COUNT(*) FROM t").fetchone()
                c.commit()
        except Exception as exc:              # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=hammer) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1200
    c.close()
