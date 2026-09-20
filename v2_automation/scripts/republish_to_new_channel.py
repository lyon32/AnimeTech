#!/usr/bin/env python
"""Republie dans le canal courant (TELEGRAM_CHANNEL_ID) les episodes deja publies aujourd'hui dans l'ancien canal.

Les MP4 sont deja sur disque (retention 14 j) : le pipeline reutilise le cache, aucun retelechargement.
Les episodes sont traites un par un, dans l'ordre chronologique de leur publication initiale.

  python scripts/republish_to_new_channel.py --dry-run
  python scripts/republish_to_new_channel.py

A lancer worker arrete (les deux ne doivent pas traiter les memes episodes).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2_automation import app_config, db, repo, service        # noqa: E402
from v2_automation.downloader import DownloadManager, default_deps   # noqa: E402


def todays_published(conn, channel_id: str):
    """Publies aujourd'hui ailleurs que dans le canal courant (ceux deja postes dedans sont ignores)."""
    lo = service.local_midnight_utc()
    return conn.execute(
        "SELECT id, label, anime_key, episode_number, file_path FROM episodes e "
        "WHERE published_at >= ? AND status IN ('cleanup_pending','published') "
        "AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.episode_id=e.id "
        "AND p.publication_type='first_publication' AND p.chat_id=?) ORDER BY published_at",
        (lo, str(channel_id))).fetchall()


def reset(conn, eid: int) -> None:
    conn.execute("DELETE FROM publications WHERE episode_id=?", (eid,))
    conn.execute("UPDATE episodes SET status='queued', video_message_id=NULL, thumbnail_message_id=NULL, "
                 "published_at=NULL, cleanup_at=NULL, retry_count=0, last_error=NULL, next_retry_at=NULL, "
                 "updated_at=datetime('now') WHERE id=?", (eid,))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = app_config.load_config()
    conn = db.connect()
    rows = todays_published(conn, cfg.channel_id)
    print(f"canal cible: {cfg.channel_id} — {len(rows)} episode(s)")
    missing = [r["id"] for r in rows if not (r["file_path"] and Path(r["file_path"]).exists())]
    for r in rows:
        print(f"  #{r['id']} {r['label'] or r['anime_key']} ep{r['episode_number']}")
    if missing:
        print(f"ATTENTION: MP4 absent pour {missing}")
    if args.dry_run:
        return 0
    deps = default_deps(cfg)
    dm = DownloadManager(conn, cfg, deps)
    ok = 0
    for r in rows:
        eid = r["id"]
        reset(conn, eid)
        try:
            final = dm.process_episode(eid)
        except Exception as exc:                 # noqa: BLE001
            final = f"erreur: {exc}"
        print(f"#{eid} -> {final}", flush=True)
        ok += final in ("published", "cleanup_pending")
    print(f"{ok}/{len(rows)} republie(s)")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
