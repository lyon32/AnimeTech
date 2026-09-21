"""run-worker — single-instance daemon (closure).

Guarantees:
  * ONE live worker via the `leases` table (owner = host:pid, heartbeat TTL).
    A stale lease held by a dead pid is safe-stolen; a fresh lease owned by
    another LIVE pid refuses to start (exit code 2) — no double processing.
  * On boot: db.migrate + recovery.run_recovery (dispatch note provided),
    so a crash never double-publishes and mid-flight state is repaired.
  * Loop: plan heads via repo.next_heads (pause/anime/retry-time gated),
    claim each head atomically, process under a bounded thread pool,
    heartbeat in the background, cleanup sweep every CLEANUP_EVERY_S,
    low-disk alert when free space dips below the configured floor.
  * SIGTERM / Ctrl+C -> stop event -> finish current work -> release lease
    -> worker_stopped alert.  The lock is also released when the process
    exits abnormally once its TTL lapses.

Everything I/O-bound is injectable: unit tests drive `run_worker` with a
thread-0 counter and fake process_episode/time without any network.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import alerts, repo
from .timeutil import now_utc

logger = logging.getLogger(__name__)

LEASE_NAME = "worker"
LEASE_TTL_S = 90.0
HEARTBEAT_S = 30.0
DEFAULT_LOOP_DELAY_S = 3.0
DEFAULT_CLEANUP_EVERY_S = 3600.0
DEFAULT_RECONCILE_EVERY_S = 120.0

ALERT_LOW_DISK = "low_disk"
ALERT_WORKER_STOPPED = "worker_stopped"


# ── lease primitives (pure; injectable clock/pid-check for tests) ───────────────

def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def pid_of(owner: str) -> int | None:
    try:
        return int(owner.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        return None


def _pid_live(owner: str) -> bool:
    import psutil
    pid = pid_of(owner)
    return pid is not None and psutil.pid_exists(pid)


def acquire_lease(conn: sqlite3.Connection, *, name: str = LEASE_NAME,
                  ttl_s: float = LEASE_TTL_S, now: str | None = None,
                  owner: str | None = None, is_live: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Try to hold `name`.  Returns {"acquired": bool, "owner", "expires_at",
    "reason"}.  A dead/stale owner is safe-stolen; a live foreign owner (or a
    fresh heartbeat we cannot age) is refused."""
    now = now or now_utc()
    owner = owner or owner_id()
    is_live = is_live or _pid_live
    row = conn.execute("SELECT owner, expires_at FROM leases WHERE name=?", (name,)).fetchone()
    if row is None:
        expires = _add_seconds(now, ttl_s)
        conn.execute("INSERT INTO leases(name, owner, acquired_at, last_heartbeat, expires_at) "
                     "VALUES (?,?,?,?,?)", (name, owner, now, now, expires))
        conn.commit()
        return {"acquired": True, "owner": owner, "expires_at": expires, "reason": "fresh"}
    if row["owner"] == owner:
        conn.execute("UPDATE leases SET last_heartbeat=?, expires_at=? WHERE name=?",
                     (now, _add_seconds(now, ttl_s), name))
        conn.commit()
        return {"acquired": True, "owner": owner, "expires_at": row["expires_at"],
                "reason": "renewed"}
    if _lease_expired(row["expires_at"], now) and not is_live(row["owner"]):
        expires = _add_seconds(now, ttl_s)
        conn.execute("UPDATE leases SET owner=?, acquired_at=?, last_heartbeat=?, expires_at=? "
                     "WHERE name=?", (owner, now, now, expires, name))
        conn.commit()
        return {"acquired": True, "owner": owner, "expires_at": expires, "reason": "stolen"}
    return {"acquired": False, "owner": row["owner"], "expires_at": row["expires_at"],
            "reason": "held-by-live-worker"}


def heartbeat(conn: sqlite3.Connection, *, name: str = LEASE_NAME,
              owner: str | None = None, ttl_s: float = LEASE_TTL_S,
              now: str | None = None) -> None:
    conn.execute("UPDATE leases SET last_heartbeat=?, expires_at=? "
                 "WHERE name=? AND owner=?",
                 (now or now_utc(), _add_seconds(now or now_utc(), ttl_s), name,
                  owner or owner_id()))
    conn.commit()


