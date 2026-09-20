"""MediaMetadata + the Telegram description built from it.

Values come from what the source and ffprobe actually report; nothing is
hard-coded when the source provides it.  The description never contains a
URL (source, page, player or manifest) — it is independent of the input URL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*Voiranime\s*$", re.IGNORECASE)
_SLUG_RE = re.compile(r"^(?P<anime>.+?)-(?:\d+|film|oav|ova)[-\w]*$")


@dataclass
class MediaMetadata:
    title: str
    episode: int | None = None
    media_type: str = "episode"          # "episode" | "film"
    language: str | None = None          # "VF" | "VOSTFR" | None (unknown)
    quality: str | None = None           # e.g. "1080p" (from the validated file)
    duration: float | None = None        # seconds (from the validated file)
    thumbnail: str | None = None         # local path of the generated thumbnail


def title_from_page(page_title_raw: str | None, fallback_url: str) -> str:
    """Anime title from the episode page's <title> ("<Anime> - <alt> - 48 VOSTFR - 48 - Voiranime");
    falls back to the URL slug when the page gives nothing usable."""
    if page_title_raw:
        head = _TITLE_SUFFIX_RE.sub("", page_title_raw).split(" - ")[0].strip()
        if head:
            return head
    parts = [p for p in urlparse(fallback_url).path.split("/") if p]
    slug = parts[1] if len(parts) > 1 and parts[0] == "anime" else (parts[-1] if parts else "")
    return slug.replace("-", " ").title() or "?"


def detect_language(url: str, source_language: str | None = None) -> str | None:
    """"VF" / "VOSTFR" from the source's own value, else from the URL suffix; None if unknown."""
    if source_language and source_language.upper() in ("VF", "VOSTFR"):
        return source_language.upper()
    low = url.lower().rstrip("/")
    if "-vostfr" in low:
        return "VOSTFR"
    if low.endswith("-vf") or "-vf/" in low or "film-vf" in low:
        return "VF"
    return None


def detect_media_type(url: str, episode_number: int | None) -> str:
    slug = url.lower().rstrip("/").rsplit("/", 1)[-1]
    if episode_number is None and re.search(r"(^|-)film(-|$)", slug):
        return "film"
    return "episode"


def quality_label(height: int | None) -> str | None:
    return f"{height}p" if height else None


def build_metadata(*, episode_url: str, page_title_raw: str | None,
                   episode_number: int | None, source_language: str | None,
                   validation=None, thumbnail: str | None = None) -> MediaMetadata:
    video = (getattr(validation, "video", None) or {}) if validation is not None else {}
    return MediaMetadata(
        title=title_from_page(page_title_raw, episode_url),
        episode=episode_number,
        media_type=detect_media_type(episode_url, episode_number),
        language=detect_language(episode_url, source_language),
        quality=quality_label(video.get("height")),
        duration=getattr(validation, "duration_seconds", None) if validation is not None else None,
        thumbnail=thumbnail,
    )


def build_caption(meta: MediaMetadata) -> str:
    """Telegram description. Only content facts; lines whose value is unknown are omitted."""
    if meta.media_type == "film":
        kind = "Film"
    else:
        kind = f"Épisode {meta.episode}" if meta.episode is not None else "Épisode"
    lines = [f"🎬 {meta.title}", "", f"📺 {kind}"]
    if meta.language:
        lines.append(f"🎙️ {meta.language}")
    if meta.quality:
        lines.append(f"🎞️ {meta.quality}")
    lines += ["", "━━━━━━━━━━━━━━", "", "📥 Disponible maintenant"]
    return "\n".join(lines)


# ── anime info card (thumbnail caption) ─────────────────────────────────────

TELEGRAM_CAPTION_LIMIT = 1024
_ELLIPSIS = "…"


@dataclass
class AnimeInfo:
    native: str | None = None
    romaji: str | None = None
    english: str | None = None
    type: str | None = None
    status: str | None = None
    studios: str | None = None
    start_date: str | None = None
    genres: list[str] | None = None
    synopsis: str | None = None

    def is_empty(self) -> bool:
        return not any((self.native, self.romaji, self.english, self.type, self.status,
                        self.studios, self.start_date, self.genres, self.synopsis))


def anime_page_url(episode_url: str) -> str | None:
    """`https://host/anime/<slug>/<episode>/` -> `https://host/anime/<slug>/`."""
    u = urlparse(episode_url)
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "anime":
        return f"{u.scheme}://{u.netloc}/anime/{parts[1]}/"
    return None


def parse_anime_info(html: str) -> AnimeInfo:
    """Facts shown on the anime page; anything the page does not give stays None."""
    from bs4 import BeautifulSoup
    from source_audit.analysis.anime import _extract_metadata_fields
    soup = BeautifulSoup(html, "lxml")
    f = _extract_metadata_fields(soup)
    synopsis_el = soup.select_one(".description-summary")
    genres = [g.strip() for g in re.split(r"\s*,\s*", f.get("Genre(s)", "")) if g.strip()]
    return AnimeInfo(
        native=f.get("Native") or None, romaji=f.get("Romaji") or None,
        english=f.get("English") or None, type=f.get("Type") or None,
        status=f.get("Status") or None, studios=f.get("Studios") or None,
        start_date=f.get("Start date") or None, genres=genres or None,
        synopsis=re.sub(r"\s+", " ", synopsis_el.get_text(" ", strip=True)).strip() or None
        if synopsis_el else None)


def build_thumbnail_caption(info: AnimeInfo | None, tag: str | None = None) -> str | None:
    """Info card for the thumbnail message (<= 1024 chars; the synopsis is what gets cut)."""
    if info is None or info.is_empty():
        return None
    head: list[str] = []
    if info.english:
        head += [f"Titre alternatif : {info.english}", ""]
    original = " / ".join(x for x in (info.romaji, info.native) if x)
    if original:
        head.append(f"Titre original : {original}")
    kind = " · ".join(x for x in (info.type, f"Statut : {info.status}" if info.status else None) if x)
    if kind:
        head.append(kind)
    facts: list[str] = []
    if info.start_date:
        facts.append(f"Début de diffusion : {info.start_date}")
    if info.genres:
        facts.append("Genres : " + " - ".join(info.genres))
    if info.studios:
        facts.append(f"Studio d'animation : {info.studios}")
    blocks = ["\n".join(head).strip("\n")] if head else []
    blocks += ["\n\n".join(facts)] if facts else []
    tail = f"\n\n{tag}" if tag else ""
    fixed = "\n\n".join(b for b in blocks if b)
    if info.synopsis:
        prefix = (fixed + "\n\n" if fixed else "") + "Synopsis\n"
        room = TELEGRAM_CAPTION_LIMIT - len(prefix) - len(tail)
        syn = info.synopsis
        if room < 40:                       # no room for a useful synopsis
            return (fixed + tail)[:TELEGRAM_CAPTION_LIMIT] or None
        if len(syn) > room:
            syn = syn[: room - 1].rstrip() + _ELLIPSIS
        return prefix + syn + tail
    return (fixed + tail)[:TELEGRAM_CAPTION_LIMIT] or None
