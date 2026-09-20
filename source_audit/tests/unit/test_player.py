from pathlib import Path

from source_audit.analysis.player import parse_embed_page

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "embed_page_sample.html"
IFRAME_URL = "https://voembed.net/embed-sample1234.html"


def test_detects_domain_and_title():
    obs = parse_embed_page(FIXTURE.read_text(encoding="utf-8"), IFRAME_URL)
    assert obs.iframe_url == IFRAME_URL
    assert obs.iframe_domain == "voembed.net"
    assert obs.embed_page_title == "Sample Show - 08 VF"


def test_detects_jwplayer():
    obs = parse_embed_page(FIXTURE.read_text(encoding="utf-8"), IFRAME_URL)
    assert obs.player_library == "jwplayer"


def test_detects_obfuscated_script_without_deobfuscating():
    obs = parse_embed_page(FIXTURE.read_text(encoding="utf-8"), IFRAME_URL)
    assert obs.has_obfuscated_script is True


def test_finds_manifest_url_in_cleartext():
    obs = parse_embed_page(FIXTURE.read_text(encoding="utf-8"), IFRAME_URL)
    assert obs.manifest_url_found is True
    assert obs.manifest_url is not None
    assert "master.m3u8" in obs.manifest_url


def test_no_signals_when_absent():
    html = "<html><head><title>empty</title></head><body></body></html>"
    obs = parse_embed_page(html, IFRAME_URL)
    assert obs.player_library is None
    assert obs.has_obfuscated_script is False
    assert obs.manifest_url_found is False
    assert obs.manifest_url is None


# Phase 13 resilience.
def test_empty_string_html_does_not_raise():
    obs = parse_embed_page("", IFRAME_URL)
    assert obs.iframe_url == IFRAME_URL
    assert obs.embed_page_title is None
    assert obs.player_library is None
    assert obs.manifest_url_found is False