def release_lease(conn: sqlite3.Connection, *, name: str = LEASE_NAME,
                  owner: str | None = None) -> bool:
    cur = conn.execute("DELETE FROM leases WHERE name=? AND owner=?",
                       (name, owner or owner_id()))
    conn.commit()
    return cur.rowcount > 0


def _add_seconds(iso: str, s: float) -> str:
    from .timeutil import add_seconds
    return add_seconds(iso, s)


def _lease_expired(expires_at: str, now: str) -> bool:
    from .timeutil import utc_diff_seconds
    try:
        return utc_diff_seconds(now, expires_at) >= 0
    except (ValueError, TypeError):
        return True  # unparseable lease = treat as expired (safe-steal candidate)


# ── worker loop ──────────────────────────────────────────────────────────────────

def system_snapshot(path=None) -> dict[str, float | None]:
    """Live host signals for the dynamic scheduler (CPU, RAM, free disk); None when unreadable."""
    try:
        import psutil
        from . import app_config
        from pathlib import Path
        p = Path(path) if path else app_config.DATA_DIR
        while not p.exists() and p != p.parent:
            p = p.parent
        return {"cpu_percent": psutil.cpu_percent(interval=None),
                "ram_percent": psutil.virtual_memory().percent,
                "free_disk_bytes": float(psutil.disk_usage(str(p)).free)}
    except Exception:
        return {"cpu_percent": None, "ram_percent": None, "free_disk_bytes": None}


