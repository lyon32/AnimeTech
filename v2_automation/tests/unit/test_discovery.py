"""Automatic discovery: baseline, new episodes, dedup, weekly cycle, FIFO order, no-overlap, failures."""
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from v2_automation import alerts, db, discovery, repo, service
from v2_automation.app_config import AppConfig, BotCapacity
from v2_automation.timeutil import add_seconds, now_utc

BASE = "https://voir-anime.to"


def make_page(post_id: int, slug: str, episodes: list[int], *, title="Anime Test", lang="vostfr") -> str:
    """Anime page with the selectors the real parser reads; newest episode first, like the site."""
    items = "".join(
        f'<li class="wp-manga-chapter"><a href="{BASE}/anime/{slug}/{slug}-{n}-{lang}/">{title} - {n} {lang.upper()}</a>'
        f'<span class="chapter-release-date"><i>September 1, 2026</i></span></li>'
        for n in sorted(episodes, reverse=True))
    return (f'<html><body class="single postid-{post_id}"><div class="post-title"><h1>{title}</h1></div>'
            f'<div class="listing-chapters_wrap"><ul>{items}</ul></div></body></html>')


def _cfg(**source) -> AppConfig:
    src = {"base_url": BASE + "/", **source}
    return AppConfig(source=src, queues={}, downloads={}, telegram={}, publication={}, limits={}, monitoring={},
                     logging={}, bot_token="", channel_id="", admin_telegram_ids=[],
                     bot_capacity=BotCapacity(True, True, "t", 10**12, None, None, 200, None))


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "t.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


class Site:
    """A fake source: pages per URL, editable between checks."""
    def __init__(self):
        self.pages, self.calls, self.fail, self.delay = {}, [], False, 0.0
        self._lock = threading.Lock()

    def set(self, slug, post_id, episodes, **kw):
        self.pages[f"{BASE}/anime/{slug}/"] = make_page(post_id, slug, episodes, **kw)

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise discovery.DiscoveryError("HTTP 503 on the anime page")
        return self.pages[url]


def _watch(conn, slug, post_id, title="Anime Test"):
    key = f"postid:{post_id}"
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES (?, ?, 1, ?)",
                 (key, title, f"{BASE}/anime/{slug}/"))
    conn.commit()
    return key


def _jobs(conn, key=None):
    q = "SELECT e.episode_number, e.status FROM queue_items q JOIN episodes e ON e.id=q.episode_id"
    q += " WHERE q.anime_key=? ORDER BY q.position" if key else " ORDER BY q.position"
    return [tuple(r) for r in conn.execute(q, (key,) if key else ()).fetchall()]


# ── baseline: an URL alone never creates jobs ───────────────────────────────────

def test_first_check_is_a_baseline_and_creates_no_job(conn):
    site = Site(); site.set("a", 1, [1, 2, 3])
    key = _watch(conn, "a", 1)
    rep = discovery.check_anime(conn, _cfg(), key, site)
    assert rep["baseline"] == 3 and rep["new"] == []
    assert _jobs(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE status='discovered'").fetchone()[0] == 3
    row = conn.execute("SELECT last_successful_check_at, last_check_error FROM animes").fetchone()
    assert row["last_successful_check_at"] and row["last_check_error"] is None


def test_backfill_latest_on_first_check_queues_only_the_newest_n(conn):
    site = Site(); site.set("a", 1, [1, 2, 3, 4])
    key = _watch(conn, "a", 1)
    rep = discovery.check_anime(conn, _cfg(backfill_latest_on_first_check=2), key, site)
    assert [n["episode_number"] for n in rep["new"]] == [3, 4] and rep["baseline"] == 2


# ── new episodes, dedup, weekly cycle ───────────────────────────────────────────

def test_new_episode_creates_exactly_one_job_and_a_notification(conn):
    site = Site(); site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    seen = []
    alert = lambda kind, akey, title, body="": seen.append((kind, akey))            # noqa: E731
    discovery.check_anime(conn, _cfg(), key, site, alert=alert)                    # baseline
    site.set("a", 1, [1, 2, 3])
    rep = discovery.check_anime(conn, _cfg(), key, site, alert=alert)
    assert [n["episode_number"] for n in rep["new"]] == [3]
    assert _jobs(conn, key) == [(3, "queued")]
    assert [k for k, _ in seen] == [alerts.KIND_NEW_EPISODE]


def test_double_detection_creates_one_episode_one_job(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)                                 # baseline: E01 known
    site.set("a", 1, [1, 2])
    for _ in range(3):                                                             # check #1, #2, #3: E02 present
        discovery.check_anime(conn, _cfg(), key, site)
    assert _jobs(conn, key) == [(2, "queued")]
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE episode_number=2").fetchone()[0] == 1


def test_weekly_cycle_only_the_new_episode_becomes_a_job(conn):
    site = Site(); site.set("a", 1, [1])                       # Thursday: E01 is the newest
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)             # E01 known (baseline)
    site.set("a", 1, [1, 2])                                   # next week: E02, E01 still listed
    rep = discovery.check_anime(conn, _cfg(), key, site)
    assert [n["episode_number"] for n in rep["new"]] == [2] and rep["known"] == 1
    assert _jobs(conn, key) == [(2, "queued")]


