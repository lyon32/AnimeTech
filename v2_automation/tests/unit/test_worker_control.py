"""Worker control from the panels: clean stop request (jobs finish first), start through the scheduled task."""
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_auto_worker import Recorder, _queue, conn  # noqa: F401
from test_discovery import _cfg

from v2_automation import admin_views, alerts, service, worker
from v2_automation.scheduler import DynamicScheduler
from v2_automation.web import create_app


def _start(conn, proc):
    out = {}
    t = threading.Thread(target=lambda: out.update(worker.run_worker(
        _cfg(), conn, stop=threading.Event(), process_episode=proc, loop_delay_s=0.02, discovery=False,
        scheduler=DynamicScheduler({"max_concurrent_downloads": 3, "min_free_disk_bytes": 0}),
        system_fn=lambda: {"cpu_percent": 10.0, "ram_percent": 30.0, "free_disk_bytes": 10 ** 12},
        reconcile_every_s=10 ** 6, cleanup_every_s=10 ** 6)))
    t.start()
    return t, out


def _wait(cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    assert cond()


def test_stop_request_lets_running_jobs_finish_then_exits_cleanly(conn):
    _queue(conn, "A", 1)
    _queue(conn, "B", 1)
    _queue(conn, "C", 5)                                                   # never started: stop comes first
    rec = Recorder(conn, work_s=1.0)
    conn.execute("UPDATE queue_items SET status='paused' WHERE anime_key='C'")   # keep the third out of this run
    conn.commit()
    t, out = _start(conn, rec)
    _wait(lambda: rec.running == 2)
    assert service.worker_state(conn)["alive"] is True and service.request_worker_stop(conn)["ok"]
    assert service.worker_state(conn)["stopping"] is True                 # visible while it drains
    t.join(20)
    assert not t.is_alive() and out["stop_reason"] == "requested"
    assert rec.running == 0 and len(rec.order) == 2                        # both running jobs completed, none cut
    assert service.worker_state(conn) == {"alive": False, "stopping": False, "active_jobs": 0}
    assert not service.worker_stop_requested(conn)                         # flag cleared: the next start is normal
    assert alerts.list_alerts(conn, status=None) == []                     # a requested stop raises no alert


def test_a_leftover_request_does_not_stop_the_next_run(conn):
    service._set_worker_stop(conn, True)
    _queue(conn, "A", 1)
    rec = Recorder(conn, work_s=0.05)
    stop = threading.Event()
    t = threading.Thread(target=lambda: worker.run_worker(
        _cfg(), conn, stop=stop, process_episode=rec, loop_delay_s=0.02, discovery=False,
        scheduler=DynamicScheduler({"max_concurrent_downloads": 1, "min_free_disk_bytes": 0}),
        system_fn=lambda: {"cpu_percent": 1.0, "ram_percent": 1.0, "free_disk_bytes": 10 ** 12},
        reconcile_every_s=10 ** 6, cleanup_every_s=10 ** 6))
    t.start()
    _wait(lambda: len(rec.order) == 1)
    stop.set()
    t.join(10)


def test_stop_request_states(conn):
    assert service.request_worker_stop(conn) == {"ok": False, "message": "le worker est déjà arrêté"}
    assert not service.worker_stop_requested(conn)
    worker.acquire_lease(conn)
    assert service.worker_state(conn)["alive"]
    assert "termine sa boucle" in service.request_worker_stop(conn)["message"]
    assert "déjà demandé" in service.request_worker_stop(conn)["message"]


def test_start_worker_uses_the_scheduled_task(conn):
    calls = []

    def ok(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    service._set_worker_stop(conn, True)                                    # leftover flag from a dead worker
    assert service.start_worker(conn, runner=ok, platform="win32")["ok"]
    assert calls == [["schtasks", "/Run", "/TN", "V2AutomationWorker"]] and not service.worker_stop_requested(conn)
    missing = service.start_worker(conn, runner=lambda c, **k: SimpleNamespace(returncode=1), platform="win32")
    assert not missing["ok"] and "install_tasks.ps1" in missing["message"]
    assert "Windows" in service.start_worker(conn, runner=ok, platform="linux")["message"]

    def boom(cmd, **kw):
        raise FileNotFoundError()
    assert not service.start_worker(conn, runner=boom, platform="win32")["ok"]
    worker.acquire_lease(conn)
    assert service.start_worker(conn, runner=ok, platform="win32")["message"] == "le worker est déjà actif"
    service._set_worker_stop(conn, True)
    assert "arrêt en cours" in service.start_worker(conn, runner=ok, platform="win32")["message"]
    assert len(calls) == 1                                                  # nothing launched in those two cases


def test_web_endpoints_and_dashboard_state(conn):
    with TestClient(create_app(connect=lambda: conn, cfg_factory=_cfg)) as c:
        assert c.get("/api/worker").json()["alive"] is False
        assert c.post("/api/worker/stop").status_code == 409                # already stopped
        worker.acquire_lease(conn)
        assert c.get("/api/dashboard").json()["worker"]["alive"] is True
        assert c.post("/api/worker/stop", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/worker/stop").status_code == 200
        d = c.get("/api/dashboard").json()
        assert d["worker"]["stopping"] is True and "Arrêt du worker en cours" in d["banner"]["issues"][0]["text"]
        assert c.get("/api/health/report").json()["worker"]["stopping"] is True
        assert c.post("/api/worker/start").status_code == 409               # still stopping


def test_telegram_system_view_offers_the_right_button(conn):
    def buttons():
        return [b for row in admin_views.system_view(conn, _cfg()).rows for b in row]
    assert ("▶️ Démarrer le worker", "act:workerstart") in buttons()
    worker.acquire_lease(conn)
    assert ("⏹ Arrêter le worker", "act:workerstop") in buttons()
    service.request_worker_stop(conn)
    view = admin_views.system_view(conn, _cfg())
    assert "arrêt en cours" in view.text and not any("worker" in b[0].lower() and "act:worker" in b[1] for r in view.rows for b in r)
    assert all(len(b[1].encode()) <= 64 for r in view.rows for b in r)
