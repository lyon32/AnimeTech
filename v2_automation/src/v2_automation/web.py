"""V2 admin panel — read-only observability + manual recovery actions, served
on 127.0.0.1 (never bound externally).  No secret is ever exposed: the bot token
is never serialized; channel id and admin ids are masked.

Endpoints
  GET  /healthz                 liveness
  GET  /readyz                  DB reachable + schema present
  GET  /api/overview            status counts, queue, capacity, limits
  GET  /api/episodes?status=&limit=&offset=&q=
  GET  /api/episodes/{id}
  GET  /api/queue               per-anime FIFO heads
  GET  /api/errors?limit=
  GET  /api/history?limit=
  GET  /api/capacity            persisted bot_capacity snapshot
  POST /api/episodes/{id}/requeue   retry_wait/failed/structure_changed -> queued
  POST /api/episodes/{id}/fail      retry_wait -> failed
  GET  /                        the panel (static files under /ui, no external assets)
  GET  /api/dashboard, /api/episodes-view, /api/queue/lanes, /api/problems, /api/capacity/live,
       /api/health/report, /api/notifications   readable data for the panel
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import app_config, db, monitoring, repo, service, web_data
from .schema import current_schema_version
from .timeutil import now_utc

UI_DIR = Path(__file__).resolve().parent / "web_ui"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _NoCacheStatic(StaticFiles):
    """The panel's own files are small and change with the code: never serve a stale copy."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


_CONN: Callable[[], sqlite3.Connection] = db.connect
_CFG: Callable[[], app_config.AppConfig] | None = None
_FETCH = None          # page fetcher for adding an anime by URL (injected in tests; default = real HTTP)


