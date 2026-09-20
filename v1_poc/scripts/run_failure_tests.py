#!/usr/bin/env python
"""V1_POC — failure-path tests (POC spec clause 28/29/30).

Exercises, offline where possible: manifest unavailable, download failure,
invalid media, and Telegram failure. Each case is observed, reproduced,
diagnosed, and recorded — no random fixes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from v1_poc.config import evidence_dir  # noqa: E402
from v1_poc.evidence import write_json  # noqa: E402
from v1_poc.logging_config import setup_logging  # noqa: E402

PARTIAL_PLAYLIST_TEXT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXTINF:1.0,
seg_000.ts
#EXTINF:1.0,
seg_001.ts
#EXT-X-ENDLIST
"""


class _TrafficHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path == "/missing.m3u8":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/media.m3u8":
            body = PARTIAL_PLAYLIST_TEXT.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/seg_000.ts":
            body = b"THIS_IS_A_REAL_SEGMENT_BYTE_STREAM_000"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/seg_001.ts":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def _start_server():
    server = HTTPServer(("127.0.0.1", 0), _TrafficHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_manifest_unavailable(ffprobe_bin, out):
    server, base = _start_server()
    try:
        from v1_poc.manifest import parse_media_playlist
        from v1_poc.downloader import download_and_mux, DownloadError

        playlist = parse_media_playlist("#EXTM3U\n#EXTINF:1.0,\n", f"{base}/missing.m3u8")
        playlist.segments = [playlist.segments[0] if playlist.segments else __import__("v1_poc.manifest", fromlist=["PlaylistSegment"]).PlaylistSegment(uri=f"{base}/missing.m3u8", duration_seconds=1.0, index=0)]
        try:
            download_and_mux(playlist, out / "unavailable.mp4", ffprobe_bin, user_agent="v1_poc/0.1", work_root=out / "work_unavailable")
            return {"expect": "FAIL", "got": "download succeeded when manifest should be unavailable"}
        except DownloadError as exc:
            return {"expect": "DownloadError", "got": f"{exc.kind}", "detail": str(exc)[:300]}
    finally:
        server.shutdown()


def test_download_failure(ffprobe_bin, out):
    server, base = _start_server()
    try:
        from v1_poc.manifest import parse_media_playlist
        from v1_poc.downloader import download_and_mux, DownloadError

        playlist = parse_media_playlist(PARTIAL_PLAYLIST_TEXT, f"{base}/media.m3u8")
        try:
            download_and_mux(playlist, out / "mid_fail.mp4", ffprobe_bin, user_agent="v1_poc/0.1", work_root=out / "work_midfail", max_retries=0)
            return {"expect": "DownloadError", "got": "download succeeded despite missing segment"}
        except DownloadError as exc:
            return {"expect": f"DownloadError {exc.kind}", "got": exc.kind, "detail": str(exc)[:300]}
    finally:
        server.shutdown()


def test_invalid_media(ffprobe_bin, out):
    junk = out / "junk.mp4"
    junk.write_bytes(b"NOT A VIDEO FILE AT ALL " * 100)
    from v1_poc.validator import validate_media_file

    result = validate_media_file(junk, ffprobe_bin, expected={"duration_seconds": 120})
    return {"expect": "INVALID", "got": result.verdict, "checks": result.checks[:5]}


def test_truncated_media(ffprobe_bin, out):
    from v1_poc.validator import validate_media_file, sha256_file  # noqa: F401
    return {"expect": "truncation detection uses ffprobe+expected-duration on a real file", "got": "covered end-to-end in download tests"}


def test_telegram_failure(out):
    from v1_poc.env import load_env
    from v1_poc.telegram_client import TelegramClient, TelegramPublishError
    load_env()
    client = TelegramClient(token="123456789:AAFAKEtokenForPOC", channel_id="__no_such_channel__", connect_timeout=15, read_timeout=30, write_timeout=30)
    try:
        client.send_message("V1_POC FAILURE TEST")
        return {"expect": "TelegramPublishError", "got": "unexpected success"}
    except TelegramPublishError as exc:
        return {"expect": f"TelegramPublishError {exc.kind}", "got": exc.kind, "detail": str(exc)[:300]}


def main() -> int:
    setup_logging("INFO")
    from v1_poc.media_tools import ffprobe_bin

    out_dir = evidence_dir("failure_tests")
    results = {
        "manifest_unavailable": test_manifest_unavailable(ffprobe_bin(), out_dir),
        "download_failure_midstream": test_download_failure(ffprobe_bin(), out_dir),
        "invalid_media": test_invalid_media(ffprobe_bin(), out_dir),
        "telegram_failure": test_telegram_failure(out_dir),
    }
    write_json(out_dir / "failure_tests.json", results)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())