from pathlib import Path

from source_audit.config import SourceAuditConfig

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"


def test_load_example_config():
    cfg = SourceAuditConfig.load(EXAMPLE_CONFIG)
    assert cfg.base_url == "https://voir-anime.to/"
    assert cfg.http.timeout_seconds == 15
    assert cfg.log_level == "INFO"
