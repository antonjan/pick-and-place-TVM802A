"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const OUTPUT_LABELS = {
  pump: "Pump", vacuum1: "Vacuum N1", vacuum2: "Vacuum N2", blow1: "Blow N1",
  blow2: "Blow N2", prick: "Prick needle", leds: "Up-cam LEDs", buzzer: "Buzzer",
};

// Setup-tab actions. Each one is a single POST; the result is logged and the
// stored calibration is re-read afterwards.
const TASKS = {
  "down-scale": { label: "Down camera scale", path: "/api/calibrate/down/scale",
                  body: () => ({ step_mm: numberInput("#down-step-mm") }) },
  "mark-centre": { label: "Auto-centre on mark", path: "/api/calibrate/down/centre",
                   body: () => ({ polarity: "auto" }) },
  "mark-set": { label: "Camera on mark", path: "/api/calibrate/nozzle-offset/camera" },
  "offset-1": { label: "Nozzle 1 offset", path: "/api/calibrate/nozzle-offset/1" },
  "offset-2": { label: "Nozzle 2 offset", path: "/api/calibrate/nozzle-offset/2" },
  "to-cam-1": { label: "Nozzle 1 to camera point", path: "/api/tools/nozzle-to-camera/1" },
  "to-cam-2": { label: "Nozzle 2 to camera point", path: "/api/tools/nozzle-to-camera/2" },
  "from-cam-1": { label: "Camera to nozzle 1", path: "/api/tools/camera-to-nozzle/1" },
  "from-cam-2": { label: "Camera to nozzle 2", path: "/api/tools/camera-to-nozzle/2" },
  "up-scale": { label: "Up camera scale", path: "/api/calibrate/up/scale",
                body: () => ({ step_mm: numberInput("#up-step-mm") }) },
  "up-centre": { label: "Auto-centre tip", path: "/api/calibrate/up/centre",
                 body: () => ({ polarity: "bright" }) },
  "up-pos-1": { label: "Nozzle 1 up position", path: "/api/calibrate/up-position/1" },
  "up-pos-2": { label: "Nozzle 2 up position", path: "/api/calibrate/up-position/2" },
  "reload-config": { label: "Reload config", path: "/api/config/reload" },
  "prick-on": { label: "Prick needle down", path: "/api/output", body: () => ({ name: "prick", on: true }) },
  "prick-off": { label: "Prick needle up", path: "/api/output", body: () => ({ name: "prick", on: false }) },
  "prick-offset": { label: "Prick needle offset", path: "/api/calibrate/prick-offset" },
};

// Feeder-tab actions. needs: a feeder must be selected; drag: drag feeders only;
// nozzle: uses the selected nozzle. Every action needs the machine homed and idle.
const FEEDER_ACTIONS = {
  "teach-pick": { label: "Teach pick", needs: true,
                  run: (id) => api(fpath(id, "/teach/pick")) },
  "teach-prick": { label: "Teach prick", needs: true, drag: true,
                   run: (id) => api(fpath(id, "/teach/prick")) },
  "goto": { label: "Go to pick", needs: true,
            run: (id) => api(fpath(id, "/goto")) },
  "feed": { label: "Feed", needs: true, drag: true,
            run: (id) => api(fpath(id, "/feed")) },
  "pick": { label: "Pick", needs: true, nozzle: true,
            run: (id) => api(fpath(id, "/pick"), { nozzle: feederNozzle }) },
  "pick-place": { label: "Pick and place", needs: true, nozzle: true,
                  run: (id) => api(fpath(id, "/pick-and-place"), { nozzle: feederNozzle, rotation: 0, depth: null }) },
  "place": { label: "Place at crosshair", nozzle: true,
             run: () => api("/api/head/place", { nozzle: feederNozzle, rotation: 0, depth: null }) },
  "discard": { label: "Discard", nozzle: true,
               run: () => api("/api/head/discard", { nozzle: feederNozzle }) },
};

