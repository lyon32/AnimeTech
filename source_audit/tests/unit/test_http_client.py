from __future__ import annotations

import httpx

from source_audit.fetch.http_client import FetchErrorType, HttpClient


def _client(handler, **kwargs) -> HttpClient:
    transport = httpx.MockTransport(handler)
    return HttpClient(timeout_seconds=1.0, max_retries=2, retry_backoff_seconds=0, transport=transport, **kwargs)


def test_200_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>ok</html>")

    with _client(handler) as client:
        result = client.get("https://example.test/ok")

    assert result.ok
    assert result.status_code == 200
    assert result.error_type == FetchErrorType.NONE
    assert result.text == "<html>ok</html>"
    assert result.content_bytes == len(b"<html>ok</html>")
    assert result.attempts == 1


def test_404_response_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="not found")

    with _client(handler) as client:
        result = client.get("https://example.test/missing")

    assert result.status_code == 404
    assert result.error_type == FetchErrorType.HTTP_404
    assert not result.ok
    assert result.attempts == 1
    assert len(calls) == 1


def test_403_response_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with _client(handler) as client:
        result = client.get("https://example.test/forbidden")

    assert result.error_type == FetchErrorType.HTTP_403
    assert result.attempts == 1


def test_429_is_retried_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="ok now")

    with _client(handler) as client:
        result = client.get("https://example.test/rate-limited")

    assert result.ok
    assert result.attempts == 3
    assert len(calls) == 3


def test_500_exhausts_retries_and_reports_last_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _client(handler) as client:
        result = client.get("https://example.test/broken")

    assert result.error_type == FetchErrorType.HTTP_500
    assert result.status_code == 500
    assert result.attempts == 3  # 1 initial + 2 retries


def test_timeout_is_classified_and_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with _client(handler) as client:
        result = client.get("https://example.test/timeout")

    assert result.error_type == FetchErrorType.TIMEOUT
    assert result.status_code is None
    assert result.attempts == 3


def test_dns_failure_is_classified():
    import socket

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail", request=request).__class__(
            "dns fail", request=request
        ) from socket.gaierror("nodename nor servname provided")

    with _client(handler) as client:
        result = client.get("https://nonexistent.invalid/")

    assert result.error_type == FetchErrorType.DNS
    assert result.attempts == 1


def test_empty_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    with _client(handler) as client:
        result = client.get("https://example.test/empty")

    assert result.ok
    assert result.text == ""
    assert result.content_bytes == 0


def test_invalid_html_is_still_returned_as_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<div><p>unclosed")

    with _client(handler) as client:
        result = client.get("https://example.test/invalid-html")

    assert result.ok
    assert result.text == "<div><p>unclosed"


def test_save_evidence_writes_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>evidence</html>")

    with _client(handler, evidence_dir=tmp_path) as client:
        result = client.get("https://example.test/evidence-page", save_evidence=True)

    assert result.ok
    saved_files = list(tmp_path.glob("*.html"))
    assert len(saved_files) == 1
    assert saved_files[0].read_text(encoding="utf-8") == "<html>evidence</html>"
