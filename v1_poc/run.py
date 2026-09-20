#!/usr/bin/env python
"""Direct test entry for the real pipeline:

    python v1_poc/run.py --url "<EPISODE_URL>"

URL -> Source -> Rendition -> Playlist -> Download -> Validation -> Thumbnail -> Telegram.

This is NOT a parallel pipeline: the URL is registered as an Episode in the
V2 database, queued, claimed and processed by the same `DownloadManager` the
worker uses (retry window, dedup, recovery, cleanup, admin surfaces all apply).
Secrets come from .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, and optionally
TELEGRAM_API_BASE_URL); nothing is hard-coded and the URL is never published.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("v1_poc", "v2_automation", "source_audit"):
    sys.path.insert(0, str(ROOT / sub / "src"))

SOURCE = "voir-anime.to"
DONE = ("published", "cleanup_pending", "cleaned")


def _register(conn, cfg, url: str):
    """Discovery step for a single URL: fetch identity from the page, upsert + queue the Episode."""
    from source_audit.analysis.episode import parse_episode_page
    from source_audit.analysis.identity import build_episode_key
    from source_audit.fetch.http_client import HttpClient
    from v2_automation import repo, service
    from v2_automation.metadata import detect_language, title_from_page
    from v2_automation.models import Episode
    from v2_automation.queues import QueueManager

    episode_key = build_episode_key(url)
    known = repo.get_by_episode_key(conn, SOURCE, episode_key)
    if known is not None:
        return known, False

    with HttpClient(timeout_seconds=20, max_retries=2, retry_backoff_seconds=1.5,
                    user_agent="v1_poc-research-bot/0.1 (+contact: lionelyvan24@gmail.com)") as client:
        page = client.get(url)
    if not page.ok:
        raise RuntimeError(f"SOURCE_EXTRACTION_FAILED: episode page HTTP {page.status_code} ({page.error_type.value})")
    rec = parse_episode_page(page.text, url)
    anime_key = rec.anime_key or f"slug:{url.rstrip('/').split('/')[-2]}"
    ep = Episode(anime_key=anime_key, episode_key=episode_key, source=SOURCE,
                 canonical_episode_url=episode_key + "/",
                 language=(detect_language(url) or "unknown").lower(),
                 episode_number=rec.episode_number, episode_url=url)
    eid, _ = repo.upsert_episode(conn, ep)
    repo.transition(conn, eid, "identified")
    repo.transition(conn, eid, "queued")
    service.upsert_anime(conn, anime_key, title=title_from_page(rec.page_title_raw, url))
    QueueManager(conn).enqueue(anime_key, eid)
    conn.commit()
    return repo.get(conn, eid), True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="episode/film page URL")
    args = ap.parse_args(argv)

    from v2_automation import app_config, db, evidence, recovery, repo, service
    from v2_automation.downloader import DownloadManager, EpisodeAlreadyDone, default_deps
    from v2_automation.queues import QueueManager

    cfg = app_config.load_config()
    if not cfg.bot_token or not cfg.channel_id:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID missing in .env", file=sys.stderr)
        return 2

    conn = db.connect()
    db.migrate(conn)
    recovery.run_recovery(conn, cfg)          # restart safety: repairs mid-flight rows, never re-publishes

    try:
        ep, created = _register(conn, cfg, args.url)
    except Exception as exc:
        print(f"[1/7] Source — FAILED: {exc}", file=sys.stderr)
        return 1

    if ep.status in DONE or ep.video_message_id:
        print(f"Already published (video_message_id={ep.video_message_id}, "
              f"thumbnail_message_id={ep.thumbnail_message_id}) — nothing re-sent.")
        return 0
    if ep.status == "failed" and "MANUELLE" in (ep.last_error or ""):
        print("Refused: publication was interrupted by a crash — a message may already be live. "
              "Check the channel, then re-queue manually from the admin.", file=sys.stderr)
        return 1
    qm = QueueManager(conn)
    if ep.status in ("retry_wait", "failed", "structure_changed", "blocked"):
        if ep.status != "retry_wait":
            service.requeue_episode(conn, ep.id)   # explicit manual re-run of this URL
        qm.unqueue(ep.anime_key, ep.id, "queued")  # item was left 'processing' by the previous run
    if not qm.dequeue_episode(ep.id):
        print(f"Episode {ep.id} is not claimable (status={repo.get(conn, ep.id).status}).", file=sys.stderr)
        return 1

    def progress(n: int, label: str, detail: str) -> None:
        print(f"[{n}/7] {label}" + (f" — {detail}" if detail else ""), flush=True)

    mgr = DownloadManager(conn, cfg, default_deps(cfg), progress=progress)
    try:
        status = mgr.process_episode(ep.id)
    except EpisodeAlreadyDone as exc:
        print(f"Already published: {exc}")
        return 0
    final = repo.get(conn, ep.id)
    summary = {
        "episode_id": final.id, "status": status, "anime_key": final.anime_key,
        "episode_number": final.episode_number, "language": final.language,
        "local_file": final.file_path, "file_size": final.file_size,
        "sha256": final.video_sha256, "thumbnail": final.thumbnail_path,
        "thumbnail_message_id": final.thumbnail_message_id,
        "video_message_id": final.video_message_id, "last_error": final.last_error,
    }
    evidence.write_json(evidence.evidence_dir("runs") / f"run_{final.id}.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    ok = status in ("cleanup_pending", "published") and final.video_message_id
    if not ok:
        print(f"FAILED: status={status} error={final.last_error}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
