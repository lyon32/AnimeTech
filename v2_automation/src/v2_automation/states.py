"""Epilogue state machine for V2 automation.

Per the master prompt, download+publication progress through a single linear
chain, plus terminal/failure side states:

  DISCOVERED -> IDENTIFIED -> QUEUED -> DOWNLOADING -> DOWNLOADED
    -> VALIDATING -> VALIDATED [-> READY, private-only media] -> PUBLISHING_THUMBNAIL -> THUMBNAIL_PUBLISHED
    -> PUBLISHING_VIDEO -> PUBLISHED -> CLEANUP_PENDING -> CLEANED

Side states (from any step): FAILED, RETRY_WAIT, STRUCTURE_CHANGED, SKIPPED_DUP.
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    DISCOVERED = "discovered"
    IDENTIFIED = "identified"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VALIDATING = "validating"
    VALIDATED = "validated"
    READY = "ready"                    # validated file kept for PRIVATE delivery only (no channel publication)
    PUBLISHING_THUMBNAIL = "publishing_thumbnail"
    THUMBNAIL_PUBLISHED = "thumbnail_published"
    PUBLISHING_VIDEO = "publishing_video"
    PUBLISHED = "published"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANUP_BLOCKED = "cleanup_blocked"
    CLEANED = "cleaned"

    FAILED = "failed"
    RETRY_WAIT = "retry_wait"
    STRUCTURE_CHANGED = "structure_changed"
    SKIPPED_DUP = "skipped_dup"
    BLOCKED = "blocked"


# Mainline order index used for "resume-after-crash" comparison.
_MAINLINE = [s for s in State if s.value not in {
    "failed", "retry_wait", "structure_changed", "skipped_dup", "blocked"}]

_ALLOWED: dict[State, set[State]] = {
    State.DISCOVERED: {State.IDENTIFIED, State.FAILED, State.BLOCKED},
    State.IDENTIFIED: {State.QUEUED, State.SKIPPED_DUP, State.FAILED,
                       State.STRUCTURE_CHANGED, State.BLOCKED},
    State.QUEUED: {State.DOWNLOADING, State.RETRY_WAIT, State.FAILED,
                    State.SKIPPED_DUP, State.STRUCTURE_CHANGED, State.BLOCKED},
    State.DOWNLOADING: {State.DOWNLOADED, State.RETRY_WAIT, State.FAILED},
    State.DOWNLOADED: {State.VALIDATING, State.RETRY_WAIT, State.FAILED},
    State.VALIDATING: {State.VALIDATED, State.RETRY_WAIT, State.FAILED},
    State.VALIDATED: {State.PUBLISHING_THUMBNAIL, State.PUBLISHED, State.READY, State.FAILED},
    State.READY: {State.PUBLISHING_THUMBNAIL, State.PUBLISHED, State.QUEUED, State.FAILED},
    State.PUBLISHING_THUMBNAIL: {State.THUMBNAIL_PUBLISHED, State.RETRY_WAIT, State.FAILED},
    State.THUMBNAIL_PUBLISHED: {State.PUBLISHING_VIDEO, State.PUBLISHED, State.RETRY_WAIT, State.FAILED},
    State.PUBLISHING_VIDEO: {State.PUBLISHED, State.RETRY_WAIT, State.FAILED},
    State.PUBLISHED: {State.CLEANUP_PENDING, State.CLEANUP_BLOCKED},
    State.CLEANUP_PENDING: {State.CLEANED, State.CLEANUP_BLOCKED},
    State.CLEANUP_BLOCKED: {State.CLEANED},
    State.CLEANED: set(),
    State.FAILED: {State.QUEUED, State.RETRY_WAIT},
    State.RETRY_WAIT: {State.QUEUED, State.FAILED, State.DOWNLOADING,
                       State.STRUCTURE_CHANGED, State.SKIPPED_DUP, State.BLOCKED},
    State.STRUCTURE_CHANGED: {State.QUEUED},
    State.SKIPPED_DUP: set(),
    State.BLOCKED: {State.QUEUED},
}


def can_transition(current: State, target: State) -> bool:
    return target in _ALLOWED.get(current, set())


def mainline_index(state: State) -> int:
    try:
        return _MAINLINE.index(state)
    except ValueError:
        return -1