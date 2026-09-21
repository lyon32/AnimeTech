"""Read-only view of the source for user requests: which episodes an anime page lists.

Reuses the parsers proven in `source_audit` exactly as `discovery.check_anime` does (same episode key, same
episode number), so an episode seen here and the same episode seen by the watcher get the SAME identity.
Nothing is fetched by the parsers themselves: `fetch(url) -> html` is injected (real HTTP by default).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable
import re
from urllib.parse import urljoin


@dataclass(frozen=True)
class SourceEpisode:
    number: int | None
    url: str
    key: str            # canonical episode key (same function as the watcher)
    label: str


class SourceCatalog:
    DETAILS_TTL_S = 1800

    def __init__(self, cfg, fetch: Callable[[str], str] | None = None):
        self.cfg = cfg
        self._fetch = fetch
        self._details: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def fetch(self, url: str) -> str:
        if self._fetch is None:
            from . import discovery
            self._fetch = discovery.default_fetch(self.cfg)
        return self._fetch(url)

    def episodes(self, source_url: str) -> list[SourceEpisode]:
        """Episodes the page lists NOW, ascending by number (unnumbered ones last).  Raises on fetch/parse error."""
        from source_audit.analysis.anime import parse_anime_page
        from source_audit.analysis.identity import build_episode_key, extract_episode_number_from_url
        from . import discovery
        source_url = discovery.validate_source_url(self.cfg, source_url)     # only an anime page of the configured source
        record = parse_anime_page(self.fetch(source_url), source_url)
        out: list[SourceEpisode] = []
        for link in record.episode_links:
            if not link.url:
                continue
            url = urljoin(source_url, link.url)
            out.append(SourceEpisode(extract_episode_number_from_url(url), url, build_episode_key(url), link.label))
        out.sort(key=lambda e: (e.number is None, e.number if e.number is not None else 0))
        return out

    def find(self, source_url: str, number: int) -> SourceEpisode | None:
        return next((e for e in self.episodes(source_url) if e.number == number), None)

    def latest(self, source_url: str) -> SourceEpisode | None:
        """The highest-numbered episode really listed — never a guessed number."""
        numbered = [e for e in self.episodes(source_url) if e.number is not None]
        return max(numbered, key=lambda e: e.number) if numbered else None

    def details(self, source_url: str) -> dict[str, Any]:
        """What the anime page itself says: type, status, episodes listed / declared, and the version its episode labels
        carry ("… 26 VF - 26" / "… VOSTFR").  Cached 30 minutes.  {} when the page cannot be read."""
        with self._lock:
            hit = self._details.get(source_url)
            if hit and time.monotonic() - hit[0] < self.DETAILS_TTL_S:
                return hit[1]
        from source_audit.analysis.anime import parse_anime_page
        from . import discovery
        try:
            url = discovery.validate_source_url(self.cfg, source_url)
            rec = parse_anime_page(self.fetch(url), url)
        except Exception:
            return {}
        labels = " ".join(l.label for l in rec.episode_links[:3]).upper()
        out = {"title": rec.title, "kind": (rec.anime_type_raw or "").upper(), "status": rec.status_raw,
               "listed": len(rec.episode_links), "declared": rec.total_episodes_declared,
               "version": "VOSTFR" if "VOSTFR" in labels else "VF" if re.search(r"VF", labels) else None}
        with self._lock:
            self._details[source_url] = (time.monotonic(), out)
        return out

    def details_many(self, urls: list[str], workers: int = 6) -> dict[str, dict[str, Any]]:
        """`details` for several pages at once (never raises)."""
        urls = list(dict.fromkeys(urls))
        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(urls)))) as pool:
            return dict(zip(urls, pool.map(self.details, urls)))
