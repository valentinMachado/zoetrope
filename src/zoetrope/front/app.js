// Zoetrope front: one page, no build step. It polls the API (no WebSocket) and only rebuilds the
// parts of the page whose data changed, so a playing video or a prompt being typed is never reset.
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
    toast(e.message);
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

function fmtDate(t) {
  return new Date(t * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

function framesFor(durationS, fps) { // same rule as presets.frames_for: Wan models take 4n+1 frames
  const n = Math.max(1, Math.round(durationS * fps) - 1);
  return 4 * Math.max(1, Math.round(n / 4)) + 1;
}

function preset(name) { return state.presets.find((p) => p.name === name); }

function kfUrl(k) { return k ? `${k.url}?v=${k.version}` : null; }

function badge(status) { return h("span", { class: `badge s-${status}` }, status); }

const PHASE_LABELS = {
  starting: "starting", loading_model: "loading the model", encoding_text: "encoding the prompt",
  inference: "denoising", decoding: "decoding", saving: "saving",
};

// Keyed list: rebuild an item only when its signature changes, keep the others (and their videos).
function syncList(container, items, keyOf, sigOf, render) {
  const old = new Map();
  for (const el of [...container.children]) {
    if (el.dataset.key) old.set(el.dataset.key, el);
    else el.remove(); // a placeholder such as "No take yet."
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
      if (el) el.replaceWith(fresh);
      el = fresh;
    }
    old.delete(key);
    const want = prev ? prev.nextSibling : container.firstChild;
    if (want !== el) container.insertBefore(el, want);
    prev = el;
  }
  for (const el of old.values()) el.remove();
}

// --- routing and polling ------------------------------------------------------------------------

function parseRoute() {
  const m = location.hash.match(/^#\/job\/(\d+)/);
  return m ? { name: "job", id: Number(m[1]) } : { name: "jobs" };
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
      h("p", {}, h("a", { href: "#/" }, "← Jobs")), h("p", { class: "error" }, e.message));
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

function renderTopbar(error) {
  const pill = document.getElementById("worker-pill");
  const banner = document.getElementById("banner");
  banner.className = "";
  if (error) {
    pill.className = "pill bad";
    pill.textContent = "API unreachable";
    banner.hidden = false;
    banner.textContent = `The API does not answer (${error.message}). Start it with scripts\\start-api.ps1.`;
    return;
  }
  const w = state.queue.worker;
  const q = state.queue;
  if (w.alive) {
    const cur = w.current_kind ? `${w.current_kind} ${w.current_id}` : "idle";
    pill.className = "pill ok";
    pill.textContent = `worker: ${cur}`;
    pill.title = `pid ${w.pid} · model ${w.loaded_model || "none"}${w.loaded_model ? ` (profile ${w.loaded_profile})` : ""}` +
      ` · heartbeat ${w.heartbeat_age_s} s ago`;
  } else {
    pill.className = "pill bad";
    pill.textContent = "worker absent";
    pill.title = w.heartbeat_age_s ? `last heartbeat ${fmtDuration(w.heartbeat_age_s)} ago` : "not running";
  }
  banner.hidden = w.alive || q.items.length === 0;
  banner.textContent = "Tasks are waiting but the worker is not running: start it with scripts\\start-worker.ps1.";

  const toggle = document.getElementById("queue-toggle");
  toggle.textContent = q.items.length ? `Queue: ${q.items.length} · ${fmtDuration(q.total_s)}` : "Queue: empty";
  renderQueuePanel();
}

document.getElementById("queue-toggle").addEventListener("click", () => {
  const p = document.getElementById("queue-panel");
  p.hidden = !p.hidden;
});

function renderQueuePanel() {
  const panel = document.getElementById("queue-panel");
  const w = state.queue.worker;
  const rows = state.queue.items.map((i) => {
    const what = i.kind === "take"
      ? `take ${i.id} · segment ${i.segment_idx + 1} · ${i.tier} (${i.preset})`
      : `assembly ${i.id}`;
    let where = "";
    if (i.status === "running") {
      where = PHASE_LABELS[i.phase] || i.phase || "starting";
      if (i.phase === "inference" && i.total_steps) where += ` ${i.step || 0}/${i.total_steps}`;
      where += ` · ${fmtDuration(i.remaining_s)} left`;
    } else {
      where = `starts in ${fmtDuration(i.starts_in_s)} · ~${fmtDuration(i.remaining_s)}`;
    }
    return h("li", { class: i.status },
      h("a", { href: `#/job/${i.job_id}` }, `job ${i.job_id}`), " · ", what, h("div", { class: "muted" }, where));
  });
  panel.replaceChildren(
    h("h3", {}, "Queue"),
    h("p", { class: "muted" },
      w.alive
        ? `Worker pid ${w.pid}, model loaded: ${w.loaded_model || "none"}` +
          (w.loaded_model ? ` (profile ${w.loaded_profile})` : "")
        : "Worker not running."),
    rows.length ? h("ol", {}, rows) : h("p", {}, "Nothing queued."),
    rows.length ? h("p", { class: "muted" }, `Everything done in about ${fmtDuration(state.queue.total_s)}.`) : null,
    h("p", { class: "muted small" },
      "Order: previews first, then assemblies, then final renders grouped by model (a model change reloads it)."),
  );
}

// --- jobs list ----------------------------------------------------------------------------------

function renderJobs(jobs) {
  const app = document.getElementById("app");
  if (!app.querySelector(".jobs-view")) {
    const finals = state.presets.filter((p) => p.tier === "final");
    const form = h("form", { class: "card new-job", onsubmit: createJob },
      h("h2", {}, "New job"),
      h("label", {}, "Title", h("input", { name: "title", placeholder: "e.g. watercolour harbour, boats rocking" })),
      h("label", {}, "Final render",
        h("select", { name: "preset_final" },
          finals.map((p) => h("option", { value: p.name, selected: p.name === "final-14b" }, p.label)))),
      h("label", {}, "End of the last segment", endModeSelect("exact")),
      h("button", { type: "submit", class: "primary" }, "Create"),
    );
    app.replaceChildren(h("section", { class: "jobs-view" }, form, h("div", { class: "job-grid" })));
  }
  const grid = app.querySelector(".job-grid");
  syncList(grid, jobs, (j) => j.id, (j) => JSON.stringify(j), (j) =>
    h("a", { class: "card job-card", href: `#/job/${j.id}` },
      j.thumbnail_url ? h("img", { src: j.thumbnail_url, alt: "" }) : h("div", { class: "no-thumb" }, "no image"),
      h("div", { class: "job-card-body" },
        h("strong", {}, j.title || `Job ${j.id}`),
        h("div", {}, badge(j.status), " ", h("span", { class: "muted" }, fmtDate(j.created_at))),
      )));
  if (!jobs.length) grid.replaceChildren(h("p", { class: "muted" }, "No job yet."));
}

async function createJob(ev) {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const job = await act(() => api("POST", "/jobs", Object.fromEntries(f)), { refresh: false });
  if (job) location.hash = `#/job/${job.id}`;
}

function endModeSelect(value, onchange) {
  return h("select", { name: "end_mode", onchange },
    h("option", { value: "exact", selected: value === "exact" }, "exact: the video reaches the last keyframe"),
    h("option", { value: "guide", selected: value === "guide" },
      "guide: start image only, the last keyframe only judges the preview"));
}

// --- job view -----------------------------------------------------------------------------------

function renderJob() {
  const job = state.job;
  const app = document.getElementById("app");
  let view = app.querySelector(".job-view");
  if (!view || Number(view.dataset.job) !== job.id) {
    view = h("section", { class: "job-view", dataset: { job: job.id } },
      h("div", { class: "job-head" }),
      h("h2", {}, "Keyframes"),
      h("p", { class: "muted small" },
        "Keyframe 0 is the start image. Segment i goes from keyframe i to keyframe i+1; ",
        "a single image gives one segment from the start image alone."),
      h("div", { class: "keyframes" }),
      h("h2", {}, "Segments"),
      h("div", { class: "segments" }),
      h("h2", {}, "Assembly"),
      h("div", { class: "assembly" }),
    );
    app.replaceChildren(view);
  }
  syncOne(view.querySelector(".job-head"), JSON.stringify([job.title, job.preset_final, job.end_mode, job.status,
    state.presets.length]), renderJobHead);
  const kfs = view.querySelector(".keyframes");
  syncList(kfs, [...job.keyframes, { id: "add" }], (k) => k.id,
    (k) => k.id === "add" ? `add${job.keyframes.length}` : JSON.stringify([k, job.keyframes.length]),
    (k) => k.id === "add" ? renderAddKeyframe(job) : renderKeyframe(job, k));
  syncList(view.querySelector(".segments"), job.segments, (s) => s.id,
    (s) => JSON.stringify([s.id, s.kf_in, s.kf_out, s.end_mode, job.preset_final, kfVersions(job, s)]),
    (s) => renderSegment(job, s));
  for (const s of job.segments) updateSegment(job, s);
  syncOne(view.querySelector(".assembly"), JSON.stringify([job.segments.map((s) => s.retained_take_id),
    job.assemblies, job.assemblies.map((a) => {
      const q = queueItem("assembly", a.id);
      return q ? [q.status, Math.round(q.starts_in_s / 10)] : null;
    })]), renderAssembly);
}

function syncOne(container, sig, render) {
  if (container.dataset.sig === sig) return;
  container.dataset.sig = sig;
  container.replaceChildren(...[render(state.job)].flat(Infinity));
}

function kfVersions(job, s) {
  return [s.kf_in, s.kf_out].map((p) => job.keyframes.find((k) => k.position === p)?.version);
}

function renderJobHead(job) {
  const finals = state.presets.filter((p) => p.tier === "final");
  const patch = (field) => (ev) => act(() => api("PATCH", `/jobs/${job.id}`, { [field]: ev.target.value }));
  return [
    h("div", { class: "row" },
      h("a", { href: "#/" }, "← Jobs"),
      h("input", { class: "title-input", value: job.title, placeholder: `Job ${job.id}`, onchange: patch("title") }),
      badge(job.status)),
    h("div", { class: "row settings" },
      h("label", {}, "Final render",
        h("select", { onchange: patch("preset_final") },
          finals.map((p) => h("option", { value: p.name, selected: p.name === job.preset_final }, p.label)))),
      h("label", {}, "End of the last segment", endModeSelect(job.end_mode, patch("end_mode")))),
    h("p", { class: "muted small" },
      job.end_mode === "guide"
        ? "Guide: the final render of the last segment starts from its start image alone and the prompt leads the " +
          "end; its end keyframe only serves to judge the preview. Inner segments always reach their keyframe."
        : "Exact: every segment's final render reaches its end keyframe (first and last frame, 14B)."),
  ];
}

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
    title: "Gap to the start image, in grey levels (0-255), before → after the match. Colour: distance between " +
      "colour histograms, 0 when the colours are the same. Pixel: mean difference pixel by pixel, which also " +
      "counts pose and framing.",
  }, part("color", "colour"), " ", part("pixel", "pixel"));
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

