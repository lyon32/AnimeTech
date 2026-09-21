"""Identifiable pipeline errors — one stable code per step.

The original exception is never masked: it is chained (`raise ... from exc`)
and its type/message stay in the text stored in `episodes.last_error`.
"""
from __future__ import annotations

SOURCE_EXTRACTION_FAILED = "SOURCE_EXTRACTION_FAILED"
NO_RENDITION = "NO_RENDITION"
PLAYLIST_FETCH_FAILED = "PLAYLIST_FETCH_FAILED"
PLAYLIST_PARSE_FAILED = "PLAYLIST_PARSE_FAILED"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
VALIDATION_FAILED = "VALIDATION_FAILED"
THUMBNAIL_FAILED = "THUMBNAIL_FAILED"
TELEGRAM_THUMBNAIL_FAILED = "TELEGRAM_THUMBNAIL_FAILED"
TELEGRAM_VIDEO_FAILED = "TELEGRAM_VIDEO_FAILED"
NOT_AVAILABLE_YET = "NOT_AVAILABLE_YET"   # not a failure: the source has not published the video yet
SOURCE_VIDEO_PROCESSING = "SOURCE_VIDEO_PROCESSING"   # page + player exist, the video file answers 404 (not ready / not served)

# User-side classification (requests / deliveries).  Pipeline codes above stay the source of truth for a download.
SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
EPISODE_NOT_AVAILABLE = "EPISODE_NOT_AVAILABLE"
STORAGE_ERROR = "STORAGE_ERROR"
TELEGRAM_ERROR = "TELEGRAM_ERROR"
AUTH_ERROR = "AUTH_ERROR"
TIMEOUT = "TIMEOUT"
UNKNOWN_ERROR = "UNKNOWN_ERROR"

ALL_CODES = (
    SOURCE_EXTRACTION_FAILED, NO_RENDITION, PLAYLIST_FETCH_FAILED, PLAYLIST_PARSE_FAILED,
    DOWNLOAD_FAILED, VALIDATION_FAILED, THUMBNAIL_FAILED,
    TELEGRAM_THUMBNAIL_FAILED, TELEGRAM_VIDEO_FAILED, NOT_AVAILABLE_YET, SOURCE_VIDEO_PROCESSING,
    SOURCE_NOT_FOUND, EPISODE_NOT_AVAILABLE, STORAGE_ERROR, TELEGRAM_ERROR, AUTH_ERROR, TIMEOUT, UNKNOWN_ERROR,
)


class PipelineError(RuntimeError):
    """A pipeline step failed. `code` is one of ALL_CODES; `__cause__` keeps the original."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


def wrap(code: str, exc: BaseException) -> PipelineError:
    """PipelineError carrying the original exception's type and message."""
    if isinstance(exc, PipelineError):
        return exc
    err = PipelineError(code, f"{type(exc).__name__}: {exc}")
    err.__cause__ = exc
    return err


def classify(exc: BaseException) -> str:
    """Stable code for any exception; the message itself is always kept next to it (never masked)."""
    if isinstance(exc, PipelineError):
        return exc.code
    name, text = type(exc).__name__, str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in name.lower() or "timed out" in text:
        return TIMEOUT
    if isinstance(exc, (PermissionError, OSError)) and not isinstance(exc, ConnectionError):
        return STORAGE_ERROR
    if "unauthorized" in text or "forbidden" in text or "401" in text or "403" in text:
        return AUTH_ERROR
    if "telegram" in name.lower() or "telegram" in text or "bot was blocked" in text or "chat not found" in text:
        return TELEGRAM_ERROR
    if "404" in text or "not found" in text or "introuvable" in text:
        return SOURCE_NOT_FOUND
    return UNKNOWN_ERROR
