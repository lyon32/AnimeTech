/* Panneau d'administration — vanilla JS, aucune dépendance. Tout texte dynamique passe par html`` (échappé). */
"use strict";

// ── escaping: html`` escapes every interpolated value unless it is already a Raw fragment ──────────
class Raw { constructor(s) { this.s = s; } }
const raw = (s) => new Raw(s);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
function part(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(part).join("");
  if (v == null || v === false) return "";
  return esc(v);
}
const html = (strings, ...vals) => raw(strings.reduce((out, s, i) => out + s + (i < vals.length ? part(vals[i]) : ""), ""));
const $ = (sel, root = document) => root.querySelector(sel);
const NB = " ";

// ── icons: one authored set, 1.6 stroke ────────────────────────────────────────────────────────────
const PATHS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
  tv: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 3l4 4 4-4"/>',
  alert: '<path d="M12 3l10 18H2L12 3z"/><path d="M12 10v5M12 18h.01"/>',
  gauge: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/>',
  pulse: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  more: '<path d="M5 12h.01M12 12h.01M19 12h.01" stroke-width="3"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M10 9l5 3-5 3z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  play: '<path d="M7 4l13 8-13 8z"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  left: '<path d="M15 5l-7 7 7 7"/>',
  right: '<path d="M9 5l7 7-7 7"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14-4M4 4v4h4M4 13a8 8 0 0 0 14 4M20 20v-4h-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.2-4.2"/>',
};
const icon = (name) => raw(`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${PATHS[name] || ""}</svg>`);

// ── formatting (French typography: no-break spaces before units and punctuation) ───────────────────
const nf = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const ni = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
function parseTs(s) {
  if (!s) return null;
  let t = String(s).replace(" ", "T");
  if (!/(Z|[+-]\d\d:?\d\d)$/.test(t)) t += "Z";
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? null : d;
}
function size(bytes) {
  if (!bytes) return "";
  return bytes >= 1073741824 ? `${nf.format(bytes / 1073741824)}${NB}Go` : `${ni.format(bytes / 1048576)}${NB}Mo`;
}
const mio = (v) => (v == null ? "—" : `${ni.format(v)}${NB}Mio`);
const pct = (v) => `${ni.format(v || 0)}${NB}%`;
function ago(ts) {
  const d = parseTs(ts);
  if (!d) return "—";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "à l’instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)}${NB}min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)}${NB}h`;
  return `il y a ${Math.floor(s / 86400)}${NB}j`;
}
function until(ts) {
  const d = parseTs(ts);
  if (!d) return "";
  const s = (d.getTime() - Date.now()) / 1000;
  if (s <= 30) return "imminent";
  if (s < 3600) return `dans ${Math.round(s / 60)}${NB}min`;
  if (s < 86400) return `dans ${Math.floor(s / 3600)}${NB}h ${Math.round((s % 3600) / 60)}${NB}min`;
  return `dans ${Math.round(s / 86400)}${NB}j`;
}
const clock = (ts) => { const d = parseTs(ts); return d ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : "—"; };
const dateTime = (ts) => {
  const d = parseTs(ts);
  return d ? d.toLocaleString("fr-FR", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "—";
};
function duration(sec) {
  if (sec == null) return "—";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m ? `${m}${NB}min ${s}${NB}s` : `${s}${NB}s`;
}
const epLabel = (n) => (n == null ? "Épisode ?" : `Épisode ${n}`);
const animeName = (r) => r.anime_title || r.anime_key || "?";

// ── api ────────────────────────────────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 12000);
  try {
    const res = await fetch(path, { ...opts, signal: ctl.signal, headers: { Accept: "application/json", ...(opts.headers || {}) } });
    let body = null;
    try { body = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) throw new Error((body && (body.detail || body.message)) || `Erreur ${res.status}`);
    return body;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Le serveur ne répond pas");
    if (e instanceof TypeError) throw new Error("Serveur injoignable");
    throw e;
  } finally { clearTimeout(timer); }
}
const post = (path) => api(path, { method: "POST" });

// ── shell ──────────────────────────────────────────────────────────────────────────────────────────
const PAGES = [
  { id: "", label: "Vue d’ensemble", icon: "grid", mobile: true },
  { id: "episodes", label: "Épisodes", icon: "list", mobile: true },
  { id: "file", label: "File", icon: "layers", mobile: true },
  { id: "anime", label: "Anime", icon: "tv" },
  { id: "erreurs", label: "Erreurs", icon: "alert", mobile: true, count: true },
  { id: "capacites", label: "Capacités", icon: "gauge" },
  { id: "sante", label: "Santé", icon: "pulse" },
  { id: "reglages", label: "Réglages", icon: "sliders" },
];
const state = { dash: null, route: { page: "", query: {} }, ep: { status: "", q: "", anime: "", offset: 0 }, epData: null, failing: false };
const main = $("#main");

function renderNav() {
  const cur = state.route.page;
  const errors = state.dash ? state.dash.attention.length + state.dash.alerts_open : 0;
  const more = ["anime", "capacites", "sante", "reglages", "plus"].includes(cur);
  $("#nav").innerHTML = html`
    <div class="brand">${icon("film")}<div>Publication<small>Panneau admin</small></div></div>
    ${PAGES.map((p) => html`<a class="nav-link ${p.mobile ? "" : "desk"}" href="#/${p.id}" ${cur === p.id ? raw('aria-current="page"') : ""}>
      ${icon(p.icon)}<span>${p.label}</span>${p.count && errors ? html`<span class="count" aria-label="${errors} à traiter">${errors}</span>` : ""}</a>`)}
    <a class="nav-link more" href="#/plus" ${more ? raw('aria-current="page"') : ""}>${icon("more")}<span>Plus</span></a>`.s;
}

function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [page, qs] = h.split("?");
  const query = Object.fromEntries(new URLSearchParams(qs || ""));
  return { page: page || "", query };
}

function setStatus(dash) {
  const el = $("#status");
  if (!dash) {
    el.dataset.tone = "bad"; $("#status-text").textContent = "Serveur injoignable"; el.href = "#/sante";
    $("#pause").hidden = true; return;
  }
  const b = dash.banner;
  el.dataset.tone = b.level;
  $("#status-text").textContent = b.text;
  el.href = b.issues[0] ? `#/${b.issues[0].page}` : "#/sante";
  const btn = $("#pause");
  btn.hidden = false;
  btn.innerHTML = html`${icon(dash.paused ? "play" : "pause")}<span>${dash.paused ? "Reprendre la file" : "Mettre en pause"}</span>`.s;
  btn.dataset.act = dash.paused ? "resume" : "pause";
  const wb = $("#worker"), w = dash.worker || {};
  if (!wb.dataset.armed) {                          // do not wipe a pending "Confirmer ?" on refresh
    wb.hidden = false;
    wb.disabled = !!w.stopping;
    wb.classList.toggle("primary", !w.alive && !w.stopping);
    wb.classList.toggle("danger", !!w.alive && !w.stopping);
    wb.innerHTML = html`${icon(w.alive ? "stop" : "play")}<span>${w.stopping ? "Arrêt en cours…" : w.alive ? "Arrêter le worker" : "Démarrer le worker"}</span>`.s;
    wb.dataset.act = w.alive ? "worker-stop" : "worker-start";
    if (w.alive && !w.stopping) wb.dataset.confirm = "Confirmer l’arrêt ?"; else delete wb.dataset.confirm;
  }
  $("#stamp").textContent = `Mis à jour à ${clock(new Date().toISOString())}`;
}

