from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class FetchErrorType(str, Enum):
    """Distinct failure categories. Never collapse these into one generic "error"."""

    NONE = "NONE"
    TIMEOUT = "TIMEOUT"
    DNS = "DNS"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_429 = "HTTP_429"
    HTTP_500 = "HTTP_500"
    HTTP_OTHER = "HTTP_OTHER"
    NETWORK_OTHER = "NETWORK_OTHER"


# Status codes worth retrying: transient server-side/rate-limit conditions.
# 403/404 are NOT retried: they are not transient, retrying would just be noise
# (and, for 403, could look like probing/evasion — the opposite of what this
# project wants to do on an unauthenticated, no-ToS site).
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    error_type: FetchErrorType
    elapsed_seconds: float
    content_bytes: int
    attempts: int
    text: Optional[str] = None
    headers: Optional[httpx.Headers] = None

    @property
    def ok(self) -> bool:
        return self.error_type == FetchErrorType.NONE and self.status_code == 200


def _classify_status(status_code: int) -> FetchErrorType:
    if status_code == 200:
        return FetchErrorType.NONE
    if status_code == 403:
        return FetchErrorType.HTTP_403
    if status_code == 404:
        return FetchErrorType.HTTP_404
    if status_code == 429:
        return FetchErrorType.HTTP_429
    if 500 <= status_code < 600:
        return FetchErrorType.HTTP_500
    return FetchErrorType.HTTP_OTHER


def _classify_exception(exc: Exception) -> FetchErrorType:
    if isinstance(exc, httpx.TimeoutException):
        return FetchErrorType.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        # httpx wraps DNS failures (socket.gaierror) inside ConnectError.
        if isinstance(exc.__cause__, socket.gaierror):
            return FetchErrorType.DNS
        return FetchErrorType.NETWORK_OTHER
    return FetchErrorType.NETWORK_OTHER


class HttpClient:
    """Thin, evidence-aware HTTP GET client.

    Retries only transient conditions (timeout, 429, 5xx) with backoff; does not
    retry 403/404/other 4xx. Every call returns a FetchResult so callers can branch
    on the exact failure category instead of treating all errors alike.
    """

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.5,
        user_agent: str = "source_audit-research-bot/0.1",
        evidence_dir: Optional[Path] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.evidence_dir = evidence_dir
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, url: str, *, save_evidence: bool = False) -> FetchResult:
        attempts = 0
        last_error_type = FetchErrorType.NETWORK_OTHER
        last_status: Optional[int] = None

        while attempts <= self.max_retries:
            attempts += 1
            start = time.monotonic()
            try:
                response = self._client.get(url)
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                elapsed = time.monotonic() - start
                error_type = _classify_exception(exc)
                logger.warning(
                    "GET %s failed on attempt %d/%d: %s (%s)",
                    url,
                    attempts,
                    self.max_retries + 1,
                    error_type.value,
                    exc,
                )
                last_error_type = error_type
                if error_type == FetchErrorType.TIMEOUT and attempts <= self.max_retries:
                    time.sleep(self.retry_backoff_seconds * attempts)
                    continue
                return FetchResult(
                    url=url,
                    status_code=None,
                    error_type=error_type,
                    elapsed_seconds=elapsed,
                    content_bytes=0,
                    attempts=attempts,
                )

            elapsed = time.monotonic() - start
            error_type = _classify_status(response.status_code)
            last_status = response.status_code
            last_error_type = error_type

            logger.info(
                "GET %s -> %d (%s) in %.3fs, %d bytes, attempt %d/%d",
                url,
                response.status_code,
                error_type.value,
                elapsed,
                len(response.content),
                attempts,
                self.max_retries + 1,
            )

            if response.status_code in _RETRYABLE_STATUS_CODES and attempts <= self.max_retries:
                time.sleep(self.retry_backoff_seconds * attempts)
                continue

            result = FetchResult(
                url=url,
                status_code=response.status_code,
                error_type=error_type,
                elapsed_seconds=elapsed,
                content_bytes=len(response.content),
                attempts=attempts,
                text=response.text if error_type == FetchErrorType.NONE else None,
                headers=response.headers,
            )
            if save_evidence and error_type == FetchErrorType.NONE:
                self._save_evidence(url, response.text)
            return result

        # Exhausted retries on a retryable status code.
        return FetchResult(
            url=url,
            status_code=last_status,
            error_type=last_error_type,
            elapsed_seconds=elapsed,
            content_bytes=0,
            attempts=attempts,
        )

    def _save_evidence(self, url: str, text: str) -> None:
        if self.evidence_dir is None:
            return
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() else "_" for c in url)[:150]
        path = self.evidence_dir / f"{safe_name}.html"
        path.write_text(text, encoding="utf-8")
        logger.debug("Saved evidence for %s to %s", url, path)