function renderAddKeyframe(job) {
  const n = job.keyframes.length;
  const upload = (file) => uploadKeyframe(job, n, file, file.name);
  return dropZone(h("div", { class: "kf add" },
    h("div", { class: "kf-drop" },
      n === 0 ? "Drop the start image here" : `Drop keyframe ${n} here`,
      h("div", { class: "muted small" },
        n === 0 ? "photo, watercolour, drawing; a cut-out (alpha) is flattened on the background colour"
          : "or take one from a take's frames (below)"),
      filePicker(upload, "Choose a file", "primary"))), upload);
}

function renderKeyframe(job, k) {
  const isStart = k.position === 0;
  const isLast = k.position === job.keyframes.length - 1;
  const p = k.match_params || {};
  const bg = "#" + (p.background || [255, 255, 255]).map((c) => c.toString(16).padStart(2, "0")).join("");
  const replace = (file) => uploadKeyframe(job, k.position, file, file.name);
  const card = h("div", { class: "kf" + (isStart ? " start" : "") },
    h("div", { class: "kf-title" }, isStart ? "0 · start image" : `${k.position} · keyframe`,
      !isStart && k.match_applied ? h("span", { class: "badge s-done" }, "matched") : null),
    h("img", { src: kfUrl(k), alt: `keyframe ${k.position}`, onclick: () => openCompare(job, k) }),
    isStart ? startControls(k, bg) : matchControls(k, bg),
    devLine(k.deviation),
    h("div", { class: "row small-buttons" },
      h("button", { type: "button", onclick: () => openCompare(job, k) }, isStart ? "View" : "Before / after"),
      filePicker(replace, "Replace"),
      isLast
        ? h("button", { type: "button", class: "danger", onclick: () => {
          if (confirm(`Remove keyframe ${k.position}?`)) act(() => api("DELETE", `/keyframes/${k.id}`));
        } }, "Remove")
        : null),
  );
  return dropZone(card, replace);
}