// ── shared pieces ──────────────────────────────────────────────────────────────────────────────────
const badge = (tone, label) => html`<span class="badge" data-tone="${tone}"><span class="dot"></span>${label}</span>`;
const stateBadge = (r) => badge(r.tone, cap(r.status_label));
const meter = (v, tone) => html`<div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(v)}" ${tone ? raw(`data-tone="${tone}"`) : ""}><i style="width:${Math.max(0, Math.min(100, v))}%"></i></div>`;
const empty = (title, text) => html`<div class="empty"><strong>${title}</strong>${text || ""}</div>`;
const skeleton = () => html`<div class="stack" aria-busy="true"><div class="sk" style="height:76px"></div><div class="sk" style="height:220px"></div><div class="sk" style="height:160px"></div></div>`;
const failure = (msg) => html`<div class="errbox" role="alert"><div><strong>Impossible de charger cette page</strong><div class="muted">${msg}</div></div><button class="btn" type="button" data-act="reload">${icon("refresh")}Réessayer</button></div>`;
const loadTone = (v) => (v >= 90 ? "bad" : v >= 75 ? "warn" : "ok");

function toast(text, tone) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = text; if (tone) t.dataset.tone = tone;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), tone === "bad" ? 6000 : 3200);
}

// ── pages ──────────────────────────────────────────────────────────────────────────────────────────
function overview(d) {
  const s = d.system || {};
  const nextCheck = !d.animes_watched ? "Aucun anime surveillé"
    : !d.worker_alive ? "À l’arrêt (worker éteint)"
    : `${clock(d.next_check)} · ${until(d.next_check)}`;
  const figure = (v, l, page, tone) => html`<a class="figure" href="#/${page}" ${tone ? raw(`data-tone="${tone}"`) : ""}><div class="v">${v}</div><div class="l">${l}</div></a>`;
  const attention = d.attention.length + d.alerts_open;
  const jobLine = (j, extra) => html`<div class="grow"><div class="title">${animeName(j)} · ${epLabel(j.episode_number)}</div><div class="sub">${extra}</div></div>`;
  const waitMax = 4;
  return html`<div class="overview">
    ${d.banner.issues.length ? html`<div class="issues">${d.banner.issues.map((i) => html`<a class="issue" href="#/${i.page}"><span class="badge" data-tone="${i.tone}"><span class="dot"></span></span>${i.text}</a>`)}</div>` : ""}
    <div class="figures five">
      ${figure(`${d.today.published}/${d.today.detected}`, "Publiés aujourd’hui", "episodes?status=published")}
      ${figure(d.published_total, "Publiés au total", "episodes?status=published")}
      ${figure(d.running.length, "En cours", "file")}
      ${figure(d.waiting.length, "En attente", "file")}
      ${figure(attention, "À traiter", "erreurs", attention ? "bad" : "")}
    </div>
    <div class="ov-grid">
      <div class="ov-col">
        <section class="panel compact"><header><h2>En cours<span class="n">${d.running.length || ""}</span></h2></header>
          ${d.running.length ? html`<ul class="rows">${d.running.map((j) => html`<li>
            ${jobLine(j, `${cap(j.status_label)} · étape ${j.progress.step}/${j.progress.of}`)}
            <div class="inline-meter">${meter(j.progress.percent, "info")}</div><div class="muted pct">${pct(j.progress.percent)}</div></li>`)}</ul>`
            : html`<div class="empty one">Rien en cours — le prochain épisode démarrera dès qu’il sera détecté.</div>`}</section>
        <section class="panel compact"><header><h2>En attente<span class="n">${d.waiting.length || ""}</span></h2><a class="btn sm ghost" href="#/file">Voir la file${d.waiting.length > waitMax ? ` (+${d.waiting.length - waitMax})` : ""}</a></header>
          ${d.waiting.length ? html`<ul class="rows">${d.waiting.slice(0, waitMax).map((j) => html`<li>
            ${jobLine(j, `${cap(j.reason)}${j.status === "retry_wait" && j.next_retry_at && j.reason !== "source pas encore prête" ? ` · essai ${clock(j.next_retry_at)}` : ""}`)}
            ${stateBadge(j)}</li>`)}</ul>`
            : html`<div class="empty one">Aucun épisode en attente.</div>`}</section>
        <section class="panel compact grow-panel"><header><h2>Derniers publiés</h2><a class="btn sm ghost" href="#/episodes?status=published">Tout voir</a></header>
          ${d.recent.length ? html`<ul class="rows scroll">${d.recent.map((r) => html`<li class="click" tabindex="0" data-act="open-ep" data-id="${r.id}" role="button" aria-label="Détails de ${animeName(r)} ${epLabel(r.episode_number)}">
            <div class="grow"><div class="title">${animeName(r)} · ${epLabel(r.episode_number)}</div></div>
            <div class="muted nowrap">${ago(r.published_at)}${r.file_size ? ` · ${size(r.file_size)}` : ""}</div></li>`)}</ul>`
            : html`<div class="empty one">Aucune publication pour l’instant.</div>`}</section>
      </div>
      <div class="ov-col">
        <section class="panel compact"><header><h2>Système</h2><span class="muted">${d.worker_alive ? "Worker actif" : "Worker arrêté"}</span></header>
          ${machine(s)}
          <dl class="kv tight"><dt>Prochain cycle</dt><dd>${nextCheck}</dd>
            <dt>Dernier cycle</dt><dd>${d.last_cycle ? `${d.last_cycle.checked} vérifié${d.last_cycle.checked > 1 ? "s" : ""} · ${d.last_cycle.new_episodes} nouveau${d.last_cycle.new_episodes > 1 ? "x" : ""} · ${ago(d.last_cycle.finished_at)}` : "—"}</dd>
            <dt>Fréquence</dt><dd>toutes les ${Math.round(d.poll_interval_seconds / 60)}${NB}min</dd></dl></section>
        <section class="panel compact grow-panel"><header><h2>Anime surveillés<span class="n">${d.animes_watched || ""}</span></h2><a class="btn sm ghost" href="#/anime">Gérer</a></header>
          ${d.animes.length ? html`<ul class="chips-grid scroll">${d.animes.map((a) => html`<li title="${a.title || a.anime_key}${a.enabled ? "" : " — en pause"}"><span class="dot" data-tone="${a.enabled ? "ok" : "muted"}"></span>
            <span class="t">${a.title || a.anime_key}${a.enabled ? "" : html` <em>en pause</em>`}</span><span class="c">${a.published}${a.queued ? html` <b>+${a.queued}</b>` : ""}</span></li>`)}</ul>`
            : empty("Aucun anime", raw('<a href="#/anime">Ajouter un anime</a>'))}</section>
      </div>
    </div></div>`;
}

