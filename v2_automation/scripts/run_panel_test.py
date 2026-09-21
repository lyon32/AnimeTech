"""Panneau V2 sur la base de TEST du bot utilisateur (http://127.0.0.1:8086, local uniquement, sans mot de passe).
La base de production et le panneau de production (port 8085) ne sont pas touches."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import uvicorn
from v2_automation import app_config, db
from v2_automation.web import create_app

conn = sqlite3.connect(str(ROOT / "data" / "v2_userbot_test.sqlite3"), check_same_thread=False, factory=db.SafeConnection)
conn.row_factory = sqlite3.Row
db.migrate(conn)
app = create_app(connect=lambda: conn, cfg_factory=app_config.load_config, auth=None)


@app.middleware("http")
async def mark_test_base(request, call_next):
    """The panel page says it is the TEST base, so it is never mistaken for the production panel (port 8085)."""
    resp = await call_next(request)
    if request.url.path == "/" and resp.headers.get("content-type", "").startswith("text/html"):
        body = b"".join([chunk async for chunk in resp.body_iterator]).replace(
            b"<title>Panneau admin</title>", "<title>BASE DE TEST - Panneau admin</title>".encode())
        headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
        from fastapi.responses import Response
        return Response(body, status_code=resp.status_code, headers=headers, media_type="text/html")
    return resp


uvicorn.run(app, host="127.0.0.1", port=8086, log_level="warning")