let state = null;
let cameras = null; // Map name -> status from /api/cameras; null until the first answer
let calibration = null; // last /api/calibration answer
let taskRunning = false;
let feeders = [];        // last GET /api/feeders
let selectedId = null;   // feeder shown in the form; null = a new feeder
let feederNozzle = 1;
let feederBusy = false;  // a feeder save/delete or head action is running
let headState = null;    // last GET /api/head

// ------------------------------------------------------------------ helpers

function log(msg, isErr = false) {
  const stamp = `${new Date().toLocaleTimeString()}  ${msg}`;
  for (const list of $$("[data-log]")) {
    const li = document.createElement("li");
    li.textContent = stamp;
    if (isErr) li.className = "err";
    list.prepend(li);
    while (list.children.length > 200) list.lastChild.remove();
  }
}

function showError(msg) {
  $("#error").hidden = !msg;
  $("#error").textContent = msg || "";
}

async function api(path, body, method = "POST") {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (method !== "DELETE") opts.body = JSON.stringify(body ?? {});
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    log(`${path}: ${msg}`, true);
    showError(msg);
    throw new Error(msg);
  }
  showError(state && state.error ? state.error : "");
  return data;
}

function selectedStep(groupId) {
  return parseFloat($(`#${groupId} button.sel`).dataset.step);
}

function numberInput(sel, fallback = 1.0) {
  const v = parseFloat($(sel).value);
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

const fmt = (v, d = 3) => Number(v).toFixed(d);
const fmtPair = (p) => `(${fmt(p[0])}, ${fmt(p[1])}) mm`;

// ------------------------------------------------------------------ tabs

function showTab(name) {
  for (const b of $$("nav button.tab[data-tab]")) b.classList.toggle("active", b.dataset.tab === name);
  for (const page of $$("main[data-tab]")) page.hidden = page.dataset.tab !== name;
  if (name === "setup") refreshCalibration();
  if (name === "feeders") {
    refreshFeeders();
    loadHead();
  }
  syncStreams();
}

// ------------------------------------------------------------------ rendering

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = `pill ${cls}`;
}

function render(s) {
  state = s;
  setPill($("#pill-conn"), s.connected ? "connected" : "offline", s.connected ? "ok" : "bad");
  setPill($("#pill-homed"), s.homed ? "homed" : "not homed", s.homed ? "ok" : "warn");
  setPill($("#pill-busy"), s.busy || "idle", s.busy ? "busy" : "idle");
  setPill($("#pill-speed"), `speed ${s.speed_mode ?? "?"}`, "idle");

  const p = s.position;
  $("#pos-x").textContent = p.x.toFixed(3);
  $("#pos-y").textContent = p.y.toFixed(3);
  $("#pos-z").textContent = p.z.toFixed(3);
  $("#pos-a1").textContent = p.a1.toFixed(2);
  $("#pos-a2").textContent = p.a2.toFixed(2);
  $("#z-hint").textContent = p.z > 0.05 ? "N1↓" : p.z < -0.05 ? "N2↓" : "up";

  for (const k of ["left", "right", "top", "bottom"]) {
    $(`#lim-${k}`).classList.toggle("on", !!s.limits[k]);
  }
  $("#vac-1").classList.toggle("on", !!s.vacuum["1"]);
  $("#vac-2").classList.toggle("on", !!s.vacuum["2"]);

  for (const btn of $$("[data-out]")) {
    btn.classList.toggle("on", !!s.outputs[btn.dataset.out]);
  }

  document.body.classList.toggle("estopped", s.estopped);
  $("#btn-reset").hidden = !s.estopped;
  const block = !s.connected || s.estopped || !!s.busy;
  for (const b of $$("[data-jog],[data-z],[data-rot],[data-zero],#btn-up,#btn-home,#goto button")) {
    b.disabled = block;
  }
  $("#goto button").disabled = block || !s.homed;
  updateFeederButtons();
  showError(s.error);
}

