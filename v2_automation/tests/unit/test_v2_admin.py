"""Web panel + Telegram admin for the user side: authentication, real actions that change the system, confirmations, audit.

Every action is checked against the DATABASE (not against the page or the HTTP status): "the pages load" is not the test.
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_admin_telegram import CQ, UCB, UMsg, Msg, FakeBot
from test_delivery import FakeTransport

from v2_automation import audit, db, media, repo, service
from v2_automation.admin_telegram import AdminRouter
from v2_automation.catalog import SourceCatalog
from v2_automation.delivery import DeliveryEngine
from v2_automation.models import Episode
from v2_automation.requests_mgr import NewRequest, RequestManager
from v2_automation.telegram_publisher import TelegramPublisher
from v2_automation.timeutil import now_utc
from v2_automation.web import create_app
from v2_automation.web_auth import WebAuth, hash_password
from v2support import BASE, Site, cfg


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    yield c
    c.close()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def auth(clock):
    return WebAuth("boss", hash_password("s3cret-pass"), secret=b"k" * 32, clock=clock)


@pytest.fixture()
def client(conn, auth):
    app = create_app(connect=lambda: conn, cfg_factory=lambda: cfg(channels=["-100P", "@b"], channel_id="-100P",
                                                                   required_channels=["@spy"], user_bot_token="x"), auth=auth)
    with TestClient(app) as tc:
        yield tc


def login(client, user="boss", pw="s3cret-pass"):
    return client.post("/api/login", json={"username": user, "password": pw})


def n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


def seed_request(conn, user=1, ep=1, state_after_process=True):
    site = Site()
    site.set("a", 1, [1, 2, 3])
    m = RequestManager(conn, SourceCatalog(cfg(), fetch=site), now=now_utc)
    m.upsert_user(user, f"user{user}")
    r = m.create(NewRequest(user_id=user, kind="episode", anime_key="postid:1", title="Anime Test", version="VOSTFR",
                            source_url=f"{BASE}/anime/a/", episode_number=ep))
    return m, m.process(r["id"])


# -- authentication ---------------------------------------------------------------------

def test_every_api_route_requires_a_session_and_root_shows_the_login_page(client):
    for path in ("/api/requests", "/api/users", "/api/dashboard", "/api/audit", "/api/telegram", "/api/system"):
        assert client.get(path).status_code == 401
    assert client.post("/api/control/pause").status_code == 401
    page = client.get("/")
    assert page.status_code == 200 and "Mot de passe" in page.text and "Panneau admin" in page.text
    assert client.get("/healthz").status_code == 200                                         # liveness stays public


def test_login_wrong_credentials_then_right_ones(client):
    assert login(client, pw="nope").status_code == 401
    assert login(client, user="intruder").status_code == 401
    ok = login(client)
    assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
    assert client.get("/api/requests").status_code == 200
    assert client.get("/api/whoami").json() == {"authenticated": True, "user": "boss", "auth": True}
    client.post("/api/logout")
    assert client.get("/api/requests").status_code == 401


def test_failed_logins_are_rate_limited(client):
    codes = [login(client, pw="bad").status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert login(client).status_code == 429                                                  # even the right password, for now


def test_forged_and_expired_sessions_are_refused(client, auth, clock):
    client.cookies.set("v2_session", "Zm9yZ2VkfDk5OTk5OTk5OTk.deadbeef")
    assert client.get("/api/requests").status_code == 401
    client.cookies.clear()
    login(client)
    assert client.get("/api/requests").status_code == 200
    clock.t += 13 * 3600                                                                     # session lifetime is 12 h
    assert client.get("/api/requests").status_code == 401


def test_password_is_never_returned_or_stored_in_clear(auth, client):
    assert "s3cret-pass" not in auth.password_hash and auth.password_hash.startswith("pbkdf2_sha256$")
    assert "s3cret-pass" not in login(client).text and "s3cret-pass" not in client.get("/api/telegram").text


def test_auth_is_configured_from_the_environment_only(monkeypatch):
    assert WebAuth.from_env({}) is None
    a = WebAuth.from_env({"ADMIN_WEB_PASSWORD": "pw", "ADMIN_WEB_USERNAME": "op"})
    assert a.username == "op" and a.check("op", "pw") and not a.check("op", "other")
    b = WebAuth.from_env({"ADMIN_WEB_PASSWORD_HASH": hash_password("h")})
    assert b.check("admin", "h")


def test_the_web_server_starts_without_a_password_and_auth_is_optional(monkeypatch, capsys):
    from v2_automation import cli
    started = {}
    monkeypatch.delenv("ADMIN_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_WEB_PASSWORD_HASH", raising=False)
    monkeypatch.setattr(cli.app_config, "load_config", lambda: cfg())
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.update(host=k.get("host"), port=k.get("port")))
    cli.cmd_serve(SimpleNamespace(host=None, port=None))
    assert started["host"] == "127.0.0.1" and "sans authentification" in capsys.readouterr().out    # local only, starts anyway


# -- real actions ------------------------------------------------------------------------

def test_requests_page_lists_what_the_database_holds(client, conn):
    login(client)
    seed_request(conn, user=7)
    items = client.get("/api/requests").json()["items"]
    assert len(items) == 1
    r = items[0]
    assert (r["user_id"], r["username"], r["title"], r["episode_number"], r["version"], r["state"]) == (7, "user7", "Anime Test", 1, "VOSTFR", "QUEUED")
    assert r["expires_at"] and r["created_at"] and r["progress"] == {"done": 0, "total": 1, "percent": 0}
    assert client.get("/api/requests?state=completed").json()["items"] == []
    assert client.get("/api/requests/1").json()["items"][0]["media_status"] == "queued"


def test_cancelling_a_request_needs_confirmation_and_really_cancels(client, conn):
    login(client)
    _, r = seed_request(conn)
    assert client.post(f"/api/requests/{r['id']}/cancel").status_code == 428                 # dangerous: asks first
    assert n(conn, "SELECT state FROM requests WHERE id=?", r["id"]) == "QUEUED"             # nothing happened
    ok = client.post(f"/api/requests/{r['id']}/cancel", json={"confirm": True})
    assert ok.status_code == 200 and n(conn, "SELECT state FROM requests WHERE id=?", r["id"]) == "CANCELLED"
    assert client.post("/api/requests/999/cancel", json={"confirm": True}).status_code == 409
    a = audit.recent(conn, action="request_cancel")
    assert [x["result"] for x in reversed(a)] == ["unconfirmed", "success", "failure"]      # every attempt is on record
    done = a[1]
    assert done["admin_user_id"] == "boss" and done["surface"] == "web" and done["target"] == str(r["id"])


def test_retry_pause_resume_force_check_change_the_system_and_are_audited(client, conn):
    login(client)
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:9','X',1,'https://voir-anime.to/anime/x/')")
    ep = Episode(anime_key="postid:9", episode_key="k", canonical_episode_url="k/", episode_number=1, status="failed", last_error="DOWNLOAD_FAILED: x")
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    assert client.post(f"/api/episodes/{eid}/requeue").status_code == 200
    assert repo.get(conn, eid).status == "queued" and n(conn, "SELECT COUNT(*) FROM queue_items WHERE episode_id=?", eid) == 1   # RETRY
    client.post("/api/control/pause")
    assert service.is_paused(conn) is True and repo.next_heads(conn, 5) == []                # PAUSE really stops the queue
    client.post("/api/control/resume")
    assert service.is_paused(conn) is False and repo.next_heads(conn, 5) == [eid]            # RESUME
    assert client.post("/api/animes/postid:9/force-check").status_code == 200
    assert n(conn, "SELECT force_check FROM animes WHERE anime_key='postid:9'") == 1         # FORCE CHECK
    client.post("/api/animes/postid:9/disable")
    assert n(conn, "SELECT enabled FROM animes WHERE anime_key='postid:9'") == 0 and repo.next_heads(conn, 5) == []
    client.post("/api/animes/postid:9/enable")
    actions = [a["action"] for a in audit.recent(conn, 50)]
    for expected in ("retry", "pause", "resume", "force_check", "anime_disable", "anime_enable"):
        assert expected in actions, expected
    assert {a["admin_user_id"] for a in audit.recent(conn, 50)} == {"boss"}


def test_download_now_queues_a_baseline_media_and_skips_a_retry_backoff(client, conn):
    login(client)
    e1, _ = repo.upsert_episode(conn, Episode(anime_key="postid:9", episode_key="a", canonical_episode_url="a/", episode_number=1, status="discovered"))
    e2, _ = repo.upsert_episode(conn, Episode(anime_key="postid:9", episode_key="b", canonical_episode_url="b/", episode_number=2, status="retry_wait"))
    conn.execute("UPDATE episodes SET next_retry_at='2999-01-01T00:00:00Z' WHERE id=?", (e2,))
    conn.commit()
    assert client.post(f"/api/episodes/{e1}/download-now").status_code == 200
    assert repo.get(conn, e1).status == "queued"
    assert client.post(f"/api/episodes/{e2}/download-now").status_code == 200
    assert n(conn, "SELECT next_retry_at FROM episodes WHERE id=?", e2) is None
    assert client.post("/api/episodes/9999/download-now").status_code == 409


def test_send_to_user_needs_confirmation_then_delivers_through_the_engine(client, conn, tmp_path):
    login(client)
    _, r = seed_request(conn)
    eid = n(conn, "SELECT id FROM episodes")
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 100)
    conn.execute("UPDATE episodes SET status='ready', file_path=?, file_size=100, publish_channel=0 WHERE id=?", (str(f), eid))
    conn.commit()
    assert client.post(f"/api/episodes/{eid}/send-to-user", json={"user_id": 555}).status_code == 428
    assert n(conn, "SELECT COUNT(*) FROM deliveries") == 0
    assert client.post(f"/api/episodes/{eid}/send-to-user", json={"user_id": 555, "confirm": True}).status_code == 200
    assert client.post(f"/api/episodes/{eid}/send-to-user", json={"user_id": 555, "confirm": True}).status_code == 409   # already pending
    tr = FakeTransport()
    DeliveryEngine(conn, TelegramPublisher(tr), cfg()).run()
    assert [c for c in tr.calls if c[0] == "send_video" and c[1] == 555]                    # really sent, to that user
    assert n(conn, "SELECT status FROM deliveries WHERE user_id=555") == "sent"
    assert client.post("/api/episodes/9999/send-to-user", json={"user_id": 1, "confirm": True}).status_code == 409


def test_uncertain_delivery_can_be_retried_only_after_confirmation(client, conn):
    login(client)
    seed_request(conn)
    eid = n(conn, "SELECT id FROM episodes")
    conn.execute("UPDATE episodes SET status='ready', file_path='x', publish_channel=0 WHERE id=?", (eid,))
    conn.execute("INSERT INTO deliveries (request_id, request_item_id, user_id, media_id, status, created_at, updated_at) "
                 "VALUES (1, 1, 1, ?, 'uncertain', 't', 't')", (eid,))
    conn.commit()
    assert client.post("/api/deliveries/1/retry").status_code == 428
    assert client.post("/api/deliveries/1/retry", json={"confirm": True}).status_code == 200
    assert n(conn, "SELECT status FROM deliveries WHERE id=1") == "pending"
    assert client.post("/api/deliveries/1/retry", json={"confirm": True}).status_code == 409   # pending: nothing to retry


def test_republish_is_dangerous_and_only_on_explicit_confirmation(client, conn):
    login(client)
    ep = Episode(anime_key="postid:9", episode_key="k", canonical_episode_url="k/", episode_number=1, status="published")
    eid, _ = repo.upsert_episode(conn, ep)
    conn.execute("UPDATE episodes SET video_message_id=55, published_at='t' WHERE id=?", (eid,))
    conn.execute("INSERT INTO publications(episode_id, publication_type, status, chat_id, message_id) VALUES (?,'first_publication','sent','-100',55)", (eid,))
    conn.commit()
    assert client.post(f"/api/episodes/{eid}/republish").status_code == 428
    assert n(conn, "SELECT video_message_id FROM episodes WHERE id=?", eid) == 55           # untouched
    assert client.post(f"/api/episodes/{eid}/republish", json={"confirm": True}).status_code == 200
    e = repo.get(conn, eid)
    assert e.status == "queued" and e.video_message_id is None and n(conn, "SELECT COUNT(*) FROM publications WHERE episode_id=?", eid) == 0
    assert audit.recent(conn, action="republish")[0]["result"] == "success"
    assert client.post(f"/api/episodes/{eid}/republish", json={"confirm": True}).status_code == 409   # not published any more


def test_users_history_telegram_stats_system_and_audit_views(client, conn):
    login(client)
    seed_request(conn, user=7)
    assert client.get("/api/users").json()["items"][0]["telegram_id"] == 7
    d = client.get("/api/users/7").json()
    assert d["user"]["username"] == "user7" and d["history"][0]["episode_number"] == 1
    assert client.get("/api/users/12345").status_code == 404
    t = client.get("/api/telegram").json()
    assert t["required_channels"] == ["@spy"] and [c["channel"] for c in t["channels"]] == ["-100P", "@b"] and t["bot_api"]["user_bot_configured"] is True
    assert "x" != t["bot_api"].get("token")                                                  # no token key exists at all
    assert client.get("/api/stats").json()["requests"] == {"QUEUED": 1}
    assert "cpu_percent" in client.get("/api/system").json()
    client.post("/api/control/pause")
    assert client.get("/api/audit").json()["items"][0]["action"] == "pause"


def test_panel_without_configured_auth_keeps_working_locally_and_still_audits(conn):
    app = create_app(connect=lambda: conn, auth=None)
    with TestClient(app) as tc:
        assert tc.get("/api/requests").status_code == 200
        tc.post("/api/control/pause")
    assert audit.recent(conn)[0]["admin_user_id"] == "local"


def test_v2_page_is_served_and_linked(client):
    assert client.get("/ui/v2.html").status_code == 200 and "Demandes utilisateurs" in client.get("/ui/v2.html").text
    login(client)
    assert "/ui/v2.html" in client.get("/").text


# -- Telegram admin ---------------------------------------------------------------------

class ConfirmBot(FakeBot):
    def send_raw(self, chat_id, text, keyboard=None):
        self.sent.append({"chat": chat_id, "text": text, "keyboard": keyboard})
        return SimpleNamespace(message_id=500 + len(self.sent))


@pytest.fixture()
def router(conn):
    return AdminRouter(ConfirmBot(), conn)


def test_admin_requests_history_stats_commands_read_the_database(router, conn):
    seed_request(conn, user=7)
    router.handle_update(UMsg(Msg(42, "/requests")))
    t = router.bot.sent[-1]["text"]
    assert "#1" in t and "QUEUED" in t and "Anime Test E1 VOSTFR" in t and "user 7" in t
    assert any(b.callback_data == "act:xreq:1" for row in router.bot.sent[-1]["keyboard"].inline_keyboard for b in row)
    router.handle_update(UMsg(Msg(42, "/history")))
    assert "voir-anime.to|postid:1|S00|E1|VOSTFR" in router.bot.sent[-1]["text"]
    router.handle_update(UMsg(Msg(42, "/stats")))
    assert "utilisateurs: 1" in router.bot.sent[-1]["text"] and "QUEUED" in router.bot.sent[-1]["text"]


def test_admin_cancels_a_request_through_a_confirmation_and_it_is_audited(router, conn):
    seed_request(conn, user=7)
    router.handle_update(UCB(CQ(42, 42, "act:xreq:1", mid=100)))
    assert n(conn, "SELECT state FROM requests") == "QUEUED"                                  # asked, not done
    ask = router.bot.sent[-1]
    assert "Annuler la demande #1" in ask["text"]
    mid = 500 + len(router.bot.sent)
    router.handle_update(UCB(CQ(42, 42, "confirm:xreq:1", mid=mid)))
    assert n(conn, "SELECT state FROM requests") == "CANCELLED"
    a = audit.recent(conn, action="request_cancel")[0]
    assert a["admin_user_id"] == "42" and a["surface"] == "telegram" and a["result"] == "success"


def test_admin_declining_the_confirmation_changes_nothing(router, conn):
    seed_request(conn, user=7)
    router.handle_update(UCB(CQ(42, 42, "act:xreq:1", mid=100)))
    router.handle_update(UCB(CQ(42, 42, "confirm:no", mid=500 + len(router.bot.sent))))
    assert n(conn, "SELECT state FROM requests") == "QUEUED" and audit.recent(conn, action="request_cancel") == []


def test_admin_pause_retry_are_audited_and_a_stranger_is_ignored(router, conn):
    eid, _ = repo.upsert_episode(conn, Episode(anime_key="a", episode_key="k", canonical_episode_url="k/", episode_number=1, status="retry_wait"))
    conn.commit()
    router.handle_update(UMsg(Msg(999, "/pause")))                                            # not on the allowlist
    assert service.is_paused(conn) is False and audit.recent(conn) == []
    router.handle_update(UMsg(Msg(42, "/pause")))
    router.handle_update(UMsg(Msg(42, f"/retry {eid}")))
    assert service.is_paused(conn) is True and repo.get(conn, eid).status == "queued"
    rows = audit.recent(conn)
    assert [(a["admin_user_id"], a["action"], a["result"]) for a in rows] == [("42", "retry", "success"), ("42", "pause", "success")]
    router.handle_update(UMsg(Msg(42, "/retry 424242")))                                      # unknown id
    assert audit.recent(conn)[0]["result"] == "failure"                                       # failures are recorded as such


def test_audit_never_contains_a_secret(conn):
    audit.record(conn, admin_id=1, surface="web", action="x", target="t", metadata={"message": "ok"})
    dump = str([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert "pbkdf2" not in dump and "token" not in dump.lower()


def test_v2_page_script_is_valid_javascript(tmp_path):
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (Path(__file__).resolve().parents[2] / "src" / "v2_automation" / "web_ui" / "v2.html").read_text(encoding="utf-8")
    js = tmp_path / "v2.js"
    js.write_text(html[html.index("<script>") + 8:html.index("</script>")], encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]


def test_bot_activity_shows_conversations_even_without_requests(client, conn):
    login(client)
    now = now_utc()
    conn.execute("INSERT INTO users (telegram_id, username, first_seen_at, last_seen_at) VALUES (5,'lyon',?,?)", (now, now))
    conn.execute("INSERT INTO conversations (user_id, step, data, updated_at) VALUES (5,'choose_episode',?,?)",
                 ('{"query": {"raw": "black torch"}, "hit": {"title": "Black Torch (VF)"}, "version": "VF"}', now))
    conn.commit()
    d = client.get("/api/bot-activity").json()
    assert d["requests_total"] == 0 and d["conversations_in_progress"] == 1
    u = d["items"][0]
    assert (u["telegram_id"], u["step"], u["last_search"], u["chosen"], u["version"]) == (5, "choose_episode", "black torch", "Black Torch (VF)", "VF")
