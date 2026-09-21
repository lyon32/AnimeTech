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

SCHEMA_VERSION = 5

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

# Migration v4 (2026-09-21) — CORE MEDIA ENGINE shared by the watcher and the user requests.
#   * episodes.media_key : deterministic media identity (see media.py); UNIQUE, so the watcher and any number of
#     users converge on ONE row = ONE download.  Existing rows are back-filled by media.backfill_media_keys().
#   * animes.season      : season of the anime page (each season/language is its own page on the source).
#   * users / requests / request_items / deliveries : the user side.  A request is a different thing from a
#     media: it waits for it, and it is delivered to the user privately (one delivery per request item).
#   * episodes.origin / publish_channel : who created the media, and whether it goes to the channel (watched anime:
#     yes, as before) or only to the requesting users (anime outside the watched list).
#   * ux_requests_one_active : at most ONE active request per user, enforced by the database itself.
SCHEMA_V4 = """
ALTER TABLE animes ADD COLUMN season INTEGER;
ALTER TABLE episodes ADD COLUMN media_key TEXT;
ALTER TABLE episodes ADD COLUMN origin TEXT NOT NULL DEFAULT 'watcher';
ALTER TABLE episodes ADD COLUMN publish_channel INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS ux_episodes_media_key ON episodes(media_key) WHERE media_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS users (
  telegram_id INTEGER PRIMARY KEY,
  username TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  access_status TEXT NOT NULL DEFAULT 'unknown',
  access_checked_at TEXT,
  blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(telegram_id),
  kind TEXT NOT NULL,
  anime_key TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  season INTEGER,
  episode_number INTEGER,
  version TEXT NOT NULL,
  source_url TEXT,
  state TEXT NOT NULL DEFAULT 'PENDING',
  notified_state TEXT,
  error_code TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_requests_user ON requests(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_requests_state ON requests(state);
CREATE UNIQUE INDEX IF NOT EXISTS ux_requests_one_active ON requests(user_id)
  WHERE state NOT IN ('COMPLETED', 'CANCELLED', 'EXPIRED', 'FAILED');

CREATE TABLE IF NOT EXISTS request_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  media_key TEXT NOT NULL,
  episode_id INTEGER REFERENCES episodes(id),
  episode_number INTEGER,
  state TEXT NOT NULL DEFAULT 'PENDING',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CONSTRAINT uq_item_request_media UNIQUE (request_id, media_key)
);
CREATE INDEX IF NOT EXISTS ix_items_episode ON request_items(episode_id);

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER REFERENCES requests(id),                 -- NULL: sent by an administrator ("Send to user")
  request_item_id INTEGER REFERENCES request_items(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL,
  media_id INTEGER NOT NULL REFERENCES episodes(id),
  status TEXT NOT NULL DEFAULT 'pending',
  telegram_message_id INTEGER,
  telegram_file_id TEXT,
  method TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  CONSTRAINT uq_delivery_item UNIQUE (request_item_id)
);
CREATE TABLE IF NOT EXISTS conversations (
  user_id INTEGER PRIMARY KEY,
  step TEXT NOT NULL DEFAULT 'idle',
  data TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  admin_user_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  result TEXT NOT NULL,
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deliveries_admin_open ON deliveries(user_id, media_id)
  WHERE request_item_id IS NULL AND status IN ('pending', 'sending');
CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries(status);
CREATE INDEX IF NOT EXISTS ix_deliveries_media ON deliveries(media_id);
"""

# Migration v5 (2026-09-21) — canonical media identity and atomic claim.
#   * episodes.media_ref : the readable identity  source|anime_id|season|episode|version ; media_key becomes its hash and now
#     includes the SOURCE (existing keys are recomputed by media.backfill_media_keys, request_items follow).
#   * publications.media_key : filled by a trigger, so every publication row names its media (search -> ... -> history).
#   * episodes.claimed_by / claimed_at : the atomic claim (repo.claim_media) — one owner processes a media at a time,
#     whatever the status says; cleared at boot by recovery.
SCHEMA_V5 = """
ALTER TABLE episodes ADD COLUMN media_ref TEXT;
ALTER TABLE episodes ADD COLUMN claimed_by TEXT;
ALTER TABLE episodes ADD COLUMN claimed_at TEXT;
ALTER TABLE publications ADD COLUMN media_key TEXT;
CREATE TRIGGER IF NOT EXISTS trg_publications_media_key AFTER INSERT ON publications
BEGIN
  UPDATE publications SET media_key=(SELECT media_key FROM episodes WHERE id=NEW.episode_id) WHERE id=NEW.id;
END;
"""

MIGRATIONS: dict[int, str] = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
}


def current_schema_version() -> int:
    return SCHEMA_VERSION