function renderCameras() {
  const empty = cameras !== null && cameras.size === 0;
  $("#cams-empty").hidden = !empty;
  $("#cam-grid").hidden = empty;
  for (const el of $$("[data-cam-card]")) {
    el.hidden = cameras !== null && !cameras.has(el.dataset.camCard);
  }
  for (const el of $$("[data-cam-status]")) {
    const c = cameras && cameras.get(el.dataset.camStatus);
    if (!c) {
      el.textContent = cameras === null ? "–" : "absent";
      el.className = "cam-status";
      continue;
    }
    const sharp = c.sharpness == null ? "" : ` · sharpness ${Math.round(c.sharpness)}`;
    el.textContent = c.connected ? `${c.fps.toFixed(1)} fps · ${c.width}×${c.height}${sharp}` : (c.error || "disconnected");
    el.className = `cam-status ${c.connected ? "ok" : "bad"}`;
  }
  syncStreams();
}

// Only fetch MJPEG streams for views that are on screen (and whose camera exists).
function syncStreams() {
  for (const img of $$("img[data-cam]")) {
    const name = img.dataset.cam;
    const visible = !img.closest("main").hidden && (cameras === null || cameras.has(name));
    if (visible && !img.getAttribute("src")) {
      img.setAttribute("src", `/cameras/${name}/stream.mjpg`);
    } else if (!visible && img.getAttribute("src")) {
      img.removeAttribute("src");
    }
  }
}

function describeCalibration(cal) {
  if (!cal) return "not calibrated";
  const date = cal.date ? cal.date.replace("T", " ").slice(0, 19) : "";
  const mm = cal.mm_per_px == null ? "?" : cal.mm_per_px.toFixed(4);
  return `${mm} mm/px · rms ${cal.rms_px.toFixed(2)} px · ${date}`;
}

function renderCalibration(c) {
  calibration = c;
  const cams = c.cameras || {};
  const none = "not recorded";
  $(`[data-cal="down"]`).textContent = describeCalibration(cams.down);
  $(`[data-cal="up"]`).textContent = describeCalibration(cams.up);
  $(`[data-cal="camera_mark"]`).textContent = c.camera_mark ? fmtPair(c.camera_mark) : none;
  $(`[data-cal="prick-offset"]`).textContent = c.prick_offset ? fmtPair(c.prick_offset) : none;
  for (const n of [1, 2]) {
    const off = c.nozzle_offset[n];
    const up = c.up_position[n];
    $(`[data-cal="offset-${n}"]`).textContent = off ? fmtPair(off) : none;
    $(`[data-cal="up-${n}"]`).textContent = up ? fmtPair(up) : none;
  }
}

async function refreshCalibration() {
  try {
    renderCalibration(await (await fetch("/api/calibration")).json());
  } catch (_) {
    /* server lost: keep what is shown */
  }
}

// ------------------------------------------------------------------ websocket

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onclose = () => {
    setPill($("#pill-conn"), "server lost", "bad");
    setTimeout(connect, 1000);
  };
}

async function pollCameras() {
  try {
    const list = await (await fetch("/api/cameras")).json();
    cameras = new Map(list.map((c) => [c.name, c]));
  } catch (_) {
    /* server lost: keep the last answer */
  }
  renderCameras();
}

// ------------------------------------------------------------------ camera views

async function viewClick(img, ev) {
  if (taskRunning) return;
  if (!img.naturalWidth) {
    log(`${img.dataset.cam} camera: no frame yet`, true);
    return;
  }
  // Full-resolution pixel coordinates of the click.
  const r = img.getBoundingClientRect();
  const u = (ev.clientX - r.left) * img.naturalWidth / r.width;
  const v = (ev.clientY - r.top) * img.naturalHeight / r.height;
  log(`${img.dataset.cam} click at pixel (${Math.round(u)}, ${Math.round(v)})`);
  await api(`/api/cameras/${img.dataset.cam}/click`, { u, v }).catch(() => {});
}

