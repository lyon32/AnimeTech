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

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import app_config, audit, db, monitoring, panel_downloads, repo, service, web_auth, web_data
from .catalog import SourceCatalog
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


PUBLIC_PATHS = ("/healthz", "/readyz", "/api/login")       # reachable without a session; /ui/* is static, holds no data


def create_app(connect: Callable[[], sqlite3.Connection] | None = None,
               cfg_factory: Callable[[], app_config.AppConfig] | None = None,
               fetch: Callable[[str], str] | None = None,
               auth: "web_auth.WebAuth | None | str" = "env") -> FastAPI:
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

    web_auth_cfg = web_auth.WebAuth.from_env() if auth == "env" else auth

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        """Every route but the public ones needs a valid session when authentication is configured; the acting admin is
        recorded for the audit log."""
        path = request.url.path
        user = "local"
        if web_auth_cfg is not None and path not in PUBLIC_PATHS and not path.startswith("/ui/"):
            user = web_auth_cfg.user_of(request.cookies.get(web_auth.COOKIE))
            if user is None:
                if path == "/":
                    return HTMLResponse(web_auth.LOGIN_PAGE, headers={"Cache-Control": "no-store"})
                return JSONResponse({"detail": "authentification requise"}, status_code=401)
        with audit.acting_as("web", user):
            return await call_next(request)

    @app.post("/api/login")
    def login(request: Request, body: dict = Body(...)):
        if web_auth_cfg is None:
            return {"ok": True, "auth": "disabled"}
        client = request.client.host if request.client else "?"
        if web_auth_cfg.limited(client):
            return JSONResponse({"detail": "trop de tentatives"}, status_code=429)
        if not web_auth_cfg.check(str(body.get("username", "")), str(body.get("password", "")), client):
            return JSONResponse({"detail": "identifiants invalides"}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(web_auth.COOKIE, web_auth_cfg.issue(), httponly=True, samesite="strict",
                        max_age=web_auth.SESSION_TTL_S, path="/")
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(web_auth.COOKIE, path="/")
        return resp

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict:
        who = web_auth_cfg.user_of(request.cookies.get(web_auth.COOKIE)) if web_auth_cfg else None
        return {"authenticated": who is not None or web_auth_cfg is None, "user": who, "auth": web_auth_cfg is not None}

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

    @app.get("/api/search")
    def panel_search(q: str = "") -> dict:
        res = panel_downloads.search(conn(), get_cfg(), q)
        if not res.get("ok"):
            raise HTTPException(400, res.get("message", "erreur"))
        return res

    @app.get("/api/anime-episodes")
    def panel_episodes(url: str) -> dict:
        res = panel_downloads.episodes(conn(), get_cfg(), url, catalog=SourceCatalog(get_cfg(), fetch=_FETCH))
        if not res.get("ok"):
            raise HTTPException(400, res.get("message", "erreur"))
        return res

    @app.post("/api/downloads")
    def panel_download(payload: dict = Body(...)) -> dict:
        res = service.panel_download(conn(), str(payload.get("source_url") or ""), cfg=get_cfg(),
                                     mode=str(payload.get("mode") or ""), version=str(payload.get("version") or "VOSTFR"),
                                     numbers=payload.get("numbers") or [], n=int(payload.get("n") or 1),
                                     title=payload.get("title"), watch=bool(payload.get("watch")), fetch=_FETCH)
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

    # ── V2 user side: requests, users, deliveries, Telegram, audit, manual actions ──

    def _confirmed(body: dict | None) -> bool:
        return bool(body and body.get("confirm") is True)

    def _act(res: dict) -> dict:
        if res.get("needs_confirmation"):
            raise HTTPException(428, res["message"])
        if not res.get("ok"):
            raise HTTPException(409, res.get("message", "refusé"))
        return res

    @app.get("/api/requests")
    def requests_list(state: str | None = None, user_id: int | None = None, limit: int = 100) -> dict:
        return {"items": service.requests_overview(conn(), state=state, user_id=user_id, limit=limit)}

    @app.get("/api/requests/{request_id}")
    def request_get(request_id: int) -> dict:
        d = service.request_detail(conn(), request_id)
        if d is None:
            raise HTTPException(404, "inconnue")
        return d

    @app.post("/api/requests/{request_id}/cancel")
    def request_cancel(request_id: int, body: dict | None = Body(None)) -> dict:
        return _act(service.cancel_request(conn(), request_id, confirm=_confirmed(body)))

    @app.get("/api/users")
    def users_list(limit: int = 200) -> dict:
        return {"items": service.users_overview(conn(), limit)}

    @app.get("/api/users/{telegram_id}")
    def user_get(telegram_id: int) -> dict:
        d = service.user_detail(conn(), telegram_id)
        if d is None:
            raise HTTPException(404, "inconnu")
        return d

    @app.get("/api/deliveries")
    def deliveries_list(limit: int = 100) -> dict:
        return {"items": service.deliveries_recent(conn(), limit)}

    @app.post("/api/deliveries/{delivery_id}/retry")
    def delivery_retry(delivery_id: int, body: dict | None = Body(None)) -> dict:
        return _act(service.retry_delivery(conn(), delivery_id, confirm=_confirmed(body)))

    @app.post("/api/episodes/{episode_id}/download-now")
    def episode_download_now(episode_id: int) -> dict:
        return _act(service.download_now(conn(), episode_id))

    @app.post("/api/episodes/{episode_id}/send-to-user")
    def episode_send_to_user(episode_id: int, body: dict = Body(...)) -> dict:
        try:
            uid = int(body.get("user_id"))
        except (TypeError, ValueError):
            raise HTTPException(422, "user_id requis")
        return _act(service.send_to_user(conn(), episode_id, uid, confirm=_confirmed(body)))

    @app.post("/api/episodes/{episode_id}/republish")
    def episode_republish(episode_id: int, body: dict | None = Body(None)) -> dict:
        return _act(service.republish_episode(conn(), episode_id, confirm=_confirmed(body)))

    @app.get("/api/bot-activity")
    def bot_activity_data(limit: int = 100) -> dict:
        return service.bot_activity(conn(), limit)

    @app.get("/api/telegram")
    def telegram_state() -> dict:
        return service.telegram_overview(conn(), get_cfg())

    @app.get("/api/stats")
    def stats_data() -> dict:
        return service.stats(conn())

    @app.get("/api/audit")
    def audit_list(limit: int = 100, action: str | None = None) -> dict:
        return {"items": audit.recent(conn(), limit, action=action)}

    @app.get("/", response_class=FileResponse)
    def dashboard() -> FileResponse:
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/ui", _NoCacheStatic(directory=str(UI_DIR)), name="ui")

    return app


app = create_app()