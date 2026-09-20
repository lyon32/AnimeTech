"""The players of an episode page and the fallback between them.

An episode page offers several players ("LECTEUR myTV / MOON / VOE / Stape"), each with its OWN address:

    myTV  -> voembed.net   HLS master manifest in the page of the player      (default; the only one read so far)
    MOON  -> mfw09.org     script shell, needs a browser                       (not supported)
    VOE   -> voe.sx        script shell, needs a browser                       (not supported)
    Stape -> streamtape.com direct MP4 behind a small script we can evaluate   (supported here)

Black Torch E12 showed why one player is not enough: myTV answered "404" for hours (its own player shows
"Video playback error" in a real browser) while Stape served the same episode as a 310 MB MP4.  When the
default player fails with an access error, the other players are tried in order; the error says what was tried.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

# players in order of preference AFTER the default one, and how each host is handled
SUPPORTED = ("streamtape.com",)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_ROBOTLINK = re.compile(r"robotlink'\)\.innerHTML\s*=\s*'([^']*)'\s*\+\s*\('([^']*)'\)((?:\.substring\(\d+\))*)")


@dataclass
class DirectExtraction:
    """Same shape as `v1_poc.source_client.SourceExtraction` for what the pipeline reads, but the video is ONE
    direct MP4 (`direct_url`) instead of an HLS rendition list."""
    episode_url: str
    episode_key: str
    anime_key: str | None
    anime_post_id: str | None
    episode_number: int | None
    player_name: str
    direct_url: str
    direct_size: int | None
    direct_referer: str
    renditions: list = field(default_factory=list)
    page_title_raw: str | None = None
    source_language: str | None = None
    thumbnail_url: str | None = None
    player_iframe_url: str | None = None


def host_names(page_html: str) -> list[str]:
    """The players offered by the page, in page order, without duplicates (the page repeats its selector)."""
    out: list[str] = []
    for m in re.finditer(r'<option[^>]*value="(LECTEUR [^"]+)"', page_html):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def iframe_of(page_html: str) -> str | None:
    m = re.search(r'<iframe[^>]+(?:src|data-src)="([^"]+)"', page_html)
    return m.group(1) if m else None


def streamtape_direct_url(embed_html: str) -> str | None:
    """Streamtape hides the file address in a tiny script: `'//streamt' + ('xcdape.com/get_video?...').substring(2)...`."""
    m = _ROBOTLINK.search(embed_html or "")
    if not m:
        return None
    head, tail = m.group(1), m.group(2)
    for n in (int(x) for x in re.findall(r"substring\((\d+)\)", m.group(3))):
        tail = tail[n:]
    url = head + tail
    return ("https:" + url) if url.startswith("//") else url


def _label(name: str) -> str:
    return name.replace("LECTEUR ", "")


def probe_direct(url: str, referer: str, http) -> int | None:
    """Size of the MP4 (a 1-byte Range request) or None when the file is not served."""
    r = http.get(url, headers={"Range": "bytes=0-0", "Referer": referer, "User-Agent": _UA})
    if r.status_code not in (200, 206) or "video" not in (r.headers.get("content-type") or ""):
        return None
    total = (r.headers.get("content-range") or "").rsplit("/", 1)[-1]
    return int(total) if total.isdigit() else (int(r.headers.get("content-length", 0)) or None)


def extract_with_fallback(episode_url: str, client, *, primary=None, http=None):
    """Default player first (HLS, unchanged); on an access failure, try the other players of the page."""
    import httpx
    from v1_poc import source_client as v1
    primary = primary or v1.extract_source
    try:
        return primary(episode_url, client)
    except v1.SourceAccessError as first:
        tried = [f"myTV : {str(first)[:110]}"]
        first_error = first
    own = http is None
    http = http or httpx.Client(timeout=30, follow_redirects=True)
    try:
        page = http.get(episode_url, headers={"User-Agent": _UA}).text
        others = [h for h in host_names(page) if h != "LECTEUR myTV"]
        from source_audit.analysis.episode import parse_episode_page
        record = parse_episode_page(page, episode_url)
        for name in others:
            try:
                alt = http.get(f"{episode_url}?host={quote(name)}", headers={"User-Agent": _UA}).text
                iframe = iframe_of(alt)
                host = (urlparse(iframe).hostname or "").lower() if iframe else ""
                if not iframe:
                    tried.append(f"{_label(name)} : aucun lecteur")
                    continue
                if not host.endswith(SUPPORTED):
                    tried.append(f"{_label(name)} : non pris en charge")
                    continue
                embed = http.get(iframe, headers={"User-Agent": _UA, "Referer": "https://voir-anime.to/"}).text
                direct = streamtape_direct_url(embed)
                size = probe_direct(direct, iframe, http) if direct else None
                if not size:
                    tried.append(f"{_label(name)} : vidéo indisponible")
                    continue
                from v1_poc.source_client import _og_image
                logger.info("[SOURCE] lecteur %s utilisé (myTV en échec) : MP4 direct %d octets", _label(name), size)
                return DirectExtraction(
                    episode_url=episode_url, episode_key=record.episode_key or "", anime_key=record.anime_key,
                    anime_post_id=getattr(record, "anime_post_id", None), episode_number=record.episode_number,
                    player_name=_label(name), direct_url=direct, direct_size=size, direct_referer=iframe,
                    page_title_raw=record.page_title_raw,
                    source_language=getattr(record.language, "value", record.language), thumbnail_url=_og_image(page),
                    player_iframe_url=iframe)
            except Exception as exc:                          # one player failing never hides the next one
                tried.append(f"{_label(name)} : erreur {type(exc).__name__}")
    except Exception as exc:
        tried.append(f"lecture de la page impossible ({type(exc).__name__})")
    finally:
        if own:
            http.close()
    raise v1.SourceAccessError(f"{first_error} | autres lecteurs — " + " ; ".join(tried[1:] or ["aucun"]))