function machine(s) {
  const row = (label, v, right) => html`<div class="meter-row"><span>${label}</span>${meter(v || 0, loadTone(v || 0))}<span class="r">${right || pct(v)}</span></div>`;
  if (s.cpu_percent == null && s.disk_percent == null) return empty("Mesures indisponibles");
  return html`<div style="padding-bottom:8px">${row("Processeur", s.cpu_percent)}${row("Mémoire", s.ram_percent)}
    ${row("Disque", s.disk_percent, s.disk_free_bytes ? `${size(s.disk_free_bytes)} libres` : pct(s.disk_percent))}</div>`;
}

// episodes: shell rendered once (inputs keep focus), the table region is repainted
const EP_CHIPS = [["", "Tous"], ["published", "Publiés"], ["running", "En cours"], ["waiting", "En attente"], ["attention", "À traiter"], ["discovered", "Déjà connus"]];
function episodesShell() {
  const f = state.ep;
  return html`<div class="stack">
    <div class="filters">
      <div class="chips" role="group" aria-label="Filtrer par état" id="ep-chips"></div>
      <span style="flex:1"></span>
      <select class="field" id="ep-anime" aria-label="Anime"><option value="">Tous les anime</option></select>
      <input class="field" id="ep-q" type="search" placeholder="Rechercher un titre ou un numéro" aria-label="Rechercher" value="${f.q}">
    </div>
    <section class="panel"><div id="ep-body" class="body flush">${skeleton()}</div></section></div>`;
}
function episodesPaint() {
  const data = state.epData; if (!data) return;
  const f = state.ep, g = data.groups;
  const counts = { "": g.published + g.running + g.waiting + g.attention, published: g.published, running: g.running, waiting: g.waiting, attention: g.attention, discovered: g.discovered };
  $("#ep-chips").innerHTML = EP_CHIPS.map(([k, l]) => html`<button class="chip" type="button" data-act="ep-status" data-status="${k}" aria-pressed="${String(f.status === k)}">${l}<span class="n">${counts[k]}</span></button>`).map((r) => r.s).join("");
  const sel = $("#ep-anime");
  if (sel.options.length !== data.animes.length + 1) sel.innerHTML = html`<option value="">Tous les anime</option>${data.animes.map((a) => html`<option value="${a.anime_key}">${a.title}</option>`)}`.s;
  sel.value = f.anime;
  const from = data.total ? data.offset + 1 : 0, to = Math.min(data.offset + data.limit, data.total);
  $("#ep-body").innerHTML = (data.items.length ? html`<div class="tablewrap"><table class="tbl">
    <thead><tr><th>Anime</th><th>Épisode</th><th>État</th><th class="num">Taille</th><th>Date</th></tr></thead>
    <tbody>${data.items.map((r) => html`<tr class="click" tabindex="0" data-act="open-ep" data-id="${r.id}">
      <td class="name" data-label="Anime">${animeName(r)}</td><td data-label="Épisode" class="keep">${epLabel(r.episode_number)}</td>
      <td data-label="État" class="keep">${stateBadge(r)}</td><td class="num" data-label="Taille">${size(r.file_size) || "—"}</td>
      <td data-label="Date">${ago(r.published_at || r.updated_at)}</td></tr>`)}</tbody></table></div>
    <div class="pager"><span>${from}–${to} sur ${data.total}</span><div class="btns">
      <button class="btn sm" type="button" data-act="ep-page" data-dir="-1" ${data.offset ? "" : raw("disabled")} aria-label="Page précédente">${icon("left")}</button>
      <button class="btn sm" type="button" data-act="ep-page" data-dir="1" ${to < data.total ? "" : raw("disabled")} aria-label="Page suivante">${icon("right")}</button></div></div>`
    : empty("Aucun épisode ne correspond", f.status || f.q || f.anime ? "Essayez un autre filtre." : "")).s;
}
async function episodesLoad() {
  const f = state.ep;
  const qs = new URLSearchParams({ limit: 50, offset: f.offset });
  if (f.status) qs.set("status", f.status);
  if (f.q) qs.set("q", f.q);
  if (f.anime) qs.set("anime", f.anime);
  state.epData = await api(`/api/episodes-view?${qs}`);
  episodesPaint();
}

function file(d) {
  if (!d.lanes.length) return html`<div class="stack">${d.paused ? pausedNote() : ""}<section class="panel">${empty("La file est vide", "Les nouveaux épisodes apparaîtront ici dès qu’ils seront détectés.")}</section></div>`;
  return html`<div class="stack">${d.paused ? pausedNote() : ""}
    <p class="muted note">Les épisodes d’un même anime passent l’un après l’autre, dans l’ordre des numéros.</p>
    <div class="lanes">${d.lanes.map((l) => html`<section class="panel lane"><header><h2>${l.anime_title}<span class="n">${l.items.length} épisode${l.items.length > 1 ? "s" : ""}</span></h2>${l.enabled ? "" : badge("muted", "Anime en pause")}</header>
      <div class="steps">${l.items.map((i) => html`<button class="step" type="button" data-act="open-ep" data-id="${i.id}" data-active="${i.progress.step >= 3 && i.status !== "retry_wait" ? 1 : 0}">
        <span class="t">${epLabel(i.episode_number)}${stateBadge(i)}</span>
        <span class="s">${i.reason ? cap(i.reason) : `Étape ${i.progress.step}/${i.progress.of} · ${pct(i.progress.percent)}`}${i.status === "retry_wait" && i.next_retry_at && i.reason !== "source pas encore prête" ? ` · essai ${clock(i.next_retry_at)}` : ""}</span></button>`)}</div>
      </section>`)}</div></div>`;
}
const pausedNote = () => html`<div class="errbox" style="border-color:var(--warn);background:color-mix(in oklch,var(--warn) 8%,var(--surface))"><div><strong>La file est en pause</strong><div class="muted">Aucun nouveau téléchargement ne démarre tant qu’elle n’est pas reprise.</div></div><button class="btn primary" type="button" data-act="resume">${icon("play")}Reprendre la file</button></div>`;

