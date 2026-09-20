"""Logging configuration — token sanitisation + rotating file + stream."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import app_config

_SENTINEL = "***TOKEN***"
_RE: re.Pattern[str] | None = None


def token_filters(cfg) -> list:
    """One masking filter per bot token (publishing bot AND admin bot); empty / short values are skipped."""
    out = []
    for secret in (cfg.bot_token, getattr(cfg, "admin_bot_token", "")):
        tok = (secret or "").strip()
        if len(tok) > 8:
            out.append(_TokenFilter(tok))
    return out


class _TokenFilter(logging.Filter):
    """Mask any occurrence of bot_token in every record (exc_info included)."""

    def __init__(self, token: str):
        super().__init__()
        self._pat = re.compile(re.escape(token), re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg:
            record.msg = self._pat.sub(_SENTINEL, record.msg)
        if isinstance(record.args, dict):          # log.info("%s", {...}): logging keeps a lone dict as the args
            record.args = {k: self._pat.sub(_SENTINEL, v) if isinstance(v, str) else v for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(
                self._pat.sub(_SENTINEL, str(a)) if isinstance(a, str) else a
                for a in record.args
            )
        if record.exc_text:
            record.exc_text = self._pat.sub(_SENTINEL, record.exc_text)
        if record.stack_info:
            record.stack_info = self._pat.sub(_SENTINEL, record.stack_info)
        return True


def configure(cfg: app_config.AppConfig | None = None) -> logging.Logger:
    """Idempotent — safe to call more than once (rotated handler only added once)."""
    cfg = cfg or app_config.load_config()
    root = logging.getLogger("v2_automation")
    if getattr(root, "_v2_configured", False):
        return root
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    # Stream (stdout)
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Rotating file
    log_cfg = cfg.logging.get("log_file", {})
    log_dir = Path(log_cfg.get("dir", str(app_config.DATA_DIR / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        str(log_dir / "v2_automation.log"),
        maxBytes=log_cfg.get("max_bytes", 20_000_000),
        backupCount=log_cfg.get("backup_count", 3),
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Token filter (best-effort; if token is empty, skip)
    for flt in token_filters(cfg):
        root.addFilter(flt)
    root._v2_configured = True  # type: ignore[attr-defined]
    return root