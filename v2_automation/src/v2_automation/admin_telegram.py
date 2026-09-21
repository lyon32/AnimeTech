"""Telegram admin (Phase 8) — inline-keyboard statistics + recovery controls,
reusing the proven `v1_poc.TelegramClient` plumbing (never edited).

Only users listed in ADMIN_TELEGRAM_IDS (config/.env) can issue commands; every
state change goes through `service` (same semantics as the web panel).

Commands
  /status                       aperçu + clavier inline (refresh / requeue …)
  /episodes [status] [n]        liste des épisodes
  /errors [n]                   dernières erreurs
  /queue                        tête de chaque file anime
  /capacity                     snapshot capacités persisté
  /requeue <id>                 retry_wait/failed/structure_changed -> queued
  /retry <id>                   = /requeue (alias)
  /fail <id>                    retry_wait -> failed
  /cancel <id>                  annuler un épisode (confirmation inline requise)
  /pause | /resume              pause / reprise globale de la file
  /anime <list|key [on|off]>    état des animes + activate/desactivate
  /alerts [n]                   alertes opérationnelles ouvertes (kind/akey/count)
  /help
"""
from __future__ import annotations

import logging
import threading
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from . import app_config, db, repo, service
from .publisher import V2TelegramClient, local_bot_base_url

logger = logging.getLogger(__name__)

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 Rafraîchir", callback_data="refresh")],
    [InlineKeyboardButton("⟳ Requeue retry_wait", callback_data="requeue:retry_wait"),
     InlineKeyboardButton("⟳ Requeue failed", callback_data="requeue:failed")],
    [InlineKeyboardButton("⟳ Requeue structure_changed", callback_data="requeue:structure_changed")],
    [InlineKeyboardButton("⏸️ Pause", callback_data="control:pause"),
     InlineKeyboardButton("▶️ Resume", callback_data="control:resume")],
])

class AdminBot:
    """Thin, proven-client-based adapter: getUpdates long-poll + admin chats."""

    def __init__(self, cfg: app_config.AppConfig):
        base = cfg.telegram.get("api_base_url") or ""
        token = cfg.notify_token()                  # the admin bot (falls back to the single bot)
        urlish = base and local_bot_base_url(base, token) or base or None
        self.client = V2TelegramClient(token, "0", urlish)
        self.admins = set(cfg.admin_telegram_ids)

    def allowed(self, chat_id) -> bool:
        return chat_id in self.admins

    def get_updates(self, offset: int | None, timeout: int = 40) -> list[Update]:
        async def _impl():
            return await self.client._bot().get_updates(
                offset=offset, timeout=timeout, limit=50, allowed_updates=["message", "callback_query"])
        try:
            return self.client._run(_impl()) or []
        except Exception as exc:
            logger.warning("getUpdates échec: %s", exc)
            return []

    @staticmethod
    def _mode(text):
        from .admin_views import Html
        return "HTML" if isinstance(text, Html) else None       # views are HTML, legacy command output is plain

    def send(self, chat_id: int, text: str, keyboard=None) -> None:
        async def _impl():
            return await self.client._bot().send_message(
                chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode=self._mode(text))
        self.client._run(_impl())

    def send_raw(self, chat_id: int, text: str, keyboard=None):
        """Send and return the created Message (needed for confirmation flows)."""
        async def _impl():
            return await self.client._bot().send_message(
                chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode=self._mode(text))
        return self.client._run(_impl())

    def edit(self, chat_id: int, message_id: int, text: str, keyboard=None) -> bool:
        """False when Telegram says the content is identical (nothing to change), True otherwise."""
        async def _impl():
            return await self.client._bot().edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard,
                parse_mode=self._mode(text))
        try:
            self.client._run(_impl())
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return False
            raise
        return True

    def set_commands(self) -> None:
        """The "/" menu of the bot, in French (best effort)."""
        from telegram import BotCommand
        cmds = [("status", "Tableau de bord"), ("jobs", "Jobs en cours"), ("anime", "Anime surveillés"),
                ("alerts", "Alertes"), ("system", "État du système"), ("check", "Contrôler un anime"),
                ("pause", "Mettre la file en pause"), ("resume", "Reprendre la file"),
                ("requests", "Demandes utilisateurs"), ("history", "Historique"), ("stats", "Statistiques"),
                ("errors", "Erreurs"), ("queue", "File d'attente"), ("help", "Aide")]

        async def _impl():
            return await self.client._bot().set_my_commands([BotCommand(c, d) for c, d in cmds])
        try:
            self.client._run(_impl())
        except Exception as exc:
            logger.warning("menu des commandes non défini: %s", exc)

    def answer(self, callback_id: str, text: str | None = None) -> None:
        async def _impl():
            return await self.client._bot().answer_callback_query(
                callback_query_id=callback_id, text=text, show_alert=False)
        try:
            self.client._run(_impl())
        except Exception as exc:        # a button pressed while the bot was off ("query is too old") is not an error
            logger.info("réponse au bouton ignorée: %s", type(exc).__name__)

    def close(self) -> None:
        self.client.close()