def test_several_new_episodes_are_queued_in_ascending_order(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)
    site.set("a", 1, [1, 2, 3, 4])                             # page lists them newest first: 4, 3, 2, 1
    discovery.check_anime(conn, _cfg(), key, site)
    assert [n for n, _ in _jobs(conn, key)] == [2, 3, 4]


def test_different_animes_do_not_share_identity(conn):
    site = Site(); site.set("a", 1, [1]); site.set("b", 2, [1])
    ka, kb = _watch(conn, "a", 1), _watch(conn, "b", 2)
    for k in (ka, kb):
        discovery.check_anime(conn, _cfg(), k, site)
    site.set("a", 1, [1, 2]); site.set("b", 2, [1, 2])
    for k in (ka, kb):
        discovery.check_anime(conn, _cfg(), k, site)
    assert len(_jobs(conn, ka)) == 1 and len(_jobs(conn, kb)) == 1               # A E02 and B E02, both created


def test_detected_episodes_of_several_animes_start_in_parallel_but_fifo_per_anime(conn):
    site = Site()
    for slug, pid in (("a", 1), ("b", 2), ("c", 3)):
        site.set(slug, pid, [1])
    keys = [_watch(conn, s, p) for s, p in (("a", 1), ("b", 2), ("c", 3))]
    for k in keys:
        discovery.check_anime(conn, _cfg(), k, site)
    for slug, pid in (("a", 1), ("b", 2), ("c", 3)):
        site.set(slug, pid, [1, 2, 3] if slug == "a" else [1, 2])
    for k in keys:
        discovery.check_anime(conn, _cfg(), k, site)
    heads = repo.next_heads(conn, 10)
    numbers = sorted(repo.get(conn, h).episode_number for h in heads)
    assert len(heads) == 3 and numbers == [2, 2, 2]              # A, B, C each start their E02; A's E03 waits


# ── scheduling: interval, no overlap, force check ───────────────────────────────

def test_interval_is_30_minutes_in_production_and_short_only_in_test_mode(monkeypatch):
    monkeypatch.delenv("V2_TEST_MODE", raising=False)
    assert discovery.poll_interval_s(_cfg()) == 1800
    assert discovery.poll_interval_s(_cfg(poll_interval_seconds=1800, test_poll_interval_seconds=10)) == 1800
    monkeypatch.setenv("V2_TEST_MODE", "1")
    assert discovery.poll_interval_s(_cfg(poll_interval_seconds=1800, test_poll_interval_seconds=7)) == 7


def test_an_anime_is_not_checked_again_before_the_interval(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    assert sch.tick() == [key]
    sch._pool.shutdown(wait=True)
    assert discovery.due_animes(conn, 1800) == []
    assert not discovery.claim_check(conn, key, 1800)           # second claim in the interval refused
    later = add_seconds(now_utc(), 1801)
    assert discovery.due_animes(conn, 1800, now=later) == [key]


def test_two_simultaneous_checks_of_the_same_anime_are_merged(conn):
    site = Site(); site.set("a", 1, [1]); site.delay = 0.5
    key = _watch(conn, "a", 1)
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=0)     # always due
    assert sch.tick() == [key]
    assert sch.tick() == []                                     # still running: not started twice
    assert sch.check_now(key) == {"anime_key": key, "skipped": "already-running"}
    sch._pool.shutdown(wait=True)
    assert len(site.calls) == 1


def test_different_animes_are_checked_independently(conn):
    site = Site()
    for slug, pid in (("a", 1), ("b", 2), ("c", 3)):
        site.set(slug, pid, [1])
    keys = [_watch(conn, s, p) for s, p in (("a", 1), ("b", 2), ("c", 3))]
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    assert sorted(sch.tick()) == sorted(keys)
    sch._pool.shutdown(wait=True)
    assert len(site.calls) == 3