function initViews() {
  for (const img of $$("img[data-cam]")) {
    img.addEventListener("click", (ev) => viewClick(img, ev));
  }
}

// ------------------------------------------------------------------ setup tasks

function describeResult(res) {
  if (res.A) {
    const mm = res.mm_per_px.toFixed(4);
    return `${mm} mm/px, rms ${res.rms_px.toFixed(2)} px, angle ${res.angle_deg.toFixed(2)}°${res.mirrored ? ", mirrored" : ""}`;
  }
  if (res.camera_mark) return `camera mark at ${fmtPair(res.camera_mark)}`;
  if (res.offset) return `offset ${fmtPair(res.offset)}`;
  if (Array.isArray(res.position)) return `position ${fmtPair(res.position)}`;
  if (res.position) return `head at X ${fmt(res.position.x)} Y ${fmt(res.position.y)}`;
  return "done";
}

function setSetupBusy(busy) {
  taskRunning = busy;
  for (const b of $$("#tab-setup button")) b.disabled = busy;
}

async function runTask(name) {
  const task = TASKS[name];
  if (!task || taskRunning) return;
  setSetupBusy(true);
  log(`${task.label}…`);
  try {
    const res = await api(task.path, task.body ? task.body() : {});
    log(`${task.label}: ${describeResult(res)}`);
  } catch (_) {
    /* api() has already logged the error and shown the banner */
  } finally {
    setSetupBusy(false);
    await refreshCalibration();
  }
}

function initSetup() {
  for (const b of $$("[data-task]")) {
    b.addEventListener("click", () => runTask(b.dataset.task));
  }
}

// ------------------------------------------------------------------ controls

function initStepGroups() {
  for (const group of ["xy-steps", "z-steps", "a-steps"]) {
    for (const b of $$(`#${group} button`)) {
      b.addEventListener("click", () => {
        $$(`#${group} button`).forEach((x) => x.classList.remove("sel"));
        b.classList.add("sel");
      });
    }
  }
}

async function jog(axis, dir) {
  const step = axis === "z" ? selectedStep("z-steps") : selectedStep("xy-steps");
  const delta = dir * step;
  log(`jog ${axis} ${delta > 0 ? "+" : ""}${delta}`);
  await api("/api/jog", { axis, delta }).catch(() => {});
}

async function nozzleStep(nozzle, dir) {
  // dir +1 = down, -1 = up. Z sign: + lowers N1, - lowers N2.
  const step = selectedStep("z-steps");
  const sign = nozzle === 1 ? 1 : -1;
  const z = state ? state.position.z : 0;
  const depth = Math.max(0, sign * z + dir * step);
  log(`N${nozzle} depth ${depth.toFixed(2)}`);
  if (depth === 0) return api("/api/nozzle/up").catch(() => {});
  await api("/api/nozzle/down", { nozzle, depth }).catch(() => {});
}

async function rotateStep(nozzle, dir) {
  const step = selectedStep("a-steps");
  const cur = state ? state.position[`a${nozzle}`] : 0;
  log(`rotate N${nozzle} ${dir > 0 ? "+" : "-"}${step}°`);
  await api("/api/move", { [`a${nozzle}`]: cur + dir * step }).catch(() => {});
}

function toggleOutput(name) {
  const on = !(state && state.outputs[name]);
  log(`${OUTPUT_LABELS[name]} ${on ? "on" : "off"}`);
  api("/api/output", { name, on }).catch(() => {});
}