class AdminRouter:
    def __init__(self, bot: AdminBot, conn, cfg=None, fetch=None):
        self.bot = bot
        self.conn = conn
        self.cfg = cfg          # needed to validate/identify an anime added by URL
        self.fetch = fetch      # page fetcher (injected in tests)
        self._pending_cancel: dict[int, tuple[str, int]] = {}  # message_id -> (episode_id)
        self._pending_add: set[int] = set()                    # chats whose next message is an anime URL

    def handle_update(self, update: Update) -> None:
        from . import audit
        who = None
        if update.message is not None:
            who = getattr(getattr(update.message, "from_user", None), "id", None) or update.message.chat.id
        elif update.callback_query is not None:
            who = getattr(getattr(update.callback_query, "from_user", None), "id", None)
        with audit.acting_as("telegram", who if who is not None else "?"):       # every admin action is audited (service.py)
            if update.message is not None:
                self._on_message(update)
            elif update.callback_query is not None:
                self._on_callback(update)

    def _on_message(self, update: Update) -> None:
        msg = update.message
        chat = msg.chat.id
        if not self.bot.allowed(chat):
            return
        text = (msg.text or "").strip()
        if text and not text.startswith("/") and chat in self._pending_add:
            self._pending_add.discard(chat)                        # answer to "➕ Ajouter un anime"
            self.bot.send(chat, self._anime(["add", text.split()[0]]))
            return
        if not text or not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd == "/help":
                self.bot.send(chat, self._help())
            elif cmd in ("/start", "/status", "/menu"):
                self.send_status(chat)
            elif cmd == "/episodes":
                self.bot.send(chat, self._episodes(args))
            elif cmd == "/errors":
                self.bot.send(chat, self._errors(args))
            elif cmd == "/queue":
                self.bot.send(chat, self._queue())
            elif cmd == "/capacity":
                self.bot.send(chat, self._capacity())
            elif cmd == "/requeue" or cmd == "/retry":
                self.bot.send(chat, self._requeue(args))
            elif cmd == "/fail":
                self.bot.send(chat, self._fail(args))
            elif cmd == "/cancel":
                self._cancel(chat, args)
            elif cmd == "/pause":
                self.bot.send(chat, service.set_paused(self.conn, True)["message"])
            elif cmd == "/resume":
                self.bot.send(chat, service.set_paused(self.conn, False)["message"])
            elif cmd == "/anime":
                if args:
                    self.bot.send(chat, self._anime(args))
                else:
                    self._send_view(chat, "anime")
            elif cmd == "/alerts":
                self._send_view(chat, "alerts")
            elif cmd == "/jobs":
                self._send_view(chat, "jobs")
            elif cmd == "/system":
                self._send_view(chat, "system")
            elif cmd in ("/notif", "/notifications"):
                self._send_view(chat, "notif")
            elif cmd == "/requests":
                self._send_requests(chat, args)
            elif cmd == "/history":
                self.bot.send(chat, self._history_text(args))
            elif cmd == "/stats":
                self.bot.send(chat, self._stats_text())
            elif cmd in ("/check", "/forcecheck"):
                self.bot.send(chat, self._anime([args[0], "check"]) if args else "usage : /check <anime_key>")
            else:
                self.bot.send(chat, "commande inconnue — /help")
        except Exception as exc:
            logger.exception("échec admin commande %s", cmd)
            self.bot.send(chat, f"⛔ erreur interne: {type(exc).__name__}")

    # ── views ────────────────────────────────────────────────────────────────────

    def _telegram_ok(self) -> bool | None:
        client = getattr(self.bot, "client", None)
        if client is None:
            return None
        try:
            client.get_me()
            return True
        except Exception:
            return False

    def _view(self, name: str, arg: str | None = None):
        from . import admin_views as v
        if name == "jobs":
            return v.jobs_view(self.conn, self.cfg)
        if name == "anime":
            return v.anime_view(self.conn, self.cfg)
        if name == "animedetail":
            return v.anime_detail(self.conn, self.cfg, arg or "")
        if name == "alerts":
            return v.alerts_view(self.conn, self.cfg)
        if name == "cycles":
            return v.cycles_view(self.conn, self.cfg)
        if name == "system":
            return v.system_view(self.conn, self.cfg, telegram_ok=self._telegram_ok())
        if name == "notif":
            return v.notifications_view(self.conn, self.cfg)
        return v.home(self.conn, self.cfg)

    @staticmethod
    def _markup(rows) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                     for row in rows])

    def _send_view(self, chat_id: int, name: str, arg: str | None = None) -> None:
        view = self._view(name, arg)
        self.bot.send(chat_id, view.text, self._markup(view.rows))

    # ── buttons ──────────────────────────────────────────────────────────────────

    def _on_callback(self, update: Update) -> None:
        cb = update.callback_query
        chat = getattr(getattr(cb, "message", None), "chat", None)
        uid = getattr(getattr(cb, "from_user", None), "id", None)
        if not (chat is not None and uid is not None and self.bot.allowed(uid)):
            return
        data = cb.data or ""
        if data.startswith("confirm:"):
            self._on_confirm(update, data, cb)
            return
        note = None
        screen, arg = "home", None
        if data.startswith("nav:"):
            parts = data.split(":", 2)
            screen, arg = parts[1], (parts[2] if len(parts) > 2 else None)
        elif data.startswith("act:"):
            note, screen, arg = self._act(chat.id, data[4:])
            if screen is None:                       # the action answered by itself (confirmation, prompt)
                self.bot.answer(cb.id, note)
                return
        elif data.startswith("requeue:"):            # legacy buttons kept working
            status = data.split(":", 1)[1]
            res = service.bulk_requeue(self.conn, status)
            note = f"requeue {status}: {res['requeued']}/{res['requeued'] + res['failed']}"
        elif data == "control:pause":
            note = service.set_paused(self.conn, True)["message"]
        elif data == "control:resume":
            note = service.set_paused(self.conn, False)["message"]
        view = self._view(screen, arg)
        changed = self.bot.edit(chat.id, cb.message.message_id, view.text, self._markup(view.rows))
        self.bot.answer(cb.id, note or ("✓ Déjà à jour" if changed is False else None))

    def _act(self, chat_id: int, action: str):
        """Run a button action. Returns (toast, screen to show next, arg); screen None = nothing to redraw."""
        kind, _, arg = action.partition(":")
        if kind == "cancel" and arg.isdigit():
            self._cancel(chat_id, [arg])                         # asks for confirmation in a new message
            return "Confirmez l'annulation ci-dessous", None, None
        if kind == "xreq" and arg.isdigit():
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Oui, annuler", callback_data=f"confirm:xreq:{arg}"),
                                        InlineKeyboardButton("❌ Non", callback_data="confirm:no")]])
            msg = self.bot.send_raw(chat_id, f"⚠️ Annuler la demande #{arg} ? Son téléchargement continue si un autre "
                                             "utilisateur (ou le canal) attend le même média.", kb)
            if msg is not None and getattr(msg, "message_id", None) is not None:
                self._pending_cancel[msg.message_id] = ("xreq", int(arg))
            return "Confirmez l'annulation ci-dessous", None, None
        if kind == "retry" and arg.isdigit():
            return service.requeue_episode(self.conn, int(arg)).get("message"), "jobs", None
        if kind == "retryall":
            res = service.bulk_requeue(self.conn, "failed", "structure_changed", "blocked")
            return f"{res['requeued']} relancé(s)", "jobs", None
        if kind == "check":
            return service.request_force_check(self.conn, arg).get("message"), "anime", None
        if kind == "toggle":
            item = next((a for a in service.anime_list(self.conn) if a["anime_key"] == arg), None)
            if item is None:
                return "anime inconnu", "anime", None
            return service.set_anime_enabled(self.conn, arg, not item["enabled"]).get("message"), "anime", None
        if kind == "add":
            self._pending_add.add(chat_id)
            self.bot.send(chat_id, "➕ Envoyez l'URL de la page de l'anime\n"
                                   "(ex. https://voir-anime.to/anime/nom-de-l-anime/)\n"
                                   "Les épisodes sortis aujourd'hui seront publiés ; les plus anciens restent connus.")
            return "En attente de l'URL", None, None
        if kind == "ack" and arg.isdigit():
            from . import alerts
            alerts.ack(self.conn, int(arg))
            self.conn.commit()
            return "✓ Acquittée", "alerts", None
        if kind == "ackall":
            from . import alerts
            n = alerts.clear_closed(self.conn)
            self.conn.commit()
            return f"{n} acquittée(s)", "alerts", None
        if kind == "workerstart":
            return service.start_worker(self.conn)["message"], "system", None
        if kind == "workerstop":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Oui, arrêter", callback_data="confirm:workerstop"),
                                        InlineKeyboardButton("❌ Non", callback_data="confirm:no")]])
            msg = self.bot.send_raw(chat_id, "⚠️ Arrêter le worker ? Les jobs en cours se terminent d'abord "
                                             "(aucune publication n'est coupée). Rien ne sera détecté ni publié "
                                             "tant qu'il n'est pas redémarré.", kb)
            if msg is not None and getattr(msg, "message_id", None) is not None:
                self._pending_cancel[msg.message_id] = ("workerstop", 0)
            return "Confirmez l'arrêt ci-dessous", None, None
        if kind in ("pause", "resume"):
            return service.set_paused(self.conn, kind == "pause")["message"], "system", None
        if kind == "notif":
            from . import notifier
            try:
                on = notifier.toggle(self.conn, arg)
            except ValueError:
                return "notification inconnue", "notif", None
            return ("🟢 activée" if on else "⚪ désactivée"), "notif", None
        return "action inconnue", "home", None

    def _on_confirm(self, update: Update, data: str, cb) -> None:
        mid = cb.message.message_id
        pending = self._pending_cancel.pop(mid, None)
        self.bot.answer(cb.id)
        if not pending:
            return
        eid = pending[1]
        if pending[0] == "workerstop":
            if data == "confirm:workerstop":
                res = service.request_worker_stop(self.conn)
                self.bot.edit(cb.message.chat.id, mid, f"✅ {res['message']}", None)
            else:
                self.bot.edit(cb.message.chat.id, mid, "Arrêt abandonné.", None)
            return
        if pending[0] == "xreq":
            if data == f"confirm:xreq:{eid}":
                res = service.cancel_request(self.conn, eid, confirm=True)
                self.bot.edit(cb.message.chat.id, mid, f"✅ {res.get('message', '?')} (#{eid})", None)
            else:
                self.bot.edit(cb.message.chat.id, mid, "Annulation abandonnée.", None)
            return
        if data == f"confirm:cancel:{eid}":
            res = service.cancel_episode(self.conn, eid)
            self.bot.edit(cb.message.chat.id, mid,
                          f"✅ {res.get('message', '?')}\n\n{done_text()}", None)
        else:
            self.bot.edit(cb.message.chat.id, mid, "Annulation abandonnée.", None)

    def _send_requests(self, chat_id: int, args: list[str]) -> None:
        rows = service.requests_overview(self.conn, state=args[0] if args else None, limit=15)
        if not rows:
            self.bot.send(chat_id, "Aucune demande.")
            return
        lines = ["📥 Demandes utilisateurs"]
        kb = []
        for r in rows:
            ep = f"E{r['episode_number']}" if r["episode_number"] is not None else ("saison" if r["kind"] == "season" else "dernier")
            prog = f" {r['progress']['done']}/{r['progress']['total']}" if r["kind"] == "season" else ""
            lines.append(f"#{r['id']} · {r['state']} · {r['title'] or r['anime_key']} {ep} {r['version']}{prog} · user {r['user_id']}"
                         + (f" · {r['error_code']}" if r["error_code"] else ""))
            if r["state"] not in ("COMPLETED", "CANCELLED", "EXPIRED", "FAILED"):
                kb.append([(f"❌ Annuler #{r['id']}", f"act:xreq:{r['id']}")])
        self.bot.send(chat_id, "\n".join(lines), self._markup(kb) if kb else None)

    def _history_text(self, args: list[str]) -> str:
        limit = int(args[0]) if args and args[0].isdigit() else 15
        rows = repo.history(self.conn, min(limit, 50))
        if not rows:
            return "Historique vide."
        return "🕘 Historique (épisodes)\n" + "\n".join(
            f"#{r['id']} · {r['media_ref'] or r['anime_key']} · {r['origin']} · {r['status']} · {r['updated_at']}" for r in rows)

    def _stats_text(self) -> str:
        s = service.stats(self.conn)
        return ("📊 Statistiques\n"
                f"épisodes: {sum(s['episodes'].values())} · publiés aujourd'hui: {s['published_today']}\n"
                f"utilisateurs: {s['users']} · demandes aujourd'hui: {s['requests_today']}\n"
                f"demandes par état: {s['requests'] or '—'}\n"
                f"livraisons: {s['deliveries'] or '—'} · aujourd'hui: {s['deliveries_today']}\n"
                f"médias partagés entre demandes: {s['shared_media']}")

    def send_status(self, chat_id: int) -> None:
        self._send_view(chat_id, "home")            # always a NEW message: never an identical edit

    def _cancel(self, chat_id: int, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            self.bot.send(chat_id, "usage : /cancel <id>  (annule + sort de file)")
            return
        eid = int(args[0])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Oui, annuler", callback_data=f"confirm:cancel:{eid}"),
             InlineKeyboardButton("❌ Non", callback_data="confirm:no")],
        ])
        msg = self.bot.send_raw(
            chat_id,
            f"⚠️ Annuler l'épisode {eid} ? Il sera retiré de la file et marqué "
            f"failed (jamais publié automatiquement).",
            kb)
        if msg is not None and getattr(msg, "message_id", None) is not None:
            self._pending_cancel[msg.message_id] = ("cancel", eid)

    def _anime(self, args: list[str]) -> str:
        if not args:
            items = service.anime_list(self.conn)
            if not items:
                return "aucun anime déclaré"
            return "animes :\n" + "\n".join(
                f"{'▶️' if r['enabled'] else '⏸️'}  {r['anime_key']:<18} {(r['title'] or '')[:22]:<22} "
                f"queue={r['queued']} publié={r['published']} contrôlé={(r['last_successful_check_at'] or 'jamais')[:16]}"
                for r in items)
        if args[0].lower() == "add":
            if len(args) < 2:
                return "usage : /anime add <url de la page anime>"
            res = service.add_anime_from_url(self.conn, self.cfg, args[1], fetch=self.fetch)
            head = ("✅ " if res.get("ok") else "⛔ ") + res.get("message", "?")
            if not res.get("ok"):
                return head
            return (head + "\n" + res["title"] + "\ncontrôle au prochain cycle, ou /check "
                    + res["anime_key"])
        key = args[0]
        if len(args) >= 2 and args[1].lower() in ("check", "force", "force-check"):
            return service.request_force_check(self.conn, key)["message"]
        if len(args) >= 3 and args[1].lower() == "title":
            return service.update_anime(self.conn, key, title=" ".join(args[2:]))["message"]
        if len(args) < 2:
            item = next((a for a in service.anime_list(self.conn) if a["anime_key"] == key), None)
            if not item:
                return f"anime inconnu : {key}"
            return f"{'▶️ ACTIF' if item['enabled'] else '⏸️ PAUSE'}  {key} " \
                   f"(queue={item['queued']}, publié={item['published']})"
        flag = args[1].lower()
        if flag in ("on", "enable", "actif", "1"):
            return service.set_anime_enabled(self.conn, key, True)["message"]
        if flag in ("off", "disable", "pause", "0"):
            return service.set_anime_enabled(self.conn, key, False)["message"]
        return "usage : /anime [key [on|off]]"

    def _alerts(self, args: list[str]) -> str:
        from . import alerts
        n = max(1, min(int(args[0]) if args and args[0].isdigit() else 10, 25))
        items = alerts.list_alerts(self.conn, limit=n)
        if not items:
            return "aucune alerte ouverte 🎉"
        out = [f"{(r['count'] if r['count'] > 1 else '')} {r['id']}  {r['kind']:<22} "
               f"{r['akey']:<18} x{r['count']}\n    {(r['title'] or '')[:80]}" for r in items]
        return f"alertes ouvertes ({len(items)}) :\n" + "\n".join(out)

    def _episodes(self, args: list[str]) -> str:
        status = args[0] if args and not args[0].isdigit() else None
        limit = int(args[1] if len(args) > 1 else (args[0] if args and args[0].isdigit() else 10))
        limit = max(1, min(limit, 50))
        rows = service.episodes_query(self.conn, status, limit)
        if not rows:
            return f"aucun épisode{f' en {status}' if status else ''}"
        lines = [f"{r['id']:>4}  {r['anime_key'][:18]:<18} E{r['episode_number'] or '?'}  {r['status']}"
                 for r in rows]
        return f"épisodes ({len(rows)}) :\n" + "\n".join(lines)

    def _errors(self, args: list[str]) -> str:
        n = max(1, min(int(args[0]) if args and args[0].isdigit() else 10, 25))
        from . import repo
        items = repo.errors_recent(self.conn, n)
        if not items:
            return "aucune erreur"
        return "erreurs :\n" + "\n".join(
            f"{r['id']}  {r['status']}  {(r['last_error'] or '')[:90]}" for r in items)

    def _queue(self) -> str:
        from . import repo
        heads = repo.next_heads(self.conn, 25)
        by_anime = repo.queue_by_anime(self.conn)
        if not by_anime:
            return "file vide"
        return "file par anime :\n" + "\n".join(
            f"{r['anime_key']} → {r['queued']} en attente" for r in by_anime) + \
            (f"\ntêtes (prochains) : {len(heads)}" if heads else "")

    def _capacity(self) -> str:
        from . import repo
        cap = repo.load_capacity(self.conn)
        lines = [f"{k.replace('_', ' ')} : {v}" for k, v in sorted(cap.items()) if v]
        return "capacités :\n" + "\n".join(lines[:22])

    def _requeue(self, args: list[str]) -> str:
        if not args or not args[0].isdigit():
            return "usage : /requeue <id>"
        res = service.requeue_episode(self.conn, int(args[0]))
        return res.get("message", "?")

    def _fail(self, args: list[str]) -> str:
        if not args or not args[0].isdigit():
            return "usage : /fail <id>"
        res = service.fail_episode(self.conn, int(args[0]))
        return res.get("message", "?")

    def _jobs(self) -> str:
        items = service.jobs(self.conn, 15)
        if not items:
            return "aucun job actif"
        return "jobs actifs :\n" + "\n".join(
            f"#{j['id']} {(j['anime_title'] or j['anime_key'])[:20]} E{j['episode_number'] or '?'} "
            f"{j['status']} {j['progress']['step']}/{j['progress']['of']}" for j in items)

    def _system(self) -> str:
        s = service.system_status(self.conn, self.cfg) if self.cfg is not None else {}
        gb = lambda n: "?" if n is None else f"{n / 1073741824:.1f} Go"          # noqa: E731
        return (f"CPU {s.get('cpu_percent', '?')} % · RAM {s.get('ram_percent', '?')} %\n"
                f"disque libre {gb(s.get('disk_free_bytes'))} · réseau ↓{gb(s.get('net_bytes_recv'))} "
                f"↑{gb(s.get('net_bytes_sent'))}\n"
                f"jobs actifs {s.get('jobs_active', '?')} · en file {s.get('queued', '?')} · "
                f"contrôle toutes les {int((s.get('poll_interval_seconds') or 0) // 60)} min\n"
                f"worker : {'actif' if s.get('worker') else 'arrêté'}"
                f"{' · ⏸️ PAUSE' if s.get('paused') else ''}")

    def _help(self) -> str:
        return ("Administration V2\n"
                "/status · /episodes [status] [n] · /errors [n]\n"
                "/queue · /jobs · /system · /capacity · /alerts [n]\n"
                "/requeue <id> · /retry <id> · /fail <id> · /cancel <id>\n"
                "/pause · /resume · /anime [key [on|off]]\n"
                "/anime add <url> · /anime <key> check|title <texte> · /check <key>\n"
                "Le clavier inline de /status permet requeue / pause en masse.")


