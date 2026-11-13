// Zoetrope front: one page, no build step. It polls the API (no WebSocket) and only rebuilds the
// parts of the page whose data changed, so a playing video or a prompt being typed is never reset.
//
// The user-facing text is in French (exception to rule 1 of CLAUDE.md, decided in P7); code and
// comments stay in English. The API keeps its own names: keyframe = « image clé », segment =
// « plan », take = « prise », retained take = « prise gardée pour le montage », assembly = « montage ».
//
// The job page follows the user's path in five steps (STEPS): start image, preview, final render,
// keep a take, assembly. nextStep() reads the job's state and says what to do now; the button that
// does it is outlined (data-next / .is-next). The page opens on the storyboard (image → shot →
// image…, P19b) and unfolds one shot at a time; reveal() unfolds whatever hides a target.
//
// Every prompt field (a shot's motion, an image to create) lists the rule findings as the user
// types and offers an AI review that proposes a corrected version (P12, « prompt review » below).
//
// A fine gesture (a wink, one hand) gets its own shot through the gesture helper of a shot (P15,
// « fine gestures » below): Klein makes the image of the gesture, inserted after an image of the job.
"use strict";

const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 8000;

const state = {
  presets: [],
  queue: { items: [], total_s: 0, worker: { alive: false } },
  job: null,
  route: { name: "jobs" },
};

// --- small helpers ------------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["content-type"] = "application/json";
  }
  const r = await fetch("/api" + path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

function toast(text, isError = true) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.className = isError ? "error" : "ok";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 3000);
}

// Run an action, report its error, refresh the job afterwards.
async function act(fn, { refresh = true } = {}) {
  try {
    const r = await fn();
    if (refresh) await refreshNow();
    return r;
  } catch (e) {
    toast(`Erreur : ${e.message}`);
    return undefined;
  }
}

