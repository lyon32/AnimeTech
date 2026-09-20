"""Screens of the Telegram admin: pure functions returning (HTML text, button rows).

No Telegram library here — views only read the database through `service` and format it, so every
screen is unit-testable.  All text coming from the source (titles, errors) goes through `esc()`.
Times are shown in local time; `now` and `tz` are injectable so tests are deterministic.

Callback data (<= 64 bytes, checked by tests):
  nav:home | nav:jobs | nav:anime | nav:animedetail:<key> | nav:alerts | nav:system | nav:notif
  act:cancel:<id> | act:retry:<id> | act:retryall | act:check:<key> | act:toggle:<key> | act:add
  act:ack:<id> | act:ackall | act:pause | act:resume | act:notif:<key>
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, NamedTuple

from . import alerts, service
from .timeutil import now_utc

MAX_LIST = 8                 # items per list screen (Telegram messages are limited to 4096 chars)
MAX_CALLBACK_BYTES = 64


class Html(str):
    """Text already formatted as Telegram HTML (the bot sends it with parse_mode=HTML)."""


class View(NamedTuple):
    text: Html
    rows: list                # list[list[tuple[label, callback_data]]]


# ── helpers ──────────────────────────────────────────────────────────────────────

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local(ts: str | None, tz: tzinfo | None = None) -> datetime | None:
    if not ts:
        return None
    try:
        return _parse(ts).astimezone(tz)
    except ValueError:
        return None


def hm(ts: str | None, tz: tzinfo | None = None) -> str:
    d = local(ts, tz)
    return d.strftime("%H:%M") if d else "—"


def hms(ts: str | None, tz: tzinfo | None = None) -> str:
    d = local(ts, tz)
    return d.strftime("%H:%M:%S") if d else "—"


def day(ts: str | None, tz: tzinfo | None = None) -> str:
    d = local(ts, tz)
    return d.strftime("%d/%m") if d else "—"


def ago(ts: str | None, now: str) -> str:
    if not ts:
        return "jamais"
    try:
        s = max(0, int((_parse(now) - _parse(ts)).total_seconds()))
    except ValueError:
        return "—"
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {s // 60} min"
    if s < 86400:
        return f"il y a {s // 3600} h {s % 3600 // 60:02d}"
    return f"il y a {s // 86400} j"


def bar(percent: int, width: int = 7) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    return "▓" * filled + "░" * (width - filled)


def size_label(n: int | None) -> str:
    if not n:
        return ""
    return f"{n / 1073741824:.1f} Go" if n >= 1073741824 else f"{round(n / 1048576)} Mo"


STATE_LABELS = {
    "queued": "en file", "retry_wait": "en attente", "downloading": "téléchargement",
    "downloaded": "téléchargé", "validating": "validation", "validated": "validé",
    "publishing_thumbnail": "envoi de la miniature", "thumbnail_published": "miniature publiée",
    "publishing_video": "envoi de la vidéo", "published": "publié", "cleanup_pending": "publié",
    "cleaned": "publié (fichier supprimé)", "failed": "échec", "structure_changed": "structure du site changée",
    "blocked": "bloqué", "cleanup_blocked": "nettoyage bloqué", "discovered": "connu", "skipped_dup": "doublon",
}

ALERT_LABELS = {
    "new_episode": "🆕 Nouvel épisode", "published": "✅ Publié", "retry": "🔁 Vidéo inaccessible",
    "definitive_failure": "❌ Échec définitif", "telegram_error": "📨 Erreur Telegram",
    "structure_changed": "🧩 Structure du site changée", "recovery_after_crash": "💥 Publication interrompue",
    "cleanup_blocked": "🧹 Nettoyage bloqué", "low_disk": "💾 Disque presque plein",
    "worker_stopped": "⏹ Worker arrêté", "discovery_error": "📡 Surveillance en erreur",
    "scheduler_problem": "⚙️ Problème du scheduler", "download_started": "⬇️ Téléchargement",
    "download_finished": "⬇️ Téléchargé",
}


def _name(row: dict) -> str:
    return row.get("anime_title") or row.get("anime_key") or "?"


def _ep(row: dict) -> str:
    n = row.get("episode_number")
    return f"E{n}" if n is not None else "E?"


def _footer(now: str, tz) -> str:
    return f"<i>mis à jour {hms(now, tz)}</i>"


HOME_ROW = [("◀ Accueil", "nav:home")]


def _check(callback: str) -> str:
    assert len(callback.encode()) <= MAX_CALLBACK_BYTES, callback
    return callback


# ── home ─────────────────────────────────────────────────────────────────────────

def home(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None) -> View:
    now = now or now_utc()
    d = service.dashboard(conn, cfg, now=now)
    s = d["system"]
    lines = [f"<b>🎬 V1 · Tableau de bord</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━"]
    lines.append(("🟢 Worker actif" if d["worker_alive"] else "🔴 Worker arrêté") +
                 (" · ⏸ file en pause" if d["paused"] else " · file active"))
    if d["animes_watched"] == 0:
        lines.append("📡 Aucun anime surveillé")
    elif not d["worker_alive"]:
        lines.append("📡 Surveillance à l'arrêt (worker éteint)")
    else:
        ok = "✅" if not d["last_check_error"] else "❌"
        lines.append(f"📡 Prochain cycle {hm(d['next_check'], tz)} (dernier contrôle {hm(d['last_check_ok'], tz)} {ok})")
    lines.append(f"📅 Aujourd'hui : {d['today']['detected']} détecté(s) · {d['today']['published']} publié(s)")
    lines.append("")

    if d["running"]:
        lines.append(f"📥 <b>EN COURS ({len(d['running'])})</b>")
        for j in d["running"][:MAX_LIST]:
            p = j["progress"]
            lines.append(f" • {esc(_name(j))} {_ep(j)} · {STATE_LABELS.get(j['status'], j['status']).capitalize()} {p['step']}/{p['of']}")
            lines.append(f"   {bar(p['percent'])} {p['percent']} %")
    else:
        lines.append("😴 Rien en cours")
    if d["waiting"]:
        lines.append(f"⏳ <b>EN ATTENTE ({len(d['waiting'])})</b>")
        for j in d["waiting"][:MAX_LIST]:
            when = f" (prochain essai {hm(j['next_retry_at'], tz)})" if j.get("next_retry_at") and j["status"] == "retry_wait" \
                and j["reason"] != "source pas encore prête" else ""
            lines.append(f" • {esc(_name(j))} {_ep(j)} · {esc(j['reason'])}{when}")
    if d["recent"]:
        lines.append("✅ <b>DERNIERS PUBLIÉS</b>")
        for r in d["recent"]:
            extra = f" · {size_label(r['file_size'])}" if r.get("file_size") else ""
            lines.append(f" • {esc(_name(r))} {_ep(r)}{extra} · {ago(r['published_at'], now)}")
    lines.append("")
    lines.append(f"📚 {d['animes_watched']} anime · {d['published_total']} publiés")
    todo = len(d["attention"])
    lines.append(f"⚠️ {todo} à traiter · {d['alerts_open']} alerte{'s' if d['alerts_open'] > 1 else ''}" if (todo or d["alerts_open"])
                 else "✨ Rien à traiter")
    if s.get("disk_free_bytes") is not None:
        lines.append(f"💾 Disque {size_label(int(s['disk_free_bytes']))} libres · CPU {round(s.get('cpu_percent') or 0)} % · "
                     f"RAM {round(s.get('ram_percent') or 0)} %")
    rows = [[("📥 Jobs", _check("nav:jobs")), ("📚 Anime", _check("nav:anime")), ("⚠️ Alertes", _check("nav:alerts"))],
            [("📊 Système", _check("nav:system")), ("🔁 Cycles", _check("nav:cycles")),
             ("🔔 Notifications", _check("nav:notif"))],
            [("🔄 Actualiser", _check("nav:home"))]]
    return View(Html("\n".join(lines)), rows)


# ── jobs ─────────────────────────────────────────────────────────────────────────

def jobs_view(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None) -> View:
    now = now or now_utc()
    d = service.dashboard(conn, cfg, now=now)
    lines = [f"<b>📥 Jobs</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━"]
    rows: list = []
    items = [(j, True) for j in d["running"]] + [(j, False) for j in d["waiting"]]
    if not items and not d["attention"]:
        lines.append("Aucun job.")
    for j, is_running in items[:MAX_LIST]:
        if is_running:
            p = j["progress"]
            lines.append(f"#{j['id']} {esc(_name(j))} {_ep(j)} · {STATE_LABELS.get(j['status'], j['status'])} "
                         f"{p['step']}/{p['of']}\n   {bar(p['percent'])} {p['percent']} %")
        else:
            lines.append(f"#{j['id']} {esc(_name(j))} {_ep(j)} · {esc(j['reason'])}")
        rows.append([(f"❌ Annuler {_ep(j)}", _check(f"act:cancel:{j['id']}")),
                     (f"🔁 Relancer {_ep(j)}", _check(f"act:retry:{j['id']}"))] if not is_running else
                    [(f"❌ Annuler #{j['id']} {_ep(j)}", _check(f"act:cancel:{j['id']}"))])
    if len(items) > MAX_LIST:
        lines.append(f"… et {len(items) - MAX_LIST} autre(s)")
    if d["attention"]:
        lines.append("")
        lines.append(f"⚠️ <b>À TRAITER ({len(d['attention'])})</b>")
        for a in d["attention"][:MAX_LIST]:
            why = (a.get("last_error") or "").split(":", 1)[0][:40]
            lines.append(f"#{a['id']} {esc(_name(a))} {_ep(a)} · {STATE_LABELS.get(a['status'], a['status'])}"
                         + (f" · {esc(why)}" if why else ""))
        rows.append([(f"🔁 Tout relancer ({len(d['attention'])})", _check("act:retryall"))])
    rows.append(HOME_ROW)
    return View(Html("\n".join(lines)), rows)


# ── animes ───────────────────────────────────────────────────────────────────────

def anime_view(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None) -> View:
    now = now or now_utc()
    items = service.anime_list(conn)
    lines = [f"<b>📚 Anime surveillés ({len(items)})</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━"]
    rows: list = []
    if not items:
        lines.append("Aucun anime. Utilisez ➕ Ajouter.")
    for a in items[:MAX_LIST]:
        state = "▶️" if a["enabled"] else "⏸"
        check = "❌ " + esc((a.get("last_check_error") or "")[:40]) if a.get("last_check_error") \
            else ("✅ " + ago(a.get("last_successful_check_at"), now) if a.get("last_successful_check_at") else "pas encore contrôlé")
        auto = " 🌐" if a.get("auto_added") else ""          # 🌐 = added automatically from the site's feed
        lines.append(f"{state} <b>{esc(a['title'] or a['anime_key'])}</b>{auto}\n   {a['published']} publiés · {a['queued']} en file · {check}")
        key = a["anime_key"]
        rows.append([("🔍 Contrôler", _check(f"act:check:{key}")),
                     ("⏸ Pause" if a["enabled"] else "▶️ Reprendre", _check(f"act:toggle:{key}")),
                     ("📄 Détail", _check(f"nav:animedetail:{key}"))])
    rows.append([("➕ Ajouter un anime", "act:add")])
    rows.append(HOME_ROW)
    return View(Html("\n".join(lines)), rows)


def anime_detail(conn: sqlite3.Connection, cfg, anime_key: str, *, now: str | None = None,
                 tz: tzinfo | None = None) -> View:
    now = now or now_utc()
    a = next((x for x in service.anime_list(conn) if x["anime_key"] == anime_key), None)
    if a is None:
        return View(Html("Anime introuvable."), [[("◀ Anime", "nav:anime")]])
    eps = conn.execute("SELECT id, episode_number, status, published_at FROM episodes WHERE anime_key=? AND status<>'discovered' "
                       "ORDER BY episode_number DESC, id DESC LIMIT 5", (anime_key,)).fetchall()
    known = conn.execute("SELECT COUNT(*) FROM episodes WHERE anime_key=? AND status='discovered'", (anime_key,)).fetchone()[0]
    lines = [f"<b>📄 {esc(a['title'] or anime_key)}</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━",
             f"{'▶️ Actif' if a['enabled'] else '⏸ En pause'} · {a['published']} publiés · {known} épisodes déjà connus",
             f"Dernier contrôle : {ago(a.get('last_checked_at'), now)}"]
    if a.get("last_check_error"):
        lines.append(f"❌ {esc(a['last_check_error'][:120])}")
    lines.append("")
    for e in eps:
        when = f" · {ago(e['published_at'], now)}" if e["published_at"] else ""
        lines.append(f" • E{e['episode_number'] if e['episode_number'] is not None else '?'} · "
                     f"{STATE_LABELS.get(e['status'], e['status'])}{when}")
    rows = [[("🔍 Contrôler maintenant", _check(f"act:check:{anime_key}"))], [("◀ Anime", "nav:anime")]]
    return View(Html("\n".join(lines)), rows)


# ── watcher cycles ───────────────────────────────────────────────────────────────

def cycles_view(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None) -> View:
    """The last global cycles: how many anime were checked, what was new, what failed."""
    from . import discovery
    now = now or now_utc()
    d = service.dashboard(conn, cfg, now=now)
    titles = {a["anime_key"]: (a["title"] or a["anime_key"]) for a in d["animes"]}
    lines = [f"<b>🔁 Cycles de surveillance</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━",
             f"Toutes les {int(d['poll_interval_seconds'] // 60)} min · prochain {hm(d['next_check'], tz)}"
             if d["worker_alive"] else "Worker arrêté : aucun cycle en cours",
             f"📅 Aujourd'hui : {d['today']['detected']} détecté(s) · {d['today']['published']} publié(s)", ""]
    items = discovery.recent_cycles(conn, 5)
    if not items:
        lines.append("Aucun cycle terminé pour l'instant.")
    for c in items:
        new = c.get("new_episodes") or 0
        lines.append(f"{hm(c.get('finished_at'), tz)} · {c.get('checked')} anime · "
                     + (f"<b>{new} nouveau{'x' if new > 1 else ''}</b>" if new else "aucun nouveau")
                     + (f" · ⚠️ {c['errors']} erreur(s)" if c.get("errors") else ""))
        feed = c.get("feed")
        if feed and not feed.get("error"):
            lines.append(f"   🌐 site : {feed.get('today', 0)} épisode(s) du jour · {feed.get('new_anime', 0)} nouvel(s) anime"
                         f" · {feed.get('already_known', 0)} déjà connu(s)")
        for key, res in (c.get("animes") or {}).items():
            if res.get("new") or res.get("error"):
                tag = " (rattrapage du jour)" if res.get("catchup") else ""
                lines.append(f"   • {esc(titles.get(key, key))} : "
                             + (f"{res['new']} nouveau(x){tag}" if res.get("new") else f"erreur — {esc(str(res['error'])[:60])}"))
    return View(Html("\n".join(lines)), [[("🔄 Actualiser", _check("nav:cycles"))], HOME_ROW])


# ── alerts ───────────────────────────────────────────────────────────────────────

def _alert_subject(conn: sqlite3.Connection, akey: str) -> str:
    if akey.startswith("ep:") and akey[3:].isdigit():
        r = conn.execute("SELECT e.episode_number, COALESCE(a.title, e.anime_key) AS t FROM episodes e "
                         "LEFT JOIN animes a ON a.anime_key=e.anime_key WHERE e.id=?", (int(akey[3:]),)).fetchone()
        if r:
            return f"{r['t']} E{r['episode_number'] if r['episode_number'] is not None else '?'}"
    if akey.startswith("anime:"):
        return akey[6:]
    return akey


def alerts_view(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None) -> View:
    now = now or now_utc()
    items = alerts.list_alerts(conn, limit=25, status="open")
    lines = [f"<b>⚠️ Alertes ({len(items)})</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━"]
    rows: list = []
    if not items:
        lines.append("✨ Aucune alerte ouverte.")
    for it in items[:MAX_LIST]:
        label = ALERT_LABELS.get(it["kind"], it["kind"])
        times = f" ×{it['count']}" if it["count"] > 1 else ""
        lines.append(f"{label} · {esc(_alert_subject(conn, it['akey']))}{times}\n   {ago(it['last_raised_at'], now)}")
        rows.append([(f"✅ OK #{it['id']}", _check(f"act:ack:{it['id']}"))])
    if len(items) > MAX_LIST:
        lines.append(f"… et {len(items) - MAX_LIST} autre(s)")
    if items:
        rows.append([("✅ Tout acquitter", "act:ackall")])
    rows.append(HOME_ROW)
    return View(Html("\n".join(lines)), rows)


# ── system ───────────────────────────────────────────────────────────────────────

def system_view(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None,
                telegram_ok: bool | None = None) -> View:
    now = now or now_utc()
    s = service.system_status(conn, cfg)
    disk_pct = round(s.get("disk_percent") or 0)
    lines = [f"<b>📊 Système</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━",
             f"CPU  {bar(round(s.get('cpu_percent') or 0))} {round(s.get('cpu_percent') or 0)} %",
             f"RAM  {bar(round(s.get('ram_percent') or 0))} {round(s.get('ram_percent') or 0)} %",
             f"Disque  {bar(disk_pct)} {disk_pct} % · {size_label(int(s['disk_free_bytes'])) if s.get('disk_free_bytes') else '?'} libres",
             f"Réseau  ↓ {size_label(int(s['net_bytes_recv'])) if s.get('net_bytes_recv') else '?'}"
             f" · ↑ {size_label(int(s['net_bytes_sent'])) if s.get('net_bytes_sent') else '?'}", ""]
    ws = service.worker_state(conn, now=now)
    lines.append("Worker : " + ("🟡 arrêt en cours (termine ses jobs)" if ws["stopping"]
                                else "🟢 actif" if ws["alive"] else "🔴 arrêté"))
    lines.append(f"Surveillance : toutes les {int(s['poll_interval_seconds'] // 60)} min")
    lines.append(f"File : {s['queued']} en attente · {s['jobs_active']} en cours" + (" · ⏸ EN PAUSE" if s["paused"] else ""))
    if telegram_ok is not None:
        lines.append("Serveur Telegram : " + ("🟢 joignable" if telegram_ok else "🔴 injoignable"))
    limit = (getattr(cfg, "limits", None) or {}).get("max_safe_publish_mib")
    if limit:
        lines.append(f"Taille max publiable : {limit} Mio")
    worker_btn = ([] if ws["stopping"] else [("⏹ Arrêter le worker", "act:workerstop")] if ws["alive"]
                  else [("▶️ Démarrer le worker", "act:workerstart")])
    rows = [[("▶️ Reprendre la file", "act:resume")] if s["paused"] else [("⏸ Mettre en pause", "act:pause")],
            worker_btn, [("🔄 Actualiser", "nav:system")], HOME_ROW[0:1]]
    rows = [r for r in rows if r]
    return View(Html("\n".join(lines)), rows)


# ── notifications settings ───────────────────────────────────────────────────────

NOTIF_LABELS = {"published": "✅ Épisode publié", "new_episode": "🆕 Nouvel épisode détecté",
                "problems": "⚠️ Problèmes", "daily": "🗓 Résumé quotidien"}


def notifications_view(conn: sqlite3.Connection, cfg, *, now: str | None = None, tz: tzinfo | None = None) -> View:
    from . import notifier
    now = now or now_utc()
    lines = [f"<b>🔔 Notifications</b>  {_footer(now, tz)}", "━━━━━━━━━━━━━━━━━━━━",
             "Messages privés envoyés aux administrateurs (jamais dans le canal).", ""]
    rows: list = []
    for key, label in NOTIF_LABELS.items():
        on = notifier.enabled(conn, key)
        lines.append(f"{'🟢' if on else '⚪'} {label}")
        rows.append([(("Désactiver " if on else "Activer ") + label.split(' ', 1)[1], _check(f"act:notif:{key}"))])
    rows.append(HOME_ROW)
    return View(Html("\n".join(lines)), rows)