function initControls() {
  for (const b of $$("[data-jog]")) {
    b.addEventListener("click", () => jog(b.dataset.jog, +b.dataset.dir));
  }
  for (const b of $$("[data-z]")) {
    b.addEventListener("click", () => nozzleStep(+b.dataset.z, +b.dataset.dir));
  }
  for (const b of $$("[data-rot]")) {
    b.addEventListener("click", () => rotateStep(+b.dataset.rot, +b.dataset.dir));
  }
  for (const b of $$("[data-zero]")) {
    b.addEventListener("click", () => api("/api/rotate", { nozzle: +b.dataset.zero, angle: 0 }).catch(() => {}));
  }
  $("#btn-up").addEventListener("click", () => api("/api/nozzle/up").catch(() => {}));
  $("#btn-home").addEventListener("click", async () => {
    log("homing…");
    await api("/api/home").then(() => log("homed")).catch(() => {});
  });
  $("#btn-estop").addEventListener("click", estop);
  $("#btn-reset").addEventListener("click", () => api("/api/reset").then(() => log("E-stop reset")));
  $("#btn-leds").addEventListener("click", () => toggleOutput("leds"));

  $("#goto").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = {};
    for (const k of ["x", "y"]) {
      const v = String(f.get(k)).trim();
      if (v !== "") body[k] = parseFloat(v);
    }
    if (!Object.keys(body).length) return;
    log(`move to ${JSON.stringify(body)}`);
    await api("/api/move", body).catch(() => {});
  });

  const toggles = $("#toggles");
  for (const [name, label] of Object.entries(OUTPUT_LABELS)) {
    const b = document.createElement("button");
    b.dataset.out = name;
    b.textContent = label;
    b.addEventListener("click", () => toggleOutput(name));
    toggles.append(b);
  }

  for (const b of $$("nav button.tab[data-tab]")) {
    b.addEventListener("click", () => showTab(b.dataset.tab));
  }
}

function estop() {
  log("E-STOP", true);
  api("/api/estop").catch(() => {});
}

function initKeys() {
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") return estop();
    if (ev.target.closest("input")) return;
    if (!state || state.busy || ev.repeat) return;
    const map = {
      ArrowUp: () => jog("y", 1), ArrowDown: () => jog("y", -1),
      ArrowLeft: () => jog("x", -1), ArrowRight: () => jog("x", 1),
      PageDown: () => nozzleStep(1, 1), PageUp: () => nozzleStep(1, -1),
    };
    if (map[ev.key]) {
      ev.preventDefault();
      map[ev.key]();
    }
  });
}


// ------------------------------------------------------------------ feeders

const fpath = (id, rest = "") => `/api/feeders/${encodeURIComponent(id)}${rest}`;
const field = (name) => $("#feeder-form").elements.namedItem(name);
const round3 = (v) => (v == null ? "" : String(Math.round(v * 1000) / 1000));

function selectedFeeder() {
  return feeders.find((f) => f.id === selectedId) || null;
}

function fillForm(f) {
  const d = f || { id: "", name: "", kind: "strip", pick: [0, 0], direction: [1, 0], pitch: 4, count: 0,
                   capacity: null, rotation: 0, pick_depth: 3, prick: null, needs_feed: true,
                   enabled: true, value: "", package: "" };
  const set = (name, v) => { field(name).value = v ?? ""; };
  set("id", d.id);
  field("id").readOnly = selectedId !== null;
  set("name", d.name);
  set("kind", d.kind);
  set("pick_x", round3(d.pick[0]));
  set("pick_y", round3(d.pick[1]));
  set("dir_x", round3(d.direction[0]));
  set("dir_y", round3(d.direction[1]));
  set("pitch", round3(d.pitch));
  set("count", d.count);
  set("capacity", d.capacity);
  set("rotation", round3(d.rotation));
  set("pick_depth", round3(d.pick_depth));
  set("prick_x", d.prick ? round3(d.prick[0]) : "");
  set("prick_y", d.prick ? round3(d.prick[1]) : "");
  field("needs_feed").checked = !!d.needs_feed;
  field("enabled").checked = !!d.enabled;
  set("value", d.value);
  set("package", d.package);
  $("#feeder-form-title").textContent = f ? `Feeder ${f.id}` : "New feeder";
  $("#feeder-delete").hidden = !f;
  applyKindVisibility();
}

function applyKindVisibility() {
  const drag = field("kind").value === "drag";
  for (const el of $$("#feeder-form .drag-only")) el.hidden = !drag;
}

