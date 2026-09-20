"""GLOBAL WATCHER — real end-to-end run.

* REAL source: the anime pages of voir-anime.to (several anime, fetched over HTTP by the production fetcher).
* The "new episode" event cannot be waited for, so the source is presented to the watcher in TWO states:
  cycle N shows some anime WITHOUT their newest episode (they are bootstrapped as known), later cycles show the
  real pages -> the newest episode of those anime is genuinely new for the database.  Nothing else is faked:
  same scheduler, same discovery diff, same queues, same DownloadManager (real HLS download, ffprobe validation,
  thumbnail), same Telegram publisher and Local Bot API server.
* SAFE delivery: a temporary database (the production database is never touched) and the videos are delivered to
  the OWNER'S PRIVATE CHAT through the admin bot, not to the public channel.
* Cycle interval: V2_TEST_MODE=1 (source.test_poll_interval_seconds, 10 s) instead of 1800 s — same code path.

Usage:  python scripts/global_watcher_e2e.py [--anime 6] [--hide 3] [--deadline-min 75]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["V2_TEST_MODE"] = "1"
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent))

from v2_automation import app_config, db, discovery, logsetup, repo, worker   # noqa: E402
from v2_automation.discovery import DiscoveryScheduler                           # noqa: E402

TERMINAL_OK = ("cleanup_pending", "published", "cleaned")
LI = re.compile(r'<li[^>]*class="[^"]*wp-manga-chapter[^"]*"[^>]*>.*?</li>', re.S)


def hide_newest_episode(html: str) -> str:
    """The source page as it was before its newest episode appeared (pages list newest first)."""
    m = LI.search(html)
    return html[:m.start()] + html[m.end():] if m else html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anime", type=int, default=6)
    ap.add_argument("--hide", type=int, default=3)
    ap.add_argument("--deadline-min", type=int, default=75)
    args = ap.parse_args()

    base_cfg = app_config.load_config()
    if not (base_cfg.admin_bot_token and base_cfg.admin_telegram_ids):
        print("ADMIN_BOT_TOKEN / ADMIN_TELEGRAM_IDS manquants : livraison privée impossible")
        return 2
    owner_chat = str(base_cfg.admin_telegram_ids[0])
    # publishing bot -> admin bot, channel -> the owner's private chat, no admin push during the run
    cfg = dataclasses.replace(base_cfg, bot_token=base_cfg.admin_bot_token, channel_id=owner_chat, admin_telegram_ids=[])
    assert discovery.poll_interval_s(cfg) == 10, "test interval expected (V2_TEST_MODE=1)"

    out_dir = ROOT / "output" / "evidence" / "global_watcher"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"e2e_{stamp}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()])
    for h in logging.getLogger().handlers:
        for f in logsetup.token_filters(base_cfg):
            h.addFilter(f)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    log = logging.getLogger("e2e")

    # ── real anime from the source's homepage ──
    real_fetch = discovery.default_fetch(cfg)
    home = real_fetch(cfg.source["base_url"])
    slugs: list[str] = []
    for m in re.finditer(r'href="https?://[^"/]+/anime/([a-z0-9\-]+)/"', home):
        if m.group(1) not in slugs:
            slugs.append(m.group(1))
    from source_audit.analysis.anime import parse_anime_page
    chosen = []
    for slug in slugs:
        url = f"{cfg.source['base_url'].rstrip('/')}/anime/{slug}/"
        try:
            rec = parse_anime_page(real_fetch(url), url)
        except Exception as exc:
            log.warning("page illisible %s: %s", slug, exc)
            continue
        if rec.post_id and len(rec.episode_links) >= 3:
            chosen.append((slug, url, rec.title or slug, rec.post_id, len(rec.episode_links)))
        if len(chosen) == args.anime:
            break
    if len(chosen) < args.hide + 1:
        print("pas assez d'anime exploitables sur la source")
        return 2
    hidden = {c[0] for c in chosen[1:1 + args.hide]}              # first anime stays complete (control), then `hide` of them
    log.info("anime surveillés: %s", [(c[0], c[4]) for c in chosen])
    log.info("état 1 (sans le dernier épisode): %s", sorted(hidden))

    # ── isolated database + watch list ──
    tmp = Path(tempfile.mkdtemp(prefix="global_watcher_"))
    conn = sqlite3.connect(str(tmp / "e2e.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.migrate(conn)
    for slug, url, title, post_id, _ in chosen:
        conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES (?, ?, 1, ?)",
                     (f"postid:{post_id}", title, url))
    conn.commit()
    key_of = {c[0]: f"postid:{c[3]}" for c in chosen}

    state = {"reveal": False, "fetches": {}}

    def source(url: str) -> str:
        page = real_fetch(url)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        state["fetches"][slug] = state["fetches"].get(slug, 0) + 1
        return page if (state["reveal"] or slug not in hidden) else hide_newest_episode(page)

    sched = DiscoveryScheduler(conn, cfg, fetch=source)
    stop = threading.Event()
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(worker.run_worker(cfg, conn, stop=stop, discovery=sched,
                                                                        cleanup_every_s=10 ** 6, reconcile_every_s=60)))
    t0 = time.monotonic()
    t.start()

    def wait(cond, what, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(2)
        log.error("délai dépassé: %s", what)
        return False

    ok_flow = wait(lambda: len(sched.cycles) >= 1, "cycle 1", 120)
    cycle1 = dict(sched.cycles[0]) if sched.cycles else {}
    log.info("CYCLE 1 (bootstrap): %s", {k: v for k, v in cycle1.items() if k != "animes"})
    jobs_after_c1 = conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0]

    state["reveal"] = True                                          # the newest episodes "appear" on the source
    ok_flow = ok_flow and wait(lambda: len(sched.cycles) >= 2, "cycle 2", 120)
    cycle2 = dict(sched.cycles[1]) if len(sched.cycles) > 1 else {}
    log.info("CYCLE 2 (incrémental): %s", {k: v for k, v in cycle2.items() if k != "animes"})

    def settled() -> bool:
        rows = conn.execute("SELECT status, last_error FROM episodes WHERE status <> 'discovered'").fetchall()
        return bool(rows) and all(r["status"] in TERMINAL_OK or r["status"] == "failed" or
                                  (r["status"] == "retry_wait" and (r["last_error"] or "").startswith("NOT_AVAILABLE_YET"))
                                  for r in rows)
    settled_ok = wait(settled, "jobs terminés", args.deadline_min * 60)
    cycles_seen = len(sched.cycles)
    log.info("cycles exécutés: %d (le watcher a continué pendant les téléchargements)", cycles_seen)
    stop.set()
    t.join(600)

    # ── evidence ──
    rows = conn.execute("""SELECT e.id, e.anime_key, a.title, e.episode_number, e.status, e.file_size, e.thumbnail_message_id,
                                  e.video_message_id, e.retry_count, e.last_error, e.file_path
                           FROM episodes e JOIN animes a ON a.anime_key=e.anime_key WHERE e.status <> 'discovered'
                           ORDER BY e.anime_key, e.episode_number""").fetchall()
    per_anime = {}
    for slug, url, title, post_id, listed in chosen:
        k = f"postid:{post_id}"
        c1 = cycle1.get("animes", {}).get(k, {})
        c2 = cycle2.get("animes", {}).get(k, {})
        per_anime[title] = {"anime_key": k, "listed_on_source": listed, "hidden_in_cycle_1": slug in hidden,
                            "cycle1_discovered": c1.get("discovered"), "cycle1_new": c1.get("new"),
                            "cycle2_discovered": c2.get("discovered"), "cycle2_new": c2.get("new"),
                            "jobs": [dict(r) for r in rows if r["anime_key"] == k]}
    publications = conn.execute("SELECT episode_id, publication_type, message_id, status FROM publications ORDER BY id").fetchall()
    summary = {
        "watched_animes": len(chosen), "checked_animes_cycle1": cycle1.get("checked"), "checked_animes_cycle2": cycle2.get("checked"),
        "episodes_listed_total_cycle2": sum((v or {}).get("discovered") or 0 for v in cycle2.get("animes", {}).values()),
        "new_episodes_cycle1": cycle1.get("new_episodes"), "jobs_after_cycle1": jobs_after_c1,
        "new_episodes_cycle2": cycle2.get("new_episodes"), "jobs_created_cycle2": cycle2.get("jobs_created"),
        "parallel_downloads_max": result.get("active_max"), "cycles_executed": cycles_seen,
        "published_episodes": sum(1 for r in rows if r["status"] in TERMINAL_OK),
        "retry_wait": sum(1 for r in rows if r["status"] == "retry_wait"), "failed": sum(1 for r in rows if r["status"] == "failed"),
        "publications_rows": len(publications), "errors": [r["last_error"] for r in rows if r["last_error"]],
        "settled": settled_ok, "flow_ok": ok_flow, "duration_s": round(time.monotonic() - t0),
        "delivery": "private chat of the owner (admin bot), not the public channel", "interval_s": discovery.poll_interval_s(cfg),
    }
    doc = {"summary": summary, "per_anime": per_anime, "publications": [dict(p) for p in publications],
           "fetch_counts": state["fetches"], "worker_stats": {k: v for k, v in result.items() if k != "lease"}}
    (out_dir / f"e2e_{stamp}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))

    for r in rows:                                                  # free the disk: these files were this run's own
        if r["file_path"]:
            Path(r["file_path"]).unlink(missing_ok=True)
    conn.close()
    return 0 if (ok_flow and settled_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
