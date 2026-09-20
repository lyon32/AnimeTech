"""Telegram admin router tests — fake bot (no network), stub updates."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from v2_automation import db, repo
from v2_automation.admin_telegram import AdminRouter, KEYBOARD
from v2_automation.models import Episode


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answers = []

    def allowed(self, chat_id):
        return chat_id == 42

    def send(self, chat_id, text, keyboard=None):
        self.sent.append({"chat": chat_id, "text": text, "keyboard": keyboard})

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.edited.append({"chat": chat_id, "mid": message_id, "text": text})

    def answer(self, cb_id, text=None):
        self.answers.append({"id": cb_id, "text": text})


class Chat:
    def __init__(self, cid): self.id = cid


class Msg:
    def __init__(self, cid, text): self.chat = Chat(cid); self.text = text


class User:
    def __init__(self, uid): self.id = uid


class CBMsg:
    def __init__(self, cid, mid): self.chat = Chat(cid); self.message_id = mid


class CQ:
    def __init__(self, cid, uid, data, mid=100): self.message = CBMsg(cid, mid); self.from_user = User(uid); self.data = data; self.id = "cb1"


class UMsg:
    def __init__(self, msg): self.message = msg; self.callback_query = None
    def __bool__(self): return True


class UCB:
    def __init__(self, cq): self.callback_query = cq; self.message = None
    def __bool__(self): return True


@pytest.fixture()
def conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "v2.sqlite3"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.migrate(c)
    repo.save_capacity(c, {"documented_upload_limit_mib": "2000",
                          "tested_upload_limit_mib": "800",
                          "first_failed_upload_mib": "900",
                          "http_server_version": "Bot API 10.3",
                          "free_disk_bytes": "107374182400"})
    c.commit()
    yield c
    c.close()


def _insert(conn, status, anime="anime-a", epnum=1) -> int:
    ep = Episode(anime_key=anime, episode_key=f"{anime}-{status}-{epnum}",
                 canonical_episode_url=f"https://voir-anime.to/anime/{anime}/e{epnum}",
                 episode_number=epnum, label=f"{anime} E{epnum}", status=status)
    eid, _ = repo.upsert_episode(conn, ep)
    conn.commit()
    return eid


@pytest.fixture()
def router(conn):
    return AdminRouter(FakeBot(), conn)


def test_non_admin_message_ignored(router):
    router.handle_update(UMsg(Msg(1, "/status")))
    assert router.bot.sent == []


def test_status_sends_summary_with_keyboard(router, conn):
    _insert(conn, "retry_wait")
    router.handle_update(UMsg(Msg(42, "/status")))
    assert len(router.bot.sent) == 1
    m = router.bot.sent[0]
    # new contract (dashboard redesign): readable home, plain words, navigation buttons
    assert "Tableau de bord" in m["text"] and "EN ATTENTE (1)" in m["text"]
    assert "retry wait" not in m["text"] and "retry_wait" not in m["text"]
    assert {b.callback_data for row in m["keyboard"].inline_keyboard for b in row} >= {
        "nav:jobs", "nav:anime", "nav:alerts", "nav:system", "nav:notif", "nav:home"}


def test_episodes_command(router, conn):
    _insert(conn, "queued", "anime-b", 2)
    router.handle_update(UMsg(Msg(42, "/episodes queued 10")))
    m = router.bot.sent[0]["text"]
    assert "anime-b" in m and "queued" in m


def test_errors_command(router, conn):
    eid = _insert(conn, "failed")
    repo.mark_failed(conn, eid, "erreur de test"); conn.commit()
    router.handle_update(UMsg(Msg(42, "/errors")))
    assert "erreur de test" in router.bot.sent[0]["text"]


def test_requeue_and_fail_commands(router, conn):
    eid = _insert(conn, "retry_wait")
    router.handle_update(UMsg(Msg(42, f"/requeue {eid}")))
    assert "queued" in router.bot.sent[-1]["text"]
    assert repo.get(conn, eid).status == "queued"


def test_callback_requeue_bulk(router, conn):
    for n in range(2):
        _insert(conn, "retry_wait", "anime-c", n + 1)
    router.handle_update(UCB(CQ(42, 42, "requeue:retry_wait", mid=7)))
    assert repo.queue_depth(conn) == 2
    assert router.bot.answers[-1]["text"].startswith("requeue retry_wait: 2/2")
    assert router.bot.edited and router.bot.edited[0]["mid"] == 7


def test_callback_refresh(router, conn):
    router.handle_update(UCB(CQ(42, 42, "refresh", mid=3)))
    assert router.bot.answers[-1]["text"] is None
    assert router.bot.edited[0]["mid"] == 3
    assert "file" in router.bot.edited[0]["text"]


def test_callback_from_non_admin_ignored(router, conn):
    router.handle_update(UCB(CQ(42, 1, "requeue:retry_wait")))
    assert router.bot.answers == [] and router.bot.edited == []


def test_help(router):
    router.handle_update(UMsg(Msg(42, "/help")))
    assert "/requeue" in router.bot.sent[0]["text"]


def test_unknown_command(router):
    router.handle_update(UMsg(Msg(42, "/bogus")))
    assert "commande inconnue" in router.bot.sent[-1]["text"]

# ── automatic mode: /anime add, /check, /jobs, /system ──────────────────────────

from test_discovery import BASE, Site, _cfg as _disc_cfg   # noqa: E402


@pytest.fixture()
def auto_router(conn):
    site = Site()
    site.set("nouveau", 77, [1, 2], title="Nouveau Titre")
    return AdminRouter(FakeBot(), conn, cfg=_disc_cfg(), fetch=site)


def test_anime_add_by_url_saves_and_activates_without_a_job(auto_router, conn):
    auto_router.handle_update(UMsg(Msg(42, f"/anime add {BASE}/anime/nouveau/")))
    text = auto_router.bot.sent[-1]["text"]
    assert "✅" in text and "Nouveau Titre" in text
    row = conn.execute("SELECT enabled, source_url FROM animes WHERE anime_key='postid:77'").fetchone()
    assert row["enabled"] == 1 and row["source_url"].endswith("/anime/nouveau/")
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_anime_add_rejects_foreign_url_and_needs_admin(auto_router, conn):
    auto_router.handle_update(UMsg(Msg(42, "/anime add https://evil.example/anime/x/")))
    assert "⛔" in auto_router.bot.sent[-1]["text"]
    auto_router.handle_update(UMsg(Msg(1, f"/anime add {BASE}/anime/nouveau/")))          # not an admin
    assert len(auto_router.bot.sent) == 1
    assert conn.execute("SELECT COUNT(*) FROM animes").fetchone()[0] == 0


def test_check_command_requests_a_force_check(auto_router, conn):
    auto_router.handle_update(UMsg(Msg(42, f"/anime add {BASE}/anime/nouveau/")))
    auto_router.handle_update(UMsg(Msg(42, "/check postid:77")))
    assert "contrôle immédiat" in auto_router.bot.sent[-1]["text"]
    assert conn.execute("SELECT force_check FROM animes").fetchone()[0] == 1
    auto_router.handle_update(UMsg(Msg(42, "/anime postid:77 off")))
    assert conn.execute("SELECT enabled FROM animes").fetchone()[0] == 0


def test_jobs_and_system_commands(auto_router, conn):
    _insert(conn, "downloading", "anime-a", 4)
    auto_router.handle_update(UMsg(Msg(42, "/jobs")))
    assert "E4" in auto_router.bot.sent[-1]["text"] and "3/7" in auto_router.bot.sent[-1]["text"]
    auto_router.handle_update(UMsg(Msg(42, "/system")))
    t = auto_router.bot.sent[-1]["text"]
    assert "CPU" in t and "RAM" in t and "30 min" in t and "Worker" in t


# ── dashboard redesign: navigation, actions, "already up to date" ───────────────

from v2_automation import alerts as _alerts, notifier as _notifier, service   # noqa: E402
from v2_automation.admin_views import Html                            # noqa: E402


class EditBot(FakeBot):
    """FakeBot whose edit() can report 'nothing changed' like the real AdminBot."""
    def __init__(self):
        super().__init__()
        self.edit_result = None

    def edit(self, chat_id, message_id, text, keyboard=None):
        super().edit(chat_id, message_id, text, keyboard)
        return self.edit_result

    def send_raw(self, chat_id, text, keyboard=None):
        self.send(chat_id, text, keyboard)
        return type("M", (), {"message_id": 500 + len(self.sent)})()


@pytest.fixture()
def nav(conn):
    bot = EditBot()
    return AdminRouter(bot, conn, cfg=_disc_cfg(), fetch=Site())


def _press(router, data, mid=9, uid=42):
    router.handle_update(UCB(CQ(42, uid, data, mid=mid)))


def test_status_sends_a_new_html_message_every_time_never_an_edit(nav):
    nav.handle_update(UMsg(Msg(42, "/status")))
    nav.handle_update(UMsg(Msg(42, "/status")))
    assert len(nav.bot.sent) == 2 and nav.bot.edited == []
    assert all(isinstance(m["text"], Html) for m in nav.bot.sent)         # sent with parse_mode=HTML


def test_navigation_buttons_edit_in_place_and_show_the_right_screen(nav, conn):
    for data, expect in (("nav:jobs", "Jobs"), ("nav:anime", "Anime surveillés"), ("nav:alerts", "Alertes"),
                         ("nav:system", "Système"), ("nav:notif", "Notifications"), ("nav:home", "Tableau de bord")):
        _press(nav, data, mid=11)
        assert expect in str(nav.bot.edited[-1]["text"]) and nav.bot.edited[-1]["mid"] == 11
    assert nav.bot.answers[-1]["text"] is None


def test_identical_content_gives_a_toast_not_an_error(nav):
    nav.bot.edit_result = False                                           # Telegram: message is not modified
    _press(nav, "nav:home")
    assert nav.bot.answers[-1]["text"] == "✓ Déjà à jour"


def test_button_actions_change_real_state(nav, conn):
    conn.execute("INSERT INTO animes (anime_key, title, enabled, source_url) VALUES ('postid:7','X',1,'https://voir-anime.to/anime/x/')")
    conn.commit()
    _press(nav, "act:check:postid:7")
    assert conn.execute("SELECT force_check FROM animes").fetchone()[0] == 1
    _press(nav, "act:toggle:postid:7")
    assert conn.execute("SELECT enabled FROM animes").fetchone()[0] == 0
    _press(nav, "act:pause")
    assert service.is_paused(conn)
    _press(nav, "act:resume")
    assert not service.is_paused(conn)
    _press(nav, "act:notif:daily")
    assert _notifier.enabled(conn, "daily") is False
    assert "⚪" in str(nav.bot.edited[-1]["text"])
    _alerts.raise_alert(conn, "retry", "ep:1", "x")
    conn.commit()
    _press(nav, "act:ackall")
    assert _alerts.open_count(conn) == 0


def test_retry_and_cancel_buttons(nav, conn):
    e = _insert(conn, "failed", "anime-a", 3)
    _press(nav, f"act:retry:{e}")
    assert repo.get(conn, e).status == "queued"
    e2 = _insert(conn, "queued", "anime-a", 4)
    _press(nav, f"act:cancel:{e2}")                                       # asks for confirmation in a new message
    assert nav.bot.sent and "Annuler" in nav.bot.sent[-1]["text"] and repo.get(conn, e2).status == "queued"


def test_add_anime_button_then_url_message(nav, conn):
    nav.bot.edit_result = None
    site = nav.fetch
    site.set("nouveau", 88, [1], title="Nouveau")
    _press(nav, "act:add")
    assert "URL" in nav.bot.sent[-1]["text"] and nav.bot.answers[-1]["text"] == "En attente de l'URL"
    nav.handle_update(UMsg(Msg(42, f"{BASE}/anime/nouveau/")))
    assert "✅" in nav.bot.sent[-1]["text"]
    assert conn.execute("SELECT anime_key FROM animes").fetchone()[0] == "postid:88"
    nav.handle_update(UMsg(Msg(42, "un texte quelconque")))              # no pending add any more: ignored
    assert len(nav.bot.sent) == 2


def test_non_admin_cannot_press_buttons_and_legacy_buttons_still_work(nav, conn):
    _press(nav, "act:pause", uid=1)
    assert not service.is_paused(conn) and nav.bot.answers == []
    _insert(conn, "retry_wait", "anime-a", 1)
    _press(nav, "requeue:retry_wait", mid=5)
    assert nav.bot.answers[-1]["text"].startswith("requeue retry_wait: 1/1") and nav.bot.edited[-1]["mid"] == 5


def test_real_admin_bot_edit_returns_false_when_not_modified(monkeypatch):
    from v2_automation.admin_telegram import AdminBot

    class Client:
        def __init__(self):
            self.mode = []

        def _bot(self):
            outer = self

            class B:
                async def edit_message_text(self, **kw):
                    outer.mode.append(kw["parse_mode"])
                    raise RuntimeError("Message is not modified: specified new message content ...")
            return B()

        def _run(self, coro):
            import asyncio
            return asyncio.new_event_loop().run_until_complete(coro)

    bot = AdminBot.__new__(AdminBot)
    bot.client = Client()
    assert bot.edit(1, 2, Html("<b>x</b>")) is False and bot.client.mode == ["HTML"]
    assert bot._mode("texte <url> brut") is None                          # legacy plain text: no parse_mode


def test_worker_stop_button_asks_confirmation_then_requests_a_clean_stop(nav, conn):
    from v2_automation import worker as _worker
    _worker.acquire_lease(conn)
    _press(nav, "act:workerstop")
    assert "Arrêter le worker" in nav.bot.sent[-1]["text"] and not service.worker_stop_requested(conn)   # nothing yet
    mid = 500 + len(nav.bot.sent)
    _press(nav, "confirm:no", mid=mid)
    assert not service.worker_stop_requested(conn) and "abandonné" in nav.bot.edited[-1]["text"]
    _press(nav, "act:workerstop")
    _press(nav, "confirm:workerstop", mid=500 + len(nav.bot.sent))
    assert service.worker_stop_requested(conn) and "arrêt demandé" in nav.bot.edited[-1]["text"]
