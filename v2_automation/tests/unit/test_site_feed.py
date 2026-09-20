"""Site-wide detection through the "latest episodes" feed: unknown anime are added and their episode of the day
is queued; nothing is duplicated; yesterday / unreadable dates are ignored; a failing page never stops the cycle."""
import logging
import sqlite3
import time
from pathlib import Path

import pytest
from test_catchup_today import page as anime_page
from test_discovery import BASE, Site, _cfg, _jobs

from v2_automation import db, discovery, site_feed
from v2_automation.discovery import DiscoveryScheduler
from v2_automation.timeutil import add_seconds, now_utc


def feed_page(entries) -> str:
    """Homepage like the real one: one block per anime with its latest episodes and their date text."""
    blocks = []
    for slug, title, eps in entries:                        # eps: [(number, date text)], newest first
        items = "".join(f'<div class="chapter-item"><a class="btn-link" href="{BASE}/anime/{slug}/{slug}-{n}-vostfr/">{n}</a>'
                        f'<span class="post-on">{raw}</span></div>' for n, raw in eps)
        blocks.append(f'<div class="page-item-detail video"><div class="item-thumb"><img src="{BASE}/{slug}.jpg"></div>'
                      f'<div class="item-summary"><div class="post-title"><a href="{BASE}/anime/{slug}/">{title}</a></div>'
                      f'<div class="list-chapter">{items}</div></div></div>')
    return "<html><body>" + "".join(blocks) + "</body></html>"


class FeedSite(Site):
    def home(self, entries, page=1):
        self.pages[BASE + "/" if page == 1 else f"{BASE}/page/{page}/"] = feed_page(entries)

    def anime(self, slug, post_id, dates, title="Anime"):
        self.pages[f"{BASE}/anime/{slug}/"] = anime_page(post_id, slug, dates, title=title)

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if url in getattr(self, "broken", set()):
            raise discovery.DiscoveryError("HTTP 503")
        return self.pages[url]


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _cfg_feed(**kw):
    cfg = _cfg(site_feed_enabled=True, catchup_today=True, max_pages=3, **kw)
    return cfg


def _cycle(conn, site, n, cfg=None):
    """Run global cycle number n (the previous cycle is aged past the 30-minute interval)."""
    sched = getattr(_cycle, "sched", None)
    if sched is None or sched.conn is not conn:
        sched = DiscoveryScheduler(conn, cfg or _cfg_feed(), fetch=site, interval_s=1800)
        _cycle.sched = sched
    if n > 1:
        conn.execute("UPDATE control SET cvalue=? WHERE ckey=?", (add_seconds(now_utc(), -1801), discovery.CYCLE_KEY))
        conn.commit()
    sched.tick()
    end = time.monotonic() + 15
    while time.monotonic() < end and len(sched.cycles) < n:
        time.sleep(0.02)
    assert len(sched.cycles) >= n, "cycle did not finish"
    return sched.cycles[n - 1]


def test_an_unknown_anime_posted_a_minute_ago_is_added_and_its_episode_of_the_day_is_queued(conn):
    _cycle.sched = None
    site = FeedSite()
    site.home([("iron-wok-jan", "Iron Wok Jan!", [(12, "1 minute ago"), (11, "September 13, 2026")]),
               ("digimon-beatbreak", "Digimon Beatbreak", [(48, "1 minute ago"), (47, "September 13, 2026")])])
    site.anime("iron-wok-jan", 501, {12: "1 minute ago", 11: "September 13, 2026", 10: "September 6, 2026"}, "Iron Wok Jan!")
    site.anime("digimon-beatbreak", 502, {48: "1 minute ago", 47: "September 13, 2026"}, "Digimon Beatbreak")
    assert conn.execute("SELECT COUNT(*) FROM animes").fetchone()[0] == 0               # nothing configured at all
    c = _cycle(conn, site, 1)
    assert c["feed"]["new_anime"] == 2 and c["feed"]["today"] == 2 and c["feed"]["entries"] == 4
    keys = {r["anime_key"] for r in conn.execute("SELECT anime_key FROM animes")}
    assert keys == {"postid:501", "postid:502"} and all(site_feed.is_auto_added(conn, k) for k in keys)
    assert _jobs(conn, "postid:501") == [(12, "queued")] and _jobs(conn, "postid:502") == [(48, "queued")]   # only the day's
    assert c["new_episodes"] == 2 and c["checked"] == 2                                  # visited in the same cycle


def test_the_next_cycle_never_detects_an_episode_again_only_the_new_one(conn):
    _cycle.sched = None
    site = FeedSite()
    site.home([("iron-wok-jan", "Iron Wok Jan!", [(12, "1 minute ago")])])
    site.anime("iron-wok-jan", 501, {12: "1 minute ago", 11: "September 13, 2026"})
    _cycle(conn, site, 1)
    assert _jobs(conn, "postid:501") == [(12, "queued")]
    site.home([("iron-wok-jan", "Iron Wok Jan!", [(12, "31 minutes ago")]),                # E12 is still on the feed
               ("digimon-beatbreak", "Digimon Beatbreak", [(48, "2 minutes ago")])])
    site.anime("digimon-beatbreak", 502, {48: "2 minutes ago", 47: "September 13, 2026"})
    c = _cycle(conn, site, 2)
    assert _jobs(conn, "postid:501") == [(12, "queued")]                                  # no duplicate of E12
    assert c["feed"]["already_known"] == 1 and c["feed"]["new_anime"] == 1
    assert _jobs(conn, "postid:502") == [(48, "queued")]
    c3 = _cycle(conn, site, 3)                                                            # nothing new: nothing created
    assert c3["new_episodes"] == 0 and c3["feed"]["new_anime"] == 0 and len(_jobs(conn)) == 2


