"""SQLite connection management + migrations.

- WAL journaling for crash-safe concurrent readers/writers.
- foreign_keys ON.
- A tiny migration runner keyed on schema_migrations.version.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import app_config
from .schema import MIGRATIONS
from .timeutil import now_utc

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_tx_lock = threading.RLock()


class SafeCursor(sqlite3.Cursor):
    """Fetching steps the statement too: without the lock, a fetch racing another thread's statement on the
    shared connection returned torn rows (a status read back as None, an older status) under parallel jobs."""

    def execute(self, *args, **kwargs):
        with _tx_lock:
            return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with _tx_lock:
            return super().executemany(*args, **kwargs)

    def fetchone(self):
        with _tx_lock:
            return super().fetchone()

    def fetchmany(self, *args, **kwargs):
        with _tx_lock:
            return super().fetchmany(*args, **kwargs)

    def fetchall(self):
        with _tx_lock:
            return super().fetchall()

    def __next__(self):
        with _tx_lock:
            return super().__next__()


class _Rows:
    """The result of a SELECT, already read under the lock: no live statement is left for another thread
    to race with.  Same read API as a cursor."""

    def __init__(self, rows, rowcount, description):
        self._rows, self._i = rows, 0
        self.rowcount, self.description, self.lastrowid = rowcount, description, None

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        out, self._i = self._rows[self._i:], len(self._rows)
        return out

    def fetchmany(self, size=1):
        out = self._rows[self._i:self._i + size]
        self._i += len(out)
        return out

    def __iter__(self):
        return iter(self.fetchall())


class SafeConnection(sqlite3.Connection):
    """The worker and its threads share ONE connection.  sqlite3 is not safe for concurrent use of a
    single connection from several threads (racing execute() gave "bad parameter or other API misuse",
    racing commit() gave "cannot commit - no transaction is active" and killed the worker loop).
    Every operation is serialised on one lock, and "nothing to commit" counts as success."""

    def cursor(self, factory=SafeCursor):
        return super().cursor(factory)

    def execute(self, *args, **kwargs):
        with _tx_lock:
            cur = super().execute(*args, **kwargs)
            if cur.description is not None:          # a query: read everything now, inside the lock
                return _Rows(cur.fetchall(), cur.rowcount, cur.description)
            return cur

    def executemany(self, *args, **kwargs):
        with _tx_lock:
            return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with _tx_lock:
            return super().executescript(*args, **kwargs)

    def commit(self) -> None:
        with _tx_lock:
            try:
                super().commit()
            except sqlite3.OperationalError as exc:
                if "no transaction is active" not in str(exc):
                    raise

    def rollback(self) -> None:
        with _tx_lock:
            try:
                super().rollback()
            except sqlite3.OperationalError as exc:
                if "no transaction is active" not in str(exc):
                    raise


def default_db_path() -> Path:
    return app_config.DATA_DIR / "v2.sqlite3"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        path = db_path or default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, factory=SafeConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _conn = conn
        return conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations; returns the list of newly applied versions."""
    applied = applied_versions(conn)
    newly: list[int] = []
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = applied_versions(conn)   # re-read inside the lock
            for version in sorted(MIGRATIONS):
                if version in applied:
                    continue
                try:
                    conn.executescript(MIGRATIONS[version])   # executescript commits first: another PROCESS may win the race
                except sqlite3.OperationalError as exc:
                    if ("duplicate column" in str(exc) or "already exists" in str(exc)) \
                            and version in applied_versions(conn):
                        continue                              # a concurrent start (worker + panel) already applied it
                    raise
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                             (version, now_utc()))
                newly.append(version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if newly:                                 # rows recorded before v4 get their media_key (idempotent)
        from . import media
        media.backfill_media_keys(conn)
    return newly