function cyclesPanel(c) {
  const rows = (c && c.items) || [];
  return html`<header><h2>Cycles de surveillance<span class="n">${rows.length || ""}</span></h2>
    <span class="muted">${c && c.worker_alive ? `toutes les ${Math.round(c.interval_seconds / 60)}${NB}min · prochain ${clock(c.next_cycle)}` : "worker arrêté"}</span></header>
    ${rows.length ? html`<div class="tablewrap"><table class="tbl"><thead><tr><th>Heure</th><th class="num">Anime vérifiés</th><th class="num">Nouveaux</th><th class="num">Erreurs</th><th>Détail</th></tr></thead><tbody>
      ${rows.map((r) => html`<tr><td class="name" data-label="Heure">${clock(r.finished_at)} <span class="muted">· ${ago(r.finished_at)}</span></td>
        <td class="num" data-label="Vérifiés">${r.checked}</td><td class="num" data-label="Nouveaux">${r.new_episodes}</td>
        <td class="num" data-label="Erreurs">${r.errors ? badge("bad", String(r.errors)) : "0"}</td>
        <td data-label="Détail" class="keep">${r.feed ? html`<div class="muted">Site : ${r.feed.today} du jour · ${r.feed.new_anime} nouvel${r.feed.new_anime > 1 ? "s" : ""} anime · ${r.feed.already_known} déjà connu${r.feed.already_known > 1 ? "s" : ""}</div>` : ""}${r.animes.filter((a) => a.new || a.error).map((a) => html`<div>${a.title} : ${a.error ? html`<span class="muted">erreur</span>` : `${a.new} nouveau${a.new > 1 ? "x" : ""}${a.catchup ? " (rattrapage du jour)" : ""}`}</div>`)}${r.animes.some((a) => a.new || a.error) ? "" : html`<span class="muted">rien de nouveau</span>`}</td></tr>`)}</tbody></table></div>`
      : empty("Aucun cycle terminé pour l’instant", "Le premier cycle démarre dès que le worker tourne.")}`;
}

// ── Anime page: find (search or address) → choose season and version → pick episodes → publish to the channel ───────
const finder = { view: "idle", q: "", results: null, open: null, bench: null, busy: false, err: "" };
const VER = { VF: { flag: "VF", name: "Version française" }, VOSTFR: { flag: "VOSTFR", name: "Version originale sous-titrée" } };
const looksLikeUrl = (s) => /^https?:\/\//i.test(s.trim());
const plural = (n, one, many) => `${n}${NB}${n > 1 ? many : one}`;
const BUSY_STATES = ["queued", "processing", "downloading", "retry_wait", "downloaded", "validating", "validated", "publishing_thumbnail", "thumbnail_published", "publishing_video"];

function animePage(d, animes, cycles) {
  return html`<div class="stack">
    <section class="panel finder" aria-labelledby="finder-h"><header><h2 id="finder-h">Télécharger un anime</h2></header>
      <form id="find-form" class="addrow" novalidate role="search"><input class="field" id="find-q" type="search" autocomplete="off" spellcheck="false" placeholder="Nom de l’anime, ou adresse de sa page" aria-label="Nom de l’anime ou adresse de sa page" value="${finder.q}">
        <button class="btn primary" type="submit" ${finder.busy ? raw("disabled") : ""}>${icon("search")}Chercher</button></form>
      <div class="formerr" id="find-err" role="alert" ${finder.err ? "" : raw("hidden")}>${finder.err}</div>
      <div id="finder-body" aria-live="polite">${finderBody()}</div></section>
    <div class="anime-split"><section class="panel" id="anime-list"><header><h2>Anime surveillés<span class="n">${animes.length || ""}</span></h2></header>${animeTable(animes)}</section>
    <section class="panel" id="cycles">${cyclesPanel(cycles)}</section></div></div>`;
}

function finderBody() {
  if (finder.busy) return html`<div class="sk" style="height:96px;margin:0 16px 16px"></div>`;
  if (finder.view === "bench") return benchView(finder.bench);
  if (finder.view === "results") return resultsView(finder.results);
  return html`<div class="hint">Le site a deux catalogues, VF et VOSTFR : la recherche interroge les deux et montre ce qui existe vraiment. Vous pouvez aussi coller l’adresse d’une page.</div>`;
}

function resultsView(r) {
  if (!r.items.length) return empty(`Aucun résultat pour « ${r.query} »`, "Le site ne tolère pas les fautes de frappe : essayez le titre japonais ou anglais, ou collez l’adresse de la page.");
  const main = r.items.filter((s) => s.is_main), other = r.items.filter((s) => !s.is_main);
  const note = r.truncated ? html`<div class="hint warn">Le site limite ses réponses : précisez le titre pour affiner.</div>` : "";
  return html`${note}<ul class="series" role="list">${[...main, ...other].map((s) => seriesRow(s, r.items.indexOf(s)))}</ul>`;
}

function seriesRow(s, i) {
  const open = finder.open === i, seasons = s.seasons.length;
  return html`<li class="srow" data-open="${open ? 1 : 0}"><button class="shead" type="button" data-act="f-open" data-i="${i}" aria-expanded="${String(open)}">
      <span class="sname">${s.name}${s.alt ? html` <span class="muted">· autre titre</span>` : ""}${!s.is_main ? html` <span class="muted">· ${s.kind === "MOVIE" ? "film" : "spécial"}</span>` : ""}</span>
      <span class="svers">${s.versions.map((v) => html`<span class="vtag" data-v="${v}">${VER[v].flag}</span>`)}</span>
      <span class="muted scount">${seasons > 1 ? `${seasons}${NB}saisons` : ""}</span>${icon(open ? "left" : "right")}</button>
    ${open ? html`<ul class="seasons" role="list">${s.seasons.map((o, j) => html`<li><span class="sl">${o.label}</span>
      <span class="vbtns">${Object.keys(o.versions).map((v) => html`<button class="btn sm" type="button" data-act="f-bench" data-i="${i}" data-j="${j}" data-v="${v}" aria-label="${o.label}, ${VER[v].name}">${VER[v].flag}</button>`)}</span></li>`)}</ul>` : ""}</li>`;
}

