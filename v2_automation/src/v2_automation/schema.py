"""SQLite schema (v1) + database bootstrap.

ForeignKey + UNIQUE constraints implement the strengthened dedup:
  - UNIQUE(source, episode_key)         : primary dedup key
  - UNIQUE(source, canonical_episode_url): canonical URL dedup
  - UNIQUE(episode_id, publication_type): one first-publication per episode
  - UNIQUE(anime_key, episode_id)       : per-anime queue membership
Metadata hash dedup is enforced at application level (soft warning), because a
re-encode legitimately shares the hash.
"""
from __future__ import annotations

SCHEMA_VERSION = 3

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_capacity (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  measured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anime_key TEXT NOT NULL,
  episode_key TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'voir-anime.to',
  canonical_episode_url TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'vostfr',
  season INTEGER,
  episode_number INTEGER,
  label TEXT,
  episode_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'discovered',
  queued_at TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  retry_until_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  last_error_at TEXT,
  media_hash TEXT,
  file_size INTEGER,
  file_path TEXT,
  thumbnail_path TEXT,
  thumbnail_sha256 TEXT,
  thumbnail_size INTEGER,
  thumbnail_message_id INTEGER,
  video_sha256 TEXT,
  video_message_id INTEGER,
  published_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CONSTRAINT uq_episodes_source_episode_key UNIQUE (source, episode_key),
  CONSTRAINT uq_episodes_canonical_url UNIQUE (source, canonical_episode_url)
);

CREATE INDEX IF NOT EXISTS ix_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS ix_episodes_anime_status ON episodes(anime_key, status);
CREATE INDEX IF NOT EXISTS ix_episodes_episode_number ON episodes(anime_key, episode_number);

CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  publication_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  chat_id TEXT NOT NULL DEFAULT '',
  message_id INTEGER,
  media_kind TEXT,
  media_sha256 TEXT,
  file_size INTEGER,
  attempted_at TEXT,
  attempted_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CONSTRAINT uq_pub UNIQUE (episode_id, publication_type)
);

CREATE TABLE IF NOT EXISTS queue_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anime_key TEXT NOT NULL,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  dequeued_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CONSTRAINT uq_queue_anime_episode UNIQUE (anime_key, episode_id)
);

CREATE INDEX IF NOT EXISTS ix_queue_anime_position ON queue_items(anime_key, position);
CREATE INDEX IF NOT EXISTS ix_queue_status ON queue_items(status);
"""

# Migration v2 (2026-09-18) — closure gaps:
#   * animes/control/alerts/leases: operational admin state (enable/disable an
#     anime, global pause, alert dedup roll-up, single-instance lease heartbeat).
#   * episodes: cleanup_at (scheduled-cleanup anchor), attempt lifecycle counters
#     (first_attempt_at / last_attempt_at / next_retry_at), so retries are
#     observable and the scheduler can gate exact retry timestamps.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS animes (
  anime_key TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS control (
  ckey TEXT PRIMARY KEY,
  cvalue TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  akey TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  raised_at TEXT NOT NULL,
  last_raised_at TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CONSTRAINT uq_alerts_kind_key UNIQUE (kind, akey)
);

CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts(status);

CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  last_heartbeat TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

ALTER TABLE episodes ADD COLUMN cleanup_at TEXT;
ALTER TABLE episodes ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE episodes ADD COLUMN first_attempt_at TEXT;
ALTER TABLE episodes ADD COLUMN last_attempt_at TEXT;
ALTER TABLE episodes ADD COLUMN next_retry_at TEXT;

CREATE INDEX IF NOT EXISTS ix_episodes_next_retry ON episodes(next_retry_at);
"""

# Migration v3 (2026-09-20) — automatic mode: a watched anime knows its source page and when it was
# last checked (discovery scheduler), and an admin can request an immediate re-check.
SCHEMA_V3 = """
ALTER TABLE animes ADD COLUMN source_url TEXT;
ALTER TABLE animes ADD COLUMN language TEXT;
ALTER TABLE animes ADD COLUMN last_checked_at TEXT;
ALTER TABLE animes ADD COLUMN last_successful_check_at TEXT;
ALTER TABLE animes ADD COLUMN last_check_error TEXT;
ALTER TABLE animes ADD COLUMN force_check INTEGER NOT NULL DEFAULT 0;
"""

MIGRATIONS: dict[int, str] = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
}


def current_schema_version() -> int:
    return SCHEMA_VERSION