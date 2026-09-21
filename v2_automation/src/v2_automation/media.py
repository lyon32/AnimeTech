"""Media identity + the single entry point that both the watcher and the user requests use.

EXISTING BEHAVIOR  the watcher identifies an episode by its canonical source URL (`episodes.episode_key`).
DESIRED BEHAVIOR   an episode/version has ONE deterministic identity, whoever asks for it (watcher or any
                   number of users) -> one row, one queue item, one download.
GAP                no `media_key`; nothing lets a user request find the row the watcher created.
CHANGE             `compute_media_key` + `ensure_media` (this module) + `episodes.media_key UNIQUE` (schema v4).

Identity
  media_ref = "source|anime_id|season|episode|version"   e.g. voir-anime.to|postid:53702|S00|E1150|VOSTFR
  media_key = "m_" + sha256("media2|" + media_ref)[:32]
  * `anime_key` is `postid:N`; on the source each (title, season, language) is its own page (source_audit,
    TESTED), so it already pins the series/season/language.  `season` (from `animes.season`, 0 when unknown)
    and `version` (VF / VOSTFR / UNKNOWN) are still part of the key: VF and VOSTFR never share a media, and
    an anime whose seasons are ever merged on one page keeps distinct keys.
  * An episode without a number (film, OAV) uses its canonical URL in place of the number.

The media *state* is a named view over `episodes.status` (see `media_state`): nothing new is stored, so the
existing state machine, recovery and cleanup keep working unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import repo
from .models import Episode
from .timeutil import now_utc

logger = logging.getLogger(__name__)

KEY_PREFIX = "media2"
SOURCE_NAME = "voir-anime.to"
VERSIONS = ("VF", "VOSTFR", "UNKNOWN")


# ── identity ─────────────────────────────────────────────────────────────────────

def normalize_version(value: str | None) -> str:
    """'vf' / 'VF' / 'vostfr' / anything else -> VF | VOSTFR | UNKNOWN."""
    v = (value or "").strip().upper()
    return v if v in ("VF", "VOSTFR") else "UNKNOWN"


def media_ref(anime_key: str, season: int | None, episode_number: int | None, version: str | None,
              episode_key: str | None = None, source: str = SOURCE_NAME) -> str:
    """The canonical, READABLE identity of a media:  source | anime_id | season | episode | version
    e.g.  voir-anime.to|postid:53702|S00|E1150|VOSTFR   (S00 = the page carries no season number; the season of a
    multi-season title is its own page, hence its own anime_id).  Used in logs, history and the panel."""
    if not anime_key:
        raise ValueError("anime_key required")
    if episode_number is not None:
        ep = f"E{int(episode_number)}"
    elif episode_key:
        ep = f"U{episode_key}"
    else:
        raise ValueError("episode_number or episode_key required")
    return "|".join((source, anime_key, f"S{int(season or 0):02d}", ep, normalize_version(version)))


def compute_media_key(anime_key: str, season: int | None, episode_number: int | None, version: str | None,
                      episode_key: str | None = None, source: str = SOURCE_NAME) -> str:
    """Deterministic, case/format independent hash of `media_ref`.  `episode_key` only replaces a missing number."""
    ref = media_ref(anime_key, season, episode_number, version, episode_key, source)
    return "m_" + hashlib.sha256(f"{KEY_PREFIX}|{ref}".encode("utf-8")).hexdigest()[:32]


def anime_season(conn: sqlite3.Connection, anime_key: str) -> int | None:
    row = conn.execute("SELECT season FROM animes WHERE anime_key=?", (anime_key,)).fetchone()
    return row["season"] if row else None


def identity_for(conn: sqlite3.Connection, anime_key: str, episode_number: int | None, version: str | None,
                 episode_key: str | None, *, season: int | None = None, source: str = SOURCE_NAME) -> tuple[str, str]:
    """(media_key, media_ref) of a media as the watcher AND the users compute it (season read from `animes`).

    If another episode already owns that key (two different source URLs for the same number, e.g. a 5.5
    parsed as 5), the newcomer keeps its own identity through its URL, so the watcher never loses an
    episode it used to record."""
    season = season if season is not None else anime_season(conn, anime_key)
    key = compute_media_key(anime_key, season, episode_number, version, episode_key, source)
    owner = conn.execute("SELECT episode_key FROM episodes WHERE media_key=?", (key,)).fetchone()
    if owner is not None and episode_key and owner["episode_key"] != episode_key:
        return (compute_media_key(anime_key, season, None, version, episode_key, source),
                media_ref(anime_key, season, None, version, episode_key, source))
    return key, media_ref(anime_key, season, episode_number, version, episode_key, source)


def key_for(conn: sqlite3.Connection, anime_key: str, episode_number: int | None, version: str | None,
            episode_key: str | None, *, season: int | None = None, source: str = SOURCE_NAME) -> str:
    return identity_for(conn, anime_key, episode_number, version, episode_key, season=season, source=source)[0]


def backfill_media_keys(conn: sqlite3.Connection) -> int:
    """(Re)compute the identity of every episode that has none of the CURRENT scheme (no `media_ref`), plus any NULL key.
    Schema v4 keys (no source in the hash) are replaced by v5 keys; `request_items` follow.  Idempotent."""
    rows = conn.execute("SELECT id, source, anime_key, season, episode_number, language, episode_key FROM episodes "
                        "WHERE media_key IS NULL OR media_ref IS NULL ORDER BY id").fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    conn.executemany("UPDATE episodes SET media_key=NULL WHERE id=?", [(i,) for i in ids])   # free the unique index first
    for r in rows:
        key, ref = identity_for(conn, r["anime_key"], r["episode_number"], r["language"], r["episode_key"],
                                season=r["season"], source=r["source"] or SOURCE_NAME)
        conn.execute("UPDATE episodes SET media_key=?, media_ref=? WHERE id=?", (key, ref, r["id"]))
    conn.execute("UPDATE request_items SET media_key=(SELECT e.media_key FROM episodes e WHERE e.id=request_items.episode_id) "
                 "WHERE episode_id IS NOT NULL")
    conn.execute("UPDATE publications SET media_key=(SELECT e.media_key FROM episodes e WHERE e.id=publications.episode_id)")
    conn.commit()
    return len(rows)


# ── media state (a view over episodes.status) ────────────────────────────────────

class MediaState(str, Enum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    VALIDATING = "VALIDATING"
    READY = "READY"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    EXPIRED = "EXPIRED"


_STATE_OF_STATUS = {
    "discovered": MediaState.DISCOVERED, "identified": MediaState.DISCOVERED,
    "queued": MediaState.QUEUED, "downloading": MediaState.DOWNLOADING,
    "downloaded": MediaState.DOWNLOADED, "validating": MediaState.VALIDATING,
    "validated": MediaState.READY, "ready": MediaState.READY,
    "publishing_thumbnail": MediaState.PUBLISHING, "thumbnail_published": MediaState.PUBLISHING,
    "publishing_video": MediaState.PUBLISHING,
    "published": MediaState.PUBLISHED, "cleanup_pending": MediaState.PUBLISHED,
    "cleanup_blocked": MediaState.PUBLISHED, "cleaned": MediaState.PUBLISHED,
    "failed": MediaState.FAILED, "structure_changed": MediaState.FAILED, "blocked": MediaState.FAILED,
    "skipped_dup": MediaState.FAILED, "retry_wait": MediaState.RETRY_WAIT,
}

PUBLISHED_STATUSES = ("published", "cleanup_pending", "cleanup_blocked", "cleaned")
ACTIVE_STATUSES = ("queued", "retry_wait", "downloading", "downloaded", "validating", "validated", "ready",
                   "publishing_thumbnail", "thumbnail_published", "publishing_video")
REVIVABLE_STATUSES = ("failed", "structure_changed", "blocked")


def media_state(status: str, last_error: str | None = None) -> MediaState:
    """FAILED because the source never published it inside the retry window is EXPIRED: the media itself
    gave up waiting (distinct from a technical failure)."""
    state = _STATE_OF_STATUS.get(status, MediaState.FAILED)
    if state is MediaState.FAILED and (last_error or "").startswith("NOT_AVAILABLE_YET"):
        return MediaState.EXPIRED
    return state


# ── the single entry point ───────────────────────────────────────────────────────

@dataclass
class EnsureResult:
    episode_id: int
    media_key: str
    action: str            # created | joined | ready | revived | unavailable
    status: str


def find_media(conn: sqlite3.Connection, media_key: str) -> Episode | None:
    row = conn.execute("SELECT id FROM episodes WHERE media_key=?", (media_key,)).fetchone()
    return repo.get(conn, row["id"]) if row else None


def find_by_number(conn: sqlite3.Connection, anime_key: str, episode_number: int, version: str | None) -> Episode | None:
    """The row the watcher may have created for (anime, number, version), whatever its key."""
    for row in conn.execute("SELECT id FROM episodes WHERE anime_key=? AND episode_number=? ORDER BY id",
                            (anime_key, episode_number)).fetchall():
        ep = repo.get(conn, row["id"])
        if normalize_version(ep.language) == normalize_version(version):
            return ep
    return None


def ensure_media(conn: sqlite3.Connection, *, anime_key: str, episode_number: int | None, version: str,
                 episode_url: str, episode_key: str, label: str | None = None,
                 source: str = SOURCE_NAME, origin: str = "user", activate: bool = True,
                 publish_channel: bool | None = None) -> EnsureResult:
    """Find or create THE media for (anime, episode, version) and make sure it is being produced.

    Called by the watcher (`origin='watcher'`, via discovery) and by user requests (`origin='user'`).  It never
    starts a second job for a media that already has one:
      created  new row, now queued            joined    already in the pipeline (nothing to do)
      ready    already published (reuse)      revived   failed before, queued again
      unavailable  cannot be produced (duplicate marker)
    """
    version = normalize_version(version)
    key, ref = identity_for(conn, anime_key, episode_number, version, episode_key, source=source)
    ep = find_media(conn, key) or repo.get_by_episode_key(conn, source, episode_key) \
        or (find_by_number(conn, anime_key, episode_number, version) if episode_number is not None else None)
    created = False
    if ep is None:
        if publish_channel is None:               # watched anime keep their channel publication; others stay private
            publish_channel = origin == "watcher" or conn.execute(
                "SELECT 1 FROM animes WHERE anime_key=? AND enabled=1", (anime_key,)).fetchone() is not None
        new = Episode(anime_key=anime_key, episode_key=episode_key, source=source,
                      canonical_episode_url=episode_key + "/", language=version.lower(),
                      episode_number=episode_number, label=label, episode_url=episode_url,
                      status="discovered", media_key=key, media_ref=ref, origin=origin,
                      publish_channel=int(bool(publish_channel)))
        try:
            eid, is_new = repo.upsert_episode(conn, new)
            created = is_new
        except sqlite3.IntegrityError:            # lost a race with the watcher / another user: take theirs
            conn.rollback()
            ep = find_media(conn, key) or repo.get_by_episode_key(conn, source, episode_key)
            if ep is None:
                raise
            eid = ep.id
        ep = repo.get(conn, eid)
    if ep.media_key is None or ep.media_ref is None:
        conn.execute("UPDATE episodes SET media_key=?, media_ref=? WHERE id=?", (key, ref, ep.id))
        ep = repo.get(conn, ep.id)

    if ep.status in PUBLISHED_STATUSES or is_deliverable(ep):
        action = "ready"
    elif ep.status == "skipped_dup":
        action = "unavailable"
    elif not activate:
        action = "created" if created else "joined"
    elif ep.status == "discovered":
        if origin == "panel" and not ep.publish_channel:
            conn.execute("UPDATE episodes SET publish_channel=1 WHERE id=?", (ep.id,))      # the admin asked for the channel
            ep = repo.get(conn, ep.id)
        if origin == "user" and ep.origin == "watcher":
            # a baseline episode (known, deliberately NOT published by the watcher) asked for by a user is delivered to
            # that user only: it must not surface in the channel as a side effect of a private request
            conn.execute("UPDATE episodes SET publish_channel=0 WHERE id=?", (ep.id,))
            ep = repo.get(conn, ep.id)
        repo.transition(conn, ep.id, "identified")
        repo.transition(conn, ep.id, "queued")
        repo.enqueue(conn, ep.anime_key, ep.id)
        action = "created" if created else "joined"
    elif ep.status == "identified":
        repo.transition(conn, ep.id, "queued")
        repo.enqueue(conn, ep.anime_key, ep.id)
        action = "created" if created else "joined"
    elif ep.status in REVIVABLE_STATUSES:
        repo.transition(conn, ep.id, "queued")
        repo.set_retry_until(conn, ep.id, None, 0)
        conn.execute("UPDATE episodes SET next_retry_at=NULL WHERE id=?", (ep.id,))
        repo.enqueue(conn, ep.anime_key, ep.id)
        action = "revived"
    else:
        action = "joined"                          # already queued / running: same job, same download
    conn.commit()
    ep = repo.get(conn, ep.id)
    logger.info("[MEDIA] media=%s job=%s anime=%s episode=%s version=%s origin=%s action=%s status=%s",
                key, ep.id, anime_key, episode_number, version, origin, action, ep.status)
    return EnsureResult(ep.id, key, action, ep.status)


def discard_unstarted(conn: sqlite3.Connection, episode_id: int) -> bool:
    """Forget a PRIVATE media nobody wants any more and that never started (queued / waiting a retry, no file, nothing
    published).  The row is removed, not marked failed: a cancelled request is not an error and must not show up as one in
    the panel.  A later request simply creates it again."""
    ep = repo.get(conn, episode_id)
    if ep is None or ep.origin != "user" or ep.publish_channel or ep.status not in ("queued", "retry_wait") or ep.file_path:
        return False
    if conn.execute("SELECT 1 FROM publications WHERE episode_id=?", (episode_id,)).fetchone() or             conn.execute("SELECT 1 FROM deliveries WHERE media_id=?", (episode_id,)).fetchone():
        return False
    conn.execute("UPDATE request_items SET episode_id=NULL WHERE episode_id=?", (episode_id,))   # history keeps the item
    conn.execute("DELETE FROM queue_items WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
    conn.commit()
    logger.info("[MEDIA] media=%s job=%s discarded (request cancelled, nobody waits for it)", ep.media_key, episode_id)
    return True


def is_deliverable(ep: Episode) -> bool:
    """A user can be served from this media now: published to Telegram, or (private-only media) validated and
    waiting on disk (READY)."""
    if ep.status in PUBLISHED_STATUSES:
        return True
    return not ep.publish_channel and ep.status == "ready"


def media_summary(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any]:
    ep = repo.get(conn, episode_id)
    if ep is None:
        return {}
    return {"episode_id": ep.id, "media_key": ep.media_key, "media_ref": ep.media_ref, "state": media_state(ep.status, ep.last_error).value,
            "status": ep.status, "video_message_id": ep.video_message_id, "local_file": bool(ep.file_path),
            "published_at": ep.published_at}
