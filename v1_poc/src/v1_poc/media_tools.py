"""Resolves the ffmpeg / ffprobe binaries used for real media handling."""
from __future__ import annotations

import shutil
from pathlib import Path

from v1_poc.config import load_config


class MediaToolsError(RuntimeError):
    pass


def _resolve(binary: str, configured_dir: str | None) -> Path:
    if configured_dir:
        candidate = Path(configured_dir) / f"{binary}.exe"
        if candidate.exists():
            return candidate
    system = shutil.which(binary)
    if system:
        return Path(system)
    raise MediaToolsError(
        f"{binary} not found. Expected a portable build at tools/ffmpeg/bin/ "
        f"(see source_audit) or on the system PATH."
    )


def ffmpeg_bin() -> Path:
    cfg = load_config()
    return _resolve("ffmpeg", cfg.get("media", {}).get("ffmpeg_bin_dir"))


def ffprobe_bin() -> Path:
    cfg = load_config()
    return _resolve("ffprobe", cfg.get("media", {}).get("ffmpeg_bin_dir"))