def run_worker(cfg, conn: sqlite3.Connection | None = None, *,
               stop: threading.Event | None = None,
               process_episode: Callable[[int], str] | None = None,
               loop_delay_s: float = DEFAULT_LOOP_DELAY_S,
               cleanup_every_s: float = DEFAULT_CLEANUP_EVERY_S,
               reconcile_every_s: float = DEFAULT_RECONCILE_EVERY_S,
               capacity_fn=None, discovery=None, scheduler=None,
               system_fn: Callable[[], dict] | None = None, user_side=None) -> dict[str, Any]:
    """Blocking worker loop.  `process_episode`, `discovery`, `scheduler` and `system_fn` are
    injectable for tests; by default a real DownloadManager, a DiscoveryScheduler (30-minute polling)
    and the DynamicScheduler (CPU / RAM / disk / throughput aware) are bound to `cfg`."""
    from . import app_config, db, queues
    from .downloader import DownloadManager, default_deps
    from .scheduler import DynamicScheduler
    stop = stop or threading.Event()
    conn = conn or db.connect()
    db.migrate(conn)

    lease = acquire_lease(conn)          # single-instance guarantee
    if not lease["acquired"]:
        return {"processed": 0, "stop_reason": f"busy:{lease.get('owner', '?')}",
                "acquisition": dict(lease)}

    from . import service
    service.clear_worker_stop(conn)      # a request left over from a previous run must not stop this one
    stats = {"processed": 0, "last_cleanup": None, "stop_reason": None,
             "lease": {k: lease[k] for k in ("owner", "reason")}}

    alerter = None
    dispatch = None
    daily_send = None
    if cfg.notify_token():
        from . import notifier
        dispatch = notifier.filtered(conn, alerts.default_dispatcher(cfg))   # honours the on/off switches
        daily_send = alerts.default_sender(cfg)
        from .alerts import raise_alert
        alerter = lambda kind, akey, title, body="": raise_alert(
            conn, kind, akey, title, body, dispatch=dispatch)

    # boot-time recovery (crash resume) — alerts on risky publications.
    from .recovery import run_recovery
    rec = run_recovery(conn, cfg, dispatch=dispatch)
    if rec.get("publishing_to_failed"):
        logger.error("reprise: %d publication(s) interrompue(s) -> FAILED (manuelle)",
                     len(rec["publishing_to_failed"]))

    qm = queues.QueueManager(conn)
    sched = scheduler or DynamicScheduler({**(cfg.queues or {}),
                                           "min_free_disk_bytes": (cfg.downloads or {}).get("min_free_disk_bytes", 0)})
    dl_dir = (cfg.downloads or {}).get("dir")
    system_fn = system_fn or (lambda: system_snapshot(dl_dir))   # l'espace libre surveillé est celui du disque des vidéos
    last_cleanup = time.monotonic()
    last_reconcile = time.monotonic()
    last_persist_check = 0.0
    # threads are only a ceiling: the real bound is the dynamic allowance minus what is active.
    # threads are only a ceiling: an episode that finished downloading keeps its thread while it is being sent,
    # but no longer holds a download slot, so the pool must be wider than the number of slots.
    executor = ThreadPoolExecutor(max_workers=max(12, sched._base * 4), thread_name_prefix="worker")
    active: set[int] = set()              # episodes holding a DOWNLOAD slot
    inflight: set[int] = set()            # every episode still being processed (also while being sent)
    active_lock = threading.Lock()
    stats["active_max"] = 0
    if process_episode is None:
        deps = default_deps(cfg)
        deps.scheduler = sched            # disk gate + observed throughput feed the same scheduler
        dl = DownloadManager(conn, cfg, deps, alerter=alerter)
        process_episode = dl.process_episode
    if discovery is None:
        from .discovery import DiscoveryScheduler
        discovery = DiscoveryScheduler(conn, cfg, alert=alerter)
    disk_alerted = False
    if user_side is None and getattr(cfg, "user_bot_token", ""):
        from .user_side import UserSide          # user requests share this worker, this database and this download engine
        user_side = UserSide(cfg, conn)
    if user_side is not None:
        user_side.recover()
        user_side.start_bot(stop)
    try:
        while not stop.is_set():
            heartbeat(conn)
            if service.worker_stop_requested(conn):       # "Arrêter le worker" from the web panel or Telegram
                logger.info("arrêt demandé depuis le panneau : plus de nouveau job, fin des jobs en cours")
                stats["requested"] = True
                stop.set()
                break

            # 0) a video that stays inaccessible: one alert after `persistent_error_alert_minutes` (not before)
            if time.monotonic() - last_persist_check >= 30:
                last_persist_check = time.monotonic()
                try:
                    alerts.raise_persistent_errors(
                        conn, minutes=float((cfg.downloads or {}).get("persistent_error_alert_minutes", 20)),
                        retry_window_hours=float((cfg.downloads or {}).get("retry_window_hours", 24)), dispatch=dispatch)
                except Exception as exc:
                    logger.warning("signalement des erreurs persistantes impossible: %s", exc)

            # 1) discovery: due animes are checked (30 min in production, never twice at once)
            try:
                started = discovery.tick() if discovery is not False else []
                if started:
                    logger.info("[DISCOVERY] contrôle démarré: %s", started)
            except Exception as exc:
                logger.warning("[DISCOVERY] tick impossible: %s", exc)
                if alerter is not None:
                    try:
                        alerter(alerts.KIND_SCHEDULER_PROBLEM, "discovery", "surveillance en erreur", str(exc)[:400])
                    except Exception:
                        pass

            # 1b) user requests: advance, deliver privately, notify, extra channels (never blocks the watcher)
            if user_side is not None:
                try:
                    user_side.tick()
                except Exception as exc:
                    logger.warning("[USERSIDE] tick impossible: %s", exc)

            # 2) dynamic capacity: parallel downloads across animes, bounded by the host
            snap = system_fn()
            allowance = sched.tick(
                pending_animes=qm.busy_anime_count(),
                free_disk_bytes=int(snap["free_disk_bytes"]) if snap.get("free_disk_bytes") is not None else 10 ** 15,
                cpu_percent=snap.get("cpu_percent"), ram_percent=snap.get("ram_percent"))
            if allowance.paused and alerter is not None and not disk_alerted:
                disk_alerted = True
                try:
                    alerter(alerts.KIND_LOW_DISK, "disk", "espace disque insuffisant", allowance.reason)
                except Exception:
                    pass
            elif not allowance.paused:
                disk_alerted = False
            with active_lock:
                capacity = max(0, allowance.downloads - len(active))
            heads = repo.next_heads(conn, capacity) if capacity > 0 else []
            for eid in heads:
                if stop.is_set():
                    break
                if qm.dequeue_episode(eid):
                    with active_lock:
                        active.add(eid)
                        inflight.add(eid)
                        stats["active_max"] = max(stats["active_max"], len(active))
                    logger.info("[QUEUE] épisode=%s démarré (actifs=%d, autorisés=%d, %s)",
                                eid, len(active), allowance.downloads, allowance.reason)
                    try:
                        executor.submit(_guarded, process_episode, eid, stats, active, active_lock, inflight)
                    except RuntimeError:
                        with active_lock:
                            active.discard(eid)
                            inflight.discard(eid)
                        break  # pool shut down during stop
            if capacity_fn is not None:
                capacity_fn(conn, cfg)
            if time.monotonic() - last_reconcile >= reconcile_every_s:
                last_reconcile = time.monotonic()
                try:        # a video the server published after a crash/drop shows up minutes later
                    from .recovery import reconcile_uncertain
                    done = reconcile_uncertain(conn, cfg)
                    if done:
                        stats.setdefault("reconciled", []).extend(done)
                        logger.info("vidéo(s) retrouvée(s) après arrêt: %s", done)
                except Exception as exc:
                    logger.warning("reconcile impossible: %s", exc)
            if daily_send is not None:
                try:
                    from . import notifier
                    notifier.maybe_send_daily(conn, cfg, daily_send,
                                              hour=int((cfg.monitoring or {}).get("daily_summary_hour", 9)))
                except Exception as exc:
                    logger.warning("résumé quotidien: %s", exc)
            if time.monotonic() - last_cleanup >= cleanup_every_s:
                from .cleanup import run_cleanup
                r = run_cleanup(conn, cfg, dispatch=dispatch)
                last_cleanup = time.monotonic()
                stats["last_cleanup"] = r
            _wait_stop_or_time(stop, loop_delay_s)
    finally:
        stats["stop_reason"] = ("requested" if stats.get("requested")
                                else "SIGTERM" if stop.is_set() else "clean-exit")
        while True:                          # let running jobs finish, staying visibly alive meanwhile
            with active_lock:
                pending = len(inflight)
            if not pending:
                break
            try:
                heartbeat(conn)
            except Exception:
                pass
            time.sleep(2)
        executor.shutdown(wait=True)
        if discovery not in (None, False):
            try:
                discovery.shutdown(wait=True)
            except Exception:
                pass
        if user_side is not None:
            user_side.close()
        release_lease(conn)
        service.clear_worker_stop(conn)
        if dispatch is not None and not stats.get("requested"):      # a stop you asked for is not a problem
            try:
                alerts.raise_alert(conn, ALERT_WORKER_STOPPED, "loop",
                                   "worker arrêté", f"raison : {stats['stop_reason']}",
                                   dispatch=dispatch)
            except Exception:
                pass
    return stats


