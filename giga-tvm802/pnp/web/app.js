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
  "prick-on": { label: "Prick needle down (jogging allowed for setup)", path: "/api/calibrate/needle",
                body: () => ({ down: true }) },
  "prick-off": { label: "Prick needle up", path: "/api/calibrate/needle", body: () => ({ down: false }) },
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
            run: (id) => api(fpath(id, "/pick"), { nozzle: feederNozzle, vision: feederVision }) },
  "pick-place": { label: "Pick and place", needs: true, nozzle: true,
                  run: (id) => api(fpath(id, "/pick-and-place"),
                                   { nozzle: feederNozzle, vision: feederVision, ...placeTarget() }) },
  "place": { label: "Place", nozzle: true,
             run: () => api("/api/head/place", { nozzle: feederNozzle, ...placeTarget() }) },
  "goto-target": { label: "Go to target",
                   run: () => {
                     const t = placeTarget();
                     if (t.x === null || t.y === null) throw new Error("enter X and Y first");
                     return api("/api/move", { x: t.x, y: t.y });
                   } },
  "inspect": { label: "Inspect part", nozzle: true,
               run: () => api("/api/head/inspect", { nozzle: feederNozzle }) },
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
// Feeders tab "Vision check" switch for manual picks; remembered per browser.
let feederVision = true;
try { feederVision = localStorage.getItem("feederVision") !== "off"; } catch (_) { /* storage blocked */ }
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
  $("#log-empty").hidden = true;
}

function clearLog() {
  for (const list of $$("[data-log]")) list.replaceChildren();
  $("#log-empty").hidden = false;
}

// The banner shows either a machine error (from the live state) or the error of
// the last failed request. The live state only clears a banner it put up itself,
// so a request error (e.g. a failed calibration) stays visible until the next
// successful request instead of vanishing on the next state update.
let bannerFromMachine = false;

function showError(msg, fromMachine = false) {
  if (!msg && fromMachine && !bannerFromMachine) return;
  bannerFromMachine = !!msg && fromMachine;
  $("#error").hidden = !msg;
  $("#error-text").textContent = msg || "";
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
    const err = new Error(msg);
    err.shown = true;  // already logged and in the banner
    throw err;
  }
  if (state && state.error) showError(state.error, true);
  else showError("");  // a successful request clears an old request error
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
const enc = (s) => encodeURIComponent(s);

// DOM and request helpers shared by banks.js, job.js and rules.js.
function elem(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}

function iconEl(id) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

function buttonEl(label, className, icon = null) {
  const btn = elem("button", className);
  btn.type = "button";
  if (icon) btn.append(iconEl(icon));
  if (label) btn.append(document.createTextNode(label));
  return btn;
}

function optionEl(value, label) {
  const o = elem("option", "", label);
  o.value = String(value);
  return o;
}

// options: [{value, label}]. A value not in the list is added so it stays visible.
function makeSelect(options, value) {
  const list = options.map((o) => ({ value: String(o.value), label: o.label }));
  const cur = value == null ? null : String(value);
  if (cur !== null && !list.some((o) => o.value === cur)) list.push({ value: cur, label: cur });
  const sel = elem("select");
  sel.append(...list.map((o) => optionEl(o.value, o.label)));
  sel.value = cur ?? (list[0] ? list[0].value : "");
  return sel;
}

function parseNum(text, label) {
  const v = parseFloat(text);
  if (!Number.isFinite(v)) throw new Error(`${label} must be a number`);
  return v;
}

function reportError(err) {
  log(err.message, true);
  showError(err.message);
}

async function getJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

// ------------------------------------------------------------------ tabs

function showTab(name) {
  for (const b of $$("nav button.tab[data-tab]")) b.classList.toggle("active", b.dataset.tab === name);
  for (const page of $$("main[data-tab]")) page.hidden = page.dataset.tab !== name;
  if (name === "setup") refreshCalibration();
  if (name === "settings") loadRules();
  if (name === "defaults") loadDefaults();
  if (name === "feeders") {
    refreshFeeders();
    refreshBanks();
    loadHead();
  }
  if (name === "job") onJobTabShown();
  else onJobTabHidden();
  syncStreams();
}

// ------------------------------------------------------------------ rendering

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = `badge ${cls}`;
}

const sentence = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);