def test_force_check_runs_before_the_interval_and_only_once(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    sch.tick(); sch._pool.shutdown(wait=True)
    assert discovery.due_animes(conn, 1800) == []
    assert service.request_force_check(conn, key)["ok"]
    assert discovery.due_animes(conn, 1800) == [key]
    sch2 = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=1800)
    assert sch2.tick() == [key]
    sch2._pool.shutdown(wait=True)
    assert discovery.due_animes(conn, 1800) == []               # the flag is consumed


def test_disabled_or_url_less_animes_are_not_checked(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    conn.execute("INSERT INTO animes (anime_key, title, enabled) VALUES ('nourl', 'x', 1)")
    service.set_anime_enabled(conn, key, False)
    assert discovery.due_animes(conn, 0) == []
    service.set_anime_enabled(conn, key, True)
    assert discovery.due_animes(conn, 0) == [key]


# ── failures ─────────────────────────────────────────────────────────────────────

def test_source_failure_is_recorded_alerted_and_retried_next_interval(conn):
    site = Site(); site.set("a", 1, [1]); site.fail = True
    key = _watch(conn, "a", 1)
    seen = []
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=0,
                                       alert=lambda kind, akey, title, body="": seen.append(kind))
    res = sch.check_now(key)
    assert "error" in res and seen == [alerts.KIND_DISCOVERY_ERROR]
    row = conn.execute("SELECT last_successful_check_at, last_check_error FROM animes").fetchone()
    assert row["last_successful_check_at"] is None and "503" in row["last_check_error"]
    site.fail = False                                            # source is back: baseline done, nothing lost
    assert "error" not in sch.check_now(key)
    assert conn.execute("SELECT last_check_error FROM animes").fetchone()[0] is None


def test_a_check_failure_after_baseline_keeps_known_episodes(conn):
    site = Site(); site.set("a", 1, [1, 2])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)
    site.fail = True
    with pytest.raises(discovery.DiscoveryError):
        discovery.check_anime(conn, _cfg(), key, site)
    site.fail = False
    site.set("a", 1, [1, 2, 3])
    assert [n["episode_number"] for n in discovery.check_anime(conn, _cfg(), key, site)["new"]] == [3]


def test_restart_between_checks_does_not_duplicate(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)
    site.set("a", 1, [1, 2])
    discovery.check_anime(conn, _cfg(), key, site)
    # "restart": brand-new scheduler and checker objects over the same database
    sch = discovery.DiscoveryScheduler(conn, _cfg(), fetch=site, interval_s=0)
    sch.check_now(key)
    assert _jobs(conn, key) == [(2, "queued")]


# ── adding an anime (single source of truth: the `animes` table) ────────────────

def test_add_anime_from_url_validates_identifies_saves_and_creates_no_job(conn):
    site = Site(); site.set("nouveau", 77, [1, 2], title="Nouveau Titre")
    res = service.add_anime_from_url(conn, _cfg(), f"{BASE}/anime/nouveau/", fetch=site)
    assert res["ok"] and res["anime_key"] == "postid:77" and res["created"]
    row = conn.execute("SELECT title, enabled, source_url FROM animes WHERE anime_key='postid:77'").fetchone()
    assert row["title"] == "Nouveau Titre" and row["enabled"] == 1 and row["source_url"] == f"{BASE}/anime/nouveau/"
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0 and _jobs(conn) == []
    assert service.add_anime_from_url(conn, _cfg(), f"{BASE}/anime/nouveau/", fetch=site)["created"] is False


@pytest.mark.parametrize("url", ["https://evil.example/anime/x/", f"{BASE}/pas-un-anime/x/", "not a url",
                                 f"{BASE}/anime/x/episode-1/"])
def test_add_anime_rejects_foreign_or_malformed_urls(conn, url):
    res = service.add_anime_from_url(conn, _cfg(), url, fetch=lambda u: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert res["ok"] is False


def test_update_and_list_expose_the_monitoring_fields(conn):
    site = Site(); site.set("a", 1, [1])
    key = _watch(conn, "a", 1)
    discovery.check_anime(conn, _cfg(), key, site)
    assert service.update_anime(conn, key, title="Autre titre", language="vf")["ok"]
    item = service.anime_list(conn)[0]
    assert item["title"] == "Autre titre" and item["language"] == "vf" and item["last_successful_check_at"]
    assert item["source_url"].endswith("/anime/a/")