function benchView(b) {
  const numbered = b.items.filter((e) => e.number != null);
  const free = numbered.filter((e) => !(e.known && (e.known.published || BUSY_STATES.includes(e.known.status))));
  const meta = [b.version ? html`<span class="vtag" data-v="${b.version}">${VER[b.version].flag}</span>` : "", b.type ? cap(String(b.type).toLowerCase()) : "",
    b.declared ? `${plural(b.declared, "épisode", "épisodes")} annoncés` : `${plural(numbered.length, "épisode", "épisodes")} listés`, b.status || ""].filter(Boolean);
  return html`<div class="bench"><div class="bhead"><button class="btn sm ghost" type="button" data-act="f-back">${icon("left")}${finder.results ? "Résultats" : "Retour"}</button>
      <div class="btitle"><strong>${b.title}</strong><div class="muted bmeta">${meta.map((m, i) => html`${i ? html`<span aria-hidden="true"> · </span>` : ""}${m}`)}</div></div></div>
    ${numbered.length ? html`<div class="bbar">
      <button class="btn primary" type="button" data-act="dl-sel" ${b.sel.size ? "" : raw("disabled")}>${icon("play")}<span>Télécharger la sélection${b.sel.size ? ` (${b.sel.size})` : ""}</span></button>
      <button class="btn" type="button" data-act="dl-season" ${free.length ? "" : raw("disabled")}>Toute la saison (${free.length})</button>
      <span class="lastn"><label for="last-n">Les</label><input class="field" id="last-n" type="number" min="1" max="${numbered.length}" value="${b.n}" inputmode="numeric"><label for="last-n">derniers</label><button class="btn" type="button" data-act="dl-last">Télécharger</button></span>
      <span class="grow"></span>
      <button class="btn sm ghost" type="button" data-act="sel-free">Cocher ce qui manque</button><button class="btn sm ghost" type="button" data-act="sel-none">Tout décocher</button></div>
      <div class="eps" role="group" aria-label="Épisodes">${numbered.map((e) => epChip(e, b))}</div>
      <div class="bfoot"><label class="check"><input type="checkbox" id="bench-watch" ${b.watch ? raw("checked") : ""}>Surveiller aussi cet anime : ses prochains épisodes seront publiés automatiquement</label>
        <span class="muted">Publication dans le canal, dans l’ordre des épisodes.</span></div>`
      : empty("Aucun épisode listé", "Le site n’a encore publié aucun épisode de cette page.")}
    ${b.msg ? html`<div class="bmsg" data-tone="${b.msg.tone}" role="status">${b.msg.text}</div>` : ""}</div>`;
}

function epChip(e, b) {
  const k = e.known, done = !!(k && k.published), busy = !!(k && !done && BUSY_STATES.includes(k.status));
  const label = done ? "publié" : busy ? "en file" : k ? "connu" : "";
  return html`<button class="ep" type="button" data-act="ep-toggle" data-n="${e.number}" aria-pressed="${String(b.sel.has(e.number))}" ${done || busy ? raw('aria-disabled="true"') : ""} data-s="${done ? "done" : busy ? "busy" : k ? "known" : "free"}" title="${epLabel(e.number)}${label ? ` : ${label}` : ""}"><b>${e.number}</b>${label ? html`<small>${label}</small>` : ""}</button>`;
}

function paintFinder() {
  const el = $("#finder-body"); if (el) el.innerHTML = finderBody().s;
  const btn = $("#find-form button[type=submit]"); if (btn) btn.disabled = finder.busy;
  const er = $("#find-err"); if (er) { er.textContent = finder.err; er.hidden = !finder.err; }
}
const jpost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function openBench(url, title, version) {
  finder.busy = true; finder.err = ""; paintFinder();
  try {
    const r = await api(`/api/anime-episodes?url=${encodeURIComponent(url)}`);
    finder.bench = { url, title, version: version || r.version || null, items: r.items, type: r.type, status: r.status, declared: r.declared, sel: new Set(), n: 3, watch: false, msg: null };
    finder.view = "bench";
  } catch (e) { finder.err = e.message; }
  finally { finder.busy = false; paintFinder(); }
}

async function finderSubmit(q) {
  q = q.trim(); finder.q = q; finder.err = "";
  if (!q) { finder.err = "Écrivez le nom d’un anime ou collez l’adresse de sa page."; return paintFinder(); }
  if (looksLikeUrl(q)) { finder.results = null; return openBench(q, q.replace(/\/+$/, "").split("/").pop().replace(/-/g, " "), null); }
  finder.busy = true; paintFinder();
  try { finder.results = await api(`/api/search?q=${encodeURIComponent(q)}`); finder.view = "results"; finder.open = finder.results.items.length === 1 ? 0 : null; }
  catch (e) { finder.err = e.message; }
  finally { finder.busy = false; paintFinder(); }
}

async function download(mode, extra) {
  const b = finder.bench;
  b.watch = !!($("#bench-watch") && $("#bench-watch").checked);
  try {
    const r = await jpost("/api/downloads", { source_url: b.url, title: b.title, version: b.version || "VOSTFR", mode, watch: b.watch, ...extra });
    b.msg = { tone: r.created.length ? "ok" : "info", text: r.message };
    b.sel.clear();
    b.items = (await api(`/api/anime-episodes?url=${encodeURIComponent(b.url)}`)).items;
    toast(r.message);
    await loadDash();
  } catch (e) { b.msg = { tone: "bad", text: e.message }; }
  paintFinder();
  const list = $("#anime-list");
  if (list && state.dash) list.innerHTML = html`<header><h2>Anime surveillés<span class="n">${state.dash.animes.length || ""}</span></h2></header>${animeTable(state.dash.animes)}`.s;
}

const FINDER_ACTS = new Set(["f-open", "f-bench", "f-back", "ep-toggle", "sel-free", "sel-none", "dl-sel", "dl-season", "dl-last", "a-download"]);
function bigConfirm(el, n) {                       // more than 5 episodes at once: confirm on the button itself, no dialog
  if (n <= 5 || el.dataset.ok) { delete el.dataset.ok; return false; }
  el.dataset.ok = "1"; el.dataset.label = el.innerHTML; el.textContent = `Confirmer : ${n} épisodes ?`; el.classList.add("armed");
  setTimeout(() => { if (el.isConnected && el.dataset.ok) { delete el.dataset.ok; el.innerHTML = el.dataset.label; el.classList.remove("armed"); } }, 4000);
  return true;
}
async function finderAct(act, el) {
  const b = finder.bench;
  if (act === "f-open") { const i = Number(el.dataset.i); finder.open = finder.open === i ? null : i; return paintFinder(); }
  if (act === "f-back") { finder.view = finder.results ? "results" : "idle"; return paintFinder(); }
  if (act === "f-bench") {
    const s = finder.results.items[Number(el.dataset.i)], o = s.seasons[Number(el.dataset.j)], v = el.dataset.v;
    return openBench(o.versions[v].url, `${s.name}${s.seasons.length > 1 ? ` · ${o.label}` : ""}`, v);
  }
  if (act === "a-download") {
    const r = state.dash.animes.find((a) => a.anime_key === el.dataset.key);
    finder.results = null; finder.q = r.title || r.anime_key; const q = $("#find-q"); if (q) q.value = finder.q;
    const lang = String(r.language || "").toUpperCase();
    $("#finder-h").scrollIntoView({ block: "start" });
    return openBench(r.source_url, r.title || r.anime_key, lang === "VF" ? "VF" : lang ? "VOSTFR" : null);
  }
  if (!b) return;
  if (act === "ep-toggle") {
    if (el.getAttribute("aria-disabled") === "true") return;
    const n = Number(el.dataset.n); b.sel.has(n) ? b.sel.delete(n) : b.sel.add(n);
    el.setAttribute("aria-pressed", String(b.sel.has(n)));
    const btn = $("[data-act=dl-sel]"); if (btn) { btn.disabled = !b.sel.size; $("span", btn).textContent = `Télécharger la sélection${b.sel.size ? ` (${b.sel.size})` : ""}`; }
    return;
  }
  if (act === "sel-free") { b.sel = new Set(b.items.filter((e) => e.number != null && !e.known).map((e) => e.number)); return paintFinder(); }
  if (act === "sel-none") { b.sel.clear(); return paintFinder(); }
  if (act === "dl-sel") { if (bigConfirm(el, b.sel.size)) return; return download("selection", { numbers: [...b.sel].sort((x, y) => x - y) }); }
  if (act === "dl-season") { const n = b.items.filter((e) => e.number != null).length; if (bigConfirm(el, n)) return; return download("season", {}); }
  if (act === "dl-last") { const n = Math.max(1, Number($("#last-n").value) || 1); b.n = n; if (bigConfirm(el, n)) return; return download("last_n", { n }); }
}

