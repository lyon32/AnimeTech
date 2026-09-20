from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class HttpConfig:
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.5
    user_agent: str = "source_audit-research-bot/0.1"


@dataclass
class EvidenceConfig:
    output_dir: str = "output"
    save_raw_html: bool = True
    save_screenshots: bool = False


@dataclass
class SourceAuditConfig:
    base_url: str
    http: HttpConfig = field(default_factory=HttpConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str | Path) -> "SourceAuditConfig":
        load_dotenv()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

        source = raw.get("source", {})
        http = raw.get("http", {})
        evidence = raw.get("evidence", {})
        logging_cfg = raw.get("logging", {})

        if "base_url" not in source:
            raise ValueError(f"Missing required 'source.base_url' in config file: {path}")

        return cls(
            base_url=source["base_url"],
            http=HttpConfig(**http),
            evidence=EvidenceConfig(**evidence),
            log_level=logging_cfg.get("level", "INFO"),
        )
