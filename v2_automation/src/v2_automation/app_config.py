"""Centralized V2 configuration + bot-capacity detection.

Reads config/config.yaml, overlays .env secrets, and probes the Local Bot API
server to build a snapshot of capabilities.  This module is safe to call at
startup (read-only, no side effects beyond creating the data/ directory).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"
DATA_DIR = ROOT / "data"
EVIDENCE_DIR = ROOT.parent / "v2_local_bot_api_poc" / "output" / "evidence"


# ── helpers ──────────────────────────────────────────────────────────────────────

def _load_yaml() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _resolve_env_var(val: str) -> str | None:
    """If val is an env-var reference (e.g. `env:TELEGRAM_CHANNEL_ID`), resolve it."""
    if isinstance(val, str) and val.startswith("env:"):
        return os.getenv(val[4:])
    return val


def _disk_free_bytes(path: Path) -> int:
    if not path.exists():
        path = path.parent
    usage = psutil.disk_usage(str(path))
    return usage.free


def _probe_local_bot_api(base_url: str) -> dict:
    """Probe local bot API status. Returns dict with keys:
    enabled, getme_ok, http_server_version, documented_limit, free_disk_bytes.
    All fields are set even on failure (enabled=False => everything else None)."""
    probe = {"enabled": False, "getme_ok": False, "http_server_version": None,
             "status_code": None, "error": None, "free_disk_bytes": None}
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not base_url:
        return probe
    # Step 1: getMe
    try:
        r = httpx.post(f"{base_url}/bot{token}/getMe", timeout=10)
        probe["status_code"] = r.status_code
        probe["getme_ok"] = r.status_code == 200
    except Exception as exc:
        probe["error"] = f"{type(exc).__name__}: {exc}"
        return probe
    # Step 2: status port (8082) — best-effort, fallback to V2 evidence build file
    stat_url = base_url.replace(":8081", ":8082")
    try:
        r = httpx.get(f"{stat_url}/status?all=1", timeout=5)
        if r.status_code == 200:
            text = r.text or ""
            line = next((ln for ln in text.splitlines() if "version" in ln.lower()), "")
            probe["http_server_version"] = line.split("\t", 1)[0] if line else None
    except Exception:
        pass
    if not probe.get("http_server_version"):
        try:
            build = json.loads((EVIDENCE_DIR / "server_build.json").read_text(encoding="utf-8"))
            probe["http_server_version"] = build.get("server_version")
        except Exception:
            pass
    probe["enabled"] = True
    probe["free_disk_bytes"] = _disk_free_bytes(DATA_DIR)
    return probe


# ── public dataclass ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BotCapacity:
    enabled: bool
    getme_ok: bool
    http_server_version: str | None
    free_disk_bytes: int | None
    documented_limit_bytes: int | None
    tested_limit_bytes: int | None
    status_code: int | None
    error: str | None


@dataclass(frozen=True)
class AppConfig:
    # raw config
    source: dict
    queues: dict
    downloads: dict
    telegram: dict
    publication: dict
    limits: dict
    monitoring: dict
    logging: dict
    # resolved secrets
    bot_token: str
    channel_id: str
    admin_telegram_ids: list[int]
    # capacity
    bot_capacity: BotCapacity | None
    # authorization gate (deny-by-default, empty = blocked)
    authorization: dict = field(default_factory=dict)
    # second bot, used ONLY for the admin panel / alerts / notifications (private chats).  The first bot
    # (bot_token) only publishes to the channel.  Empty = single-bot mode (bot_token does both).
    admin_bot_token: str = ""

    def notify_token(self) -> str:
        """Token of the bot that talks to the administrators."""
        return self.admin_bot_token or self.bot_token


# ── builder ──────────────────────────────────────────────────────────────────────

def _load_admin_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [int(p) for p in parts]


def load_config() -> AppConfig:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT.parent / "v1_poc" / ".env", override=False)          # v1 secrets as fallback
    cfg = _load_yaml()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    channel = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
    admin_raw = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    api_base = cfg.get("telegram", {}).get("api_base_url", "").strip()
    env_base = os.getenv("TELEGRAM_API_BASE_URL", "").strip()
    if env_base:                                   # .env overrides config.yaml
        api_base = env_base
        cfg.setdefault("telegram", {})["api_base_url"] = env_base

    # capacity probe
    probe = _probe_local_bot_api(api_base) if api_base else {"enabled": False}
    caps = BotCapacity(
        enabled=probe.get("enabled", False),
        getme_ok=probe.get("getme_ok", False),
        http_server_version=probe.get("http_server_version"),
        free_disk_bytes=probe.get("free_disk_bytes"),
        documented_limit_bytes=cfg.get("limits", {}).get("documented_upload_limit_mb") and (
            cfg["limits"]["documented_upload_limit_mb"] * 1024 * 1024),
        tested_limit_bytes=cfg.get("limits", {}).get("tested_upload_limit_mb") and (
            cfg["limits"]["tested_upload_limit_mb"] * 1024 * 1024),
        status_code=probe.get("status_code"),
        error=probe.get("error"),
    )
    return AppConfig(
        source=cfg.get("source", {}),
        queues=cfg.get("queues", {}),
        downloads=cfg.get("downloads", {}),
        telegram=cfg.get("telegram", {}),
        publication=cfg.get("publication", {}),
        limits=cfg.get("limits", {}),
        monitoring=cfg.get("monitoring", {}),
        logging=cfg.get("logging", {}),
        bot_token=token,
        admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", "").strip(),
        channel_id=channel,
        admin_telegram_ids=_load_admin_ids(admin_raw),
        bot_capacity=caps,
        authorization=cfg.get("authorization", {}),
    )