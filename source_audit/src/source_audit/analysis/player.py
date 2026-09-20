"""Parses the third-party embed page linked by an episode's iframe (Phase 5).

Evidence (2026-09-17, see SESSION_REPORT.md Phase 5), based on one real embed page
fetched live (`voembed.net/embed-1jpcypqgkz16.html`, linked from "The Exiled Heavy
Knight..." episode 12):

- `voembed.net` has no `robots.txt` (404) — no explicit crawling policy either way,
  same "no ToS found" situation as the main site (see Phase 0 authorization check).
- The embed page has no `<video>`/`<source>` tag; playback is entirely JS-driven.
  15 `<script>` tags were present; one matched a known JS-packer signature
  (`eval(function(p,a,c,k,e,d)` — the classic Dean Edwards packer, commonly used for
  light obfuscation, not by itself proof of DRM). This project does **not**
  deobfuscate or execute that script — its presence is recorded as a fact only.
- A separate, non-obfuscated script (~20KB) contained a literal `jwplayer(...)` call
  and a `sources: [{ file: '...master.m3u8?...' }]` config — i.e. **JW Player** is
  the player library, and the manifest URL is handed to any viewer's browser in
  cleartext as part of normal page load (not extracted by bypassing anything).
- The manifest URL carries a signed, time-limited token (`?t=...&s=<epoch>&e=43200`
  — an expiry offset in seconds, resolved to ~12h from issuance in the one sample
  checked) on a separate CDN-style host (`*.vmget.online`, distinct again from both
  `voir-anime.to` and `voembed.net`) — i.e. **three different domains** are involved
  end-to-end (site → embed → CDN). This is a legitimate, ordinary anti-hotlink
  mechanism (the token is issued to any normal viewer), not a protection this
  project bypasses.

**BLOCKED (by design, not by necessity):** deobfuscating the packed script to
understand what it does, and downloading actual video segments, are both out of
scope for `source_audit` regardless of technical feasibility — see MASTER_PLAN.md
§16 ("ne pas contourner DRM/CAPTCHA/authentification") and §51 (no downloading
pipeline in this project).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from source_audit.models import PlayerObservation

logger = logging.getLogger(__name__)

# Dean Edwards-style JS packer signature — a known light-obfuscation technique,
# recorded as a fact only; this project never deobfuscates it.
_PACKED_JS_RE = re.compile(r"eval\(function\(p,a,c,k,e,")
_JWPLAYER_RE = re.compile(r"\bjwplayer\s*\(")
_MASTER_MANIFEST_RE = re.compile(r"file:\s*'([^']+master\.m3u8[^']*)'")


def parse_embed_page(html: str, iframe_url: str) -> PlayerObservation:
    soup = BeautifulSoup(html, "lxml")

    domain = urlparse(iframe_url).netloc or None
    embed_title = soup.title.get_text(strip=True) if soup.title else None

    script_texts = [s.get_text() for s in soup.select("script") if s.get_text().strip()]
    full_text = "\n".join(script_texts)

    has_obfuscated_script = bool(_PACKED_JS_RE.search(full_text))
    player_library = "jwplayer" if _JWPLAYER_RE.search(full_text) else None

    manifest_match = _MASTER_MANIFEST_RE.search(full_text)
    manifest_url = manifest_match.group(1) if manifest_match else None

    if player_library is None:
        logger.info("No known player library detected in embed page %s", iframe_url)
    if manifest_url is None:
        logger.info("No HLS master manifest URL found in cleartext in embed page %s", iframe_url)

    return PlayerObservation(
        iframe_url=iframe_url,
        iframe_domain=domain,
        embed_page_title=embed_title,
        player_library=player_library,
        has_obfuscated_script=has_obfuscated_script,
        manifest_url_found=manifest_url is not None,
        manifest_url=manifest_url,
    )
