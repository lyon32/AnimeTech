"""Lance le bot utilisateur seul (long-poll) sur une base DEDIEE de test : la base de production n'est pas migree ni touchee.
Pas de worker : les demandes sont enregistrees et mises en file, rien n'est telecharge.  Arret : Ctrl+C."""
import logging
import sqlite3
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from v2_automation import app_config, db
from v2_automation.user_bot import run_user_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
for noisy in ("httpx", "httpcore", "telegram"):
    logging.getLogger(noisy).setLevel(logging.WARNING)      # httpx logs the full URL, which contains the bot token
cfg = app_config.load_config()
path = ROOT / "data" / "v2_userbot_test.sqlite3"
conn = sqlite3.connect(str(path), check_same_thread=False, factory=db.SafeConnection)
conn.row_factory = sqlite3.Row
db.migrate(conn)
print(f"bot utilisateur demarre (base {path.name}, canaux requis {cfg.required_channels})", flush=True)
run_user_bot(cfg, threading.Event(), conn=conn)