function animeTable(animes) {
  if (!animes.length) return empty("Aucun anime surveillé", "Cherchez un anime ci-dessus et cochez « Surveiller aussi » : ses nouveaux épisodes seront publiés automatiquement.");
  return html`<ul class="alist" role="list">${animes.map((a) => html`<li class="arow">
    <div class="ainfo"><div class="aname" title="${a.title || a.anime_key}">${a.title || a.anime_key}${a.auto_added ? html` <span class="badge" data-tone="info"><span class="dot"></span>ajouté depuis le site</span>` : ""}</div>
      <div class="muted ameta">${a.enabled ? badge("ok", "Surveillé") : badge("muted", "En pause")}
        <span>${plural(a.published, "publié", "publiés")}</span>${a.today ? html`<span>Aujourd’hui : ${a.today}</span>` : ""}${a.queued ? html`<span>${a.queued}${NB}en file</span>` : ""}
        <span>${a.last_check_error ? badge("bad", "Contrôle en échec") : a.last_successful_check_at ? `contrôlé ${ago(a.last_successful_check_at)}` : "jamais contrôlé"}${a.force_check ? " · demandé" : ""}</span></div></div>
    <div class="aact"><button class="btn sm" type="button" data-act="a-download" data-key="${a.anime_key}" ${a.source_url ? "" : raw("disabled")}>${icon("play")}Télécharger…</button>
      <button class="btn sm" type="button" data-act="check" data-key="${a.anime_key}" ${a.source_url ? "" : raw("disabled")}>Contrôler</button>
      <button class="btn sm ghost" type="button" data-act="toggle-anime" data-key="${a.anime_key}" data-enabled="${a.enabled ? 1 : 0}">${a.enabled ? "Pause" : "Reprendre"}</button></div></li>`)}</ul>`;
}

function errorsPage(d) {
  if (!d.total) return html`<section class="panel">${empty("Rien à traiter", "Aucun échec et aucune alerte ouverte.")}</section>`;
  return html`<div class="grid2 even">
    ${d.episodes.length ? html`<section class="panel"><header><h2>Épisodes à traiter<span class="n">${d.episodes.length}</span></h2></header>
      ${d.episodes.map((e) => html`<div class="problem"><div class="head">${stateBadge(e)}<div class="grow"><strong>${e.subject}</strong>${e.retry_count ? html` <span class="muted">· ${e.retry_count} tentative${e.retry_count > 1 ? "s" : ""}</span>` : ""}</div><span class="muted">${ago(e.last_error_at)}</span></div>
        ${e.detail ? html`<details class="tech"><summary>Détail technique</summary><pre>${e.detail}</pre></details>` : ""}
        <div class="actions">${["failed", "structure_changed", "blocked"].includes(e.status) ? html`<button class="btn sm primary" type="button" data-act="requeue" data-id="${e.id}">${icon("refresh")}Relancer</button>` : ""}
          ${["failed", "structure_changed", "blocked"].includes(e.status) ? html`<button class="btn sm danger" type="button" data-act="cancel" data-id="${e.id}" data-confirm="Confirmer l’annulation ?">Annuler</button>` : ""}
          <button class="btn sm ghost" type="button" data-act="open-ep" data-id="${e.id}">Détails</button></div></div>`)}</section>` : ""}
    ${d.alerts.length ? html`<section class="panel"><header><h2>Alertes ouvertes<span class="n">${d.alerts.length}</span></h2></header>
      ${d.alerts.map((a) => html`<div class="problem"><div class="head"><div class="grow"><strong>${a.title}</strong> <span class="muted">· ${a.subject}${a.count > 1 ? ` · ${a.count} fois` : ""}</span></div><span class="muted">${ago(a.last_at)}</span></div>
        ${a.detail ? html`<details class="tech"><summary>Détail technique</summary><pre>${a.detail}</pre></details>` : ""}
        <div class="actions"><button class="btn sm" type="button" data-act="ack" data-id="${a.id}">${icon("check")}Acquitter</button></div></div>`)}</section>` : ""}
  </div>`;
}

function capacityPage(d) {
  const sv = d.server, lim = d.limits;
  const reach = sv.reachable === true ? badge("ok", "Joignable") : sv.reachable === false ? badge("bad", "Injoignable") : badge("muted", "Non applicable");
  return html`<div class="stack"><div class="grid2">
    <section class="panel"><header><h2>Serveur Telegram</h2>${reach}</header><div class="body"><dl class="kv">
      <dt>Mode</dt><dd>${sv.mode === "local" ? "Serveur local (fichiers volumineux)" : "Serveur public de Telegram"}</dd>
      <dt>Envoi par fichier</dt><dd>${sv.upload_by_file_path ? "Oui — sans copie réseau" : "Non"}</dd>
      <dt>Taille maximale publiée</dt><dd><strong>${mio(lim.max_safe_publish_mib)}</strong></dd>
      <dt>Plus gros envoi réussi</dt><dd>${mio(lim.largest_proven_mib)}</dd>
      <dt>Limite annoncée</dt><dd>${mio(lim.documented_mib)}</dd>
      <dt>Téléchargements en parallèle</dt><dd>${d.max_concurrent_downloads ?? "—"} au maximum</dd></dl></div></section>
    <section class="panel"><header><h2>Machine</h2></header>${machine(d.host)}</section></div>
    <section class="panel"><header><h2>Envois de gros fichiers testés</h2></header>
      ${d.proven_uploads.length ? html`<div class="tablewrap"><table class="tbl"><thead><tr><th>Taille visée</th><th class="num">Taille réelle</th><th class="num">Durée d’envoi</th><th class="num">Débit</th><th>Résultat</th></tr></thead><tbody>
        ${d.proven_uploads.map((u) => html`<tr><td class="name" data-label="Visée">${mio(u.target_mib)}</td><td class="num" data-label="Réelle">${mio(u.size_mib)}</td>
          <td class="num" data-label="Durée">${duration(u.upload_s)}</td><td class="num" data-label="Débit">${u.throughput_mibs ? `${nf.format(u.throughput_mibs)}${NB}Mio/s` : "—"}</td>
          <td class="keep">${u.result === "PASS" ? badge("ok", "Réussi") : badge("bad", "Échec")}</td></tr>`)}</tbody></table></div>`
        : empty("Aucun test enregistré")}</section></div>`;
}

