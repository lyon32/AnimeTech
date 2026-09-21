"""v2_automation CLI entry points."""
from __future__ import annotations

import argparse
import json
import sys

from . import app_config, capacities, db


def cmd_doctor(args):
    payload = capacities.doctor()
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if not payload.get("local_bot_api_enabled"):
        print("AVERTISSEMENT: serveur Local Bot API non joignable — publications impossibles.",
              file=sys.stderr)
        sys.exit(1)


def cmd_init_db(args):
    conn = db.connect()
    applied = db.migrate(conn)
    conn.commit()
    print(f"db OK: {db.default_db_path()} (appliquées: {applied or 'aucune'})")
    caps = capacities.persist()
    print(json.dumps(caps, indent=2, ensure_ascii=False))


def cmd_capacities(args):
    print(json.dumps(capacities.persist(), indent=2, ensure_ascii=False))


def cmd_serve(args):
    cfg = app_config.load_config()
    mon = cfg.monitoring
    host = args.host or mon.get("web_host", "127.0.0.1")
    port = args.port or int(mon.get("web_port", 8085))
    from . import web_auth
    if web_auth.WebAuth.from_env() is None:      # authentication is optional: enabled as soon as ADMIN_WEB_PASSWORD(_HASH) is set
        print("panneau web sans authentification (127.0.0.1 uniquement) : définissez ADMIN_WEB_PASSWORD pour l'activer.")
    import uvicorn
    uvicorn.run("v2_automation.web:app", host=host, port=port, log_level="warning")


def cmd_run_admin(args):
    cfg = app_config.load_config()
    from . import admin_telegram
    print("admin telegram démarré (arrêt: Ctrl+C)")
    admin_telegram.run_admin_loop(cfg)


def cmd_run_worker(args):
    from . import worker
    cfg = app_config.load_config()
    print("worker démarré (instance unique via lease; arrêt: Ctrl+C)")
    stats = worker.run(cfg)
    print(json.dumps(stats, indent=2, ensure_ascii=False))


def cmd_cleanup(args):
    import json as _json
    from . import cleanup, db
    cfg = app_config.load_config()
    conn = db.connect()
    db.migrate(conn)
    res = cleanup.run_cleanup(conn, cfg)
    print(_json.dumps(res, indent=2, ensure_ascii=False))


def cmd_recover(args):
    import json as _json
    from . import db, recovery
    cfg = app_config.load_config()
    conn = db.connect()
    db.migrate(conn)
    res = recovery.run_recovery(conn, cfg)
    print(_json.dumps(res, indent=2, ensure_ascii=False))


def cmd_reconcile(args):
    import json as _json
    from . import db, recovery
    cfg = app_config.load_config()
    conn = db.connect()
    db.migrate(conn)
    print(_json.dumps(recovery.reconcile_uncertain(conn, cfg, wait_s=60), indent=2, ensure_ascii=False))


def _ensure_streams() -> None:
    """Under pythonw (scheduled tasks, no console) sys.stdout / sys.stderr are None: uvicorn's logging and
    print() then crash at startup.  Give them a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    import os
    out = open(app_config.DATA_DIR / "background.log", "a", encoding="utf-8", buffering=1)         if app_config.DATA_DIR.exists() else open(os.devnull, "w")
    sys.stdout = sys.stdout or out
    sys.stderr = sys.stderr or out


def main(argv: list[str] | None = None):
    _ensure_streams()
    ap = argparse.ArgumentParser(prog="v2_automation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="diagnostic capacites (lecture seule)").set_defaults(func=cmd_doctor)
    sub.add_parser("init-db", help="creer la base + snapshot capacites").set_defaults(func=cmd_init_db)
    sub.add_parser("capacities", help="mesurer et persister les capacites").set_defaults(func=cmd_capacities)
    p_serve = sub.add_parser("serve", help="panneau admin web (FastAPI/uvicorn)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)
    p_admin = sub.add_parser("run-admin", help="boucle admin Telegram (long-poll)")
    p_admin.set_defaults(func=cmd_run_admin)
    p_worker = sub.add_parser("run-worker", help="daemon worker (lease, recovery au boot, heartbeat)")
    p_worker.set_defaults(func=cmd_run_worker)
    sub.add_parser("cleanup", help="nettoyage 14 jours (fichiers >= retention, messages conservés)").set_defaults(func=cmd_cleanup)
    sub.add_parser("recover", help="reprise après crash (mid-flight → queued, retry expiré → failed)").set_defaults(func=cmd_recover)
    sub.add_parser("reconcile", help="retrouve la video publiee par le serveur apres un arret pendant l'envoi (sans renvoi)").set_defaults(func=cmd_reconcile)
    args = ap.parse_args(argv)
    from . import logsetup
    try:
        logsetup.configure()
    except Exception:
        pass  # logging must never block the CLI
    args.func(args)


if __name__ == "__main__":
    main()