def create_app(connect: Callable[[], sqlite3.Connection] | None = None,
               cfg_factory: Callable[[], app_config.AppConfig] | None = None,
               fetch: Callable[[str], str] | None = None) -> FastAPI:
    global _CONN, _CFG, _FETCH
    if connect is not None:
        _CONN = connect
    if cfg_factory is not None:
        _CFG = cfg_factory
    if fetch is not None:
        _FETCH = fetch
    app = FastAPI(title="v2_automation admin", version="2.0.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """A page open in the browser on another site must not be able to act on this local panel."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
                return JSONResponse({"detail": "origine refusée"}, status_code=403)
        return await call_next(request)

    def conn() -> sqlite3.Connection:
        return _CONN()

    def get_cfg() -> app_config.AppConfig:
        return _CFG() if _CFG is not None else app_config.load_config()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "ts": now_utc()}

    @app.get("/readyz")
    def readyz() -> dict:
        try:
            c = conn()
            version = c.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()
            ok = version is not None and version["version"] >= 1
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": ok, "schema_version": version["version"] if version else None,
                "expected_schema_version": current_schema_version()}

    @app.get("/api/overview")
    def overview() -> dict:
        return service.overview(conn())

    @app.get("/api/health")
    def health() -> dict:
        return monitoring.pipeline_health(conn(), get_cfg())

    @app.get("/api/episodes")
    def episodes(status: str | None = None, q: str | None = None,
                 limit: int = 50, offset: int = 0) -> dict:
        limit, offset = max(1, min(limit, 200)), max(0, offset)
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if q:
            where.append("(label LIKE ? OR episode_key LIKE ? OR anime_key LIKE ?)")
            args += [f"%{q}%"] * 3
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        c = conn()
        total = c.execute(f"SELECT COUNT(*) AS n FROM episodes {wsql}", args).fetchone()["n"]
        rows = c.execute(
            f"""SELECT id, anime_key, episode_number, language, label, status,
                       retry_count, published_at, updated_at, last_error,
                       file_size, video_message_id
                FROM episodes {wsql}
                ORDER BY updated_at DESC LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "items": [dict(r) for r in rows]}

    @app.get("/api/episodes/{episode_id}")
    def episode(episode_id: int) -> dict:
        c = conn()
        row = c.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "inconnu")
        pubs = c.execute(
            "SELECT publication_type, status, message_id, media_kind, file_size, attempted_at "
            "FROM publications WHERE episode_id=?", (episode_id,)).fetchall()
        return {"episode": dict(row), "publications": [dict(p) for p in pubs]}

    @app.get("/api/queue")
    def queue_route() -> dict:
        c = conn()
        heads = [{"episode_id": eid} for eid in repo.next_heads(c, 50)]
        items = c.execute(
            "SELECT q.anime_key, q.position, q.status, q.episode_id, e.status AS ep_status "
            "FROM queue_items q JOIN episodes e ON e.id=q.episode_id "
            "WHERE q.status='queued' ORDER BY q.anime_key, q.position").fetchall()
        return {"heads": heads, "items": [dict(r) for r in items]}

    @app.get("/api/errors")
    def errors(limit: int = 50) -> dict:
        return {"items": repo.errors_recent(conn(), max(1, min(limit, 200)))}

    @app.get("/api/history")
    def history_route(limit: int = 100) -> dict:
        return {"items": repo.history(conn(), max(1, min(limit, 200)))}

    @app.get("/api/capacity")
    def capacity() -> dict:
        return {"items": repo.load_capacity(conn())}

    def _manual(episode_id: int, action: str) -> dict:
        if action == "requeue":
            res = service.requeue_episode(conn(), episode_id)
        elif action == "fail":
            res = service.fail_episode(conn(), episode_id)
        elif action == "cancel":
            res = service.cancel_episode(conn(), episode_id)
        else:
            raise HTTPException(400, f"action {action} inconnue")
        if not res.get("ok"):
            raise HTTPException(409 if res.get("message") not in (None, "inconnu") else 404,
                                res.get("message", "erreur"))
        res["action"] = action
        res["ts"] = now_utc()
        return res

    @app.post("/api/episodes/{episode_id}/requeue")
    def manual_requeue(episode_id: int) -> dict:
        return _manual(episode_id, "requeue")

    @app.post("/api/episodes/{episode_id}/fail")
    def manual_fail(episode_id: int) -> dict:
        return _manual(episode_id, "fail")

    @app.post("/api/episodes/{episode_id}/cancel")
    def manual_cancel(episode_id: int) -> dict:
        return _manual(episode_id, "cancel")

    # ── operational control (pause / animes / alerts) ──────────────────────────

    @app.get("/api/control")
    def control_status() -> dict:
        from . import alerts
        c = conn()
        return {"paused": service.is_paused(c),
                "animes": service.anime_list(c),
                "alerts_open": alerts.open_count(c)}

    @app.post("/api/control/pause")
    def control_pause() -> dict:
        return service.set_paused(conn(), True)

    @app.post("/api/control/resume")
    def control_resume() -> dict:
        return service.set_paused(conn(), False)

    @app.post("/api/animes/{anime_key}/enable")
    def animes_enable(anime_key: str) -> dict:
        return _anime_toggle(anime_key, True)

    @app.post("/api/animes/{anime_key}/disable")
    def animes_disable(anime_key: str) -> dict:
        return _anime_toggle(anime_key, False)

    @app.post("/api/animes/{anime_key}")
    def animes_upsert(anime_key: str, title: str | None = None, enabled: bool | None = None) -> dict:
        return service.upsert_anime(conn(), anime_key, title=title, enabled=enabled)

    @app.get("/api/alerts")
    def alerts_route(status: str | None = "open", limit: int = 50) -> dict:
        from . import alerts
        return {"items": alerts.list_alerts(conn(), limit=max(1, min(limit, 200)), status=status)}

    @app.post("/api/alerts/{alert_id}/ack")
    def alerts_ack(alert_id: int) -> dict:
        from . import alerts
        ok = alerts.ack(conn(), alert_id)
        if not ok:
            raise HTTPException(404, "alerte inconnue ou déjà acquittée")
        return {"ok": True, "alert_id": alert_id, "ts": now_utc()}

    def _anime_toggle(key: str, enabled: bool) -> dict:
        res = service.set_anime_enabled(conn(), key, enabled)
        if not res.get("ok"):
            raise HTTPException(404, res.get("message", "erreur"))
        return res

    # ── automatic mode: watched animes, jobs, host state ───────────────────────

    @app.get("/api/animes")
    def animes_list() -> dict:
        return {"items": service.anime_list(conn())}

    @app.post("/api/animes")
    def animes_add(source_url: str, title: str | None = None, language: str | None = None) -> dict:
        res = service.add_anime_from_url(conn(), get_cfg(), source_url, fetch=_FETCH, title=title, language=language)
        if not res.get("ok"):
            raise HTTPException(400, res.get("message", "erreur"))
        return res

    @app.post("/api/animes/{anime_key}/edit")
    def animes_edit(anime_key: str, title: str | None = None, source_url: str | None = None,
                    language: str | None = None) -> dict:
        if source_url:
            from . import discovery
            try:
                source_url = discovery.validate_source_url(get_cfg(), source_url)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        res = service.update_anime(conn(), anime_key, title=title, source_url=source_url, language=language)
        if not res.get("ok"):
            raise HTTPException(404, res.get("message", "erreur"))
        return res

    @app.post("/api/animes/{anime_key}/force-check")
    def animes_force_check(anime_key: str) -> dict:
        res = service.request_force_check(conn(), anime_key)
        if not res.get("ok"):
            raise HTTPException(404, res.get("message", "erreur"))
        return res

    @app.get("/api/jobs")
    def jobs(limit: int = 100) -> dict:
        return {"items": service.jobs(conn(), max(1, min(limit, 300)))}

    @app.get("/api/system")
    def system() -> dict:
        return service.system_status(conn(), get_cfg())

    # ── the panel itself: readable views for people (the /api routes above stay for machines) ──

    @app.get("/api/dashboard")
    def dashboard_data() -> dict:
        return web_data.dashboard(conn(), get_cfg())

    @app.get("/api/episodes-view")
    def episodes_view(status: str | None = None, q: str | None = None, anime: str | None = None,
                      limit: int = 25, offset: int = 0) -> dict:
        return web_data.episodes_page(conn(), status=status or None, q=(q or "").strip() or None,
                                      anime=anime or None, limit=max(1, min(limit, 100)), offset=max(0, offset))

    @app.get("/api/episodes-view/{episode_id}")
    def episode_view(episode_id: int) -> dict:
        d = web_data.episode_detail(conn(), episode_id)
        if d is None:
            raise HTTPException(404, "inconnu")
        return d

    @app.get("/api/cycles")
    def cycles_route() -> dict:
        return web_data.cycles(conn(), get_cfg())

    @app.get("/api/queue/lanes")
    def queue_lanes() -> dict:
        return web_data.queue_lanes(conn())

    @app.get("/api/problems")
    def problems() -> dict:
        return web_data.problems(conn())

    @app.get("/api/capacity/live")
    def capacity_live() -> dict:
        return web_data.capacity_live(conn(), get_cfg())

    @app.get("/api/health/report")
    def health_report() -> dict:
        return web_data.health_page(conn(), get_cfg())

    @app.get("/api/worker")
    def worker_status() -> dict:
        return service.worker_state(conn())

    @app.post("/api/worker/stop")
    def worker_stop() -> dict:
        res = service.request_worker_stop(conn())
        if not res["ok"]:
            raise HTTPException(409, res["message"])
        return res

    @app.post("/api/worker/start")
    def worker_start() -> dict:
        res = service.start_worker(conn())
        if not res["ok"]:
            raise HTTPException(409, res["message"])
        return res

    @app.get("/api/notifications")
    def notifications_list() -> dict:
        return web_data.notifications(conn())

    @app.post("/api/notifications/{key}/toggle")
    def notifications_toggle(key: str) -> dict:
        from . import notifier
        try:
            on = notifier.toggle(conn(), key)
        except ValueError:
            raise HTTPException(404, "notification inconnue")
        return {"ok": True, "key": key, "enabled": on}

    @app.get("/", response_class=FileResponse)
    def dashboard() -> FileResponse:
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/ui", _NoCacheStatic(directory=str(UI_DIR)), name="ui")

    return app


app = create_app()