function healthPage(d) {
  const mark = (ok) => html`<span class="mark" data-tone="${ok ? "ok" : "warn"}">${icon(ok ? "check" : "alert")}</span>`;
  return html`<div class="stack"><section class="panel"><header><h2>Contrôles</h2>${d.ok && d.worker.alive ? badge("ok", "Tout est bon") : badge("warn", "À vérifier")}</header>
    <div class="checks"><div class="check">${mark(d.worker.alive)}<div><div class="t">Worker</div><div class="d">${d.worker.explain}</div></div></div>
    ${d.checks_list.map((c) => html`<div class="check">${mark(c.ok)}<div><div class="t">${c.label}</div><div class="d">${c.explain}</div><div class="muted mono" style="margin-top:4px">${c.detail}</div></div></div>`)}</div></section></div>`;
}

function settingsPage(d) {
  return html`<section class="panel"><header><h2>Notifications Telegram</h2></header>
    <div class="body muted" style="padding-bottom:8px">Messages privés envoyés à l’administrateur uniquement — jamais dans le canal public.</div>
    ${d.items.map((n) => html`<div class="switch"><div><div class="t" id="n-${n.key}">${n.label}</div><div class="muted">${n.help}</div></div>
      <button class="toggle" type="button" role="switch" aria-checked="${String(n.enabled)}" aria-labelledby="n-${n.key}" data-act="notif" data-key="${n.key}"></button></div>`)}</section>`;
}

function morePage() {
  return html`<nav class="panel more-list" aria-label="Autres pages" style="padding:8px">${PAGES.filter((p) => !p.mobile).map((p) => html`<a class="nav-link" href="#/${p.id}">${icon(p.icon)}<span>${p.label}</span></a>`)}</nav>`;
}

// ── drawer (episode detail) ────────────────────────────────────────────────────────────────────────
const PUB_TYPES = { thumbnail: "Miniature", first_publication: "Vidéo", video: "Vidéo" };
let lastFocus = null;
async function openEpisode(id) {
  lastFocus = document.activeElement;
  const dr = $("#drawer");
  dr.hidden = false; $("#scrim").hidden = false;
  dr.innerHTML = html`<header><h2 id="drawer-title">Chargement…</h2><button class="btn iconbtn ghost" type="button" data-act="close" aria-label="Fermer">${icon("x")}</button></header><div class="content">${skeleton()}</div>`.s;
  $("[data-act=close]", dr).focus();
  try {
    const e = await api(`/api/episodes-view/${id}`);
    const pubs = e.publications.map((p) => html`<li>${badge(p.status === "published" || p.status === "sent" ? "ok" : "muted", PUB_TYPES[p.publication_type] || cap(String(p.publication_type).replace(/_/g, " ")))}<div>${p.status === "published" || p.status === "sent" ? "Publiée" : cap(p.status)}${p.message_id ? ` · message n°${NB}${p.message_id}` : ""}<div class="muted">${ago(p.attempted_at)}</div></div></li>`);
    dr.innerHTML = html`<header><div><h2 id="drawer-title">${animeName(e)} · ${epLabel(e.episode_number)}</h2><div style="margin-top:8px">${stateBadge(e)}</div></div>
      <button class="btn iconbtn ghost" type="button" data-act="close" aria-label="Fermer">${icon("x")}</button></header>
      <div class="content">
        ${["queued", "retry_wait", "downloading", "downloaded", "validating", "validated", "publishing_thumbnail", "thumbnail_published", "publishing_video"].includes(e.status)
          ? html`<div>${meter(e.progress.percent, "info")}<div class="muted" style="margin-top:6px">Étape ${e.progress.step} sur ${e.progress.of}</div></div>` : ""}
        <dl class="kv"><dt>Langue</dt><dd>${(e.language || "—").toUpperCase()}</dd>
          ${e.player ? html`<dt>Lecteur</dt><dd>${e.player}</dd>` : ""}
          <dt>Taille</dt><dd>${size(e.file_size) || "—"}</dd>
          <dt>Tentatives</dt><dd>${e.retry_count}</dd>
          ${e.published_at ? html`<dt>Publié</dt><dd>${dateTime(e.published_at)} · ${ago(e.published_at)}</dd>` : ""}
          ${e.next_retry_at && e.status === "retry_wait" ? html`<dt>Prochain essai</dt><dd>${dateTime(e.next_retry_at)}</dd>` : ""}
          ${e.cleanup_at ? html`<dt>Fichier supprimé le</dt><dd>${dateTime(e.cleanup_at)}</dd>` : ""}</dl>
        ${pubs.length ? html`<div><h3 style="margin-bottom:10px">Publications Telegram</h3><ul class="timeline">${pubs}</ul></div>` : ""}
        ${e.last_error ? html`<details class="tech" open><summary>Dernière erreur</summary><pre>${e.last_error}</pre></details>` : ""}
        <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
          ${e.actionable ? html`<button class="btn primary" type="button" data-act="requeue" data-id="${e.id}">${icon("refresh")}Relancer</button>` : ""}
          ${e.cancellable ? html`<button class="btn danger" type="button" data-act="cancel" data-id="${e.id}" data-confirm="Confirmer l’annulation ?">Annuler l’épisode</button>` : ""}
        </div></div>`.s;
    $("[data-act=close]", dr).focus();
  } catch (err) {
    $(".content", dr).innerHTML = failure(err.message).s;
  }
}
function closeDrawer() {
  $("#drawer").hidden = true; $("#scrim").hidden = true;
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}

// ── routing and refresh ────────────────────────────────────────────────────────────────────────────
const TITLES = { "": "Vue d’ensemble", episodes: "Épisodes", file: "File d’attente", anime: "Anime", erreurs: "Erreurs et alertes", capacites: "Capacités", sante: "Santé", reglages: "Réglages", plus: "Plus" };
let routeToken = 0;

async function loadDash() {
  try { state.dash = await api("/api/dashboard"); state.failing = false; } catch (e) { state.dash = null; state.failing = e.message; }
  setStatus(state.dash); renderNav();
}