def test_yesterday_and_unreadable_dates_are_ignored(conn):
    _cycle.sched = None
    site = FeedSite()
    site.home([("old-one", "Old One", [(5, "1 day ago"), (4, "September 12, 2026")]),
               ("odd-one", "Odd One", [(3, "a while ago"), (2, "")])])
    c = _cycle(conn, site, 1)
    assert c["feed"]["today"] == 0 and c["feed"]["new_anime"] == 0
    assert conn.execute("SELECT COUNT(*) FROM animes").fetchone()[0] == 0


def test_an_episode_already_known_through_a_watched_anime_is_not_created_twice(conn):
    _cycle.sched = None
    site = FeedSite()
    site.anime("bleach", 88795, {49: "2 minutes ago", 48: "September 13, 2026"}, "Bleach")
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:88795', 'Bleach', 1, ?)",
                 (f"{BASE}/anime/bleach/",))
    conn.commit()
    site.home([("bleach", "Bleach", [(49, "2 minutes ago")])])
    _cycle(conn, site, 1)
    assert _jobs(conn, "postid:88795") == [(49, "queued")]
    assert conn.execute("SELECT COUNT(*) FROM animes").fetchone()[0] == 1                # not re-added as "auto"
    assert not site_feed.is_auto_added(conn, "postid:88795")
    site.home([("bleach", "Bleach", [(49, "32 minutes ago")])])
    c = _cycle(conn, site, 2)
    assert c["feed"]["already_known"] == 1 and len(_jobs(conn, "postid:88795")) == 1


def test_the_same_anime_under_another_address_is_not_added_twice(conn):
    _cycle.sched = None
    site = FeedSite()
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:501', 'Iron Wok Jan!', 1, ?)",
                 (f"{BASE}/anime/iron-wok-jan-old/",))
    conn.commit()
    site.home([("iron-wok-jan", "Iron Wok Jan!", [(12, "1 minute ago")])])
    site.anime("iron-wok-jan", 501, {12: "1 minute ago"}, "Iron Wok Jan!")             # same post id, new address
    site.pages[f"{BASE}/anime/iron-wok-jan-old/"] = site.pages[f"{BASE}/anime/iron-wok-jan/"]
    _cycle(conn, site, 1)
    assert conn.execute("SELECT COUNT(*) FROM animes WHERE anime_key='postid:501'").fetchone()[0] == 1


def test_a_failing_feed_page_never_stops_the_cycle_or_the_other_pages(conn, caplog):
    caplog.set_level(logging.INFO)
    _cycle.sched = None
    site = FeedSite()
    site.home([("a-one", "A One", [(3, "5 minutes ago")])], page=1)
    site.home([("b-two", "B Two", [(9, "3 hours ago")])], page=3)
    site.broken = {f"{BASE}/page/2/"}
    site.anime("a-one", 601, {3: "5 minutes ago"}, "A One")
    site.anime("b-two", 602, {9: "3 hours ago"}, "B Two")
    c = _cycle(conn, site, 1)
    assert c["feed"]["pages"] == 2 and c["feed"]["errors"] == 1 and c["feed"]["new_anime"] == 2
    assert any("site_feed pages=2 entries=2 today=2 new_anime=2" in r.getMessage() for r in caplog.records)


def test_the_feed_can_be_switched_off(conn):
    _cycle.sched = None
    site = FeedSite()
    site.home([("a-one", "A One", [(3, "5 minutes ago")])])
    cfg = _cfg(catchup_today=True)                                   # site_feed_enabled absent = off
    c = _cycle(conn, site, 1, cfg=cfg)
    assert c["feed"] is None and conn.execute("SELECT COUNT(*) FROM animes").fetchone()[0] == 0
    assert BASE + "/" not in site.calls


def test_feed_urls_follow_max_pages():
    assert site_feed.feed_urls(_cfg(max_pages=3)) == [f"{BASE}/", f"{BASE}/page/2/", f"{BASE}/page/3/"]
    assert site_feed.feed_urls(_cfg(max_pages=1)) == [f"{BASE}/"]
    assert site_feed.anime_page_url(_cfg(), f"{BASE}/anime/x-y/x-y-12-vostfr/") == f"{BASE}/anime/x-y/"
    assert site_feed.anime_page_url(_cfg(), "https://other.example/none") is None


def test_panels_show_the_site_feed_the_auto_added_anime_and_the_player(conn):
    from v2_automation import admin_views, service, web_data
    _cycle.sched = None
    site = FeedSite()
    site.home([("iron-wok-jan", "Iron Wok Jan!", [(12, "1 minute ago")])])
    site.anime("iron-wok-jan", 501, {12: "1 minute ago"}, "Iron Wok Jan!")
    _cycle(conn, site, 1)
    cfg = _cfg_feed()
    cyc = web_data.cycles(conn, cfg)["items"][0]
    assert cyc["feed"]["new_anime"] == 1 and cyc["feed"]["today"] == 1
    text = str(admin_views.cycles_view(conn, cfg).text)
    assert "site : 1 épisode(s) du jour · 1 nouvel(s) anime" in text
    assert service.anime_list(conn)[0]["auto_added"] == 1
    assert "🌐" in str(admin_views.anime_view(conn, cfg).text)
    eid = conn.execute("SELECT id FROM episodes WHERE status='queued'").fetchone()["id"]
    conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, 'Stape', 'x')", (f"player:{eid}",))
    conn.commit()
    assert web_data.episode_detail(conn, eid)["player"] == "Stape"
    assert service.waiting_reason({"status": "retry_wait", "last_error": "SOURCE_VIDEO_PROCESSING: 404"}) == \
        "vidéo en cours de préparation par la source"