function render(s) {
  state = s;
  renderPanel(s.panel);
  setPill($("#pill-conn"), s.connected ? "Connected" : "Offline", s.connected ? "success" : "danger");
  $("#pill-conn").title = s.connected ? "" : (s.link_error || "Not connected to the machine");
  const connectBtn = $("#btn-connect");
  connectBtn.hidden = s.connected;
  if (s.connected && connectBtn.disabled) {
    connectBtn.disabled = false;
    log("machine connected");
  }
  setPill($("#pill-homed"), s.homed ? "Homed" : "Not homed", s.homed ? "success" : "warning");
  setPill($("#pill-busy"), s.busy ? sentence(s.busy) : "Idle", s.busy ? "accent" : "");
  setPill($("#pill-speed"), `Speed ${s.speed_mode ?? "?"}`, "");

  const p = s.position;
  $("#pos-x").textContent = p.x.toFixed(3);
  $("#pos-y").textContent = p.y.toFixed(3);
  $("#pos-z").textContent = p.z.toFixed(3);
  $("#pos-a1").textContent = p.a1.toFixed(2);
  $("#pos-a2").textContent = p.a2.toFixed(2);
  $("#z-hint").textContent = p.z > 0.05 ? "N1 down" : p.z < -0.05 ? "N2 down" : "mm";

  for (const k of ["left", "right", "top", "bottom"]) {
    $(`#lim-${k}`).classList.toggle("on", !!s.limits[k]);
  }
  $("#vac-1").classList.toggle("on", !!s.vacuum["1"]);
  $("#vac-2").classList.toggle("on", !!s.vacuum["2"]);
  $("#needle-led").classList.toggle("on", s.needle_sensor === "down");
  renderAlarm(s);

  for (const btn of $$("[data-out]")) {
    btn.classList.toggle("on", !!s.outputs[btn.dataset.out]);
  }

  document.body.classList.toggle("estopped", s.estopped);
  $("#btn-reset").hidden = !s.estopped;
  const block = !s.connected || s.estopped || !!s.busy;
  for (const b of $$("[data-jog],[data-z],[data-rot],[data-zero],#btn-up,#btn-zero-z,#btn-home,#goto button")) {
    b.disabled = block;
  }
  $("#goto button").disabled = block || !s.homed;
  updateFeederButtons();
  showError(s.error, true);
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
    // Views in the pinned top-bar strip are always on screen; others only on the visible tab.
    const main = img.closest("main");
    const visible = (!main || !main.hidden) && (cameras === null || cameras.has(name));
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
    setPill($("#pill-conn"), "Server lost", "danger");
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
  // Full-resolution pixel coordinates of the click. The image is drawn with
  // object-fit: contain, so allow for the letterbox bars around the picture.
  const r = img.getBoundingClientRect();
  const nw = img.naturalWidth;
  const nh = img.naturalHeight;
  const scale = Math.min(r.width / nw, r.height / nh);
  const left = r.left + (r.width - nw * scale) / 2;
  const top = r.top + (r.height - nh * scale) / 2;
  const u = (ev.clientX - left) / scale;
  const v = (ev.clientY - top) / scale;
  if (u < 0 || v < 0 || u > nw || v > nh) return; // click on a letterbox bar
  log(`${img.dataset.cam} click at pixel (${Math.round(u)}, ${Math.round(v)})`);
  await api(`/api/cameras/${img.dataset.cam}/click`, { u, v }).catch(() => {});
}

function initViews() {
  for (const img of $$("img[data-cam]")) {
    img.addEventListener("click", (ev) => viewClick(img, ev));
  }
}

// ------------------------------------------------------------------ setup tasks

// Inspection results (head.last_inspection and POST /api/head/inspect).
const INSPECT_BADGE = {
  ok: ["success", "OK"],
  skipped: ["", "Vision off"],
  "no part": ["danger", "No part"],
  rejected: ["danger", "Rejected"],
};

function inspectionDetail(insp) {
  if (insp.result !== "ok") return insp.message || "";
  const bits = [];
  if (insp.offset) bits.push(`offset ${Math.hypot(insp.offset[0], insp.offset[1]).toFixed(2)} mm`);
  if (insp.angle_error != null) {
    const a = insp.angle_error;
    bits.push(`angle ${a < 0 ? "−" : ""}${Math.abs(a).toFixed(1)}°`);
  }
  if (insp.size) bits.push(`${insp.size[0].toFixed(2)} × ${insp.size[1].toFixed(2)} mm`);
  return bits.join(" · ");
}

