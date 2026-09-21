"""Shared helpers for the core-media-engine / user-request tests (fake source, config, clock)."""
import threading

from v2_automation.app_config import AppConfig, BotCapacity

BASE = "https://voir-anime.to"


def make_page(post_id: int, slug: str, episodes: list[int], *, title="Anime Test", lang="vostfr") -> str:
    items = "".join(
        f'<li class="wp-manga-chapter"><a href="{BASE}/anime/{slug}/{slug}-{n}-{lang}/">{title} - {n} {lang.upper()}</a>'
        f'<span class="chapter-release-date"><i>September 1, 2026</i></span></li>'
        for n in sorted(episodes, reverse=True))
    return (f'<html><body class="single postid-{post_id}"><div class="post-title"><h1>{title}</h1></div>'
            f'<div class="listing-chapters_wrap"><ul>{items}</ul></div></body></html>')


def cfg(**over) -> AppConfig:
    base = dict(source={"base_url": BASE + "/"}, queues={}, downloads={}, telegram={}, publication={}, limits={},
                monitoring={}, logging={}, bot_token="", channel_id="", admin_telegram_ids=[],
                bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))
    base.update(over)
    return AppConfig(**base)


def search_page(items: list[tuple]) -> str:
    """Search-results page with the markup the real parser reads (see source_audit/output/raw/search_*.html).
    items: (title, url) or (title, url, kind, latest_episode)."""
    rows = ""
    for it in items:
        title, url = it[0], it[1]
        kind = it[2] if len(it) > 2 else None
        last = it[3] if len(it) > 3 else None
        typ = (f'<div class="post-content_item"><div class="summary-heading"><h5>Type</h5></div>'
               f'<div class="summary-content">{kind}</div></div>') if kind else ""
        lc = (f'<div class="tab-meta"><div class="meta-item latest-chap"><span class="font-meta chapter"><a href="{url}">{last}</a></span></div></div>'
              if last is not None else "")
        rows += (f'<div class="row c-tabs-item__content"><div class="tab-summary"><div class="post-title"><h3 class="h4">'
                 f'<a href="{url}">{title}</a></h3></div><div class="post-content">{typ}</div></div>{lc}</div>')
    return f"<html><body>{rows}</body></html>"


def asp_response(items: list[tuple]) -> str:
    """An Ajax Search Pro answer as the site sends it: HTML block + JSON block.  items: (title, url)."""
    import json as _json
    html = "".join(f"<div class='item'><h3><a class=\"asp_res_url\" href='{it[1]}'>{it[0]}</a></h3></div>" for it in items)
    data = {"results_count": len(items), "full_results_count": len(items),
            "results": [{"title": it[0], "post_title": it[0], "link": it[1], "relevance": 1000 - i, "content": ""} for i, it in enumerate(items)]}
    return f"___ASPSTART_HTML___{html}___ASPEND_HTML______ASPSTART_DATA___{_json.dumps(data)}___ASPEND_DATA___"


class Site:
    """A fake source: pages per URL, editable between calls; `searches` maps a query to its result hits."""

    def __init__(self):
        self.pages, self.calls, self.searches = {}, [], {}
        self._lock = threading.Lock()

    def add_search(self, query: str, items: list[tuple[str, str]]):
        self.searches[query.lower()] = items

    def set(self, slug, post_id, episodes, **kw):
        self.pages[f"{BASE}/anime/{slug}/"] = make_page(post_id, slug, episodes, **kw)

    def url(self, slug):
        return f"{BASE}/anime/{slug}/"

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if "?s=" in url:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(url).query)["s"][0].lower()
            page = 2 if "/page/" in url else 1
            return search_page(self.searches.get(q, []) if page == 1 else [])
        return self.pages[url]


class Clock:
    """Controllable UTC clock (no real waiting in expiry tests)."""

    def __init__(self, start="2026-09-21T10:00:00Z"):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        from v2_automation.timeutil import add_seconds
        self.t = add_seconds(self.t, seconds)
