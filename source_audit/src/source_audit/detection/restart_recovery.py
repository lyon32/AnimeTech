"""Restart/recovery policy per episode status (Phase 11).

`source_audit` has no real downloader or publisher (MASTER_PLAN.md §51) — a
"crash" here is simulated as: an `EpisodeStore` (Phase 10) is left in some status,
a brand-new process starts (`EpisodeStore.restore()` from a persisted snapshot,
exactly as Phase 10 scenario 5 already validated), and this module answers "what
should happen to this episode now?" MASTER_PLAN.md §22 requires every state to have
a *defined* recovery strategy — the point of this module is to make that mapping
explicit and total (every `EpisodeStatus` value maps to exactly one
`RecoveryAction`), not something a future V1 has to improvise per crash.

Recovery reasoning per state (see SESSION_REPORT.md Phase 11 for the evidence this
is based on):

- `DISCOVERED`: nothing was in flight — safe to (re)start the download exactly as
  if discovered for the first time.
- `DOWNLOADING`: the crash happened *during* an in-progress download — any partial
  artifact on disk is untrustworthy (could be truncated/corrupt). The only safe
  recovery is to discard whatever partial state exists and restart the download
  from scratch, i.e. treat it the same as `DISCOVERED`. This module does NOT
  attempt to resume a partial download — resuming safely would require knowing the
  exact byte offset and validating it, which needs a real downloader/storage layer
  this project doesn't have; guessing here would violate MASTER_PLAN.md §43
  ("never turn an error into a hypothesis").
- `DOWNLOADED`: the file finished downloading but publishing hadn't completed.
  Recovery should validate the existing artifact (a real V1's job, not this
  project's) and then proceed to publish — NOT re-download, since the download
  itself already succeeded.
- `PUBLISHED`: already fully done. Recovery action is explicitly "do nothing" —
  this is the same guarantee Phase 10 already established for deduplication
  (re-discovering a PUBLISHED episode must not regress or reprocess it), restated
  here as the crash-recovery case of the same underlying fact.
"""
from __future__ import annotations

from enum import Enum

from source_audit.detection.deduplication import EpisodeStatus


class RecoveryAction(str, Enum):
    RESTART_DOWNLOAD = "RESTART_DOWNLOAD"
    VALIDATE_THEN_PUBLISH = "VALIDATE_THEN_PUBLISH"
    NO_ACTION = "NO_ACTION"


_RECOVERY_MAP: dict[EpisodeStatus, RecoveryAction] = {
    EpisodeStatus.DISCOVERED: RecoveryAction.RESTART_DOWNLOAD,
    EpisodeStatus.DOWNLOADING: RecoveryAction.RESTART_DOWNLOAD,
    EpisodeStatus.DOWNLOADED: RecoveryAction.VALIDATE_THEN_PUBLISH,
    EpisodeStatus.PUBLISHED: RecoveryAction.NO_ACTION,
}


def determine_recovery_action(status: EpisodeStatus) -> RecoveryAction:
    """Total mapping: every EpisodeStatus has exactly one defined recovery action.

    Raises KeyError (loudly, not silently) if a new EpisodeStatus value is ever
    added without updating _RECOVERY_MAP -- MASTER_PLAN.md §22 requires every
    state to have a defined strategy, so an unmapped state must fail visibly,
    never fall through to a guessed default.
    """
    return _RECOVERY_MAP[status]
