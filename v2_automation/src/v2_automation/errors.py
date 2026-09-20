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

ALL_CODES = (
    SOURCE_EXTRACTION_FAILED, NO_RENDITION, PLAYLIST_FETCH_FAILED, PLAYLIST_PARSE_FAILED,
    DOWNLOAD_FAILED, VALIDATION_FAILED, THUMBNAIL_FAILED,
    TELEGRAM_THUMBNAIL_FAILED, TELEGRAM_VIDEO_FAILED, NOT_AVAILABLE_YET, SOURCE_VIDEO_PROCESSING,
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