// Form values -> request body. Throws Error with a readable message.
function readFeederForm() {
  const text = (name) => field(name).value.trim();
  const num = (name, label) => {
    const v = parseFloat(text(name));
    if (!Number.isFinite(v)) throw new Error(`${label} must be a number`);
    return v;
  };
  const id = selectedId ?? text("id");
  if (!id) throw new Error("feeder id is required");
  const prick = text("prick_x") !== "" && text("prick_y") !== ""
    ? [num("prick_x", "prick X"), num("prick_y", "prick Y")] : null;
  const body = {
    name: text("name"),
    kind: text("kind"),
    pick: [num("pick_x", "pick X"), num("pick_y", "pick Y")],
    direction: [num("dir_x", "direction X"), num("dir_y", "direction Y")],
    pitch: num("pitch", "pitch"),
    count: Math.trunc(num("count", "count")),
    capacity: text("capacity") === "" ? null : Math.trunc(num("capacity", "capacity")),
    rotation: num("rotation", "part rotation"),
    pick_depth: num("pick_depth", "pick depth"),
    prick,
    needs_feed: field("needs_feed").checked,
    enabled: field("enabled").checked,
    value: text("value"),
    package: text("package"),
  };
  return { id, body };
}

function feederChips(f) {
  const chips = [];
  const add = (text, cls) => {
    const s = document.createElement("span");
    s.className = `chip ${cls}`;
    s.textContent = text;
    chips.push(s);
  };
  if (!f.enabled) add("disabled", "idle");
  if (f.kind === "drag" && f.needs_feed) add("needs feed", "warn");
  if (f.remaining === 0) add("empty", "bad");
  if (!chips.length) add("ready", "ok");
  return chips;
}

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = String(text);
  if (cls) td.className = cls;
  return td;
}

function renderFeederTable() {
  $("#feeder-rows").replaceChildren(...feeders.map((f) => {
    const tr = document.createElement("tr");
    tr.dataset.id = f.id;
    tr.classList.toggle("sel", f.id === selectedId);
    const count = f.remaining == null ? `${f.count}` : `${f.count} (${f.remaining} left)`;
    const loc = `(${f.pick_location[0].toFixed(2)}, ${f.pick_location[1].toFixed(2)})`;
    const status = document.createElement("td");
    status.append(...feederChips(f));
    tr.append(cell(f.id), cell(f.name || "–"), cell(f.kind), cell(f.pitch), cell(count),
              cell(loc, "mono"), status);
    tr.addEventListener("click", () => selectFeeder(f.id));
    return tr;
  }));
  $("#feeder-empty").hidden = feeders.length > 0;
}

function selectFeeder(id) {
  selectedId = id;
  fillForm(selectedFeeder());
  renderFeederTable();
  updateFeederButtons();
}

function newFeeder() {
  selectedId = null;
  fillForm(null);
  renderFeederTable();
  updateFeederButtons();
}

async function refreshFeeders() {
  try {
    feeders = await (await fetch("/api/feeders")).json();
  } catch (_) {
    return; /* server lost: keep the last list */
  }
  if (selectedId !== null && !selectedFeeder()) {
    selectedId = null;
    fillForm(null);
  }
  renderFeederTable();
  updateFeederButtons();
}

async function loadHead() {
  try {
    headState = await (await fetch("/api/head")).json();
  } catch (_) {
    return;
  }
  const h = headState.holding || {};
  $("#holding").textContent = [1, 2].map((n) => `N${n} ${h[n] ? h[n] : "empty"}`).join(" · ");
  updateFeederButtons();
}

function describeFeederResult(res) {
  if (res && res.pick_location) {
    const left = res.remaining == null ? "" : `, ${res.remaining} left`;
    return `${res.id} count ${res.count}${left}`;
  }
  return describeResult(res);
}