// Nodes for one inspection row: nozzle, result badge, detail text. Caller puts them in .inspection.
function inspectionParts(insp) {
  const [cls, label] = INSPECT_BADGE[insp.result] || ["", insp.result];
  const nodes = [
    elem("span", "node", `N${insp.nozzle}`),
    elem("span", `badge ${cls}`.trim(), label),
  ];
  const detail = inspectionDetail(insp);
  if (detail) nodes.push(elem("span", "detail", detail));
  return nodes;
}

function inspectionSummary(insp) {
  const [, label] = INSPECT_BADGE[insp.result] || ["", insp.result];
  const detail = inspectionDetail(insp);
  return detail ? `${label}, ${detail}` : label;
}

// The newest of several inspection records (by `at`), or null.
function latestInspection(list) {
  let best = null;
  for (const insp of Object.values(list || {})) {
    if (!insp) continue;
    if (!best || Date.parse(insp.at) > Date.parse(best.at)) best = insp;
  }
  return best;
}

function describeResult(res) {
  if (res.nozzle !== undefined && res.result !== undefined) return inspectionSummary(res);
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
  for (const b of $$("#tab-setup button, [data-task]")) b.disabled = busy;
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
        // The X/Y step is shared with the machine's front-panel jog keys.
        if (group === "xy-steps") setPanel({ step: parseFloat(b.dataset.step) });
      });
    }
  }
  for (const b of $$("#panel-mode button")) {
    b.addEventListener("click", () => setPanel({ mode: b.dataset.mode }));
  }
}

async function setPanel(change) {
  try {
    await api("/api/panel", change);
  } catch (_) { /* shown by api() */ }
}

// Front panel (Step / DotMove-ConMove keys) -> the Jog card's step and mode buttons.
let shownPanel = { mode: null, step: null };

function renderPanel(panel) {
  if (!panel) return;
  if (panel.step !== shownPanel.step) {
    for (const b of $$("#xy-steps button")) {
      b.classList.toggle("sel", Math.abs(parseFloat(b.dataset.step) - panel.step) < 1e-9);
    }
  }
  if (panel.mode !== shownPanel.mode) {
    for (const b of $$("#panel-mode button")) b.classList.toggle("sel", b.dataset.mode === panel.mode);
  }
  shownPanel = { mode: panel.mode, step: panel.step };
  const names = { "x-": "X−", "x+": "X+", "y+": "Y+", "y-": "Y−", step: "Step", mode: "Dot/Con",
                  start: "Start/Stop", pause: "Pause/Cont" };
  $("#panel-keys").textContent = (panel.keys || []).map((k) => names[k] || k).join(" ");
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

// dir: +1 = anticlockwise, -1 = clockwise, seen from above. A positive angle turns the
// nozzle anticlockwise on the machine (checked 2026-11-16), the same sense as KiCad's
// part rotation, which the placement math relies on.
async function rotateStep(nozzle, dir) {
  const step = selectedStep("a-steps");
  const cur = state ? state.position[`a${nozzle}`] : 0;
  log(`rotate N${nozzle} ${dir > 0 ? "anticlockwise +" : "clockwise -"}${step}°`);
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
  $("#btn-zero-z").addEventListener("click", async () => {
    const z = state ? state.position.z.toFixed(3) : "?";
    if (!await askConfirm(`Set the current Z (${z} mm) as 0?\n\nOnly do this when both nozzle tips are level.`,
                          "Set Z zero")) return;
    try {
      await api("/api/machine/zero-z");
      log(`Z zeroed here (was ${z} mm)`);
    } catch (_) { /* shown by api() */ }
  });
  $("#btn-home").addEventListener("click", async () => {
    log("homing…");
    await api("/api/home").then(() => log("homed")).catch(() => {});
  });
  $("#btn-estop").addEventListener("click", estop);
  $("#btn-connect").addEventListener("click", connectMachine);
  $("#btn-reset").addEventListener("click", () => api("/api/reset").then(() => log("E-stop reset")));
  $("#btn-leds").addEventListener("click", () => toggleOutput("leds"));
  $("#log-clear").addEventListener("click", clearLog);

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
    b.className = "btn";
    b.dataset.out = name;
    b.textContent = label;
    b.addEventListener("click", () => toggleOutput(name));
    toggles.append(b);
  }

  for (const b of $$("nav button.tab[data-tab]")) {
    b.addEventListener("click", () => showTab(b.dataset.tab));
  }
}