function fmtDuration(s) {
  if (s === null || s === undefined || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

function fmtMinutes(s) { // for estimates: "~5 min"
  if (!s) return "";
  return s < 90 ? `~${Math.max(1, Math.round(s))} s` : `~${Math.round(s / 60)} min`;
}

function fmtDate(t) {
  return new Date(t * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

function fmtAgo(t) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
  if (s < 7 * 86400) return `il y a ${Math.floor(s / 86400)} j`;
  return `le ${new Date(t * 1000).toLocaleDateString("fr-FR", { dateStyle: "short" })}`;
}

function fmtBytes(n) {
  if (!n) return "0 Mo";
  if (n < 1e6) return `${Math.max(1, Math.round(n / 1e3))} ko`;
  if (n < 1e9) return `${(n / 1e6).toFixed(n < 1e7 ? 1 : 0).replace(".", ",")} Mo`;
  return `${(n / 1e9).toFixed(1).replace(".", ",")} Go`;
}

// Delete after a confirmation; says how much was freed. `done`: « Prise supprimée ».
async function deleteThing(path, msg, done) {
  if (!confirm(msg)) return null;
  const r = await act(() => api("DELETE", path));
  if (r) toast(`${done}${r.freed_bytes ? `, ${fmtBytes(r.freed_bytes)} libérés` : ""}.`, false);
  return r;
}

function framesFor(durationS, fps) { // same rule as presets.frames_for: Wan models take 4n+1 frames
  const n = Math.max(1, Math.round(durationS * fps) - 1);
  return 4 * Math.max(1, Math.round(n / 4)) + 1;
}

function preset(name) { return state.presets.find((p) => p.name === name); }

function kfUrl(k) { return k ? `${k.url}?v=${k.version}` : null; }

const STATUS_LABELS = {
  draft: "brouillon", ready: "prises choisies", queued: "en attente", running: "en cours", done: "terminé",
  failed: "échec", cancelled: "annulé",
};

function badge(status) { return h("span", { class: `badge s-${status}` }, STATUS_LABELS[status] || status); }

const PHASE_LABELS = {
  starting: "démarrage", loading_model: "chargement du modèle", encoding_text: "lecture du prompt",
  inference: "génération", decoding: "décodage", saving: "enregistrement",
};

const TIER_LABELS = { preview: "aperçu", final: "rendu final", keyframe: "image créée" };

// Folded block whose open state survives the rebuilds of its card (a polled take is rebuilt
// every few seconds).
const openFolds = new Set();

function fold(key, cls, label, ...children) {
  const d = h("details", { class: cls, open: openFolds.has(key), dataset: { fold: key } },
    h("summary", {}, label), ...children);
  d.addEventListener("toggle", () => {
    if (!d.open) { openFolds.delete(key); return; }
    if (openFolds.has(key)) return; // reopened by a rebuild: restoreLogScrolls placed it
    openFolds.add(key);
    for (const pre of d.querySelectorAll("pre.log")) pre.scrollTop = pre.scrollHeight; // newest lines
  });
  return d;
}

function openFold(key) {
  openFolds.add(key);
  for (const d of document.querySelectorAll(`details[data-fold="${key}"]`)) d.open = true;
}

// A help text: one line, and a « ? » that unfolds the rest (`more`, optional). Its state survives
// rebuilds like a fold's.
function helpLine(key, short, ...more) {
  const k = `help-${key}`;
  const extra = more.length ? h("span", { class: "help-more", hidden: !openFolds.has(k) }, " ", ...more) : null;
  const toggle = more.length ? h("button", { type: "button", class: "help-toggle", title: "En savoir plus",
    onclick: () => {
      extra.hidden = !extra.hidden;
      if (extra.hidden) openFolds.delete(k); else openFolds.add(k);
    } }, "?") : null;
  return h("p", { class: "muted help" }, short, " ", toggle, extra);
}

// « Avancé » block; the first child may be a string that replaces that label.
function advanced(key, ...children) {
  const label = typeof children[0] === "string" ? children.shift() : "Avancé";
  return fold(`adv-${key}`, "adv", label, ...children);
}

// Keyed list: rebuild an item only when its signature changes, keep the others (and their videos).
function syncList(container, items, keyOf, sigOf, render) {
  const old = new Map();
  for (const el of [...container.children]) {
    if (el.dataset.key) old.set(el.dataset.key, el);
    else el.remove(); // a placeholder such as "Pas encore de prise."
  }
  let prev = null;
  for (const item of items) {
    const key = String(keyOf(item));
    const sig = sigOf(item);
    let el = old.get(key);
    if (!el || el.dataset.sig !== sig) {
      const fresh = render(item);
      fresh.dataset.key = key;
      fresh.dataset.sig = sig;
      const scrolls = logScrolls(el); // measured while el is still laid out
      if (el) el.replaceWith(fresh);
      restoreLogScrolls(fresh, scrolls);
      el = fresh;
    }
    old.delete(key);
    const want = prev ? prev.nextSibling : container.firstChild;
    if (want !== el) container.insertBefore(el, want);
    prev = el;
  }
  for (const el of old.values()) el.remove();
}

// A rebuilt log keeps where its reader was: following the end (the default, newest lines) or
// reading higher up.
// null = following the end.
function logScrolls(el) {
  return el ? [...el.querySelectorAll("pre.log")].map((p) =>
    p.scrollHeight - p.scrollTop - p.clientHeight < 8 ? null : p.scrollTop) : [];
}

function restoreLogScrolls(el, scrolls) {
  [...el.querySelectorAll("pre.log")].forEach((pre, i) => {
    pre.scrollTop = scrolls[i] ?? pre.scrollHeight;
  });
}

function syncOne(container, sig, render) {
  if (container.dataset.sig === sig) return;
  container.dataset.sig = sig;
  container.replaceChildren(...[render(state.job)].flat(Infinity));
}

// --- routing and polling ------------------------------------------------------------------------

function parseRoute() {
  // #/job/8, or #/job/8/take-42 to scroll to a take (data-next of the element) once the page is built.
  const m = location.hash.match(/^#\/job\/(\d+)(?:\/([\w-]+))?/);
  return m ? { name: "job", id: Number(m[1]), focus: m[2] || null } : { name: "jobs" };
}

window.addEventListener("hashchange", () => {
  state.route = parseRoute();
  state.job = null;
  document.getElementById("app").replaceChildren();
  refreshNow();
});

let pollTimer = null;
let refreshing = null;
let refreshAgain = false;

// One refresh at a time; a refresh asked for during another one (after an action) runs right after
// it, so the page never shows data older than the action.
async function refreshNow() {
  if (refreshing) {
    refreshAgain = true;
    return refreshing;
  }
  refreshing = (async () => {
    do {
      refreshAgain = false;
      await refreshOnce();
    } while (refreshAgain);
  })();
  try {
    await refreshing;
  } finally {
    refreshing = null;
  }
  schedulePoll();
}

async function refreshOnce() {
  try {
    state.queue = await api("GET", "/queue");
  } catch (e) {
    renderTopbar(e);
    return;
  }
  renderTopbar();
  try {
    if (state.route.name === "jobs") renderJobs(await api("GET", "/jobs"));
    else {
      state.job = await api("GET", `/jobs/${state.route.id}`);
      renderJob();
    }
  } catch (e) {
    document.getElementById("app").replaceChildren(
      h("p", {}, h("a", { href: "#/" }, "← Mes projets")), h("p", { class: "error" }, e.message));
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = state.queue.items.length > 0;
  pollTimer = setTimeout(refreshNow, busy ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

function queueItem(kind, id) {
  return state.queue.items.find((i) => i.kind === kind && i.id === id);
}

// --- top bar, worker, queue ---------------------------------------------------------------------

const KIND_PATHS = { take: "takes", keygen: "keygens", assembly: "assemblies", review: "reviews" };

// What a queue task (or a recently finished one) is, in words.
function taskWhat(i) {
  if (i.kind === "take") return `plan ${i.segment_idx + 1} · ${TIER_LABELS[i.tier] || i.tier} (prise #${i.id})`;
  if (i.kind === "keygen") return `création de l'${posName(i.position)} (#${i.id})`;
  if (i.kind === "review") return `relecture IA du prompt ${i.target === "motion" ? `du plan ${i.segment_idx + 1}` : "d'image"}`;
  return `montage ${i.id}`;
}

// Link to a task in its job page: the page scrolls to it once loaded (see renderJob).
function taskHref(i) {
  const focus = { take: `take-${i.id}`, keygen: `keygen-${i.id}`, assembly: `asm-${i.id}` }[i.kind];
  return `#/job/${i.job_id}${focus ? `/${focus}` : ""}`;
}

function taskPercent(i) {
  if (i.phase === "inference" && i.total_steps) return Math.round(100 * (i.step || 0) / i.total_steps);
  return i.progress ?? 0;
}

function renderTopbar(error) {
  const pill = document.getElementById("worker-pill");
  const banner = document.getElementById("banner");
  if (error) {
    pill.className = "pill bad";
    pill.textContent = "API injoignable";
    banner.hidden = false;
    banner.textContent = `L'API ne répond pas (${error.message}). Lance-la avec scripts\\start-api.ps1.`;
    return;
  }
  const w = state.queue.worker;
  const q = state.queue;
  if (w.alive) {
    const cur = w.current_kind
      ? ({ take: `rend la prise #${w.current_id}`, keygen: `crée une image (création #${w.current_id})`,
        review: "relit un prompt" }[w.current_kind] ||
        `monte (montage ${w.current_id})`)
      : "prêt, rien à faire";
    pill.className = "pill ok";
    pill.textContent = `Moteur de rendu : ${cur}`;
    pill.title = `pid ${w.pid} · modèle ${w.loaded_model || "aucun"}${w.loaded_model ? ` (profil ${w.loaded_profile})` : ""}` +
      ` · dernier signe de vie il y a ${w.heartbeat_age_s} s`;
  } else {
    pill.className = "pill bad";
    pill.textContent = "Moteur de rendu arrêté";
    pill.title = "Les rendus ne démarrent que s'il tourne : scripts\\start-worker.ps1" +
      (w.heartbeat_age_s ? ` (dernier signe de vie il y a ${fmtDuration(w.heartbeat_age_s)})` : "");
  }
  banner.hidden = w.alive || q.items.length === 0;
  banner.textContent = "Des rendus attendent mais le moteur de rendu est arrêté : lance scripts\\start-worker.ps1.";

  // The queue pill: how many tasks and how long; a thin bar under it follows the running one.
  const toggle = document.getElementById("queue-toggle");
  const running = q.items.find((i) => i.status === "running");
  toggle.replaceChildren(...[
    q.items.length ? `File d'attente : ${q.items.length} · ${fmtDuration(q.total_s)}` : "File d'attente : vide",
    running ? h("span", { class: "mini-bar" }, h("span", { style: `width:${Math.max(3, taskPercent(running))}%` })) : null,
  ].filter(Boolean));
  toggle.classList.toggle("busy", Boolean(running));
  renderQueuePanel();
}

document.getElementById("queue-toggle").addEventListener("click", () => {
  const p = document.getElementById("queue-panel");
  p.hidden = !p.hidden;
});
// A click outside closes the panel. In the capture phase, so a button that stops its click (the
// « ⋯ » of a project card) closes it too.
document.addEventListener("click", (ev) => {
  const p = document.getElementById("queue-panel");
  if (!p.hidden && !p.contains(ev.target) && !document.getElementById("queue-toggle").contains(ev.target)) {
    p.hidden = true;
  }
}, true);

function taskThumb(i) {
  return i.thumb_url ? h("img", { class: "task-thumb", src: i.thumb_url, alt: "" }) : h("span", { class: "task-thumb" });
}

function renderQueueRow(i) {
  let where;
  if (i.status === "running") {
    where = PHASE_LABELS[i.phase] || i.phase || "démarrage";
    if (i.phase === "inference" && i.total_steps) where += ` ${i.step || 0}/${i.total_steps}`;
    where += ` · encore ${fmtDuration(i.remaining_s)}`;
  } else {
    where = `commence dans ${fmtDuration(i.starts_in_s)} · dure ~${fmtDuration(i.remaining_s)}`;
  }
  return h("li", { class: `task ${i.status}` }, taskThumb(i),
    h("div", { class: "task-text" },
      h("a", { href: taskHref(i) }, i.job_title || `Projet ${i.job_id}`), h("div", {}, taskWhat(i)),
      i.status === "running" ? h("div", { class: "bar" }, h("div", { style: `width:${Math.max(2, taskPercent(i))}%` })) : null,
      h("div", { class: "muted small" }, where)),
    h("button", { type: "button", class: "danger", title: "Annuler cette tâche",
      onclick: () => act(() => api("POST", `/${KIND_PATHS[i.kind]}/${i.id}/cancel`)) }, "Annuler"));
}

function renderRecentRow(i) {
  return h("li", { class: `task ${i.status}` }, taskThumb(i),
    h("div", { class: "task-text" },
      h("a", { href: taskHref(i) }, i.job_title || `Projet ${i.job_id}`), h("div", {}, taskWhat(i)),
      h("div", { class: "muted small" }, i.status === "done" ? h("span", { class: "ok-text" }, "terminé") : badge(i.status),
        ` ${fmtAgo(i.finished_at)}`)));
}

function renderQueuePanel() {
  const panel = document.getElementById("queue-panel");
  const q = state.queue;
  const w = q.worker;
  // Rebuilt only when what it shows changes (a button pressed during a rebuild would be lost).
  const sig = JSON.stringify([w.alive, w.loaded_model, q.items.map((i) => [i.kind, i.id, i.status, i.phase, i.step,
    Math.round(i.starts_in_s / 10), Math.round(i.remaining_s / 10)]), (q.recent || []).map((i) => [i.id, i.kind, i.status]),
  Math.floor(Date.now() / 60000)]);
  if (panel.dataset.sig === sig) return;
  panel.dataset.sig = sig;
  const top = panel.scrollTop; // emptying the panel would scroll it back to the top
  panel.replaceChildren(...[
    h("h3", {}, "File d'attente"),
    h("p", { class: "muted small" },
      w.alive
        ? `Moteur de rendu actif (pid ${w.pid}), modèle chargé : ${w.loaded_model || "aucun"}` +
          (w.loaded_model ? ` (profil ${w.loaded_profile})` : "")
        : "Moteur de rendu arrêté : lance scripts\\start-worker.ps1."),
    q.items.length ? h("ol", { class: "tasks" }, q.items.map(renderQueueRow)) : h("p", {}, "Rien en attente."),
    q.items.length ? h("p", { class: "muted" }, `Tout sera fini dans ${fmtDuration(q.total_s)} environ.`) : null,
    q.recent?.length ? [h("h3", { class: "recent-title" }, "Terminé récemment"),
      h("ol", { class: "tasks recent" }, q.recent.map(renderRecentRow))] : null,
    fold("queue-order", "adv", "Dans quel ordre ?", h("p", { class: "muted small" },
      "Les relectures de prompt, puis les aperçus et les images créées, puis les montages, puis les rendus finaux " +
      "regroupés par modèle (changer de modèle le recharge). Tu peux fermer la page : les rendus continuent.")),
  ].flat().filter(Boolean));
  panel.scrollTop = top;
}

// --- jobs list ----------------------------------------------------------------------------------

// The projects, most recently active first (the API sorts them). A card says where the project
// stands (what to do, what runs, a finished video) and holds the « ⋯ » menu to delete it.

function renderJobs(jobs) {
  const app = document.getElementById("app");
  if (!app.querySelector(".jobs-view")) {
    const finals = state.presets.filter((p) => p.tier === "final");
    const form = h("form", { class: "card new-job", hidden: true, onsubmit: createJob },
      h("label", {}, "Titre", h("input", { name: "title", placeholder: "ex. port à l'aquarelle, bateaux qui tanguent" })),
      advanced("new-job",
        h("label", {}, "Modèle du rendu final",
          h("select", { name: "preset_final" },
            finals.map((p) => h("option", { value: p.name, selected: p.name === "final-14b" }, p.label)))),
        h("label", {}, "Fin du dernier plan (si tu ajoutes une image de fin)", endModeSelect("exact"))),
      h("div", { class: "row" }, h("button", { type: "submit", class: "primary" }, "Créer"),
        h("button", { type: "button", onclick: () => toggleNewJob(false) }, "Annuler")),
    );
    app.replaceChildren(h("section", { class: "jobs-view" },
      h("div", { class: "jobs-head" },
        h("h2", {}, "Mes projets"), h("span", { class: "muted jobs-count" }), h("span", { class: "spacer" }),
        h("button", { type: "button", class: "primary new-job-button", onclick: () => toggleNewJob(true) },
          "+ Nouveau projet")),
      form,
      h("div", { class: "job-grid" }),
      h("div", { class: "how-box" })));
  }
  const total = jobs.reduce((n, j) => n + j.disk_bytes, 0);
  app.querySelector(".jobs-count").textContent = jobs.length
    ? `${jobs.length} projet${jobs.length > 1 ? "s" : ""} · ${fmtBytes(total)} sur le disque` : "";
  const grid = app.querySelector(".job-grid");
  syncList(grid, jobs, (j) => j.id, (j) => JSON.stringify([j, Math.floor(Date.now() / 60000)]), renderJobCard);
  if (!jobs.length) {
    grid.replaceChildren(h("p", { class: "muted" }, "Aucun projet pour l'instant : crée le premier."));
    toggleNewJob(true);
  }
  // The help: open when there is no project yet, a fold under the cards afterwards.
  const how = app.querySelector(".how-box");
  if (how.dataset.sig !== String(jobs.length > 0)) {
    how.dataset.sig = String(jobs.length > 0);
    how.replaceChildren(jobs.length ? fold("how", "adv how-fold", "Comment ça marche", howItWorks()) : howItWorks());
  }
}

function toggleNewJob(open) {
  const form = document.querySelector(".new-job");
  if (!form) return;
  form.hidden = !open;
  document.querySelector(".new-job-button").hidden = open;
  if (open) form.querySelector("input[name=title]").focus();
}

// Where a project stands, from the list summary: [text, class].
function jobTodo(j) {
  if (j.running) return [`rendu en cours${j.queued ? `, ${j.queued} en file` : ""}`, "live"];
  if (j.queued) return [`${j.queued} rendu${j.queued > 1 ? "s" : ""} en file`, "queued"];
  if (!j.keyframes) return ["à faire : déposer l'image de départ", ""];
  if (j.status === "failed") return ["dernière prise en échec", "bad"];
  if (j.video_url) return ["vidéo prête", "ok"];
  if (j.segments && j.retained === j.segments) return ["à faire : monter la vidéo", ""];
  if (!j.takes_done) return ["à faire : un premier aperçu", ""];
  return [`à faire : garder une prise par plan (${j.retained}/${j.segments})`, ""];
}

function renderJobCard(j) {
  const [todo, cls] = jobTodo(j);
  const name = j.title || `Projet ${j.id}`;
  const menu = h("div", { class: "card-menu", hidden: true },
    h("button", { type: "button", class: "danger", onclick: () => deleteJob(j) }, "Supprimer le projet…"));
  return h("div", { class: "card job-card" },
    h("a", { href: `#/job/${j.id}`, class: "job-card-link" },
      j.thumbnail_url ? h("img", { src: j.thumbnail_url, alt: "" }) : h("div", { class: "no-thumb" }, "pas d'image"),
      h("div", { class: "job-card-body" },
        h("strong", {}, name),
        h("div", { class: `job-todo ${cls}` }, todo),
        h("div", { class: "muted small" },
          [j.segments ? `${j.segments} plan${j.segments > 1 ? "s" : ""}` : null,
            j.takes ? `${j.takes} prise${j.takes > 1 ? "s" : ""}` : null,
            fmtBytes(j.disk_bytes), fmtAgo(j.updated_at)].filter(Boolean).join(" · ")))),
    h("button", { type: "button", class: "card-menu-button", title: "Plus d'actions",
      onclick: (ev) => { ev.stopPropagation(); closeMenus(menu); menu.hidden = !menu.hidden; } }, "⋯"),
    menu);
}

function closeMenus(except) {
  for (const m of document.querySelectorAll(".card-menu")) if (m !== except) m.hidden = true;
}
document.addEventListener("click", () => closeMenus(null));

async function deleteJob(j) {
  closeMenus(null);
  const name = j.title || `Projet ${j.id}`;
  if (j.running || j.queued) {
    toast(`« ${name} » a des rendus en file ou en cours : annule-les d'abord (file d'attente, en haut).`);
    return false;
  }
  if (!confirm(`Supprimer « ${name} » ? Ses images, ses prises et ses vidéos (${fmtBytes(j.disk_bytes)}) sont ` +
    "effacées du disque. C'est définitif.")) return false;
  const r = await act(() => api("DELETE", `/jobs/${j.id}`), { refresh: state.route.name === "jobs" });
  if (r) toast(`Projet supprimé, ${fmtBytes(r.freed_bytes)} libérés.`, false);
  return Boolean(r);
}

function howItWorks() {
  const prev = preset("preview");
  const fin = preset("final-14b") || state.presets.find((p) => p.tier === "final");
  return h("div", { class: "card how" },
    h("h3", {}, "Comment ça marche"),
    h("ol", {},
      h("li", {}, "Crée un projet et dépose une image : photo, aquarelle, dessin."),
      h("li", {}, "Décris ce qui bouge, puis lance un ", h("b", {}, "aperçu"),
        ` (modèle rapide, ${fmtMinutes(prev && estimate(prev)) || "quelques minutes"}) pour vérifier le mouvement.`),
      h("li", {}, "Quand le mouvement te va, lance le ", h("b", {}, "rendu final"),
        ` (meilleure qualité, ${fmtMinutes(fin && estimate(fin)) || "long"}) : il passe en file, la nuit par exemple.`),
      h("li", {}, h("b", {}, "Garde"), " la prise qui te plaît."),
      h("li", {}, h("b", {}, "Monte"), " la vidéo et télécharge-la.")),
    h("p", { class: "muted small" },
      "Pour imposer la fin du mouvement, ajoute une 2ᵉ image : le rendu final arrivera dessus. ",
      "Avec plusieurs images, la vidéo a un plan entre chaque paire d'images, montés bout à bout."));
}

async function createJob(ev) {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const job = await act(() => api("POST", "/jobs", Object.fromEntries(f)), { refresh: false });
  if (job) location.hash = `#/job/${job.id}`;
}

function endModeSelect(value, onchange) {
  return h("select", { name: "end_mode", onchange },
    h("option", { value: "exact", selected: value === "exact" }, "finir exactement sur la dernière image"),
    h("option", { value: "guide", selected: value === "guide" },
      "dernière image indicative : le rendu final part de l'image de départ seule"));
}

// --- the path through a job ---------------------------------------------------------------------

const STEPS = [
  { n: 1, label: "Image de départ", section: "step-images" },
  { n: 2, label: "Aperçu", section: "step-shots" },
  { n: 3, label: "Rendu final", section: "step-shots" },
  { n: 4, label: "Garder une prise", section: "step-shots" },
  { n: 5, label: "Montage", section: "step-assembly" },
];

const isLive = (x) => x.status === "queued" || x.status === "running";
const doneOf = (s, tier) => s.takes.filter((t) => t.status === "done" && (!tier || t.tier === tier));

// Which of the five steps are done, for the whole job.
function stepsDone(job) {
  const segs = job.segments;
  const all = (f) => segs.length > 0 && segs.every((s) => s.retained_take_id || f(s));
  return {
    1: job.keyframes.length > 0,
    2: job.keyframes.length > 0 && all((s) => doneOf(s).length > 0),
    3: job.keyframes.length > 0 && all((s) => doneOf(s, "final").length > 0),
    4: job.keyframes.length > 0 && segs.length > 0 && segs.every((s) => s.retained_take_id),
    5: job.assemblies.some((a) => a.status === "done"),
  };
}

// What to do now: { text, target (data-next of the element to outline), wait (a render runs) }.
function nextStep(job) {
  const n = findNextStep(job);
  n.text = n.text.replace(/\s+/g, " ").replace(/ ([,.])/g, "$1"); // optional words leave gaps
  return n;
}

function findNextStep(job) {
  const liveGen = job.keygens.find(isLive);
  if (liveGen) {
    const q = queueItem("keygen", liveGen.id);
    const when = liveGen.status === "running"
      ? `en cours${q ? `, encore ${fmtDuration(q.remaining_s)} environ` : ""}`
      : `en attente${q ? `, commence dans ${fmtDuration(q.starts_in_s)}` : ""}`;
    return { text: `Création de l'${posName(liveGen.position)} ${when}.` +
      (state.queue.worker.alive ? "" : " Le moteur de rendu est arrêté : lance scripts\\start-worker.ps1."),
    target: `keygen-${liveGen.id}`, wait: true };
  }
  if (!job.keyframes.length) {
    return { text: "Dépose l'image de départ (photo, aquarelle, dessin) à l'étape 1, ou crée-la juste en dessous.",
      target: "add-kf" };
  }
  const multi = job.segments.length > 1;
  const name = (s) => (multi ? `du plan ${s.idx + 1}` : "");
  let waiting = null;
  for (const s of job.segments) {
    if (s.retained_take_id) continue;
    const live = s.takes.find(isLive);
    if (live) {
      waiting = waiting || { s, t: live };
      continue;
    }
    const last = s.takes[s.takes.length - 1];
    if (last && last.status === "failed") {
      return { text: `La dernière prise ${name(s)} a échoué (erreur sous la prise). Corrige si besoin et relance.`,
        target: `preview-${s.id}` };
    }
    if (!s.prompt.trim()) {
      return { text: `Décris ce qui doit bouger ${multi ? `dans le plan ${s.idx + 1}` : ""}, puis lance un aperçu.`,
        target: `prompt-${s.id}` };
    }
    const finals = doneOf(s, "final");
    if (finals.length) {
      return { text: `Le rendu final ${name(s)} est prêt : regarde-le, puis coche « Garder pour le montage » ` +
        "sur la prise qui te plaît.", target: `retain-${finals[finals.length - 1].id}` };
    }
    if (doneOf(s, "preview").length) {
      return { text: `Regarde l'aperçu ${name(s)}. Le mouvement te va ? Lance le rendu final. ` +
        "Sinon, change le prompt et refais un aperçu.", target: `final-${s.id}` };
    }
    return { text: `Lance un aperçu ${name(s)} pour vérifier le mouvement (quelques minutes).`,
      target: `preview-${s.id}` };
  }
  const workerOff = state.queue.worker.alive ? "" : " Le moteur de rendu est arrêté : lance scripts\\start-worker.ps1.";
  if (waiting) {
    const q = queueItem("take", waiting.t.id);
    const when = waiting.t.status === "running"
      ? `en cours${q ? `, encore ${fmtDuration(q.remaining_s)} environ` : ""}`
      : `en attente${q ? `, commence dans ${fmtDuration(q.starts_in_s)}` : ""}`;
    return { text: `${TIER_LABELS[waiting.t.tier][0].toUpperCase()}${TIER_LABELS[waiting.t.tier].slice(1)} ` +
      `${name(waiting.s)} ${when}. Tu peux fermer la page : le rendu continue.` + workerOff,
    target: `take-${waiting.t.id}`, wait: true };
  }
  const liveAsm = job.assemblies.find(isLive);
  if (liveAsm) return { text: "Montage en cours." + workerOff, target: `asm-${liveAsm.id}`, wait: true };
  const doneAsm = [...job.assemblies].reverse().find((a) => a.status === "done");
  if (doneAsm) {
    return { text: "La vidéo est prête : télécharge-la en bas de page. Tu peux encore changer de prise et remonter.",
      target: `download-${doneAsm.id}`, done: true };
  }
  return { text: "Toutes les prises sont choisies : monte la vidéo (étape 5).", target: "assemble" };
}

function renderStepper(job) {
  const done = stepsDone(job);
  const current = STEPS.find((s) => !done[s.n])?.n;
  return h("ol", { class: "stepper" }, STEPS.map((s) =>
    h("li", { class: done[s.n] ? "done" : s.n === current ? "current" : "" },
      h("a", { href: `#/job/${job.id}`, onclick: (ev) => { ev.preventDefault(); scrollToId(s.section); } },
        h("span", { class: "num" }, done[s.n] ? "✓" : s.n), s.label))));
}

function renderNext(job) {
  const n = nextStep(job);
  return h("div", { class: `next-step${n.wait ? " wait" : ""}${n.done ? " done" : ""}` },
    h("span", { class: "next-label" }, n.wait ? "En cours" : n.done ? "Terminé" : "À faire"),
    h("span", { class: "next-text" }, n.text),
    h("button", { type: "button", onclick: () => goTo(n.target) }, n.wait ? "Voir" : "Y aller"));
}

function scrollToId(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Scroll to the element of a target (data-next), after unfolding what hides it; `flash` marks it
// for a moment (a link from the queue panel), else its first field gets the focus.
function goTo(target, { flash = false } = {}) {
  reveal(state.job, target);
  const el = document.querySelector(`[data-next="${target}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  if (flash) {
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 2000);
    return;
  }
  const focusable = el.matches("textarea, input, button") ? el : el.querySelector("textarea, input, button");
  if (focusable) setTimeout(() => focusable.focus({ preventScroll: true }), 400);
}

// --- what the job page shows: one shot, one image panel ----------------------------------------
//
// The job page unfolds one shot at a time, chosen in the storyboard (the film's strip, at the top);
// the other shots' cards stay in the page, hidden, so a video or a prompt being typed survives.
// A click on an image of the strip opens its panel (replace, adjust, copy, remove). Per job, kept
// across polls and route changes.

const views = new Map(); // job id -> { seg: segment id, kf: position of the open image, keygen: bool }

function viewOf(job) {
  let v = views.get(job.id);
  if (!v) {
    v = { seg: null, kf: null, keygen: null };
    views.set(job.id, v);
  }
  return v;
}

// The segment a target of nextStep() or of a link points into, or null.
function segOfTarget(job, target) {
  let m = /^(?:preview|final|prompt)-(\d+)$/.exec(target || "");
  if (m) return job.segments.some((s) => s.id === Number(m[1])) ? Number(m[1]) : null;
  m = /^(?:take|retain)-(\d+)$/.exec(target || "");
  if (m) return job.segments.find((s) => s.takes.some((t) => t.id === Number(m[1])))?.id ?? null;
  return null;
}

// The unfolded shot: the one chosen, else the one the next step is about, else the first without
// a retained take.
function currentSeg(job) {
  const v = viewOf(job);
  if (!job.segments.some((s) => s.id === v.seg)) {
    v.seg = segOfTarget(job, nextStep(job).target) ?? job.segments.find((s) => !s.retained_take_id)?.id ??
      job.segments[0]?.id ?? null;
  }
  return v.seg;
}

function keygenOpen(job) {
  const v = viewOf(job);
  return v.keygen ?? job.keyframes.length === 0;
}

// The takes shown in a shot: the retained one, the latest and the running ones; the others are
// folded in « N autres prises ».
function shownTakeIds(s) {
  return new Set([s.retained_take_id, s.takes.at(-1)?.id, ...s.takes.filter(isLive).map((t) => t.id)]);
}

// Unfold what hides the element of `target`: its shot, the older takes, the history of created images.
function reveal(job, target) {
  if (!job || !target) return;
  const v = viewOf(job);
  const segId = segOfTarget(job, target);
  if (segId !== null) {
    v.seg = segId;
    const m = /^(?:take|retain)-(\d+)$/.exec(target);
    const s = job.segments.find((x) => x.id === segId);
    if (m && !shownTakeIds(s).has(Number(m[1]))) openFold(`older-${segId}`);
  }
  const g = /^keygen-(\d+)$/.exec(target);
  if (g && !isLive(job.keygens.find((x) => x.id === Number(g[1])) || { status: "done" })) openFold(`keygens-${job.id}`);
  if (target === "keygen") v.keygen = true;
  renderJob();
}

function selectSeg(job, id) {
  const v = viewOf(job);
  v.seg = id;
  v.kf = null;
  renderJob();
  scrollToId("step-shots");
}

function toggleKf(job, position) {
  const v = viewOf(job);
  v.kf = v.kf === position ? null : position;
  renderJob();
}

function openKeygen(job, target) {
  const v = viewOf(job);
  v.keygen = true;
  if (target !== undefined) {
    keygenDraft(job).target = target;
    delete document.querySelector(".keygen-form").dataset.sig; // rebuild it with that target
  }
  renderJob();
  const field = document.querySelector(".keygen-form textarea.prompt");
  field?.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(() => field?.focus({ preventScroll: true }), 400);
}

function highlightNext(target) {
  for (const el of document.querySelectorAll(".is-next")) {
    if (el.dataset.next !== target) el.classList.remove("is-next");
  }
  document.querySelector(`[data-next="${target}"]`)?.classList.add("is-next");
}

// --- job view -----------------------------------------------------------------------------------

function section(id, num, title, intro) {
  return h("section", { id, class: "step-section" },
    h("h2", {}, h("span", { class: "step-num" }, num), title), intro);
}

function renderJob() {
  const job = state.job;
  const app = document.getElementById("app");
  let view = app.querySelector(".job-view");
  if (!view || Number(view.dataset.job) !== job.id) {
    const film = section("step-images", "1", "Le film : images et plans",
      helpLine(`film-${job.id}`, "De gauche à droite : une image, le plan qui en part, l'image suivante… " +
        "Clique un plan pour le travailler, une image pour la changer.",
      "L'image de départ ouvre la vidéo. Une 2ᵉ image, facultative, impose la fin : le rendu final arrivera " +
        "dessus. Avec plusieurs images, la vidéo a un plan entre chaque paire, montés bout à bout."));
    film.append(h("div", { class: "storyboard" }), h("div", { class: "kf-panel-box" }),
      h("div", { class: "card keygen" },
        h("div", { class: "keygen-bar" }), h("div", { class: "keygen-form" }), h("div", { class: "keygen-live" }),
        fold(`keygens-${job.id}`, "adv keygen-history", "Images créées", h("div", { class: "keygen-list" }))));
    const shots = section("step-shots", "2-4", "Plan",
      helpLine(`shots-${job.id}`, "Décris le mouvement, vérifie-le avec un aperçu, lance le rendu final, " +
        "garde la prise qui te plaît."));
    shots.append(h("div", { class: "segments" }));
    const assembly = section("step-assembly", "5", "Montage",
      helpLine(`asm-${job.id}`, "Colle bout à bout la prise gardée de chaque plan ; la vidéo se télécharge ici."));
    assembly.append(h("div", { class: "assembly" }));
    view = h("div", { class: "job-view", dataset: { job: job.id } },
      h("div", { class: "job-head" }), h("div", { class: "guide" }, h("div", { class: "stepper-box" }),
        h("div", { class: "next-box" })),
      film, shots, assembly);
    app.replaceChildren(view);
  }
  syncOne(view.querySelector(".job-head"), JSON.stringify([job.title, job.preset_final, job.end_mode, job.status,
    job.keyframes.length, state.presets.length, job.style]), renderJobHead);
  const next = nextStep(job);
  syncOne(view.querySelector(".stepper-box"), JSON.stringify(stepsDone(job)), renderStepper);
  syncOne(view.querySelector(".next-box"), JSON.stringify([next, state.queue.worker.alive]), renderNext);
  const v = viewOf(job);
  const cur = currentSeg(job);
  syncList(view.querySelector(".storyboard"), storyItems(job), (it) => it.key, (it) => storySig(job, it, next),
    (it) => renderStoryItem(job, it));
  const openKf = job.keyframes.find((k) => k.position === v.kf);
  if (!openKf) v.kf = null;
  syncOne(view.querySelector(".kf-panel-box"), JSON.stringify(openKf ? [openKf, job.keyframes.length,
    job.keygens.some((g) => isLive(g) && g.position === openKf.position)] : null),
  () => (openKf ? renderKeyframe(job, openKf) : []));
  updateKeygens(view, job);
  syncList(view.querySelector(".segments"), job.segments, (s) => s.id,
    (s) => JSON.stringify([s.id, s.idx, s.kf_in, s.kf_out, s.end_mode, job.preset_final, job.segments.length,
      kfVersions(job, s), job.style]),
    (s) => renderSegment(job, s));
  for (const s of job.segments) updateSegment(job, s);
  for (const card of view.querySelectorAll(".segments > .segment")) card.hidden = Number(card.dataset.key) !== cur;
  syncOne(view.querySelector(".assembly"), JSON.stringify([job.segments.map((s) => s.retained_take_id),
    job.assemblies, job.assemblies.map((a) => {
      const q = queueItem("assembly", a.id);
      return q ? [q.status, Math.round(q.starts_in_s / 10)] : null;
    })]), renderAssembly);
  view.querySelector("#step-shots").classList.toggle("locked", !job.keyframes.length);
  highlightNext(next.target);
  if (state.route.focus) { // a link such as #/job/8/take-42 (queue panel)
    const focus = state.route.focus;
    state.route.focus = null;
    goTo(focus, { flash: true });
  }
}

// --- storyboard: the film's strip ---------------------------------------------------------------
//
// image 0 → [shot 1] → image 1 → … → [+ image]. A shot's card shows its retained take (else its
// latest finished one; it plays on hover), where it stands and how many takes it has.

function storyItems(job) {
  const items = [];
  for (const k of job.keyframes) {
    items.push({ key: `kf-${k.position}`, k });
    const s = job.segments.find((x) => x.kf_in === k.position);
    if (s) items.push({ key: `seg-${s.id}`, s });
  }
  items.push({ key: "add" });
  return items;
}

// The take a shot's card shows, the running one, and its line of status: [text, class].
function shotState(s) {
  const live = s.takes.find((t) => t.status === "running") || s.takes.find(isLive);
  const retained = s.takes.find((t) => t.id === s.retained_take_id);
  const shown = retained || [...s.takes].reverse().find((t) => t.status === "done" && t.output_url);
  const done = doneOf(s).length;
  let line;
  if (live) {
    const q = queueItem("take", live.id);
    line = live.status === "running"
      ? [`${TIER_LABELS[live.tier]} ${q ? taskPercent(q) : 0} %`, "live"] : [`${TIER_LABELS[live.tier]} en file`, "queued"];
  } else if (retained) line = [`✓ prise #${retained.id} gardée`, "ok"];
  else if (s.takes.at(-1)?.status === "failed") line = ["dernière prise en échec", "bad"];
  else if (done) line = ["aucune prise gardée", "warn"];
  else if (!s.prompt.trim()) line = ["à décrire", ""];
  else line = ["pas encore de prise", ""];
  return { live, shown, line };
}

function storySig(job, it, next) {
  const v = viewOf(job);
  if (it.k) {
    const k = it.k;
    return JSON.stringify([k.id, k.version, k.source_keygen_id, k.match_applied, job.keyframes.length, v.kf === k.position,
      job.keygens.some((g) => isLive(g) && g.position === k.position)]);
  }
  if (it.s) {
    const st = shotState(it.s);
    return JSON.stringify([it.s.idx, it.s.duration_s, it.s.kf_out, st.shown?.id, st.line, it.s.takes.length,
      currentSeg(job) === it.s.id, segOfTarget(job, next.target) === it.s.id]);
  }
  return `add${job.keyframes.length}`;
}

function renderStoryItem(job, it) {
  if (!it.k && !it.s) return renderAddKeyframe(job);
  if (it.k) {
    const k = it.k;
    const isStart = k.position === 0;
    const isLast = k.position === job.keyframes.length - 1 && job.keyframes.length > 1;
    const creating = job.keygens.some((g) => isLive(g) && g.position === k.position);
    return h("button", { type: "button", class: `sb-kf${viewOf(job).kf === k.position ? " sel" : ""}`,
      title: "Voir, remplacer, ajuster cette image", onclick: () => toggleKf(job, k.position) },
    h("img", { src: kfUrl(k), alt: kfName(k) }),
    h("span", { class: "sb-label" }, isStart ? "départ" : isLast ? `image ${k.position} · fin` : `image ${k.position}`,
      k.source_keygen_id ? h("span", { class: "badge generated", title: "Créée par Flux 2 Klein" }, "créée") : null,
      creating ? h("span", { class: "badge s-running" }, "en création") : null));
  }
  const s = it.s;
  const { live, shown, line } = shotState(s);
  const sel = currentSeg(job) === s.id;
  let thumb;
  if (shown) {
    const mid = (shown.settings?.video_length || 1) / (shown.fps || 16) / 2;
    thumb = h("video", { src: `${shown.output_url}#t=${mid.toFixed(2)}`, muted: true, preload: "metadata",
      playsInline: true, loop: true, onmouseenter: (ev) => ev.target.play().catch(() => {}),
      onmouseleave: (ev) => ev.target.pause() });
  } else if (live?.preview_url) {
    thumb = h("img", { src: `${live.preview_url}?t=${Date.now()}`, alt: "" });
  } else {
    thumb = h("span", { class: "sb-empty" }, live ? "en file…" : "—");
  }
  const todo = segOfTarget(job, nextStep(job).target) === s.id;
  return h("button", { type: "button", class: `sb-seg${sel ? " sel" : ""}${s.kf_out === null ? " open-end" : ""}`,
    title: sel ? "Ce plan est déplié plus bas" : "Déplier ce plan", onclick: () => selectSeg(job, s.id) },
  h("span", { class: "sb-head" }, h("strong", {}, `Plan ${s.idx + 1}`),
    h("span", { class: "muted" }, ` · ${String(s.duration_s).replace(".", ",")} s`),
    todo ? h("span", { class: "sb-todo", title: "La prochaine chose à faire est dans ce plan" }, "à faire") : null),
  h("span", { class: "sb-thumb" }, thumb),
  h("span", { class: `sb-status ${line[1]}` }, line[0],
    s.takes.length ? h("span", { class: "muted" }, ` · ${s.takes.length} prise${s.takes.length > 1 ? "s" : ""}`) : null));
}

function kfVersions(job, s) {
  return [s.kf_in, s.kf_out].map((p) => job.keyframes.find((k) => k.position === p)?.version);
}

function renderJobHead(job) {
  const finals = state.presets.filter((p) => p.tier === "final");
  const patch = (field) => (ev) => act(() => api("PATCH", `/jobs/${job.id}`, { [field]: ev.target.value }));
  return [
    h("div", { class: "row" },
      h("a", { href: "#/" }, "← Mes projets"),
      h("input", { class: "title-input", value: job.title, placeholder: `Projet ${job.id}`, onchange: patch("title"),
        title: "Titre du projet (clique pour le changer)" }),
      badge(job.status)),
    styleField(job),
    advanced(`job-${job.id}`, "Réglages du projet",
      h("div", { class: "row settings" },
        h("label", {}, "Modèle du rendu final",
          h("select", { onchange: patch("preset_final") },
            finals.map((p) => h("option", { value: p.name, selected: p.name === job.preset_final }, p.label)))),
        h("label", {}, "Fin du dernier plan", endModeSelect(job.end_mode, patch("end_mode")))),
      h("p", { class: "muted small" },
        job.keyframes.length < 2 ? "La fin du dernier plan ne compte que si tu ajoutes une image de fin. " : "",
        job.end_mode === "guide"
          ? "Indicative : le rendu final du dernier plan part de son image de départ seule et le prompt mène la " +
            "fin ; l'image de fin sert seulement à juger l'aperçu. Les plans du milieu arrivent toujours sur leur image."
          : "Exacte : le rendu final de chaque plan arrive exactement sur son image de fin (modèle 14B)."),
      h("div", { class: "row" }, h("button", { type: "button", class: "danger", onclick: async () => {
        const live = state.queue.items.filter((i) => i.job_id === job.id);
        const summary = (await api("GET", "/jobs").catch(() => [])).find((j) => j.id === job.id) ||
          { ...job, running: live.length, queued: 0, disk_bytes: 0 };
        if (await deleteJob(summary)) location.hash = "#/";
      } }, "Supprimer le projet…"))),
  ];
}

// The job's style (P9): put in front of every motion prompt and of an image created from text alone,
// so it is written once. A painted or drawn image needs it: without it the video turns photorealistic.
function styleField(job) {
  const input = h("input", { class: "style-input", value: job.style,
    placeholder: "ex. « A watercolor painting, loose wet brushstrokes, visible paper texture » (vide pour une photo)",
    onchange: (ev) => act(() => api("PATCH", `/jobs/${job.id}`, { style: ev.target.value })) });
  return h("div", { class: "style-field" },
    h("label", {}, "Style des images", input),
    fold(`style-${job.id}`, "adv", "À quoi ça sert, exemples",
      h("p", { class: "muted small" },
        "Pour une aquarelle, un dessin, une peinture : sans le style nommé, la vidéo vire à la photo. En anglais. " +
        "Il est mis devant chaque prompt de mouvement et devant une image créée depuis le texte seul (une image " +
        "retouchée garde le style de sa source) : plus besoin de le répéter. Il vaut pour les prochains rendus, " +
        "pas pour les prises déjà faites."),
      h("div", { class: "tips-examples" }, h("span", { class: "muted" }, "Exemples (clic pour le prendre) :"),
        STYLE_EXAMPLES.map((e) => h("button", { type: "button", class: "link", title: "Prendre ce style",
          onclick: () => act(() => api("PATCH", `/jobs/${job.id}`, { style: e })) }, `« ${e} »`)))));
}

const STYLE_EXAMPLES = [
  "A watercolor painting, loose wet brushstrokes, soft pigment bleeding, visible paper texture",
  "A pencil drawing, graphite lines and hatching on white paper",
  "An oil painting, thick visible brushstrokes, canvas texture",
];

// --- keyframes ----------------------------------------------------------------------------------

function devClass(kind, v) {
  const [ok, warn] = kind === "color" ? [2, 6] : [12, 25];
  return v <= ok ? "ok" : v <= warn ? "warn" : "bad";
}

function devLine(dev) {
  if (!dev) return null;
  const part = (kind, label) => h("span", { class: `dev ${devClass(kind, dev.after[kind])}` },
    `${label} ${dev.before[kind]} → ${dev.after[kind]}`);
  return h("div", {
    class: "devs",
    title: "Écart avec l'image de départ, en niveaux de gris (0-255), avant → après l'ajustement. Couleurs : " +
      "distance entre histogrammes, 0 quand les couleurs sont les mêmes. Pixels : différence moyenne pixel " +
      "par pixel, qui compte aussi la pose et le cadrage.",
  }, part("color", "écart couleurs"), " ", part("pixel", "pixels"));
}

function filePicker(onfile, label, cls = "") {
  const input = h("input", { type: "file", accept: "image/*", hidden: true,
    onchange: (ev) => ev.target.files[0] && onfile(ev.target.files[0]) });
  return h("button", { type: "button", class: cls, onclick: () => input.click() }, label, input);
}

function uploadKeyframe(job, position, blob, name = "keyframe.png") {
  const f = new FormData();
  f.append("position", position);
  f.append("file", blob, name);
  return act(() => api("POST", `/jobs/${job.id}/keyframes`, f));
}

function dropZone(el, onfile) {
  el.addEventListener("dragover", (ev) => { ev.preventDefault(); el.classList.add("drag"); });
  el.addEventListener("dragleave", () => el.classList.remove("drag"));
  el.addEventListener("drop", (ev) => {
    ev.preventDefault();
    el.classList.remove("drag");
    const f = ev.dataTransfer.files[0];
    if (f) onfile(f);
  });
  return el;
}

// The last tile of the storyboard: the start image (large, alone) or one more image at the end.
function renderAddKeyframe(job) {
  const n = job.keyframes.length;
  const upload = (file) => uploadKeyframe(job, n, file, file.name);
  if (n === 0) {
    return dropZone(h("div", { class: "kf add first", dataset: { next: "add-kf" } },
      h("div", { class: "kf-drop" },
        h("strong", {}, "Dépose l'image de départ ici"),
        h("div", { class: "muted small" },
          "photo, aquarelle, dessin ; une image détourée (transparence) est posée sur un fond uni"),
        filePicker(upload, "Choisir un fichier", "primary"))), upload);
  }
  return dropZone(h("div", { class: "sb-add", dataset: { next: "add-kf" },
    title: "Le rendu final arrivera sur cette image. Tu peux aussi la créer avec l'IA, ou en prendre une dans " +
      "une prise (« Utiliser cette image », sous la vidéo)." },
  h("strong", {}, n === 1 ? "+ image de fin" : "+ image"),
  h("span", { class: "muted small" }, "facultatif · dépose-la ici"),
  filePicker(upload, "Fichier…"),
  h("button", { type: "button", onclick: () => openKeygen(job, n) }, "Créer…")), upload);
}

function kfName(k) { return k.position === 0 ? "image de départ" : `image ${k.position}`; }

function renderKeyframe(job, k) {
  const isStart = k.position === 0;
  const isLast = k.position === job.keyframes.length - 1;
  const p = k.match_params || {};
  const bg = "#" + (p.background || [255, 255, 255]).map((c) => c.toString(16).padStart(2, "0")).join("");
  const replace = (file) => uploadKeyframe(job, k.position, file, file.name);
  const title = isStart ? "Image de départ" : isLast ? `Image ${k.position} · fin` : `Image ${k.position}`;
  const creating = job.keygens.find((g) => isLive(g) && g.position === k.position);
  const card = h("div", { class: "card kf-panel" + (isStart ? " start" : "") },
    h("img", { src: kfUrl(k), alt: kfName(k), title: "Agrandir", onclick: () => openCompare(job, k) }),
    h("div", { class: "kf-panel-body" },
      h("div", { class: "kf-title" }, title,
        k.source_keygen_id ? h("span", { class: "badge generated", title: `Créée par Flux 2 Klein ` +
          `(création #${k.source_keygen_id}, dans « Images créées »)` }, "créée") : null,
        !isStart && k.match_applied ? h("span", { class: "badge s-done", title: "Ajustée sur l'image de départ" }, "ajustée") : null,
        creating ? h("span", { class: "badge s-running", title: `L'image créée (création #${creating.id}) la remplacera` },
          "en création") : null,
        h("span", { class: "spacer" }),
        h("button", { type: "button", class: "close", title: "Fermer", onclick: () => toggleKf(job, k.position) }, "×")),
      isStart ? null : matchButtons(k, bg),
      h("div", { class: "row small-buttons" },
        h("button", { type: "button", onclick: () => openCompare(job, k) }, isStart ? "Agrandir" : "Avant / après"),
        filePicker(replace, "Remplacer"),
        h("button", { type: "button", title: "Flux 2 Klein crée une image qui remplacera celle-ci",
          onclick: () => openKeygen(job, k.position) }, "Recréer avec l'IA…"),
        h("button", { type: "button", title: "Insère une copie exacte juste après cette image (les suivantes se " +
          "décalent) : pour un geste qui revient à cette image, un plan va vers le geste, le suivant revient sur la copie.",
        onclick: () => act(() => api("POST", `/keyframes/${k.id}/copy`, {})) }, "Dupliquer"),
        isLast || !isStart
          ? h("button", { type: "button", class: "danger", onclick: () => {
            const msg = isLast ? `Retirer l'${kfName(k)} ?`
              : `Retirer l'${kfName(k)} ? Les deux plans autour n'en font plus qu'un (celui qui en part garde son ` +
                "prompt et ses prises) ; les images suivantes se décalent.";
            if (confirm(msg)) act(() => api("DELETE", `/keyframes/${k.id}`));
          } }, "Retirer")
          : null),
      isStart ? startAdvanced(k, bg) : matchAdvanced(k),
      h("p", { class: "muted small" }, "Dépose un fichier sur ce panneau pour remplacer l'image.")));
  return dropZone(card, replace);
}

function startAdvanced(k, bg) {
  const color = h("input", { type: "color", value: bg });
  const fringes = h("input", { type: "checkbox", checked: k.match_params?.remove_fringes !== false });
  const apply = () => act(() => api("POST", `/keyframes/${k.id}/match`,
    { background: color.value, remove_fringes: fringes.checked }));
  return advanced(`kf-${k.id}`,
    h("div", { class: "kf-controls" },
      h("label", { class: "inline" }, "Fond", color),
      h("label", { class: "inline" }, fringes, "retirer les liserés"),
      h("button", { type: "button", onclick: () => apply() }, "Appliquer")),
    h("p", { class: "muted small" },
      "Fond : couleur sur laquelle une image détourée est posée ; les autres images suivent. ",
      "Liserés : rogne le détourage de 2 px pour enlever le halo de l'ancien fond."));
}

// The match flags live in the « Avancé » block; the buttons read them from the card.
function matchButtons(k, bg) {
  const run = (enabled, ev) => {
    const card = ev.target.closest(".kf-panel");
    act(() => api("POST", `/keyframes/${k.id}/match`, {
      enabled, background: bg,
      color_match: card.querySelector(".opt-colors").checked,
      remove_fringes: card.querySelector(".opt-fringes").checked,
    }));
  };
  return h("div", { class: "kf-controls" },
    h("button", { type: "button", class: k.match_applied ? "" : "primary", onclick: (ev) => run(true, ev),
      title: "Même taille, même cadrage, même fond et mêmes couleurs que l'image de départ. " +
        "Corrige les couleurs et le format, pas l'identité (visage, âge)." },
    k.match_applied ? "Réajuster" : "Ajuster sur le départ"),
    k.match_applied ? h("button", { type: "button", onclick: (ev) => run(false, ev) }, "Annuler l'ajustement") : null,
    k.match_applied ? null : h("div", { class: "muted small" }, "même format et mêmes couleurs que l'image de départ"));
}

function matchAdvanced(k) {
  const p = k.match_params || {};
  return advanced(`kf-${k.id}`,
    h("div", { class: "kf-controls" },
      h("label", { class: "inline" },
        h("input", { type: "checkbox", class: "opt-colors", checked: !k.match_applied || p.color_match !== false }),
        "ajuster les couleurs"),
      h("label", { class: "inline" },
        h("input", { type: "checkbox", class: "opt-fringes", checked: p.remove_fringes !== false }),
        "retirer les liserés")),
    devLine(k.deviation));
}

function openCompare(job, k) {
  const start = job.keyframes.find((x) => x.position === 0);
  const fig = (src, caption, cls = "") => h("figure", {}, h("img", { src, class: cls }), h("figcaption", {}, caption));
  const body = k.position === 0
    ? [fig(k.orig_url, "Image déposée", "checker"), fig(kfUrl(k), "Préparée (envoyée au modèle)")]
    : [fig(kfUrl(start), "Image de départ (référence)"), fig(k.orig_url, "Avant : image déposée", "checker"),
      fig(kfUrl(k), k.match_applied ? "Après : ajustée" : "Préparée (pas ajustée)")];
  openModal(h("div", {},
    h("h3", {}, k.position === 0 ? "Image de départ" : `Image ${k.position} : avant / après l'ajustement`),
    h("div", { class: "compare" }, body),
    devLine(k.deviation),
    k.position === 0 ? null : h("p", { class: "muted small" },
      "L'ajustement corrige la taille, le cadrage, le fond et les couleurs. Une image prise dans un aperçu garde ",
      "sa dérive d'identité (visage, âge) : le modèle 14B la reproduit à la fin du plan.")));
}

// --- prompt help --------------------------------------------------------------------------------
//
// Folded « Conseils » under a text field: what worked in the measures (docs/benchmark.md: P8 for
// the keygen prompt; P9 notes for the motion prompt), plus examples that fill the field.

// Put a text in a prompt field as if typed: its listeners save it (segment) or keep it (keygen draft).
function fillField(field, text) {
  field.value = text;
  field.dispatchEvent(new Event("input"));
  field.dispatchEvent(new Event("change"));
  field.focus();
}

function promptTips(key, field, tips, examples) {
  const use = (text) => fillField(field, text);
  return fold(`tips-${key}`, "adv", "Conseils pour bien l'écrire",
    h("ul", { class: "tips" }, tips.map((t) => h("li", {}, t))),
    h("div", { class: "tips-examples" }, h("span", { class: "muted" }, "Exemples (clic pour reprendre) :"),
      examples.map((e) => h("button", { type: "button", class: "link", title: "Mettre cet exemple dans le champ",
        onclick: () => use(e) }, `« ${e} »`))));
}

const MOTION_TIPS = [
  "En anglais : les modèles vidéo le suivent mieux qu'en français.",
  "Décris l'image, puis ce qui bouge, dans l'ordre : sujet, mouvement, caméra. Ex. « … slowly smiles. Static camera ».",
  "Nomme le style s'il n'est pas photo (« a watercolor painting, loose wet brushstrokes, paper texture ») : sans ça, " +
    "une aquarelle ou un dessin vire vite à la photo. Le plus simple : l'écrire une fois dans « Style des images », en haut.",
  "Image peinte ou dessinée : pour que le style tienne pendant le mouvement, crée l'image de fin depuis l'image de " +
    "départ (« Créer une image », ex. « Make her smile widely ») ; le rendu final arrive sur une image peinte.",
  "Un ou deux mouvements par plan, pas une histoire : 2 à 3 s, c'est court. Pour une suite d'actions, ajoute des images et des plans.",
  "Dis ce qui ne bouge pas (« static camera », « plain white background ») pour éviter les mouvements parasites.",
  "Avec une image de fin, décris le chemin vers elle : le rendu final y arrive de toute façon.",
  "Un geste fin et d'un seul côté (clin d'œil, une main, un sourcil) ne sort pas du texte seul : le rendu final ferme " +
    "les deux yeux. Donne-lui son image et son plan (« Découper le geste », plus bas), puis décris-le partie par " +
    "partie, lentement, du côté où il est sur l'image : « She slowly closes her right eye in a wink; her left eye stays " +
    "open and she keeps the same smile ». L'image garantit le geste à la fin du plan ; avant, le rendu final ferme " +
    "souvent les deux yeux (1 prise réussie sur 3 mesurées) : fais plusieurs prises (autre graine) et garde la meilleure.",
  "L'aperçu ne juge pas un geste fin : sans image de fin, il ferme les deux yeux quel que soit le prompt. Il montre le " +
    "rythme ; seul le rendu final vers l'image du geste dit si le geste passe.",
];

const MOTION_EXAMPLES = [
  "The man smiles warmly, slowly closes his eyes and lowers his head slightly. Subtle natural facial motion, static camera, plain white background.",
  "A watercolor painting of a woman's face, loose wet brushstrokes, paint splashes on white paper. She slowly smiles. Static camera.",
  "She slowly closes her right eye in a wink; her left eye stays open and she keeps the same smile. Static camera.",
  "The boats rock gently on the water, light shimmers on the waves, clouds drift slowly. Static camera.",
];

const KEYGEN_TIPS = [
  "En anglais de préférence (c'est ce qui a été mesuré).",
  "Depuis une image : une consigne d'édition, ce qui change, à l'impératif (« Make him close his eyes and smile " +
    "widely »). Pas besoin de redécrire l'image : une phrase qui demande de garder le reste est ajoutée.",
  "Le visage n'est gardé que si l'image source principale (la 1re) est l'image de départ. Depuis une autre image, " +
    "ou du texte seul, le personnage peut changer : la vidéo devra alors passer d'un visage à l'autre.",
  "L'expression et le regard sont bien suivis ; un mouvement de tête ou de corps l'est mal (« lower his head » ignoré en P8).",
  "Gauche et droite : Klein les lit souvent du côté du spectateur (« her left eye » lui a fermé l'œil droit). Regarde " +
    "l'image créée et décris ensuite le geste du plan comme il est dessus.",
  "Texte seul : décris toute l'image, sujet, cadrage, fond et style (« a watercolor painting of… »).",
  "Plusieurs images : la 1re est la scène principale, les suivantes des personnes ou des objets à y mettre. " +
    "Dis le rôle de chacune (« Put the cat from the second image on her lap »). Une 2ᵉ image donnée comme modèle " +
    "d'expression est recopiée presque telle quelle (P8) : pour une expression, écris-la plutôt.",
  "Pour une keyframe, reste proche de l'image de départ : le modèle vidéo devra y arriver en 2 à 3 s.",
  "Résultat moyen ? « Autre tirage » relance avec une autre graine, puis garde la meilleure (« Utiliser celle-ci »).",
];

const KEYGEN_EXAMPLES = [
  "Make the man close his eyes, smile widely and warmly.",
  "Make her look to the left with a surprised expression, mouth slightly open.",
  "Make her wink: she closes only her left eye, her right eye stays wide open. She keeps smiling.",
  "Turn this photo into a watercolor painting, loose wet brushstrokes, white paper.",
  "A watercolor portrait of an old fisherman with a grey beard, facing the viewer, plain white background.",
];

// --- prompt review (P12) ------------------------------------------------------------------------
//
// Two levels (zoetrope/prompt_review.py): instant rules, checked by the API as the user types and
// listed under the field; and on request an AI review (Qwen3 4B, a short task at the head of the
// queue) that rewrites the prompt to fix them, which the user takes or not. The AI does not see
// the images: it knows the duration, the end image and the prompts the images were created with.

const REVIEW_ESTIMATE = "~15 s";

const CHECK_TEXT = {
  empty: (_f, t) => t === "motion" ? "Prompt vide : décris ce qui bouge."
    : "Prompt vide : décris toute l'image, ou ajoute une image source.",
  french: () => "En français : les modèles suivent mieux l'anglais (la relecture IA le traduit).",
  short: (f, t) => `Très court (${f.words} mot${f.words > 1 ? "s" : ""}) : ` +
    (t === "motion" ? "dis le sujet, ce qui bouge, la caméra et le style." : "dis le sujet, le cadrage, le fond et le style."),
  no_style: (_f, t) => "Le style n'est pas nommé (« a watercolor painting… », « a pencil drawing… », « a photo… ») : " +
    (t === "motion" ? "sans ça, une aquarelle ou un dessin vire vite à la photo." : "sans ça, le modèle choisit."),
  no_camera: () => "La caméra n'est pas dite (« static camera », « slow push-in ») : sans ça, elle bouge souvent toute seule.",
  too_many_actions: (f) => `${f.actions} actions à la suite pour ${f.duration_s ?? "?"} s : ${f.max} au plus tiennent ` +
    "dans un plan. Pour une suite d'actions, ajoute des images et des plans.",
  negation: () => "Une négation (« no », « without », « sans »…) : les modèles la suivent mal, dis plutôt ce que tu veux voir.",
  fine_gesture: (f) => `Geste fin (« ${f.gesture} ») : le texte seul ne le donne pas, le rendu final le fait des deux ` +
    "côtés (les deux yeux pour un clin d'œil). Donne-lui son image et son plan : « Découper le geste », plus bas.",
  gesture_chained: (f) => `Ce plan finit sur l'image d'un geste fin (« ${f.gesture} ») : qu'il ne fasse que ce geste. ` +
    "Mets les autres mouvements dans un autre plan.",
  gesture_explicit: (f) => `Ce plan finit sur l'image d'un geste fin (« ${f.gesture} ») : décris-le partie par partie, ` +
    "lentement, du côté où il est sur l'image, et ce qui ne bouge pas (« She slowly closes her right eye in a wink; " +
    "her left eye stays open and she keeps the same smile »). Un prompt court ne le montre qu'aux 2 dernières images ; " +
    "même explicite, le geste n'est pas toujours suivi : fais plusieurs prises et garde la meilleure.",
  side: () => "Gauche / droite : Klein les lit souvent du côté du spectateur. Regarde l'image créée, et dans le prompt " +
    "du plan, décris le geste comme il est dessus.",
};

// Rule findings under a field, refreshed as the user types. `body()` gives the prompt and where
// it is (as POST /prompt-check takes it). Returns the list element and a function that re-runs it.
function promptChecks(job, field, body) {
  const box = h("ul", { class: "checks" });
  let timer = null;
  let asked = 0;
  const run = async () => {
    const b = body();
    const n = ++asked;
    try {
      const r = await api("POST", `/jobs/${job.id}/prompt-check`, b);
      if (n !== asked) return; // a newer check is on its way
      box.replaceChildren(...(r.findings.length
        ? r.findings.map((f) => h("li", {}, (CHECK_TEXT[f.code] || (() => f.code))(f, b.target)))
        : (b.text.trim() ? [h("li", { class: "ok" }, "Rien à redire selon les règles simples.")] : [])));
    } catch (_) { /* the API is down: the top bar says so */ }
  };
  field.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 400); });
  run();
  return { box, run };
}

// « Relire avec l'IA »: `beforeSend` may save the field first (a segment's prompt).
function reviewButton(job, body, beforeSend = async () => true) {
  return h("button", { type: "button", title: "Un modèle de langue local (Qwen3 4B) relit le prompt et propose " +
    "une version corrigée. Il passe en tête de la file.", onclick: async () => {
    const b = body();
    if (!b.text.trim()) { toast("Écris d'abord un prompt."); return; }
    if (!(await beforeSend())) return;
    await act(() => api("POST", `/jobs/${job.id}/reviews`, b));
  } }, `Relire avec l'IA (${REVIEW_ESTIMATE})`);
}

const dismissedReviews = new Set();

function latestReview(job, match) {
  const r = job.reviews.filter(match).at(-1);
  return r && r.status !== "cancelled" && !dismissedReviews.has(r.id) ? r : null;
}

// Fill `box` with the review `r` (or nothing), rebuilt only when it changes.
function syncReview(box, r, field) {
  const q = r ? queueItem("review", r.id) : null;
  const sig = JSON.stringify(r ? [r.id, r.status, r.phase, r.result, r.error, q ? Math.round(q.starts_in_s / 5) : 0] : null);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren(...(r ? [renderReview(r, field, q)] : []));
}

function renderReview(r, field, q) {
  const cancel = h("button", { type: "button", class: "danger",
    onclick: () => act(() => api("POST", `/reviews/${r.id}/cancel`)) }, r.cancel_requested ? "annulation…" : "Annuler");
  const close = h("button", { type: "button", title: "Masquer cette relecture",
    onclick: () => { dismissedReviews.add(r.id); refreshNow(); } }, "Fermer");
  const head = h("div", { class: "review-head" }, h("strong", {}, "Relecture IA"),
    h("span", { class: "muted small" }, ` de « ${r.text} »`), h("span", { class: "spacer" }),
    isLive(r) ? cancel : close);
  let body;
  if (r.status === "queued") {
    body = h("p", { class: "muted" }, q ? `En file : commence dans ${fmtDuration(q.starts_in_s)} environ.` : "En file.");
  } else if (r.status === "running") {
    body = h("p", { class: "muted" }, r.phase === "inference" ? "Le modèle écrit…" : "Chargement du modèle…");
  } else if (r.status === "failed") {
    body = h("pre", { class: "error" }, r.error || "échec");
  } else {
    const res = r.result || { issues: [], suggestion: "" };
    const placeholder = res.suggestion.includes("[style]");
    body = [
      res.issues.length ? [h("p", { class: "small" }, "Autres remarques :"),
        h("ul", { class: "checks" }, res.issues.map((t) => h("li", {}, t)))] : null,
      res.suggestion ? h("blockquote", { class: "suggestion" }, res.suggestion) : h("p", { class: "muted" }, "Pas de proposition."),
      placeholder ? h("p", { class: "muted small" }, "Remplace [style] par le style de l'image " +
        "(« A watercolor painting of… », « A pencil drawing of… »…) : l'IA ne voit pas l'image. Ou écris-le une " +
        "fois dans « Style des images », en haut du projet.") : null,
      res.suggestion ? h("div", { class: "row" },
        h("button", { type: "button", class: "primary", title: "Remplace le prompt par cette version",
          onclick: () => fillField(field, res.suggestion) }, "Utiliser cette version"),
        r.durations ? h("span", { class: "muted small" }, `relu en ${fmtDuration(r.durations.total)}`) : null) : null,
    ];
  }
  return h("div", { class: `review ${r.status}` }, head, body);
}

// --- keygens: an image created by Flux 2 Klein ----------------------------------------------------
//
// From a prompt and/or up to three source images (the job's images or files from the computer),
// to any image of the job, the start image included. Only an edit of the start image keeps its
// face (docs/benchmark.md, P8). When it finishes, the backend puts it at its position (technical
// match applied to a keyframe); older ones stay listed and can be used again.

const KEYGEN_SOURCES_MAX = 3;
const ASPECTS = [["portrait", "portrait (3:4)"], ["square", "carré"], ["landscape", "paysage (4:3)"]];
// job id -> { instruction, sources: [{ keyframe } | { file, url }], target, aspect }: survives rebuilds
const keygenDrafts = new Map();

function keygenDraft(job) {
  let d = keygenDrafts.get(job.id);
  if (!d) {
    d = { instruction: "", sources: job.keyframes.length ? [{ keyframe: 0 }] : [], target: null, aspect: "portrait" };
    keygenDrafts.set(job.id, d);
  }
  d.sources = d.sources.filter((s) => s.file || job.keyframes.some((k) => k.position === s.keyframe));
  return d;
}

function posName(p) { return p < 0 ? "image retirée" : p === 0 ? "image de départ" : `image ${p}`; }

function keygenTargets(job) {
  const n = job.keyframes.length;
  if (n === 0) return [[0, "comme image de départ"]];
  const opts = [];
  for (let i = n - 1; i >= 1; i--) opts.push([i, i === n - 1 ? `à la place de l'image ${i} (fin)` : `à la place de l'image ${i}`]);
  opts.push([n, n === 1 ? "comme image de fin" : `comme nouvelle image ${n}`]);
  opts.push([0, "à la place de l'image de départ"]);
  return opts;
}

const hasTakes = (job) => job.segments.some((s) => s.takes.length);

// Replacing the start image while takes exist: they keep the old one, say so before going on.
function confirmStartReplace(job) {
  return !hasTakes(job) || confirm("Des prises partent de l'image de départ actuelle. Elles restent telles quelles, " +
    "mais les nouvelles prises partiront de l'image créée, et les autres images seront réajustées dessus. Continuer ?");
}

function renderKeygenForm(job) {
  const d = keygenDraft(job);
  const n = job.keyframes.length;
  const targets = keygenTargets(job);
  if (!targets.some(([p]) => p === d.target)) d.target = targets[0][0];

  const instruction = h("textarea", { class: "prompt", rows: 2, dataset: { next: "keygen" },
    oninput: (ev) => { d.instruction = ev.target.value; } });
  instruction.value = d.instruction;
  const target = h("select", { onchange: (ev) => { d.target = Number(ev.target.value); update(); } },
    targets.map(([pos, label]) => h("option", { value: pos, selected: pos === d.target }, label)));
  const aspect = h("label", { class: "inline" }, "Format",
    h("select", { onchange: (ev) => { d.aspect = ev.target.value; } },
      ASPECTS.map(([v, label]) => h("option", { value: v, selected: v === d.aspect }, label))));
  const sourcesBox = h("div", { class: "keygen-sources" });
  const notes = h("div", { class: "keygen-notes" });
  const promptBody = () => ({ target: "image", text: d.instruction, position: d.target,
    sources: d.sources.map((s) => (s.file ? { upload: 0 } : { keyframe: s.keyframe })) });
  const checks = promptChecks(job, instruction, promptBody);

  const addFile = (file) => {
    if (d.sources.length >= KEYGEN_SOURCES_MAX) { toast(`${KEYGEN_SOURCES_MAX} images source au plus.`); return; }
    d.sources.push({ file, url: URL.createObjectURL(file) });
    update();
  };
  const remove = (i) => {
    const [s] = d.sources.splice(i, 1);
    if (s.url) URL.revokeObjectURL(s.url);
    update();
  };
  const move = (i) => { // to the front: becomes the main scene
    d.sources.unshift(...d.sources.splice(i, 1));
    update();
  };

  function renderSources() {
    const chips = d.sources.map((s, i) => {
      const k = s.file ? null : job.keyframes.find((x) => x.position === s.keyframe);
      return h("figure", { class: "source" + (i === 0 ? " main" : "") },
        h("img", { src: s.file ? s.url : kfUrl(k), alt: "" }),
        h("figcaption", {}, s.file ? s.file.name : posName(s.keyframe),
          h("span", { class: "muted" }, i === 0 ? " · scène principale" : " · personne / objet")),
        h("div", { class: "row small-buttons" },
          i > 0 ? h("button", { type: "button", title: "En faire la scène principale", onclick: () => move(i) }, "1re") : null,
          h("button", { type: "button", title: "Retirer cette image source", onclick: () => remove(i) }, "×")));
    });
    const free = job.keyframes.filter((k) => !d.sources.some((s) => s.keyframe === k.position));
    const add = d.sources.length < KEYGEN_SOURCES_MAX ? dropZone(h("div", { class: "source add" },
      free.length ? h("select", { onchange: (ev) => {
        if (ev.target.value !== "") { d.sources.push({ keyframe: Number(ev.target.value) }); update(); }
      } }, h("option", { value: "" }, "+ une image du projet"),
      free.map((k) => h("option", { value: k.position }, posName(k.position)))) : null,
      filePicker(addFile, "+ une image de l'ordinateur"),
      h("span", { class: "muted small" }, "ou dépose-la ici")), addFile) : null;
    return [
      chips.length ? chips : h("p", { class: "muted small source-none" },
        "Aucune image source : l'image vient du texte seul."),
      add,
    ];
  }

  function update() {
    sourcesBox.replaceChildren(...renderSources().flat().filter(Boolean));
    const fromStart = d.sources[0] && !d.sources[0].file && d.sources[0].keyframe === 0;
    instruction.placeholder = d.sources.length
      ? "ce qui change, ex. « Make her close her eyes and smile widely » (facultatif) ; le reste est gardé"
      : "décris toute l'image, ex. « A watercolor portrait of an old fisherman, plain white background »";
    aspect.hidden = n > 0 || d.sources.length > 0;
    const lines = [];
    if (d.target !== 0 && !fromStart) {
      lines.push("Sans l'image de départ comme 1re image source, le visage et le style peuvent changer : la vidéo " +
        "devra passer de l'un à l'autre.");
    }
    if (d.target === 0 && n > 0) {
      lines.push("L'image créée remplacera l'image de départ ; les autres images seront réajustées dessus." +
        (hasTakes(job) ? " Les prises déjà faites restent, mais partent de l'ancienne." : ""));
    }
    notes.replaceChildren(...lines.map((t) => h("p", { class: "warn-note" }, t)));
    checks.run(); // an edit or a text-only image is checked differently
  }

  const go = async () => {
    if (!d.sources.length && !d.instruction.trim()) {
      toast("Écris un prompt, ou ajoute une image source.");
      return;
    }
    if (d.target === 0 && n > 0 && !confirmStartReplace(job)) return;
    const f = new FormData();
    f.append("position", d.target);
    f.append("instruction", d.instruction);
    const files = d.sources.filter((s) => s.file);
    f.append("sources", JSON.stringify(d.sources.map((s) => (s.file ? { upload: files.indexOf(s) } : { keyframe: s.keyframe }))));
    for (const s of files) f.append("files", s.file, s.file.name);
    if (!aspect.hidden) f.append("aspect", d.aspect);
    const g = await act(() => api("POST", `/jobs/${job.id}/keygens`, f));
    if (g) toast(`Création de l'${posName(g.position)} lancée (~40 s).`, false);
  };

  update();
  const est = estimate(preset("keyframe-klein") || {});
  return [
    h("div", { class: "keygen-title" }, h("h3", {}, n === 0 ? "Ou crée l'image de départ" : "Créer une image"),
      h("span", { class: "spacer" }),
      n ? h("button", { type: "button", class: "close", title: "Replier",
        onclick: () => { viewOf(job).keygen = false; renderJob(); } }, "×") : null),
    helpLine(`keygen-${job.id}`, "Flux 2 Klein crée une image depuis un texte et/ou des images source ; " +
      "prompt en anglais de préférence.",
    n === 0 ? "Sans image source, décris toute l'image dans le prompt."
      : "Pour une image de fin qui garde le visage, le cadrage et le fond du départ, garde l'image de départ comme " +
        "1re source et écris ce qui change (expression, regard, geste) : mieux qu'une image prise dans un aperçu, " +
        "qui dérive (visage, âge)."),
    h("div", { class: "keygen-field" }, h("span", { class: "field-label" }, "Images source (3 au plus)"), sourcesBox),
    h("div", { class: "keygen-row" }, instruction,
      h("div", { class: "keygen-actions" }, target, aspect,
        h("button", { type: "button", class: "primary", onclick: go },
          `Créer l'image${est ? ` (~${fmtDuration(est)})` : ""}`))),
    notes,
    checks.box,
    h("div", { class: "row" }, reviewButton(job, promptBody)),
    h("div", { class: "review-box" }),
    promptTips(`keygen-${job.id}`, instruction, KEYGEN_TIPS, KEYGEN_EXAMPLES),
  ];
}

function sourceName(s) { return s.kind === "upload" ? "image déposée" : posName(s.position); }

function isCurrentKeygen(job, g) {
  return job.keyframes.some((k) => k.position === g.position && k.source_keygen_id === g.id);
}

function useKeygen(job, g) {
  if (g.position === 0 && job.keyframes.length && !confirmStartReplace(job)) return;
  act(() => api("POST", `/keygens/${g.id}/use`));
}

function keygenFigures(g, onclick) {
  const fig = (src, caption) => h("figure", {}, h("img", { src, title: onclick ? "Agrandir" : null, onclick }),
    h("figcaption", {}, caption));
  const srcs = g.sources.length
    ? g.sources.map((s, i) => fig(s.url, `source${g.sources.length > 1 ? ` ${i + 1}` : ""} : ${sourceName(s)}`))
    : [h("div", { class: "text-only muted small" }, "texte seul")];
  return [...srcs, h("span", { class: "arrow" }, "→"), fig(g.output_url, "image créée")];
}

function renderKeygen(job, g) {
  const q = queueItem("keygen", g.id);
  const current = isCurrentKeygen(job, g);
  const again = () => act(() => api("POST", `/keygens/${g.id}/again`));
  const head = h("div", { class: "take-head" },
    h("strong", {}, `Création #${g.id}`), " ", h("span", { class: "tier keyframe" }, posName(g.position)), " ", badge(g.status),
    current ? h("span", { class: "badge retained" }, `c'est l'${posName(g.position)}`) : null,
    h("span", { class: "muted small", title: g.preset }, ` graine ${g.seed} · ${g.settings.resolution || ""}`),
    h("span", { class: "spacer" }),
    isLive(g)
      ? h("button", { type: "button", class: "danger", onclick: () => act(() => api("POST", `/keygens/${g.id}/cancel`)) },
        g.cancel_requested ? "annulation…" : "Annuler")
      : [g.status === "done" && !current && g.position >= 0
        ? h("button", { type: "button", title: `Remet cette image comme ${posName(g.position)}`,
          onclick: () => useKeygen(job, g) }, "Utiliser celle-ci") : null,
      h("button", { type: "button", title: "Mêmes images source et même prompt, autre tirage (graine)", onclick: again },
        "Autre tirage"),
      h("button", { type: "button", class: "danger", disabled: current,
        title: current ? `C'est l'${posName(g.position)} actuelle : remplace-la d'abord pour pouvoir la supprimer`
          : "Supprimer cette création et ses fichiers",
        onclick: () => deleteThing(`/keygens/${g.id}`, `Supprimer la création #${g.id} ? Ses images sont effacées du disque.`,
          "Création supprimée") }, "Supprimer")]);
  const body = [g.instruction
    ? h("p", { class: "keygen-instruction" }, `« ${g.instruction} »`)
    : h("p", { class: "keygen-instruction muted" }, "sans prompt")];
  if (g.status === "queued") {
    body.push(h("p", { class: "muted" }, q ? `En file : commence dans ${fmtDuration(q.starts_in_s)} environ, ` +
      `dure ${fmtDuration(q.remaining_s)}.` : "En file."));
  } else if (g.status === "running") {
    body.push(renderProgress(g, q));
  } else if (g.status === "done" && g.output_url) {
    body.push(h("div", { class: "keygen-pair" }, keygenFigures(g, () => openKeygenCompare(g))));
    if (g.durations) body.push(h("p", { class: "muted small" }, `Créée en ${fmtDuration(g.durations.total)}, le ${fmtDate(g.finished_at)}.`));
    if (g.error) body.push(h("p", { class: "muted small" }, g.error));
  } else if (g.status === "failed") {
    body.push(h("pre", { class: "error" }, g.error || "échec"));
  }
  if (g.log_tail.length && g.status !== "done") {
    body.push(fold(`klog-${g.id}`, "", "Journal", h("pre", { class: "log" }, g.log_tail.join("\n"))));
  }
  return h("div", { class: `take ${g.status}${current ? " is-retained" : ""}`, dataset: { next: `keygen-${g.id}` } }, head, body);
}

function openKeygenCompare(g) {
  openModal(h("div", {},
    h("h3", {}, `Création #${g.id} : ${posName(g.position)}`),
    g.instruction ? h("p", {}, `« ${g.instruction} »`) : null,
    h("div", { class: "compare" }, keygenFigures(g, null))));
}

// « Créer une image » is folded behind a button (open when the job has no image yet); the created
// images are folded in « Images créées », except the ones queued or running.
function updateKeygens(view, job) {
  const open = keygenOpen(job);
  syncOne(view.querySelector(".keygen-bar"), JSON.stringify([open, job.keyframes.length]), () => (open ? [] : [
    h("button", { type: "button", onclick: () => openKeygen(job) }, "Créer une image avec l'IA…"),
    h("span", { class: "muted small" }, "Flux 2 Klein : une image de fin, un geste, une variante, depuis un texte et/ou des images."),
  ]));
  view.querySelector(".keygen-bar").hidden = open;
  syncOne(view.querySelector(".keygen-form"), JSON.stringify([job.id, job.keyframes.map((k) => [k.position, k.version]),
    hasTakes(job), state.presets.length, job.style]), renderKeygenForm);
  const form = view.querySelector(".keygen-form");
  form.hidden = !open;
  syncReview(form.querySelector(".review-box"), latestReview(job, (r) => r.target === "image"),
    form.querySelector("textarea.prompt"));
  const sig = (g) => {
    const q = queueItem("keygen", g.id);
    return JSON.stringify([g.status, g.phase, g.step, g.progress, g.preview_url, g.output_url, g.error,
      g.cancel_requested, isCurrentKeygen(job, g), hasTakes(job),
      isLive(g) ? [q?.starts_in_s, Math.round(q?.remaining_s / 5)] : 0]);
  };
  const gens = [...job.keygens].reverse();
  const past = gens.filter((g) => !isLive(g));
  syncList(view.querySelector(".keygen-live"), gens.filter(isLive), (g) => g.id, sig, (g) => renderKeygen(job, g));
  syncList(view.querySelector(".keygen-list"), past, (g) => g.id, sig, (g) => renderKeygen(job, g));
  const history = view.querySelector(".keygen-history");
  history.hidden = !past.length;
  history.querySelector("summary").textContent = `Images créées (${past.length})`;
}

function openModal(content) {
  const m = document.getElementById("modal");
  m.replaceChildren(h("div", { class: "modal-box" },
    h("button", { type: "button", class: "close", title: "Fermer", onclick: closeModal }, "×"), content));
  m.hidden = false;
}

function closeModal() {
  const m = document.getElementById("modal");
  m.hidden = true;
  m.replaceChildren();
}

document.getElementById("modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeModal(); });

// --- segments -----------------------------------------------------------------------------------

function renderSegment(job, s) {
  const kin = job.keyframes.find((k) => k.position === s.kf_in);
  const kout = s.kf_out === null ? null : job.keyframes.find((k) => k.position === s.kf_out);
  const prev = preset("preview");
  const fin = preset(job.preset_final);
  const prompt = h("textarea", { class: "prompt", rows: 3, dataset: { next: `prompt-${s.id}` },
    placeholder: "Ce qui bouge et comment, la caméra, le style… ex. « les bateaux tanguent doucement, l'eau scintille, caméra fixe »" });
  const duration = h("input", { type: "number", class: "duration", min: 0.25, max: 10, step: 0.25 });
  const frames = h("span", { class: "muted small" });
  const seed = h("input", { type: "number", class: "seed", placeholder: "au hasard", min: 0 });
  prompt.value = s.prompt;
  duration.value = s.duration_s;
  const showFrames = () => {
    const d = Number(duration.value) || 0;
    frames.textContent = [prev, fin].filter(Boolean).map((p) =>
      `${TIER_LABELS[p.tier]} : ${framesFor(d, p.fps)} images à ${p.fps} images/s`).join(" · ");
  };
  showFrames();
  const save = async () => {
    const body = {};
    if (prompt.value !== s.prompt) body.prompt = prompt.value;
    if (Number(duration.value) !== s.duration_s) body.duration_s = Number(duration.value);
    if (!Object.keys(body).length) return true;
    const r = await act(() => api("PATCH", `/segments/${s.id}`, body), { refresh: false });
    if (r) { s.prompt = r.prompt; s.duration_s = r.duration_s; }
    return Boolean(r);
  };
  prompt.addEventListener("change", save);
  duration.addEventListener("change", save);
  duration.addEventListener("input", showFrames);
  const promptBody = () => ({ target: "motion", text: prompt.value, segment_id: s.id,
    duration_s: Number(duration.value) || null });
  const checks = promptChecks(job, prompt, promptBody);
  duration.addEventListener("change", checks.run);
  const launch = async (tier) => {
    if (!(await save())) return;
    const body = { tier };
    if (seed.value !== "") body.seed = Number(seed.value);
    await act(() => api("POST", `/segments/${s.id}/takes`, body));
  };
  const flf = s.kf_out !== null && s.end_mode === "exact";
  const thumb = (k, label) => k
    ? h("figure", { class: "seg-kf" }, h("img", { src: kfUrl(k) }), h("figcaption", {}, label))
    : h("figure", { class: "seg-kf empty" }, h("div", {}, label === "début" ? "pas encore d'image" : "pas d'image de fin"),
      h("figcaption", {}, label));
  const multi = job.segments.length > 1;
  const kinName = kin ? kfName(kin) : "image de départ";
  const prevSeg = job.segments.find((x) => x.idx === s.idx - 1);
  const nextSeg = job.segments.find((x) => x.idx === s.idx + 1);
  return h("div", { class: "card segment" },
    h("div", { class: "seg-head" },
      h("h3", {}, multi ? `Plan ${s.idx + 1} sur ${job.segments.length}` : "Plan"),
      h("span", { class: "muted" },
        s.kf_out === null ? `part de l'${kinName}, le prompt mène la suite`
          : s.end_mode === "guide" ? `de l'${kinName} vers l'image ${s.kf_out} (fin indicative)`
            : `de l'${kinName} à l'image ${s.kf_out}`),
      h("span", { class: "spacer" }),
      prevSeg ? h("button", { type: "button", onclick: () => selectSeg(job, prevSeg.id) }, `◀ Plan ${prevSeg.idx + 1}`) : null,
      nextSeg ? h("button", { type: "button", onclick: () => selectSeg(job, nextSeg.id) }, `Plan ${nextSeg.idx + 1} ▶`) : null),
    h("div", { class: "seg-body" },
      h("div", { class: "seg-kfs" },
        thumb(kin, "début"),
        thumb(kout, s.kf_out === null ? "fin libre" : s.end_mode === "guide" ? "fin indicative" : "fin")),
      h("div", { class: "seg-form" },
        h("label", {}, "Que se passe-t-il ? (prompt)", prompt),
        job.style ? h("p", { class: "muted small" }, `Mis devant, style du projet : « ${job.style} »`) : null,
        checks.box,
        h("div", { class: "row" }, reviewButton(job, promptBody, save)),
        h("div", { class: "review-box" }),
        promptTips(`seg-${s.id}`, prompt, MOTION_TIPS, MOTION_EXAMPLES),
        gestureHelper(job, s),
        h("div", { class: "row" },
          h("label", { class: "inline" }, "Durée (s)", duration),
          h("span", { class: "spacer" }),
          h("button", { type: "button", dataset: { next: `preview-${s.id}` }, onclick: () => launch("preview"),
            title: prev ? prev.label : "" }, `Aperçu ${prev ? `(${fmtMinutes(estimate(prev))})` : ""}`),
          h("button", { type: "button", class: "primary", dataset: { next: `final-${s.id}` },
            onclick: () => launch("final"), title: fin ? fin.label : "" },
          `Rendu final ${fin ? `(${fmtMinutes(estimate(fin))})` : ""}`)),
        helpLine(`tiers-${s.id}`, "L'aperçu (rapide) juge le mouvement et le rythme ; le rendu final est la version " +
          "de qualité, à lancer quand l'aperçu te va.",
        "L'aperçu part toujours de l'image de départ seule" +
          (flf ? ", donc il ne montre pas la fin que le rendu final atteindra." : ".") +
          " Il ne garde pas un style peint ou dessiné : dès sa 4ᵉ image il redessine le visage dans un rendu lisse ;" +
          " le rendu final (14B) garde le style de l'image." +
          (s.kf_out !== null && s.end_mode === "guide"
            ? " Compare la dernière image de l'aperçu avec l'image de fin indicative." : "")),
        advanced(`seg-${s.id}`,
          h("div", { class: "row" },
            h("label", { class: "inline", title: "Vide : un tirage au hasard à chaque prise. Chaque prise garde le sien." },
              "Graine (seed)", seed),
            frames)))),
    h("h4", { class: "takes-title" }, "Prises", h("span", { class: "muted small" },
      " · garde pour le montage celle qui te plaît")),
    h("div", { class: "takes" }),
    fold(`older-${s.id}`, "adv older-takes", "Autres prises", h("div", { class: "takes" })),
  );
}

// --- fine gestures (P15) ------------------------------------------------------------------------
//
// A wink, one hand, an eyebrow: the 14B does not make them from text alone (P14, docs/benchmark.md,
// « Following a fine motion prompt »). What works: Klein makes the image of the gesture from the
// image before it, the gesture gets its own shot with an explicit prompt, and a gesture that comes
// back gets a second shot to a copy of that image. The helper does all of it in one request
// (POST /segments/{id}/gesture); the gesture's image holds a copy until Klein's replaces it.

const GESTURE_EXAMPLE = {
  instruction: "Make her wink: she closes only her left eye, her right eye stays wide open. She keeps smiling.",
  prompt: "She slowly closes her right eye in a wink; her left eye stays open and she keeps the same smile. Static camera.",
  back: "She opens her right eye again and keeps the same smile. Static camera.",
};

function gestureHelper(job, s) {
  const field = (placeholder) => h("textarea", { class: "prompt", rows: 2, placeholder: `ex. « ${placeholder} »` });
  const base = h("select", {},
    s.kf_out !== null ? h("option", { value: "end" }, `après l'image de fin du plan (${posName(s.kf_out)})`) : null,
    h("option", { value: "start" }, `au début du plan, depuis l'${posName(s.kf_in)}`));
  const instruction = field(GESTURE_EXAMPLE.instruction);
  const back = h("input", { type: "checkbox", checked: true });
  const prompt = field(GESTURE_EXAMPLE.prompt);
  const backPrompt = field(GESTURE_EXAMPLE.back);
  const backLabel = h("label", {}, "Prompt du plan retour (facultatif)", backPrompt);
  const result = h("p", { class: "muted small" });
  const update = () => {
    backLabel.hidden = !back.checked;
    const b = base.value === "end" ? s.kf_out : s.kf_in;
    const next = b + 1 < job.keyframes.length ? b + 1 : null;
    const chain = [posName(b), "image du geste", back.checked ? `copie de l'${posName(b)}` : null,
      next !== null ? `${posName(next)} actuelle` : null].filter(Boolean);
    result.textContent = `Résultat : ${chain.join(" → ")}, un plan entre chaque image. ` +
      (next === null ? "" : back.checked
        ? `Le plan actuel vers l'${posName(next)} repart de la copie, identique : ses prises restent valables.`
        : `Le plan actuel vers l'${posName(next)} repartira de l'image du geste : sa prise gardée ne l'est plus.`) +
      (base.value === "start" ? " Ce plan garde son prompt : retires-en le geste." : "");
  };
  base.addEventListener("change", update);
  back.addEventListener("change", update);
  update();
  const go = async () => {
    if (!instruction.value.trim()) { toast("Décris le geste à créer (la consigne de l'image)."); return; }
    const g = await act(() => api("POST", `/segments/${s.id}/gesture`, {
      instruction: instruction.value, base: base.value, back: back.checked,
      prompt: prompt.value, back_prompt: back.checked ? backPrompt.value : "" }));
    if (g) toast(`Image du geste en création (${posName(g.position)}, ~40 s) ; les plans sont découpés.`, false);
  };
  const est = estimate(preset("keyframe-klein") || {});
  return fold(`gesture-${s.id}`, "adv gesture", "Un geste fin (clin d'œil, une main, un sourcil) ? Découper le geste",
    helpLine(`gesture-${s.id}`, "Le geste a son image (créée par Klein) et son plan ; prévois plusieurs prises.",
      "Le rendu final ne fait pas un geste fin depuis le texte seul (il ferme les deux yeux pour un clin d'œil). " +
      "Ce qui l'approche : une image du geste créée depuis l'image qui le précède, un plan rien que pour lui, et, " +
      "pour un geste qui revient, un second plan vers une copie de cette image. Le plan finit toujours sur le geste ; " +
      "pendant le plan, le rendu final ferme souvent les deux yeux avant (1 prise réussie sur 3 mesurées)."),
    h("label", {}, "Le geste se fait", base),
    h("label", {}, "Le geste, pour créer son image (consigne d'édition, en anglais)", instruction),
    h("p", { class: "muted small" }, "Gauche / droite : Klein les lit souvent du côté du spectateur. Regarde l'image " +
      "créée et écris le prompt du plan comme le geste est dessus."),
    h("label", { class: "inline" }, back, "le geste revient (un plan retour vers une copie de l'image)"),
    h("label", {}, "Prompt du plan vers le geste (facultatif) : partie par partie, lentement, ce qui ne bouge pas",
      prompt),
    backLabel,
    result,
    h("div", { class: "row" }, h("button", { type: "button", class: "primary", onclick: go },
      `Créer l'image du geste et découper${est ? ` (~${fmtDuration(est)})` : ""}`)));
}

// Measured phase durations of the preset, model load included (the queue gives refined ones).
function estimate(p) {
  const e = p.estimates || {};
  return (e.loading_model || 0) + (e.encoding_text || 0) + (e.inference_step || 0) * (p.steps || 1) + (e.decoding || 0);
}

function updateSegment(job, s) {
  const card = document.querySelector(`.segments [data-key="${s.id}"]`);
  if (!card) return;
  const prompt = card.querySelector(".prompt");
  const duration = card.querySelector(".duration");
  if (document.activeElement !== prompt && prompt.value !== s.prompt) prompt.value = s.prompt;
  if (document.activeElement !== duration && Number(duration.value) !== s.duration_s) duration.value = s.duration_s;
  syncReview(card.querySelector(".review-box"), latestReview(job, (r) => r.segment_id === s.id), prompt);
  const takes = [...s.takes].reverse();
  const shownIds = shownTakeIds(s);
  const older = takes.filter((t) => !shownIds.has(t.id));
  const sig = (t) => {
    const q = queueItem("take", t.id);
    const live = isLive(t);
    return JSON.stringify([t.status, t.phase, t.step, t.progress, t.preview_url, t.error, t.output_url,
      s.retained_take_id === t.id, job.keyframes.length, live ? [q?.starts_in_s, Math.round(q?.remaining_s / 5)] : 0,
      live ? t.log_tail.slice(-1) : 0]);
  };
  const box = card.querySelector(":scope > .takes");
  syncList(box, takes.filter((t) => shownIds.has(t.id)), (t) => t.id, sig, (t) => renderTake(job, s, t));
  if (!takes.length) {
    box.replaceChildren(h("p", { class: "muted small" }, "Pas encore de prise : lance un aperçu."));
  }
  const olderFold = card.querySelector(".older-takes");
  syncList(olderFold.querySelector(".takes"), older, (t) => t.id, sig, (t) => renderTake(job, s, t));
  olderFold.hidden = !older.length;
  olderFold.querySelector("summary").textContent = `${older.length} autre${older.length > 1 ? "s" : ""} ` +
    `prise${older.length > 1 ? "s" : ""}`;
}

// --- takes --------------------------------------------------------------------------------------

function renderTake(job, s, t) {
  const q = queueItem("take", t.id);
  const retained = s.retained_take_id === t.id;
  const set = t.settings || {};
  const head = h("div", { class: "take-head" },
    h("strong", {}, `Prise #${t.id}`), " ", h("span", { class: `tier ${t.tier}` }, TIER_LABELS[t.tier] || t.tier),
    " ", badge(t.status),
    retained ? h("span", { class: "badge retained" }, "gardée pour le montage") : null,
    h("span", { class: "muted small", title: t.preset },
      ` graine ${t.seed} · ${set.video_length} images · ${set.resolution} · ` +
      `${set.image_prompt_type === "SE" ? "début + fin" : "début seul"}`),
    h("span", { class: "spacer" }),
    t.status === "done" ? h("label", { class: "inline retain", dataset: { next: `retain-${t.id}` },
      title: "La prise utilisée pour ce plan dans le montage (une par plan)" },
    h("input", { type: "radio", name: `retain-${s.id}`, checked: retained,
      onchange: () => act(() => api("PUT", `/segments/${s.id}/retained`, { take_id: t.id })) }),
    "Garder pour le montage") : null,
    retained ? h("button", { type: "button", class: "link",
      onclick: () => act(() => api("PUT", `/segments/${s.id}/retained`, { take_id: null })) }, "ne plus garder") : null,
    isLive(t)
      ? h("button", { type: "button", class: "danger", onclick: () => act(() => api("POST", `/takes/${t.id}/cancel`)) },
        t.cancel_requested ? "annulation…" : "Annuler")
      : [h("button", { type: "button", title: "Une nouvelle prise avec exactement les mêmes réglages, graine comprise",
        onclick: () => act(() => api("POST", `/takes/${t.id}/replay`)) }, "Refaire à l'identique"),
      h("button", { type: "button", class: "danger", title: "Supprimer cette prise et sa vidéo du disque",
        onclick: () => deleteThing(`/takes/${t.id}`, retained
          ? `La prise #${t.id} est gardée pour le montage. La supprimer quand même ? Le plan n'aura plus de prise ` +
            "gardée ; les vidéos déjà montées restent."
          : `Supprimer la prise #${t.id} ? Sa vidéo est effacée du disque.`, "Prise supprimée") }, "Supprimer")],
  );
  const body = [];
  if (t.status === "queued") {
    body.push(h("p", { class: "muted" }, q ? `En file : commence dans ${fmtDuration(q.starts_in_s)} environ, ` +
      `dure ${fmtDuration(q.remaining_s)}.` : "En file."));
  } else if (t.status === "running") {
    body.push(renderProgress(t, q));
  } else if (t.status === "done" && t.output_url) {
    const kout = s.kf_out === null ? null : job.keyframes.find((k) => k.position === s.kf_out);
    body.push(frameTool(job, t.output_url, t.fps, kout));
    const d = t.durations;
    if (d) {
      const ph = Object.entries(d.phases || {}).map(([k, v]) => `${PHASE_LABELS[k] || k} ${fmtDuration(v)}`).join(" · ");
      body.push(h("p", { class: "muted small" }, `Rendue en ${fmtDuration(d.total)} (${ph}), le ${fmtDate(t.finished_at)}.`));
    }
  } else if (t.status === "failed") {
    body.push(h("pre", { class: "error" }, t.error || "échec"));
  }
  if (t.log_tail.length && t.status !== "done") {
    body.push(fold(`log-${t.id}`, "", "Journal", h("pre", { class: "log" }, t.log_tail.join("\n"))));
  }
  return h("div", { class: `take ${t.status}${retained ? " is-retained" : ""}`, dataset: { next: `take-${t.id}` } },
    head, body);
}

function renderProgress(t, q) {
  const label = PHASE_LABELS[t.phase] || t.phase || "démarrage";
  const steps = t.phase === "inference" && t.total_steps ? ` · étape ${t.step || 0}/${t.total_steps}` : "";
  const pct = t.phase === "inference" && t.total_steps ? Math.round(100 * (t.step || 0) / t.total_steps)
    : (t.progress ?? 0);
  return h("div", { class: "progress-box" },
    t.preview_url ? h("img", { class: "live-preview", src: `${t.preview_url}?t=${Date.now()}`,
      title: "Dernier état de la génération" }) : null,
    h("div", { class: "progress-text" },
      h("div", {}, h("strong", {}, label), steps),
      h("div", { class: "bar" }, h("div", { style: `width:${Math.max(2, Math.min(100, pct))}%` })),
      h("div", { class: "muted" }, q ? `encore ${fmtDuration(q.remaining_s)} environ` : "",
        t.started_at ? ` · commencée il y a ${fmtDuration(Date.now() / 1000 - t.started_at)}` : ""),
      t.log_tail.length ? h("div", { class: "muted small mono" }, t.log_tail[t.log_tail.length - 1]) : null));
}

// --- video player with frame extraction ---------------------------------------------------------

function frameTool(job, url, fps, endKeyframe) {
  // metadata only: the hidden shots' and the folded takes' videos stay in the page
  const video = h("video", { src: url, controls: true, preload: "metadata", muted: true, playsInline: true });
  const counter = h("span", { class: "mono small" }, "image –");
  const target = h("select", {});
  const n = job.keyframes.length;
  target.append(h("option", { value: n }, n === 1 ? "comme image de fin" : `comme nouvelle image ${n}`));
  for (let i = n - 1; i >= 0; i--) {
    target.append(h("option", { value: i }, i === 0 ? "à la place de l'image de départ" : `à la place de l'image ${i}`));
  }
  const total = () => Math.max(1, Math.round(video.duration * fps));
  const current = () => Math.min(total() - 1, Math.floor(video.currentTime * fps + 1e-3));
  const show = () => { counter.textContent = isFinite(video.duration) ? `image ${current()} / ${total() - 1}` : "image –"; };
  video.addEventListener("timeupdate", show);
  video.addEventListener("seeked", show);
  video.addEventListener("loadedmetadata", show);
  const seek = (i) => new Promise((resolve) => {
    video.pause();
    i = Math.max(0, Math.min(total() - 1, i));
    const done = () => {
      // seeked can fire before the frame is painted: wait for it when the browser can tell us,
      // but not forever (no new frame is presented when the seek lands on the frame shown)
      let settled = false;
      const finish = () => { if (!settled) { settled = true; resolve(i); } };
      if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(finish);
      setTimeout(finish, 200);
    };
    video.addEventListener("seeked", done, { once: true });
    video.currentTime = (i + 0.5) / fps;
  });
  const grab = async () => {
    video.pause();
    await seek(current());
    const c = h("canvas", { width: video.videoWidth, height: video.videoHeight });
    c.getContext("2d").drawImage(video, 0, 0);
    return new Promise((resolve) => c.toBlob(resolve, "image/png"));
  };
  const send = async () => {
    const pos = Number(target.value);
    const i = current();
    const name = pos === 0 ? "l'image de départ" : `l'image ${pos}`;
    if (pos < n && !confirm(`Remplacer ${name} par l'image ${i} de cette vidéo ? Les prises déjà faites gardent leur copie.`)) return;
    const blob = await grab();
    await uploadKeyframe(job, pos, blob, `frame-${i}.png`);
    toast(`L'image ${i} est maintenant ${name}.`, false);
  };
  const save = async () => {
    const blob = await grab();
    const a = h("a", { href: URL.createObjectURL(blob), download: `${url.split("/").pop().replace(/\.\w+$/, "")}-frame-${current()}.png` });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  const tool = h("div", { class: "frame-tool" },
    h("div", { class: "frame-videos" }, video,
      endKeyframe ? h("figure", { class: "end-ref" }, h("img", { src: kfUrl(endKeyframe) }),
        h("figcaption", {}, `image de fin (${endKeyframe.position}), pour comparer`)) : null),
    h("div", { class: "row frame-controls" },
      h("button", { type: "button", title: "Première image", onclick: () => seek(0) }, "⏮"),
      h("button", { type: "button", title: "Image précédente (←)", onclick: () => seek(current() - 1) }, "◀"),
      counter,
      h("button", { type: "button", title: "Image suivante (→)", onclick: () => seek(current() + 1) }, "▶"),
      h("button", { type: "button", title: "Dernière image", onclick: () => seek(total() - 1) }, "⏭"),
      h("span", { class: "spacer" }),
      h("span", { class: "small", title: "Par exemple la dernière image d'un aperçu comme image de fin : le rendu " +
        "final arrivera dessus." }, "Utiliser cette image"), target,
      h("button", { type: "button", onclick: send }, "Envoyer"),
      h("button", { type: "button", onclick: save, title: "Télécharger cette image en PNG" }, "PNG")),
  );
  video.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") { ev.preventDefault(); seek(current() - 1); }
    if (ev.key === "ArrowRight") { ev.preventDefault(); seek(current() + 1); }
  });
  return tool;
}

// --- assembly -----------------------------------------------------------------------------------

function renderAssembly(job) {
  const missing = job.segments.filter((s) => !s.retained_take_id).map((s) => s.idx + 1);
  const multi = job.segments.length > 1;
  const cm = h("input", { type: "checkbox", checked: true });
  const fps = h("select", {},
    h("option", { value: "native" }, "normale"),
    h("option", { value: "x2" }, "x2, plus fluide (interpolation RIFE)"));
  const form = h("div", { class: "card" },
    h("div", { class: "row" },
      h("button", { type: "button", class: "primary", dataset: { next: "assemble" },
        disabled: !job.segments.length || missing.length > 0,
        onclick: () => act(() => api("POST", `/jobs/${job.id}/assemblies`, { color_match: cm.checked, fps_mode: fps.value })) },
      "Monter la vidéo"),
      missing.length
        ? h("span", { class: "muted" }, multi
          ? `D'abord, garde une prise pour le plan ${missing.join(", ")} (« Garder pour le montage »).`
          : "D'abord, garde une prise (« Garder pour le montage »).")
        : h("span", { class: "muted" }, `Prises gardées : ${job.segments.map((s) => "#" + s.retained_take_id).join(", ")}.`)),
    advanced(`asm-${job.id}`,
      h("div", { class: "row" },
        h("label", { class: "inline", title: "Ajuste les couleurs de chaque plan sur le premier, contre la dérive entre plans" },
          cm, "raccord des couleurs entre plans"),
        h("label", { class: "inline" }, "Fluidité", fps))));
  const list = [...job.assemblies].reverse().map((a) => {
    const q = queueItem("assembly", a.id);
    const fpsOf = a.fps_mode === "x2" ? 2 : 1;
    const first = job.segments[0]?.takes.find((t) => t.id === a.take_ids[0]);
    return h("div", { class: `take ${a.status}`, dataset: { next: `asm-${a.id}` } },
      h("div", { class: "take-head" },
        h("strong", {}, `Montage ${a.id}`), " ", badge(a.status),
        h("span", { class: "muted small" },
          ` prises ${a.take_ids.map((i) => "#" + i).join(", ")} · ${a.fps_mode === "x2" ? "fluidité x2" : "fluidité normale"}` +
          ` · raccord des couleurs ${a.color_match ? "oui" : "non"}`),
        h("span", { class: "spacer" }),
        isLive(a)
          ? h("button", { type: "button", class: "danger",
            onclick: () => act(() => api("POST", `/assemblies/${a.id}/cancel`)) }, "Annuler")
          : h("button", { type: "button", class: "danger", title: "Supprimer ce montage et sa vidéo du disque",
            onclick: () => deleteThing(`/assemblies/${a.id}`, `Supprimer le montage ${a.id} ? Sa vidéo est effacée ` +
              "du disque (les prises restent : tu peux remonter).", "Montage supprimé") }, "Supprimer")),
      a.status === "running" ? h("p", {}, a.phase || "démarrage", q ? ` · encore ${fmtDuration(q.remaining_s)} environ` : "") : null,
      a.status === "queued" && q ? h("p", { class: "muted" }, `Commence dans ${fmtDuration(q.starts_in_s)} environ.`) : null,
      a.status === "failed" ? h("pre", { class: "error" }, a.error) : null,
      a.status === "done" && a.url
        ? [frameTool(job, a.url, (first?.fps || 16) * fpsOf, null),
          h("p", {}, h("a", { class: "button primary", href: a.url, dataset: { next: `download-${a.id}` },
            download: `zoetrope-projet${job.id}-montage${a.id}.mp4` }, "Télécharger la vidéo"))]
        : null);
  });
  return [form, list];
}

// --- start --------------------------------------------------------------------------------------

(async () => {
  state.route = parseRoute();
  try {
    state.presets = await api("GET", "/presets");
  } catch (e) {
    renderTopbar(e);
  }
  refreshNow();
})();