function startControls(k, bg) {
  const color = h("input", { type: "color", value: bg });
  const fringes = h("input", { type: "checkbox", checked: k.match_params?.remove_fringes !== false });
  const apply = () => act(() => api("POST", `/keyframes/${k.id}/match`,
    { background: color.value, remove_fringes: fringes.checked }));
  return h("div", { class: "kf-controls" },
    h("label", { class: "inline", title: "Colour a cut-out (alpha) is flattened on. Every keyframe follows it." },
      "Background", color),
    h("button", { type: "button", onclick: () => apply() }, "Apply"),
    h("label", { class: "inline", title: "Choke the matte by 2 px to drop the old background's halo" },
      fringes, "remove fringes"),
  );
}

function matchControls(k, bg) {
  const p = k.match_params || {};
  const colorMatch = h("input", { type: "checkbox", checked: !k.match_applied || p.color_match !== false });
  const fringes = h("input", { type: "checkbox", checked: p.remove_fringes !== false });
  const run = (enabled) => act(() => api("POST", `/keyframes/${k.id}/match`,
    { enabled, color_match: colorMatch.checked, remove_fringes: fringes.checked, background: bg }));
  return h("div", { class: "kf-controls" },
    h("button", { type: "button", class: k.match_applied ? "" : "primary", onclick: () => run(true),
      title: "Same size and framing as the start image, same background, colours matched on it. " +
        "Fixes colour, format and background, not identity." },
    k.match_applied ? "Match again" : "Match to start"),
    k.match_applied ? h("button", { type: "button", onclick: () => run(false) }, "Undo") : null,
    h("label", { class: "inline" }, colorMatch, "colours"),
    h("label", { class: "inline" }, fringes, "fringes"),
  );
}