async function connectMachine() {
  const b = $("#btn-connect");
  b.disabled = true;
  log("connecting to the machine…");
  try {
    await api("/api/machine/reconnect");
  } catch (_) {
    b.disabled = false;
    return;
  }
  // Re-enable after a while if the machine still does not answer; render()
  // re-enables it (and logs) as soon as the link is up.
  setTimeout(() => {
    if (b.disabled && !(state && state.connected)) {
      b.disabled = false;
      log(`still not connected${state && state.link_error ? ": " + state.link_error : ""}`, true);
    }
  }, 8000);
}

// ------------------------------------------------------------------ confirmation dialog

// In-page replacement for window.confirm(): the Claude browser pane (and some kiosk
// browsers) suppress native dialogs, which made confirm() silently return false.
// A machine.yaml default from the Defaults tab (defaults.js loads after this file,
// so this works before it has loaded too, falling back to the given value).
function configDefault(section, key, fallback) {
  return typeof machineDefault === "function" ? machineDefault(section, key, fallback) : fallback;
}

function askConfirm(message, okLabel = "OK", { title = "Please confirm", danger = false } = {}) {
  return new Promise((resolve) => {
    const box = $("#confirm");
    const ok = $("#confirm-ok");
    const cancel = $("#confirm-cancel");
    $("#confirm-title").textContent = title;
    $("#confirm-msg").textContent = message;
    ok.textContent = okLabel;
    ok.classList.toggle("btn-danger", danger);
    ok.classList.toggle("btn-primary", !danger);
    const finish = (answer) => {
      box.hidden = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      resolve(answer);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    box.hidden = false;
    ok.focus();
  });
}

// Detection overlay on the camera views: what the down camera's Centre on mark and the up
// camera's bottom vision would detect in the live picture, refreshed while switched on.
// The shapes come in camera pixels; the SVG's viewBox is the picture size, so they line up
// with the picture whatever size it is shown at.
const detectOn = { down: false, up: false };
let detectTimer = null;

function detectQuery(name) {
  if (name === "up") {
    const sel = document.querySelector("#nozzle-sel button.sel");
    return `nozzle=${sel ? sel.dataset.nozzle : 1}`;
  }
  const size = document.querySelector("#ref-mark-size");
  const [lo, hi] = (size ? size.value : "0.3,4").split(",");
  return `mark_min=${lo}&mark_max=${hi}&method=auto`;
}

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function drawDetection(name, res) {
  const svg = document.querySelector(`[data-det-overlay="${name}"]`);
  const label = document.querySelector(`[data-det-label="${name}"]`);
  if (!res) {
    svg.replaceChildren();
    label.textContent = "Detect: no picture";
    return;
  }
  svg.setAttribute("viewBox", `0 0 ${res.width} ${res.height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const parts = [];
  for (const s of res.shapes) {
    const pts = s.points.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    parts.push(svgEl("polygon", { points: pts, class: s.kind === "found" ? "det-found" : "det-zone" }));
    if (s.centre) {
      const [u, v] = s.centre;
      parts.push(svgEl("path", { d: `M${u - 8},${v} H${u + 8} M${u},${v - 8} V${v + 8}`, class: "det-found" }));
    }
  }
  svg.replaceChildren(...parts);
  const found = res.shapes.find((s) => s.kind === "found");
  label.textContent = found ? `Detected: ${found.label}` : (name === "up" ? "Detected: no part" : "Detected: no mark");
  label.classList.toggle("none", !found);
}

async function pollDetection() {
  for (const name of ["down", "up"]) {
    if (!detectOn[name]) continue;
    try {
      const res = await fetch(`/api/vision/detect/${name}?${detectQuery(name)}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        drawDetection(name, { width: 1, height: 1, shapes: [] });
        document.querySelector(`[data-det-label="${name}"]`).textContent = `Detect: ${data.detail || res.status}`;
        continue;
      }
      if (detectOn[name]) drawDetection(name, data);
    } catch (_) { /* server busy or gone: try again next time */ }
  }
}