def status_line(conn) -> str:
    """Live control-state line appended to the /status render."""
    try:
        parts = []
        if service.is_paused(conn):
            parts.append("⏸️ FILE EN PAUSE")
        disabled = [a["anime_key"] for a in service.anime_list(conn) if not a["enabled"]]
        if disabled:
            parts.append("animes en pause : " + ", ".join(disabled[:5]))
        from . import alerts
        open_a = alerts.open_count(conn)
        parts.append(f"alertes ouvertes : {open_a}")
        return "\n" + " · ".join(parts)
    except Exception:
        return ""


def done_text() -> str:
    return "─ done — /status pour l'état courant."


def run_admin_loop(cfg: app_config.AppConfig, stop: threading.Event | None = None) -> None:
    """Blocking long-poll loop; start it in a daemon thread."""
    conn = db.connect()
    db.migrate(conn)
    bot = AdminBot(cfg)
    router = AdminRouter(bot, conn, cfg)
    bot.set_commands()
    offset: int | None = None
    while not (stop is not None and stop.is_set()):
        updates = bot.get_updates(offset)
        for u in updates:
            if u.update_id is not None:
                offset = u.update_id + 1
            try:
                router.handle_update(u)
            except Exception:
                logger.exception("échec traitement update")
        if not updates:
            time.sleep(0.5)
    bot.close()
    db.close()