def _accepts_callback(fn: Callable) -> bool:
    import inspect
    try:
        return "on_downloaded" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _guarded(fn: Callable, eid: int, stats: dict, active: set | None = None,
             lock: threading.Lock | None = None, inflight: set | None = None) -> None:
    def release(done_eid: int) -> None:                    # download finished: free the slot, keep the episode going
        if active is not None and lock is not None:
            with lock:
                active.discard(done_eid)
    try:
        if _accepts_callback(fn):
            fn(eid, on_downloaded=release)
        else:
            fn(eid)
        stats["processed"] += 1
    except Exception as exc:
        if type(exc).__name__ == "EpisodeBusy":
            logger.info("épisode %d: déjà pris en charge par un autre worker", eid)
        else:
            logger.exception("épisode %d: erreur non rattrapée", eid)
    finally:
        if active is not None and lock is not None:
            with lock:
                active.discard(eid)
                if inflight is not None:
                    inflight.discard(eid)


def _wait_stop_or_time(stop: threading.Event, delay_s: float) -> None:
    if delay_s <= 0:
        return
    stop.wait(delay_s)


def run(cfg, *args, **kwargs) -> dict[str, Any]:
    """CLI entry — installs the SIGTERM handler then runs the loop."""
    stop = threading.Event()

    def _sig(signum, frame):
        logger.info("signal %s reçu — arrêt gracieux", signum)
        stop.set()

    import signal
    for sig in (signal.SIGTERM, getattr(signal, "SIGINT", signal.SIGTERM)):
        try:
            signal.signal(sig, _sig)
        except (ValueError, OSError):
            pass
    return run_worker(cfg, stop=stop, *args, **kwargs)