function setDetect(name, on) {
  detectOn[name] = on;
  document.querySelector(`[data-detect="${name}"]`).classList.toggle("on", on);
  // An <svg> has no .hidden property (HTML elements only): set the attribute itself.
  document.querySelector(`[data-det-overlay="${name}"]`).toggleAttribute("hidden", !on);
  document.querySelector(`[data-det-label="${name}"]`).hidden = !on;
  if (!on) document.querySelector(`[data-det-overlay="${name}"]`).replaceChildren();
  const any = detectOn.down || detectOn.up;
  if (any && detectTimer === null) {
    pollDetection();
    detectTimer = setInterval(pollDetection, 500);
  } else if (!any && detectTimer !== null) {
    clearInterval(detectTimer);
    detectTimer = null;
  }
}

function initDetection() {
  for (const b of $$("[data-detect]")) {
    b.addEventListener("click", (e) => {
      e.stopPropagation();  // not a click on the picture
      setDetect(b.dataset.detect, !detectOn[b.dataset.detect]);
    });
  }
}

// ------------------------------------------------------------------ alarm popup

function renderAlarm(s) {
  const a = s.alarm;
  const box = $("#alarm");
  if (!a) {
    box.hidden = true;
    return;
  }
  const wasHidden = box.hidden;
  box.hidden = false;
  $("#alarm-title span").textContent = a.title || "Alarm";
  $("#alarm-msg").textContent = a.message || "";
  const sensor = s.needle_sensor || "unknown";
  const sensorEl = $("#alarm-sensor");
  sensorEl.textContent = sensor.toUpperCase();
  sensorEl.className = sensor;
  $("#alarm-silence").disabled = !!a.silenced;
  $("#alarm-silence").textContent = a.silenced ? "Buzzer silenced" : "Silence buzzer";
  if (wasHidden) {
    log(`ALARM: ${a.title}`, true);
    $("#alarm-clear").focus();
  }
}

async function alarmAction(path, label) {
  try {
    await api(path);
    log(label);
  } catch (_) { /* shown by api() */ }
}

function initAlarm() {
  $("#alarm-clear").addEventListener("click", () => alarmAction("/api/alarm/clear", "alarm: clear and continue"));
  $("#alarm-silence").addEventListener("click", () => alarmAction("/api/alarm/silence", "alarm: buzzer silenced"));
  $("#alarm-abort").addEventListener("click", async () => {
    if (await askConfirm("Abort the action that is waiting on the needle?", "Abort")) {
      alarmAction("/api/alarm/abort", "alarm: aborted");
    }
  });
}

function estop() {
  log("E-STOP", true);
  api("/api/estop").catch(() => {});
}

function initKeys() {
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") return estop();
    if (ev.target.closest("input, select, textarea")) return;
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

// Feed direction select: +X/−X/+Y/−Y. A feeder saved with an angled direction gets an
// extra "Custom" option holding its exact vector, so saving the form doesn't change it.
function setDirection(dir) {
  const sel = field("direction");
  sel.querySelector("option[data-custom]")?.remove();
  const [dx, dy] = dir.map((v) => Math.round(v * 1000) / 1000);
  const value = `${dx},${dy}`;
  if (![...sel.options].some((o) => o.value === value)) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.dataset.custom = "";
    opt.textContent = `Custom (${dx}, ${dy})`;
    sel.append(opt);
  }
  sel.value = value;
}

