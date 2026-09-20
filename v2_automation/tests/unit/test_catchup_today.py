"""Catch-up of the day at the first check, no duplicates at the next cycle, cycle history."""
import sqlite3
import time
from pathlib import Path

import pytest
from test_discovery import BASE, Site, _cfg, _jobs

from v2_automation import db, discovery
from v2_automation.discovery import DiscoveryScheduler
from v2_automation.timeutil import add_seconds, now_utc


def page(post_id: int, slug: str, dates: dict[int, str], title="Anime Test") -> str:
    """Anime page, newest first, each episode with its own date text like the real source."""
    items = "".join(
        f'<li class="wp-manga-chapter"><a href="{BASE}/anime/{slug}/{slug}-{n}-vostfr/">{title} - {n} VOSTFR</a>'
        f'<span class="chapter-release-date"><i>{raw}</i></span></li>' for n, raw in sorted(dates.items(), reverse=True))
    return (f'<html><body class="single postid-{post_id}"><div class="post-title"><h1>{title}</h1></div>'
            f'<div class="listing-chapters_wrap"><ul>{items}</ul></div></body></html>')


class DatedSite(Site):
    def show(self, slug, post_id, dates, **kw):
        self.pages[f"{BASE}/anime/{slug}/"] = page(post_id, slug, dates, **kw)


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False, factory=db.SafeConnection)   # like production
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


def _add(conn, slug, post_id, title="Anime Test"):
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES (?, ?, 1, ?)",
                 (f"postid:{post_id}", title, f"{BASE}/anime/{slug}/"))
    conn.commit()
    return f"postid:{post_id}"


def _first_check(conn, site, key, **src):
    return discovery.check_anime(conn, _cfg(**src), key, site)


def test_only_episodes_released_today_are_queued_at_the_first_check(conn):
    site = DatedSite()
    site.show("a", 1, {12: "3 seconds ago", 11: "September 12, 2026", 10: "September 5, 2026"})
    key = _add(conn, "a", 1)
    rep = _first_check(conn, site, key, catchup_today=True)
    assert [n["episode_number"] for n in rep["new"]] == [12] and rep["catchup"] == 1 and rep["baseline"] == 2
    assert _jobs(conn, key) == [(12, "queued")]


def test_yesterday_and_unreadable_dates_are_never_published(conn):
    site = DatedSite()
    site.show("a", 1, {5: "1 day ago", 4: "yesterday", 3: "a month ago", 2: "", 1: "September 19, 2026"})
    key = _add(conn, "a", 1)
    rep = _first_check(conn, site, key, catchup_today=True)
    assert rep["new"] == [] and rep["baseline"] == 5 and _jobs(conn, key) == []


def test_the_old_behaviour_is_kept_when_catch_up_is_off(conn):
    site = DatedSite()
    site.show("a", 1, {2: "3 seconds ago", 1: "September 12, 2026"})
    key = _add(conn, "a", 1)
    assert _first_check(conn, site, key)["new"] == []                 # option absent = pure baseline
    assert _jobs(conn, key) == []


def test_several_episodes_of_the_day_are_queued_in_ascending_order(conn):
    site = DatedSite()
    site.show("a", 1, {8: "5 minutes ago", 7: "2 hours ago", 6: "September 12, 2026"})
    key = _add(conn, "a", 1)
    _first_check(conn, site, key, catchup_today=True)
    assert sorted(n for n, _ in _jobs(conn, key)) == [7, 8]


def test_next_cycle_never_detects_the_same_episode_again_only_the_new_one(conn):
    """12:00 detection, 12:30 cycle: the episode already detected is not new any more; the newer one is."""
    site = DatedSite()
    site.show("a", 1, {12: "3 seconds ago", 11: "September 12, 2026"})
    key = _add(conn, "a", 1)
    sched = DiscoveryScheduler(conn, _cfg(catchup_today=True), fetch=site, interval_s=1800)
    sched.tick()
    end = time.monotonic() + 10
    while time.monotonic() < end and not sched.cycles:
        time.sleep(0.02)
    assert _jobs(conn, key) == [(12, "queued")]
    conn.execute("UPDATE control SET cvalue=? WHERE ckey=?", (add_seconds(now_utc(), -1801), discovery.CYCLE_KEY))
    conn.commit()
    site.show("a", 1, {13: "4 seconds ago", 12: "31 minutes ago", 11: "September 12, 2026"})   # E13 appeared
    sched.tick()
    end = time.monotonic() + 10
    while time.monotonic() < end and len(sched.cycles) < 2:
        time.sleep(0.02)
    assert sorted(n for n, _ in _jobs(conn, key)) == [12, 13]            # E12 not duplicated, E13 added once
    assert sched.cycles[1]["new_episodes"] == 1 and sched.cycles[1]["animes"][key]["new"] == 1
    conn.execute("UPDATE control SET cvalue=? WHERE ckey=?", (add_seconds(now_utc(), -1801), discovery.CYCLE_KEY))
    conn.commit()
    sched.tick()                                                          # nothing new: still no duplicate
    end = time.monotonic() + 10
    while time.monotonic() < end and len(sched.cycles) < 3:
        time.sleep(0.02)
    assert sched.cycles[2]["new_episodes"] == 0 and len(_jobs(conn, key)) == 2
    sched.shutdown()


def test_a_worker_that_was_off_for_hours_catches_up_at_start(conn):
    """The slot passed while the program was stopped: at start the cycle is due at once and the day's episode is queued."""
    site = DatedSite()
    site.show("a", 1, {3: "6 hours ago", 2: "September 12, 2026"})     # released early this morning (or yesterday night)
    key = _add(conn, "a", 1)
    conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?)",
                 (discovery.CYCLE_KEY, add_seconds(now_utc(), -6 * 3600), now_utc()))
    conn.commit()                                                         # last cycle was 6 h ago
    sched = DiscoveryScheduler(conn, _cfg(catchup_today=True), fetch=site, interval_s=1800)
    assert sched.tick() == [key]                                          # no waiting for a slot
    end = time.monotonic() + 10
    while time.monotonic() < end and not sched.cycles:
        time.sleep(0.02)
    from v2_automation.release_date import released_today
    assert bool(_jobs(conn, key)) == released_today("6 hours ago")        # midnight rule decides, not the clock slot
    sched.shutdown()


def test_cycle_history_is_kept_newest_first_and_capped(conn):
    site = DatedSite()
    site.show("a", 1, {1: "September 12, 2026"})
    _add(conn, "a", 1)
    sched = DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    for i in range(discovery.CYCLES_KEPT + 3):
        conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) ON CONFLICT(ckey) "
                     "DO UPDATE SET cvalue=excluded.cvalue", (discovery.CYCLE_KEY, add_seconds(now_utc(), -1801), now_utc()))
        conn.commit()
        sched.tick()
        end = time.monotonic() + 10
        while time.monotonic() < end and len(sched.cycles) < i + 1:
            time.sleep(0.01)
    hist = discovery.recent_cycles(conn)
    assert len(hist) == discovery.CYCLES_KEPT and hist[0]["started_at"] >= hist[-1]["started_at"]
    assert discovery.last_cycle(conn)["started_at"] == hist[0]["started_at"]
    sched.shutdown()