async function showPage(fresh) {
  const { page } = state.route;
  const token = ++routeToken;
  document.title = `${TITLES[page] || "Panneau"} · Panneau admin`;
  $("#title").textContent = TITLES[page] || "Panneau";
  renderNav();
  if (fresh) { main.innerHTML = skeleton().s; }
  try {
    let out;
    if (page === "") { if (!state.dash) await loadDash(); if (!state.dash) throw new Error(state.failing); out = overview(state.dash); }
    else if (page === "episodes") {
      if (fresh) { main.innerHTML = episodesShell().s; }
      await episodesLoad();
      if (token === routeToken && fresh) enter();
      return;
    }
    else if (page === "file") out = file(await api("/api/queue/lanes"));
    else if (page === "anime") {
      if (!state.dash) await loadDash();
      const cyc = await api("/api/cycles").catch(() => null);
      if (fresh || !$("#find-form")) out = animePage(state.dash, state.dash ? state.dash.animes : [], cyc);
      else {
        $("#anime-list").innerHTML = html`<header><h2>Anime surveillés<span class="n">${state.dash.animes.length || ""}</span></h2></header>${animeTable(state.dash.animes)}`.s;
        $("#cycles").innerHTML = cyclesPanel(cyc).s;
        return;
      }
    }
    else if (page === "erreurs") out = errorsPage(await api("/api/problems"));
    else if (page === "capacites") out = capacityPage(await api("/api/capacity/live"));
    else if (page === "sante") out = healthPage(await api("/api/health/report"));
    else if (page === "reglages") out = settingsPage(await api("/api/notifications"));
    else if (page === "plus") out = morePage();
    else out = empty("Page introuvable", raw('<a href="#/">Retour à la vue d’ensemble</a>'));
    if (token !== routeToken) return;
    main.innerHTML = out.s;
    if (fresh) enter();
  } catch (err) {
    if (token === routeToken) main.innerHTML = failure(err.message).s;
  }
}
function enter() {
  main.classList.remove("enter"); void main.offsetWidth; main.classList.add("enter");
  main.focus({ preventScroll: true });
}

async function onRoute() {
  const r = parseRoute();
  const pageChanged = r.page !== state.route.page;
  state.route = r;
  if (r.page === "episodes" && (pageChanged || r.query.status !== undefined)) {
    state.ep = { status: r.query.status || "", q: "", anime: "", offset: 0 };
  }
  if (!$("#drawer").hidden) closeDrawer();
  window.scrollTo(0, 0); main.scrollTop = 0;
  await showPage(true);
}

async function refresh() {
  if (document.hidden) return;
  await loadDash();
  if (["", "anime"].includes(state.route.page) || !state.dash) { if (!$("[data-armed]")) await showPage(false); }
  else if (state.route.page === "episodes") { try { await episodesLoad(); } catch (_) { /* keep the last table */ } }
  else if (!$("#drawer").hidden || $("[data-armed]")) { /* do not redraw under the user's hands */ }
  else await showPage(false);
}

// ── actions ────────────────────────────────────────────────────────────────────────────────────────
async function run(label, fn, after) {
  try { const r = await fn(); toast(label || (r && r.message) || "Fait"); if (after) await after(); }
  catch (e) { toast(e.message, "bad"); }
}
const reloadAll = async () => { await loadDash(); await showPage(false); if (!$("#drawer").hidden) closeDrawer(); };

document.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) { if (ev.target.id === "scrim") closeDrawer(); return; }
  const act = el.dataset.act;
  if (el.dataset.confirm && !el.dataset.armed) {          // destructive: confirm on the spot, no modal
    el.dataset.armed = "1"; el.dataset.label = el.textContent; el.textContent = el.dataset.confirm; el.classList.add("armed");
    setTimeout(() => { if (el.isConnected) { delete el.dataset.armed; el.textContent = el.dataset.label; el.classList.remove("armed"); } }, 4000);
    return;
  }
  if (FINDER_ACTS.has(act)) return finderAct(act, el);
  if (el.tagName === "BUTTON") el.disabled = true;
  try {
    if (act === "open-ep") { el.disabled = false; return openEpisode(el.dataset.id); }
    if (act === "close") { el.disabled = false; return closeDrawer(); }
    if (act === "reload") { el.disabled = false; return onRoute(); }
    if (act === "pause" || act === "resume") return await run(null, () => post(`/api/control/${act}`), reloadAll);
    if (act === "worker-stop") return await run(null, () => post("/api/worker/stop"), reloadAll);
    if (act === "worker-start") return await run(null, () => post("/api/worker/start"), reloadAll);
    if (act === "requeue") return await run("Épisode remis en file", () => post(`/api/episodes/${el.dataset.id}/requeue`), reloadAll);
    if (act === "cancel") return await run("Épisode annulé", () => post(`/api/episodes/${el.dataset.id}/cancel`), reloadAll);
    if (act === "ack") return await run("Alerte acquittée", () => post(`/api/alerts/${el.dataset.id}/ack`), reloadAll);
    if (act === "check") return await run("Contrôle demandé : il aura lieu au prochain passage du worker", () => post(`/api/animes/${encodeURIComponent(el.dataset.key)}/force-check`), reloadAll);
    if (act === "toggle-anime") {
      const on = el.dataset.enabled === "1";
      return await run(on ? "Anime mis en pause" : "Anime repris", () => post(`/api/animes/${encodeURIComponent(el.dataset.key)}/${on ? "disable" : "enable"}`), reloadAll);
    }
    if (act === "notif") {
      const r = await post(`/api/notifications/${el.dataset.key}/toggle`);
      el.setAttribute("aria-checked", String(r.enabled)); el.disabled = false; return toast(r.enabled ? "Notification activée" : "Notification désactivée");
    }
    if (act === "ep-status") { el.disabled = false; state.ep.status = el.dataset.status; state.ep.offset = 0; return episodesLoad(); }
    if (act === "ep-page") { el.disabled = false; state.ep.offset = Math.max(0, state.ep.offset + 50 * Number(el.dataset.dir)); return episodesLoad(); }
  } finally { if (el.isConnected && el.tagName === "BUTTON" && act !== "notif") el.disabled = false; }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("#drawer").hidden) return closeDrawer();
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches && ev.target.matches("tr.click, li.click")) { ev.preventDefault(); ev.target.click(); }
  if (ev.key === "Tab" && !$("#drawer").hidden) {                       // keep focus inside the open drawer
    const f = [...$("#drawer").querySelectorAll("button:not(:disabled), summary, a[href], input, select")];
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }
});

let typing;
document.addEventListener("input", (ev) => {
  if (ev.target.id === "ep-q") { clearTimeout(typing); typing = setTimeout(() => { state.ep.q = ev.target.value.trim(); state.ep.offset = 0; episodesLoad(); }, 250); }
});
document.addEventListener("change", (ev) => {
  if (ev.target.id === "ep-anime") { state.ep.anime = ev.target.value; state.ep.offset = 0; episodesLoad(); }
});
document.addEventListener("submit", (ev) => {
  if (ev.target.id !== "find-form") return;
  ev.preventDefault();
  finderSubmit($("#find-q").value);
});

window.addEventListener("hashchange", onRoute);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(refresh, 12000);
loadDash().then(onRoute);