function fillForm(f) {
  const d = f || { id: "", name: "", kind: "strip", pick: [0, 0], direction: [1, 0], pitch: 4, count: 0,
                   capacity: null, rotation: 0, pick_depth: configDefault("head", "pick_depth", 3), prick: null, needs_feed: true,
                   enabled: true, value: "", package: "" };
  const set = (name, v) => { field(name).value = v ?? ""; };
  set("id", d.id);
  field("id").readOnly = selectedId !== null;
  set("name", d.name);
  set("kind", d.kind);
  set("pick_x", round3(d.pick[0]));
  set("pick_y", round3(d.pick[1]));
  setDirection(d.direction);
  set("pitch", round3(d.pitch));
  set("count", d.count);
  set("capacity", d.capacity);
  set("rotation", round3(-d.rotation + 0));  // shown as an offset: + = anticlockwise
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
    direction: field("direction").value.split(",").map(Number),
    pitch: num("pitch", "pitch"),
    count: Math.trunc(num("count", "count")),
    capacity: text("capacity") === "" ? null : Math.trunc(num("capacity", "capacity")),
    rotation: -num("rotation", "rotation offset") + 0,  // offset -> part rotation in the tape
    pick_depth: num("pick_depth", "pick depth"),
    prick,
    needs_feed: field("needs_feed").checked,
    enabled: field("enabled").checked,
    value: text("value"),
    package: text("package"),
    // Bank membership is not edited in this form: send back what the server has.
    bank: (selectedFeeder() || {}).bank || "",
    lane: (selectedFeeder() || {}).lane ?? null,
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
  if (!f.enabled) add("Disabled", "idle");
  if (f.kind === "drag" && f.needs_feed) add("Needs feed", "warn");
  if (f.remaining === 0) add("Empty", "bad");
  if (!chips.length) add("Ready", "ok");
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
    tr.append(cell(f.id), cell(f.name || "–"), cell(f.bank ? `${f.bank} · ${f.lane ?? "–"}` : "–"),
              cell(f.kind), cell(f.pitch), cell(count),
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

// Export / import the whole feeder configuration (feeders + banks + layouts).
function exportFeeders() {
  // A plain link download: the server names the file feeders-<date>.yaml.
  const a = document.createElement("a");
  a.href = "/api/feeders/export";
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
  log("feeders exported");
}

async function importFeeders(ev) {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";  // allow picking the same file again
  if (!file) return;
  const merge = $("#import-merge").checked;
  const reset = $("#import-reset").checked;
  const what = merge
    ? `Merge "${file.name}" into the current feeders? Feeders and banks in the file are added or updated; others are kept.`
    : `Replace ALL current feeders and banks with "${file.name}"? Feeders not in the file are removed.`;
  if (!await askConfirm(`${what}${reset ? "\n\nPart counts will be reset (fresh tape)." : ""}`,
                        merge ? "Merge" : "Replace all")) return;
  let content;
  try {
    content = await file.text();
  } catch (err) {
    showError(`Couldn't read ${file.name}: ${err.message}`);
    return;
  }
  await feederAction(`Import ${file.name}`, async () => {
    const res = await api("/api/feeders/import", { content, mode: merge ? "merge" : "replace", reset_counts: reset });
    if (typeof refreshBanks === "function") await refreshBanks();
    return res;
  });
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
  const last = headState.last_inspection || {};
  $("#inspections").replaceChildren(...[1, 2].map((n) => {
    const row = elem("div", "inspection");
    const insp = last[n];
    if (insp) row.append(...inspectionParts(insp));
    else row.append(elem("span", "node", `N${n}`), elem("span", "detail", "Not inspected yet"));
    return row;
  }));
  updateFeederButtons();
}

function describeFeederResult(res) {
  if (res && res.imported_feeders !== undefined) {
    return `${res.imported_feeders} feeders and ${res.imported_banks} banks imported ` +
           `(now ${res.feeders} feeders, ${res.banks} banks)`;
  }
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
  } catch (err) {
    // api() errors are already logged and shown; input errors (e.g. a bad
    // number in the place target) are not.
    if (!err.shown) {
      log(`${label}: ${err.message}`, true);
      showError(err.message);
    }
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

// In-page confirmation (window.confirm is blocked in some browsers and webviews,
// where it returns false at once). Resolves true only for the confirm button.
// Confirmation for destructive actions (delete, reset, load layout): the in-page box
// with a red confirm button.
function confirmDialog({ title, body, confirmLabel }) {
  return askConfirm(body, confirmLabel, { title, danger: true });
}

async function deleteFeeder() {
  const id = selectedId;
  if (id === null) return;
  const ok = await confirmDialog({
    title: `Delete feeder '${id}'?`,
    body: "It is removed from the feeder file. This can't be undone.",
    confirmLabel: "Delete feeder",
  });
  if (!ok) return;
  await feederAction(`Delete ${id}`, async () => {
    await api(fpath(id), undefined, "DELETE");
    selectedId = null;
    fillForm(null);
    return { ok: true };
  });
}

// Manual place target on the Feeders tab: empty X/Y = the camera crosshair,
// empty depth = the configured default place depth.
function placeTarget() {
  const num = (id) => {
    const raw = $(id).value.trim();
    if (raw === "") return null;
    const v = Number(raw);
    if (!Number.isFinite(v)) throw new Error(`${$(id).closest("label").textContent.trim()} is not a number`);
    return v;
  };
  return { x: num("#pt-x"), y: num("#pt-y"), rotation: num("#pt-rot") ?? 0, depth: num("#pt-depth") };
}

function initVisionSwitch() {
  const show = () => {
    for (const x of $$("#vision-sel button")) x.classList.toggle("sel", (x.dataset.vision === "on") === feederVision);
  };
  for (const b of $$("#vision-sel button")) {
    b.addEventListener("click", () => {
      feederVision = b.dataset.vision === "on";
      try { localStorage.setItem("feederVision", feederVision ? "on" : "off"); } catch (_) { /* ignore */ }
      show();
      log(`Vision check for manual picks: ${feederVision ? "on" : "off"}`);
    });
  }
  show();
}

function initPlaceTarget() {
  $("#pt-use-crosshair").addEventListener("click", () => {
    if (!state) return;
    $("#pt-x").value = state.position.x.toFixed(3);
    $("#pt-y").value = state.position.y.toFixed(3);
  });
  $("#pt-clear").addEventListener("click", () => {
    $("#pt-x").value = "";
    $("#pt-y").value = "";
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
  for (const b of $$("#vision-sel button")) b.disabled = feederBusy;
  $("#feeder-new").disabled = feederBusy;
  $("#feeder-import").disabled = feederBusy;
  // Saving and deleting only edit the feeder file: no homing or connection needed.
  // They wait only while a feeder action runs (the server refuses while the head is busy).
  const fileBusy = feederBusy || headBusy;
  $("#feeder-delete").disabled = fileBusy || selectedId === null;
  $("#feeder-form button[type=submit]").disabled = fileBusy;
  let hint = "Select a feeder in the table, or press New feeder.";
  if (!state || !state.connected) hint = "Not connected to the machine.";
  else if (!state.homed) hint = "Home the machine first (Machine tab) before teaching or moving.";
  else if (selectedId === null) hint = "New feeder: fill in the form and press Save.";
  else if (sel) hint = `${sel.id} · ${sel.kind} · ${sel.name || "unnamed"}`;
  $("#feeder-selected").textContent = hint;
  // banks.js loads after this script; the check covers calls made before it runs.
  if (typeof updateBankButtons === "function") updateBankButtons();
}

function initFeeders() {
  $("#feeder-new").addEventListener("click", newFeeder);
  $("#feeder-export").addEventListener("click", exportFeeders);
  $("#feeder-import").addEventListener("click", () => $("#feeder-import-file").click());
  $("#feeder-import-file").addEventListener("change", importFeeders);
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

// ------------------------------------------------------------------ theme

function applyTheme(choice) {
  if (choice === "dark" || choice === "light") document.documentElement.setAttribute("data-theme", choice);
  else document.documentElement.removeAttribute("data-theme");
  for (const b of $$("[data-theme-choice]")) {
    const on = b.dataset.themeChoice === choice;
    b.classList.toggle("sel", on);
    b.setAttribute("aria-checked", String(on));
  }
}

function initTheme() {
  let saved = "system";
  try { saved = localStorage.getItem("theme") || "system"; } catch (_) { /* storage blocked */ }
  applyTheme(saved);
  for (const b of $$("[data-theme-choice]")) {
    b.addEventListener("click", () => {
      const choice = b.dataset.themeChoice;
      try {
        if (choice === "system") localStorage.removeItem("theme");
        else localStorage.setItem("theme", choice);
      } catch (_) { /* storage blocked: applies for this visit only */ }
      applyTheme(choice);
    });
  }
}

initTheme();
initStepGroups();
initControls();
initFeeders();
initPlaceTarget();
initAlarm();
initDetection();
initVisionSwitch();
initViews();
initSetup();
initKeys();
connect();
fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
pollCameras();
setInterval(pollCameras, 1000);  // 1 s so the sharpness readout is usable while focusing
refreshCalibration();