function openCompare(job, k) {
  const start = job.keyframes.find((x) => x.position === 0);
  const fig = (src, caption, cls = "") => h("figure", {}, h("img", { src, class: cls }), h("figcaption", {}, caption));
  const body = k.position === 0
    ? [fig(k.orig_url, "Uploaded", "checker"), fig(kfUrl(k), "Prepared (sent to the model)")]
    : [fig(kfUrl(start), "Start image (reference)"), fig(k.orig_url, "Before: uploaded", "checker"),
      fig(kfUrl(k), k.match_applied ? "After: matched" : "Prepared (not matched)")];
  openModal(h("div", {},
    h("h3", {}, k.position === 0 ? "Start image" : `Keyframe ${k.position}: before / after the match`),
    h("div", { class: "compare" }, body),
    devLine(k.deviation),
    k.position === 0 ? null : h("p", { class: "muted small" },
      "The technical match fixes size, framing, background and colours. A keyframe taken from a preview keeps ",
      "its drift in identity (face, age): the 14B reproduces it at the end of the segment.")));
}

function openModal(content) {
  const m = document.getElementById("modal");
  m.replaceChildren(h("div", { class: "modal-box" },
    h("button", { type: "button", class: "close", onclick: closeModal }, "×"), content));
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
  const prompt = h("textarea", { class: "prompt", rows: 3, placeholder: "What moves, how; camera; style…" });
  const duration = h("input", { type: "number", class: "duration", min: 0.25, max: 10, step: 0.25 });
  const frames = h("span", { class: "muted small" });
  const seed = h("input", { type: "number", class: "seed", placeholder: "random", min: 0 });
  prompt.value = s.prompt;
  duration.value = s.duration_s;
  const showFrames = () => {
    const d = Number(duration.value) || 0;
    frames.textContent = [prev, fin].filter(Boolean).map((p) =>
      `${p.tier} ${framesFor(d, p.fps)} frames at ${p.fps} fps`).join(" · ");
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
  const launch = async (tier) => {
    if (!(await save())) return;
    const body = { tier };
    if (seed.value !== "") body.seed = Number(seed.value);
    await act(() => api("POST", `/segments/${s.id}/takes`, body));
  };
  const flf = s.kf_out !== null && s.end_mode === "exact";
  const thumb = (k, label) => k
    ? h("figure", { class: "seg-kf" }, h("img", { src: kfUrl(k) }), h("figcaption", {}, label))
    : h("figure", { class: "seg-kf empty" }, h("div", {}, "no end image"), h("figcaption", {}, label));
  return h("div", { class: "card segment" },
    h("div", { class: "seg-head" },
      h("h3", {}, `Segment ${s.idx + 1}`),
      h("span", { class: "muted" },
        s.kf_out === null ? "start image only"
          : `keyframe ${s.kf_in} → ${s.kf_out}` + (s.end_mode === "guide" ? " (guide: final starts from the start alone)" : ""))),
    h("div", { class: "seg-body" },
      h("div", { class: "seg-kfs" },
        thumb(kin, `start · ${s.kf_in}`),
        thumb(kout, s.kf_out === null ? "end" : s.end_mode === "guide" ? `guide · ${s.kf_out}` : `end · ${s.kf_out}`)),
      h("div", { class: "seg-form" },
        h("label", {}, "Prompt", prompt),
        h("div", { class: "row" },
          h("label", { class: "inline" }, "Duration (s)", duration), frames),
        h("div", { class: "row" },
          h("label", { class: "inline", title: "Empty: a new random seed per take. Every take stores its seed." },
            "Seed", seed),
          h("button", { type: "button", onclick: () => launch("preview"),
            title: prev ? `${prev.label}. ~${fmtDuration(estimate(prev))}` : "" }, "Preview"),
          h("button", { type: "button", class: "primary", onclick: () => launch("final"),
            title: fin ? `${fin.label}. ~${fmtDuration(estimate(fin))}` : "" }, "Final render")),
        h("p", { class: "muted small" },
          "The preview (FastWan 5B, a few minutes) always starts from the start image alone: it checks the " +
          "prompt and the motion" + (flf ? ", not the ending the 14B will reach." : "."),
          s.kf_out !== null && s.end_mode === "guide"
            ? " Compare its last frame with the guide keyframe." : ""))),
    h("div", { class: "takes" }),
  );
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
  const takes = [...s.takes].reverse();
  const box = card.querySelector(".takes");
  syncList(box, takes, (t) => t.id, (t) => {
    const q = queueItem("take", t.id);
    const live = t.status === "running" || t.status === "queued";
    return JSON.stringify([t.status, t.phase, t.step, t.progress, t.preview_url, t.error, t.output_url,
      s.retained_take_id === t.id, job.keyframes.length, live ? [q?.starts_in_s, Math.round(q?.remaining_s / 5)] : 0,
      live ? t.log_tail.slice(-1) : 0]);
  }, (t) => renderTake(job, s, t));
  if (!takes.length) box.replaceChildren(h("p", { class: "muted small" }, "No take yet."));
}

// --- takes --------------------------------------------------------------------------------------

function renderTake(job, s, t) {
  const q = queueItem("take", t.id);
  const retained = s.retained_take_id === t.id;
  const set = t.settings || {};
  const head = h("div", { class: "take-head" },
    h("strong", {}, `#${t.id}`), " ", h("span", { class: `tier ${t.tier}` }, t.tier), " ", badge(t.status),
    retained ? h("span", { class: "badge retained" }, "retained") : null,
    h("span", { class: "muted small" },
      ` ${t.preset} · seed ${t.seed} · ${set.video_length} frames · ${set.resolution} · ` +
      `${set.image_prompt_type === "SE" ? "start + end" : "start only"}`),
    h("span", { class: "spacer" }),
    t.status === "done" ? h("label", { class: "inline", title: "The take used for this segment in the assembly" },
      h("input", { type: "radio", name: `retain-${s.id}`, checked: retained,
        onchange: () => act(() => api("PUT", `/segments/${s.id}/retained`, { take_id: t.id })) }), "retain") : null,
    retained ? h("button", { type: "button", class: "link",
      onclick: () => act(() => api("PUT", `/segments/${s.id}/retained`, { take_id: null })) }, "unretain") : null,
    ["queued", "running"].includes(t.status)
      ? h("button", { type: "button", class: "danger", onclick: () => act(() => api("POST", `/takes/${t.id}/cancel`)) },
        t.cancel_requested ? "cancelling…" : "Cancel")
      : h("button", { type: "button", title: "A new take with exactly these settings, seed included",
        onclick: () => act(() => api("POST", `/takes/${t.id}/replay`)) }, "Replay"),
  );
  const body = [];
  if (t.status === "queued") {
    body.push(h("p", { class: "muted" }, q ? `Starts in about ${fmtDuration(q.starts_in_s)}, ` +
      `takes about ${fmtDuration(q.remaining_s)}.` : "Queued."));
  } else if (t.status === "running") {
    body.push(renderProgress(t, q));
  } else if (t.status === "done" && t.output_url) {
    const kout = s.kf_out === null ? null : job.keyframes.find((k) => k.position === s.kf_out);
    body.push(frameTool(job, t.output_url, t.fps, kout));
    const d = t.durations;
    if (d) {
      const ph = Object.entries(d.phases || {}).map(([k, v]) => `${PHASE_LABELS[k] || k} ${fmtDuration(v)}`).join(" · ");
      body.push(h("p", { class: "muted small" }, `Rendered in ${fmtDuration(d.total)} (${ph}), finished ${fmtDate(t.finished_at)}.`));
    }
  } else if (t.status === "failed") {
    body.push(h("pre", { class: "error" }, t.error || "failed"));
  }
  if (t.log_tail.length && t.status !== "done") {
    body.push(h("details", {}, h("summary", {}, "Log"), h("pre", { class: "log" }, t.log_tail.join("\n"))));
  }
  return h("div", { class: `take ${t.status}${retained ? " is-retained" : ""}` }, head, body);
}

function renderProgress(t, q) {
  const label = PHASE_LABELS[t.phase] || t.phase || "starting";
  const steps = t.phase === "inference" && t.total_steps ? ` · step ${t.step || 0}/${t.total_steps}` : "";
  const pct = t.phase === "inference" && t.total_steps ? Math.round(100 * (t.step || 0) / t.total_steps)
    : (t.progress ?? 0);
  return h("div", { class: "progress-box" },
    t.preview_url ? h("img", { class: "live-preview", src: `${t.preview_url}?t=${Date.now()}`,
      title: "Latest denoising preview from Wan2GP" }) : null,
    h("div", { class: "progress-text" },
      h("div", {}, h("strong", {}, label), steps),
      h("div", { class: "bar" }, h("div", { style: `width:${Math.max(2, Math.min(100, pct))}%` })),
      h("div", { class: "muted" }, q ? `about ${fmtDuration(q.remaining_s)} left` : "",
        t.started_at ? ` · running for ${fmtDuration(Date.now() / 1000 - t.started_at)}` : ""),
      t.log_tail.length ? h("div", { class: "muted small mono" }, t.log_tail[t.log_tail.length - 1]) : null));
}

// --- video player with frame extraction ---------------------------------------------------------

function frameTool(job, url, fps, endKeyframe) {
  const video = h("video", { src: url, controls: true, preload: "auto", muted: true, playsInline: true });
  const counter = h("span", { class: "mono small" }, "frame –");
  const target = h("select", {});
  const n = job.keyframes.length;
  target.append(h("option", { value: n }, `as new keyframe ${n}`));
  for (let i = n - 1; i >= 0; i--) target.append(h("option", { value: i }, `replacing keyframe ${i}${i === 0 ? " (start)" : ""}`));
  const total = () => Math.max(1, Math.round(video.duration * fps));
  const current = () => Math.min(total() - 1, Math.floor(video.currentTime * fps + 1e-3));
  const show = () => { counter.textContent = isFinite(video.duration) ? `frame ${current()} / ${total() - 1}` : "frame –"; };
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
    if (pos < n && !confirm(`Replace keyframe ${pos} with frame ${i}? Takes already made keep their own copy.`)) return;
    const blob = await grab();
    await uploadKeyframe(job, pos, blob, `frame-${i}.png`);
    toast(`Frame ${i} is now keyframe ${pos}.`, false);
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
        h("figcaption", {}, `keyframe ${endKeyframe.position} (end)`)) : null),
    h("div", { class: "row frame-controls" },
      h("button", { type: "button", title: "First frame", onclick: () => seek(0) }, "⏮"),
      h("button", { type: "button", title: "Previous frame (←)", onclick: () => seek(current() - 1) }, "◀"),
      counter,
      h("button", { type: "button", title: "Next frame (→)", onclick: () => seek(current() + 1) }, "▶"),
      h("button", { type: "button", title: "Last frame", onclick: () => seek(total() - 1) }, "⏭"),
      h("span", { class: "spacer" }),
      h("span", { class: "small" }, "Use this frame"), target,
      h("button", { type: "button", onclick: send }, "Send"),
      h("button", { type: "button", onclick: save, title: "Download this frame as PNG" }, "PNG")),
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
  const cm = h("input", { type: "checkbox", checked: true });
  const fps = h("select", {},
    h("option", { value: "native" }, "native fps"),
    h("option", { value: "x2" }, "x2 (RIFE interpolation)"));
  const form = h("div", { class: "card row" },
    h("label", { class: "inline", title: "Match each segment's colours on the first one, against drift between segments" },
      cm, "colour match"),
    h("label", { class: "inline" }, "Frame rate", fps),
    h("button", { type: "button", class: "primary", disabled: !job.segments.length || missing.length > 0,
      onclick: () => act(() => api("POST", `/jobs/${job.id}/assemblies`, { color_match: cm.checked, fps_mode: fps.value })) },
    "Assemble"),
    missing.length ? h("span", { class: "muted" }, `Retain a take for segment ${missing.join(", ")} first.`)
      : h("span", { class: "muted" }, `Takes ${job.segments.map((s) => "#" + s.retained_take_id).join(", ")}.`));
  const list = [...job.assemblies].reverse().map((a) => {
    const q = queueItem("assembly", a.id);
    const fpsOf = a.fps_mode === "x2" ? 2 : 1;
    const first = job.segments[0]?.takes.find((t) => t.id === a.take_ids[0]);
    return h("div", { class: `take ${a.status}` },
      h("div", { class: "take-head" },
        h("strong", {}, `assembly ${a.id}`), " ", badge(a.status),
        h("span", { class: "muted small" },
          ` takes ${a.take_ids.map((i) => "#" + i).join(", ")} · ${a.fps_mode} · colour match ${a.color_match ? "on" : "off"}`),
        h("span", { class: "spacer" }),
        ["queued", "running"].includes(a.status)
          ? h("button", { type: "button", class: "danger",
            onclick: () => act(() => api("POST", `/assemblies/${a.id}/cancel`)) }, "Cancel") : null),
      a.status === "running" ? h("p", {}, a.phase || "starting", q ? ` · about ${fmtDuration(q.remaining_s)} left` : "") : null,
      a.status === "queued" && q ? h("p", { class: "muted" }, `Starts in about ${fmtDuration(q.starts_in_s)}.`) : null,
      a.status === "failed" ? h("pre", { class: "error" }, a.error) : null,
      a.status === "done" && a.url
        ? [frameTool(job, a.url, (first?.fps || 16) * fpsOf, null),
          h("p", { class: "small" }, h("a", { href: a.url, download: `zoetrope-job${job.id}-assembly${a.id}.mp4` }, "Download"))]
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
