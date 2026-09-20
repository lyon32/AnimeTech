"""Real HLS downloader.

Downloads every segment of the chosen rendition over HTTP into local files,
then muxes them into a single MP4 with ffmpeg (stream copy — no re-encode,
no fabrication). Measurements are recorded from the actual transfer.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from v1_poc.manifest import MediaPlaylist, PlaylistSegment


class DownloadError(RuntimeError):
    def __init__(self, message: str, kind: str, context: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.context = context or {}


@dataclass
class DownloadMeasurements:
    download_started_at: str
    download_finished_at: str
    download_duration_seconds: float
    bytes_downloaded: int
    segment_count: int
    average_download_rate_bps: float
    product_file_size: int = 0


@dataclass
class DownloadResult:
    output_path: Path
    segments_dir: Path
    measurements: DownloadMeasurements
    mux_command: str
    mux_returncode: int
    mux_stderr_tail: str


_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classify_http(status: int | None, exc: Exception | None) -> str:
    if status is not None:
        if status == 403:
            return "HTTP_403"
        if status == 404:
            return "HTTP_404"
        if status in _TRANSIENT_STATUS:
            return f"HTTP_{status}"
        if 500 <= status < 600:
            return "HTTP_5XX"
        return f"HTTP_{status}"
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    if isinstance(exc, httpx.ConnectError):
        return "NETWORK"
    return "OTHER"


def download_segments(
    playlist: MediaPlaylist,
    dest_dir: Path,
    *,
    user_agent: str,
    timeout_seconds: float = 20.0,
    max_retries: int = 2,
    retry_backoff_seconds: float = 1.5,
) -> tuple[list[Path], int, float]:
    """Downloads every segment sequentially. Returns (local_paths, bytes, seconds)."""
    if playlist.encrypted:
        raise DownloadError(
            f"encrypted HLS (METHOD={playlist.encryption_method}) is not supported — BLOCKED",
            "ENCRYPTED",
        )
    if not playlist.segments:
        raise DownloadError("media playlist has zero segments", "EMPTY_PLAYLIST")

    dest_dir.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        timeout=timeout_seconds,
    )

    paths: list[Path] = []
    total_bytes = 0
    started = time.monotonic()
    try:
        for seg in playlist.segments:
            local_path = dest_dir / f"seg_{seg.index:05d}.ts"
            attempts = 0
            while True:
                attempts += 1
                try:
                    resp = client.get(seg.uri)
                except Exception as exc:  # classified below
                    kind = _classify_http(None, exc)
                    if kind in ("TIMEOUT", "NETWORK") and attempts <= max_retries:
                        time.sleep(retry_backoff_seconds * attempts)
                        continue
                    raise DownloadError(
                        f"segment {seg.index} fetch failed: {kind} ({exc})",
                        kind,
                        {"segment_index": seg.index, "uri": seg.uri},
                    ) from exc
                if resp.status_code == 200:
                    local_path.write_bytes(resp.content)
                    total_bytes += len(resp.content)
                    paths.append(local_path)
                    break
                kind = _classify_http(resp.status_code, None)
                if resp.status_code in _TRANSIENT_STATUS and attempts <= max_retries:
                    time.sleep(retry_backoff_seconds * attempts)
                    continue
                raise DownloadError(
                    f"segment {seg.index} returned HTTP {resp.status_code}",
                    kind,
                    {"segment_index": seg.index, "uri": seg.uri, "status": resp.status_code},
                )
    finally:
        client.close()

    elapsed = time.monotonic() - started
    return paths, total_bytes, elapsed


def _write_concat_list(paths: list[Path], list_path: Path) -> None:
    lines = "\n".join(f"file '{p.as_posix()}'" for p in paths)
    list_path.write_text(lines, encoding="utf-8")


def _run(cmd: list[str], timeout_seconds: int) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    return proc.returncode, (proc.stderr or "")[-3000:]


def mux_to_mp4(
    paths: list[Path],
    output_path: Path,
    ffmpeg_bin: Path,
    *,
    timeout_seconds: int = 1200,
) -> tuple[str, int, str]:
    """Muxes downloaded TS segments into one MP4 (stream copy). Returns (cmd, rc, stderr)."""
    if not paths:
        raise DownloadError("no segments to mux", "EMPTY")
    tmp_dir = output_path.parent / (output_path.stem + "_concat")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    concat_list = tmp_dir / "concat.txt"
    _write_concat_list(paths, concat_list)
    cmd = [
        str(ffmpeg_bin), "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(output_path),
    ]
    rc, stderr = _run(cmd, timeout_seconds)

    if rc != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        cmd2 = [
            str(ffmpeg_bin), "-y", "-loglevel", "error",
            "-i", f"concat:{'|'.join(p.as_posix() for p in paths)}",
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(output_path),
        ]
        rc2, stderr2 = _run(cmd2, timeout_seconds)
        if rc2 != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            raise DownloadError(
                f"mux failed (concat rc={rc}, fallback rc={rc2}): {stderr} | {stderr2}",
                "MUX_FAILED",
            )
        return " ".join(cmd2), rc2, stderr2

    return " ".join(cmd), rc, stderr


def download_and_mux(
    playlist: MediaPlaylist,
    output_path: Path,
    ffmpeg_bin: Path,
    *,
    user_agent: str,
    work_root: Path,
    timeout_seconds: float = 20.0,
    max_retries: int = 2,
    retry_backoff_seconds: float = 1.5,
    keep_segments: bool = False,
) -> DownloadResult:
    """Downloads the full rendition and muxes it to a single MP4."""
    started_at = _now_utc()
    segments_dir = work_root / "segments"
    paths, total_bytes, elapsed = download_segments(
        playlist,
        segments_dir,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    finished_at = _now_utc()

    cmd, rc, stderr = mux_to_mp4(paths, output_path, ffmpeg_bin)

    if not keep_segments:
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            segments_dir.rmdir()
        except OSError:
            pass

    rate_bps = int(total_bytes * 8 / elapsed) if elapsed > 0 else 0
    measurements = DownloadMeasurements(
        download_started_at=started_at,
        download_finished_at=finished_at,
        download_duration_seconds=round(elapsed, 3),
        bytes_downloaded=total_bytes,
        segment_count=len(paths),
        average_download_rate_bps=rate_bps,
        product_file_size=output_path.stat().st_size if output_path.exists() else 0,
    )

    return DownloadResult(
        output_path=output_path,
        segments_dir=segments_dir,
        measurements=measurements,
        mux_command=cmd,
        mux_returncode=rc,
        mux_stderr_tail=stderr,
    )