// Runs one feeder/head request (or a save/delete) with the UI locked, then refreshes.
async function feederAction(label, fn) {
  if (feederBusy) return;
  feederBusy = true;
  updateFeederButtons();
  log(`${label}…`);
  try {
    const res = await fn();
    log(`${label}: ${describeFeederResult(res)}`);
    if (res && res.id !== undefined && res.id === selectedId) fillForm(res);
  } catch (_) {
    /* api() has already logged the error and shown the banner */
  } finally {
    feederBusy = false;
    await Promise.all([refreshFeeders(), loadHead()]);
  }
}

async function saveFeeder() {
  let parsed;
  try {
    parsed = readFeederForm();
  } catch (err) {
    log(err.message, true);
    showError(err.message);
    return;
  }
  const { id, body } = parsed;
  await feederAction(`Save ${id}`, async () => {
    const saved = await api(fpath(id), body, "PUT");
    selectedId = saved.id;
    return saved;
  });
}

async function deleteFeeder() {
  const id = selectedId;
  if (id === null || !confirm(`Delete feeder ${id} from the feeder file?`)) return;
  await feederAction(`Delete ${id}`, async () => {
    await api(fpath(id), undefined, "DELETE");
    selectedId = null;
    fillForm(null);
    return { ok: true };
  });
}

function runFeederAction(name) {
  const act = FEEDER_ACTIONS[name];
  const id = selectedId;
  if (!act || feederBusy || (act.needs && id === null)) return;
  const label = [act.label, act.needs ? id : null, act.nozzle ? `N${feederNozzle}` : null]
    .filter(Boolean).join(" ");
  feederAction(label, () => act.run(id));
}

function updateFeederButtons() {
  const sel = selectedFeeder();
  const machineBusy = !state || !state.connected || state.estopped || !!state.busy;
  const headBusy = !!(headState && headState.busy);
  const idle = !feederBusy && !taskRunning && !machineBusy && !headBusy && !!state.homed;
  const drag = !!sel && sel.kind === "drag";
  for (const b of $$("[data-feeder-action]")) {
    const act = FEEDER_ACTIONS[b.dataset.feederAction];
    if (act.drag !== undefined) b.hidden = !drag;
    b.disabled = !idle || (act.needs && selectedId === null);
  }
  for (const b of $$("#nozzle-sel button")) b.disabled = feederBusy;
  $("#feeder-new").disabled = feederBusy;
  $("#feeder-delete").disabled = !idle || selectedId === null;
  $("#feeder-form button[type=submit]").disabled = !idle;
  let hint = "Select a feeder in the table, or press New feeder.";
  if (!state || !state.connected) hint = "Not connected to the machine.";
  else if (!state.homed) hint = "Home the machine first (Machine tab) before teaching or moving.";
  else if (selectedId === null) hint = "New feeder: fill in the form and press Save.";
  else if (sel) hint = `${sel.id} · ${sel.kind} · ${sel.name || "unnamed"}`;
  $("#feeder-selected").textContent = hint;
}

function initFeeders() {
  $("#feeder-new").addEventListener("click", newFeeder);
  $("#feeder-delete").addEventListener("click", deleteFeeder);
  $("#feeder-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveFeeder();
  });
  field("kind").addEventListener("change", applyKindVisibility);
  for (const b of $$("#nozzle-sel button")) {
    b.addEventListener("click", () => {
      feederNozzle = +b.dataset.nozzle;
      for (const x of $$("#nozzle-sel button")) x.classList.toggle("sel", x === b);
    });
  }
  for (const b of $$("[data-feeder-action]")) {
    b.addEventListener("click", () => runFeederAction(b.dataset.feederAction));
  }
  fillForm(null);
  updateFeederButtons();
  // Holding status refreshes every 2 s while the Feeders tab is on screen.
  setInterval(() => {
    if (!$("#tab-feeders").hidden) loadHead();
  }, 2000);
}

initStepGroups();
initControls();
initFeeders();
initViews();
initSetup();
initKeys();
connect();
fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
pollCameras();
setInterval(pollCameras, 1000);  // 1 s so the sharpness readout is usable while focusing
refreshCalibration();
