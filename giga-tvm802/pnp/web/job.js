"use strict";

// Job tab: import, board and panel setup, the parts list and the run controls.
// Polls GET /api/job every 500 ms while the tab is on screen. Loaded after app.js.

const RUN_BUSY = ["running", "pausing", "stopping", "stepping"];
const RUN_LABEL = {
  idle: "Idle", running: "Running", pausing: "Pausing", paused: "Paused",
  stopping: "Stopping", done: "Done", error: "Error", stepping: "Step mode",
};
const RUN_BADGE = { running: "accent", stepping: "accent", paused: "warning", error: "danger", done: "success" };

let jobData = null;        // last GET /api/job
let jobFeederIds = [];     // feeder ids for the Feeder selects
let jobFilter = "all";     // all | todo | problem
let jobBusy = false;       // a job request is running
let jobFetching = false;   // a GET /api/job is in flight
let jobTimer = null;
let boardDirty = false;    // board fields were edited and not saved yet
let partsSig = "";
let boardRowsSig = "";
let marksSig = "";         // board marks table: last rendered state (renderMarkRows)

// Where the parts are in the position file's coordinates (the frame the reference points use).
function partsExtent(job) {
  const top = job ? job.parts.filter((p) => p.side !== "bottom") : [];
  if (!top.length) return null;
  const xs = top.map((p) => p.x), ys = top.map((p) => p.y);
  return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
}

function renderExtent(job) {
  const el = $("#marks-extent");
  const e = partsExtent(job);
  el.hidden = !e;
  if (e) {
    el.textContent = `Parts in this file: X ${e.x0.toFixed(1)} to ${e.x1.toFixed(1)}, Y ${e.y0.toFixed(1)} to ${e.y1.toFixed(1)} mm. `
      + "Both marks of each board must be in these coordinates (on or near the board).";
  }
}

let fidMeasuring = false;  // a measure request is running (drives the button label)
let fidRowsSig = "";
let fidPointsSig = "";

function runStatus() {
  return (jobData && jobData.status) || {
    state: "idle", placed: 0, failed: 0, skipped: 0, pending: 0, total: 0, message: null,
  };
}

const runnerBusy = () => RUN_BUSY.includes(runStatus().state);
const jobHeadLocked = () => !state || !state.connected || state.estopped || !!state.busy
  || !!(headState && headState.busy);
// Origin, rotation and go-to need the machine homed and free.
const jobReady = () => !jobHeadLocked() && !!state.homed;

function currentPart(ref) {
  return jobData && jobData.job ? jobData.job.parts.find((p) => p.ref === ref) : undefined;
}

function onJobTabShown() {
  refreshJob();
  refreshJobFeeders();
  if (jobTimer === null) jobTimer = setInterval(refreshJob, 500);
}

function onJobTabHidden() {
  if (jobTimer !== null) {
    clearInterval(jobTimer);
    jobTimer = null;
  }
}

async function refreshJob() {
  if (jobFetching) return;
  jobFetching = true;
  try {
    jobData = await getJson("/api/job");
  } catch (_) {
    return; /* server lost: keep the last answer */
  } finally {
    jobFetching = false;
  }
  renderJob();
}

async function refreshJobFeeders() {
  try {
    const list = await getJson("/api/feeders");
    jobFeederIds = list.map((f) => f.id);
  } catch (_) {
    /* keep the last list */
  }
}

// Runs one job request with the controls locked, then refreshes the job.
async function jobCall(label, path, body, method = "POST") {
  if (jobBusy) return null;
  jobBusy = true;
  updateJobButtons(jobData && jobData.job);
  log(`${label}…`);
  let res = null;
  try {
    res = await api(path, body, method);
    log(`${label}: done`);
  } catch (_) {
    /* api() has logged the error and shown the banner */
  } finally {
    jobBusy = false;
    await refreshJob();
    updateJobButtons(jobData && jobData.job);
  }
  return res;
}

// ------------------------------------------------------------------ render

function renderJob() {
  const job = jobData && jobData.job ? jobData.job : null;
  const plan = (jobData && jobData.plan) || [];
  renderRun(job);
  renderImport(job);
  renderBoardForm(job);
  renderMarks(job);
  renderBoardRows(job);
  renderFiducials(job);
  renderParts(job, plan);
  updateJobButtons(job);
}

function renderRun(job) {
  const s = runStatus();
  const badge = $("#job-state");
  badge.textContent = RUN_LABEL[s.state] || s.state;
  badge.className = `badge ${RUN_BADGE[s.state] || ""}`.trim();
  const total = s.total || 0;
  $("#job-bar").style.width = `${total ? Math.round((s.placed / total) * 100) : 0}%`;
  if (s.current === "fiducials") {
    $("#job-counts").textContent = "Measuring fiducials";
  } else {
    $("#job-counts").textContent = job
      ? `${s.placed} of ${total} placed · ${s.failed} failed · ${s.skipped} skipped`
      : "No job loaded.";
  }
  const last = latestInspection(s.last_inspection);
  const lastEl = $("#job-last");
  lastEl.hidden = !last;
  if (last) {
    const row = elem("div", "inspection");
    row.append(elem("span", "detail", "Last part"), ...inspectionParts(last));
    lastEl.replaceChildren(row);
  } else {
    lastEl.replaceChildren();
  }
  const held = s.holding || {};
  $("#job-holding").textContent = [1, 2].map((n) => `N${n} ${held[n] ? `holds a part from ${held[n]}` : "empty"}`).join(" · ");
  $("#job-holding").classList.toggle("holds", !!(held[1] || held[2]));

  // Step mode: what is running now and what the next press of Step does.
  const info = $("#job-step-info");
  const st = s.step;
  info.hidden = !st;
  if (st) {
    const part = st.ref ? `${st.ref} · stage ${st.index} of ${st.count}` : "";
    info.textContent = (st.running ? `Running: ${st.running}. ` : "")
      + `Next press: ${st.next}` + (part ? ` (${part})` : "");
  }
  const box = $("#job-message");
  box.hidden = !s.message;
  box.classList.toggle("warn", s.state === "paused");
  $("#job-message-text").textContent = s.message || "";
}

function renderImport(job) {
  $("#job-empty").hidden = !!job;
  const facts = $("#job-facts");
  facts.hidden = !job;
  if (job) {
    const n = job.boards.length;
    facts.textContent = `${job.name} · ${job.source || "no source file"} · ${job.parts.length} parts · `
      + `${n} board${n === 1 ? "" : "s"} · ${job.origin ? "origin set" : "origin not set"}`;
  }
}

function renderBoardForm(job) {
  // The scale is read-only (set by the two-point or fiducial measurement), so it is shown even while the form is being edited.
  $("#job-scale").textContent = (job && Number.isFinite(job.scale) ? job.scale : 1).toFixed(5);
  // The PCB thickness saves on its own (change), so it is kept out of the board form's dirty flag.
  if (document.activeElement !== $("#job-pcb")) $("#job-pcb").value = job ? String(job.pcb_thickness) : "";
  if (boardDirty) return;
  $("#job-ox").value = job && job.origin ? round3(job.origin[0]) : "";
  $("#job-oy").value = job && job.origin ? round3(job.origin[1]) : "";
  $("#job-rot").value = job ? round3(job.rotation) : "";
}

// Board marks: each board has two marks (board X/Y from the position file, measured machine
// X/Y) and is placed from its own fit. One row per board; the server refits on every change.
function renderMarks(job) {
  renderExtent(job);
  renderMarkRows(job);
}

function renderMarkRows(job) {
  const refs = [...new Set(job ? job.parts.map((p) => p.ref) : [])]
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const sig = JSON.stringify([job ? job.board_marks : [], job ? job.mark_results : [],
    job ? job.boards.map((b) => !!b.skip) : [], refs]);
  const tbody = $("#mark-rows");
  // A field being typed in is left alone; the next poll redraws the table.
  const typing = !!document.activeElement && tbody.contains(document.activeElement)
    && document.activeElement.matches("input, select");
  if (sig === marksSig || typing) return;
  marksSig = sig;
  tbody.replaceChildren(...(job ? job.board_marks.map((entry, b) => markRow(job, b, entry, refs)) : []));
}

function markRow(job, b, entry, refs) {
  const board = job.boards[b];
  const res = job.mark_results[b] || {};
  const tr = elem("tr", board.skip ? "skipped" : "");
  tr.dataset.board = String(b);
  tr.append(cell(b + 1, "mono"), ...markCells(entry.m1, "m1", refs), ...markCells(entry.m2, "m2", refs),
            markResultCell(board, res));
  return tr;
}

// Six cells for one mark: part, board X, board Y, machine X, machine Y, actions.
function markCells(mark, name, refs) {
  const n = name === "m1" ? 1 : 2;
  const sel = elem("select", "mark-part");
  sel.dataset.mark = name;
  sel.setAttribute("aria-label", `Part for mark ${n}`);
  sel.append(optionEl("", "Custom"), ...refs.map((r) => optionEl(r, r)));
  sel.value = mark.ref && refs.includes(mark.ref) ? mark.ref : "";
  const cells = [tdWith(sel)];
  for (const [field, label] of [["x", "Board X"], ["y", "Board Y"], ["mx", "Machine X"], ["my", "Machine Y"]]) {
    const inp = elem("input", "num");
    inp.type = "number";
    inp.step = "any";
    inp.value = mark[field] == null ? "" : round3(mark[field]);
    inp.placeholder = field.startsWith("m") ? "capture" : "board";
    inp.dataset.mark = name;
    inp.dataset.field = field;
    inp.setAttribute("aria-label", `${label}, mark ${n}`);
    cells.push(tdWith(inp));
  }
  const actions = elem("td", "mark-actions");
  for (const [action, icon, text, title] of [
    ["capture", "crosshair", "Capture", "Record the camera's current position as this mark's machine position"],
    ["centre", "scan", "Centre", "Centre the camera on the round mark nearest the crosshair (size from Mark Ø), then record it"],
  ]) {
    const btn = buttonEl(text, action === "centre" ? "btn btn-ghost" : "btn", icon);
    btn.dataset.mark = name;
    btn.dataset.action = action;
    btn.title = title;
    actions.append(btn);
  }
  cells.push(actions);
  return cells;
}

function markResultCell(board, res) {
  const td = elem("td", "mark-result");
  let node;
  if (board.skip) node = elem("span", "", "skipped");
  else if (res.error) node = elem("span", "mark-err", res.error);
  else if (res.ok) {
    node = elem("span", "mark-ok", `✓ rot ${fmt(res.rotation, 3)}° · scale ${fmt(res.scale, 5)} · `
      + `Δ ${fmt(res.distance_error, 2)} mm`);
  } else node = elem("span", "", "–");
  td.append(node);
  return td;
}

function saveMark(b, body) {
  return jobCall(`Board ${b + 1} marks`, `/api/job/marks/${b}`, body, "PUT");
}

// A board X/Y or machine X/Y typed in: empty clears it.
function saveMarkField(b, name, field, input) {
  const text = input.value.trim();
  const value = text === "" ? null : Number(text);
  if (value !== null && !Number.isFinite(value)) {
    showError("Enter a number, or leave the field empty to clear it");
    return null;
  }
  return saveMark(b, { [name]: { [field]: value } });
}

// A part picked: its file X/Y and reference fill in the mark; Custom keeps the X/Y as they are.
function markPartChanged(b, name, sel) {
  const part = sel.value ? currentPart(sel.value) : undefined;
  return saveMark(b, { [name]: part ? { ref: part.ref, x: part.x, y: part.y } : { ref: null } });
}

// Record the camera position as a mark, or centre on the nearest round mark first (Mark Ø range).
function captureMark(b, name, centre) {
  const n = name === "m1" ? 1 : 2;
  const mark_mm = centre ? $("#ref-mark-size").value.split(",").map(Number) : null;
  return jobCall(`Board ${b + 1} mark ${n}: ${centre ? "centre and capture" : "capture"}`,
                 `/api/job/marks/${b}/${name}/capture`, { centre, mark_mm, method: "auto" });
}

function copyMarksToAll() {
  return jobCall("Copy board 1 marks to all boards", "/api/job/marks/design-to-all");
}

async function clearMarks() {
  const ok = await confirmDialog({
    title: "Clear all measurements?",
    body: "Every board's captured machine positions and its mark fit are removed. The board X/Y and the parts stay.",
    confirmLabel: "Clear measurements",
  });
  if (!ok) return;
  await jobCall("Clear measurements", "/api/job/marks/clear");
}

function renderBoardRows(job) {
  const locked = jobBusy || runnerBusy();
  const sig = JSON.stringify([job ? job.boards : [], locked]);
  const tbody = $("#job-boards");
  if (sig === boardRowsSig || tbody.contains(document.activeElement)) return;
  boardRowsSig = sig;
  if (!job) {
    tbody.replaceChildren();
    return;
  }
  tbody.replaceChildren(...job.boards.map((b, i) => {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!b.skip;
    cb.disabled = locked;
    cb.setAttribute("aria-label", `Skip board ${i + 1}`);
    cb.addEventListener("change", () => {
      savePanel(job.boards.map((x, j) => (j === i ? { ...x, skip: cb.checked } : x)));
    });
    const skipCell = elem("td");
    skipCell.append(cb);
    const tr = elem("tr");
    tr.append(cell(i + 1, "mono"), cell(fmt(b.x, 2), "mono"), cell(fmt(b.y, 2), "mono"),
              cell(fmt(b.rotation, 2), "mono"), skipCell);
    return tr;
  }));
}

function matchesFilter(item) {
  if (jobFilter === "todo") return item.status === "pending";
  if (jobFilter === "problem") return !!item.problem || item.status === "failed";
  if (jobFilter === "off") return partOff(currentPart(item.ref));
  return true;
}

// Speed % of a part (the vendor's per-component "Speed"): applies to the moves carrying it.
function speedCell(item, part, locked) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.min = "10";
  inp.max = "100";
  inp.step = "10";
  inp.className = "num part-coord part-speed";
  inp.value = part ? part.speed ?? 100 : "";
  inp.disabled = locked || !part;
  inp.setAttribute("aria-label", `Speed % of ${item.ref}`);
  inp.addEventListener("change", () => {
    const v = parseInt(inp.value, 10);
    if (!(v >= 10 && v <= 100)) { showError(`Speed of ${item.ref} must be 10–100 %`); return; }
    savePart(item.ref, { speed: v });
  });
  return tdWith(inp);
}

// Height (mm) of a part, held by its top. Empty = no own height: the package's, else the default.
function heightCell(item, part, locked) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.min = "0";
  inp.max = "30";
  inp.step = "0.1";
  inp.className = "num part-coord part-height";
  inp.value = part && part.height != null ? String(part.height) : "";
  inp.placeholder = fmt(item.height, 2);
  inp.disabled = locked || !part;
  inp.setAttribute("aria-label", `Height of ${item.ref}`);
  inp.addEventListener("change", () => {
    if (inp.value.trim() === "") { savePart(item.ref, { height_auto: true }); return; }
    const v = parseFloat(inp.value);
    if (!(v >= 0 && v <= 30)) { showError(`Height of ${item.ref} must be 0–30 mm`); return; }
    savePart(item.ref, { height: v });
  });
  return tdWith(inp);
}

// Job speed level (Run card), stored as motion.job_speed_level in machine.yaml.
async function setJobSpeed(level) {
  await jobCall(`Job speed ${(level + 1) * 10} %`, "/api/config/defaults",
                { changes: { motion: { job_speed_level: level } } }, "PUT");
}

function renderJobSpeed() {
  const sel = $("#job-speed");
  const lv = state && state.speed ? state.speed.job_level : null;
  if (lv != null && document.activeElement !== sel) sel.value = String(lv);
  sel.disabled = jobBusy || runnerBusy();
}

// Angle as -180..180 (what the nozzle turns to; it takes the shortest way).
function fmtAngle(a) {
  const w = ((a + 180) % 360 + 360) % 360 - 180;
  return (Math.round(w * 10) / 10).toString();
}

// A part switched off (JobPart.skip) is kept in the job but not placed.
const partOff = (part) => !!part && !!part.skip;

function statusChip(status) {
  if (status === "placed") return elem("span", "chip ok", "Placed");
  if (status === "failed") return elem("span", "chip bad", "Failed");
  if (status === "skipped") return elem("span", "chip", "Skipped");
  return elem("span", "badge accent", "To do");
}

function tdWith(node) {
  const td = elem("td");
  td.append(node);
  return td;
}

function renderParts(job, plan) {
  for (const b of $$("#job-filter button")) b.classList.toggle("sel", b.dataset.filter === jobFilter);
  const rows = plan.filter(matchesFilter);
  const locked = jobBusy || runnerBusy();
  const ready = jobReady();
  const overrides = job ? job.parts.map((p) => [p.ref, p.skip, p.nozzle ?? null, p.feeder ?? null, p.vision ?? null,
                                                   p.x, p.y, p.rotation, p.speed, p.height ?? null]) : [];
  const sig = JSON.stringify([rows, jobFeederIds, overrides, locked, ready]);
  const tbody = $("#job-rows");
  if (sig === partsSig) return;
  // Keep an open select or a field being typed in: a rebuild would close or reset it. The next poll retries.
  if (tbody.contains(document.activeElement) && ["SELECT", "INPUT"].includes(document.activeElement.tagName)
      && document.activeElement.type !== "checkbox") return;
  partsSig = sig;
  const parts = new Map(job ? job.parts.map((p) => [p.ref, p]) : []);
  tbody.replaceChildren(...rows.map((item) => partRow(item, parts.get(item.ref), locked, ready)));
  $("#job-parts-empty").hidden = rows.length > 0;
}

function partRow(item, part, locked, ready) {
  const tr = elem("tr");
  tr.dataset.key = item.key;

  const nozOverride = !!part && part.nozzle != null;
  const noz = makeSelect([
    { value: "auto", label: nozOverride ? "Auto" : `Auto (N${item.nozzle})` },
    { value: 1, label: "N1 thick" },
    { value: 2, label: "N2 thin" },
  ], nozOverride ? part.nozzle : "auto");
  noz.disabled = locked;
  noz.setAttribute("aria-label", `Nozzle for ${item.ref}`);
  noz.addEventListener("change", () => savePart(item.ref, { nozzle: noz.value === "auto" ? null : Number(noz.value) }));

  const feedOverride = !!part && part.feeder != null;
  const feed = makeSelect([
    { value: "auto", label: feedOverride ? "Auto" : `Auto (${item.feeder || "none"})` },
    ...jobFeederIds.map((id) => ({ value: id, label: id })),
  ], feedOverride ? part.feeder : "auto");
  feed.disabled = locked;
  feed.setAttribute("aria-label", `Feeder for ${item.ref}`);
  feed.addEventListener("change", () => savePart(item.ref, { feeder: feed.value === "auto" ? null : feed.value }));

  const visOverride = !!part && part.vision != null;
  const vis = makeSelect([
    { value: "auto", label: visOverride ? "Auto" : `Auto (${item.vision ? "on" : "off"})` },
    { value: "on", label: "On" },
    { value: "off", label: "Off" },
  ], visOverride ? (part.vision ? "on" : "off") : "auto");
  vis.disabled = locked;
  vis.setAttribute("aria-label", `Vision for ${item.ref}`);
  vis.addEventListener("change", () => savePart(item.ref, { vision: vis.value === "auto" ? null : vis.value === "on" }));

  const actions = elem("div", "row-actions");
  const go = buttonEl("", "btn btn-ghost btn-icon", "i-crosshair");
  go.title = "Go to";
  go.setAttribute("aria-label", `Go to ${item.ref}`);
  go.disabled = locked || !ready || item.x == null;
  go.addEventListener("click", () => gotoPlacement(item.ref, item.key));

  const off = partOff(part);
  const bottom = !!part && part.side === "bottom";
  const on = document.createElement("input");
  on.type = "checkbox";
  on.className = "part-on";
  on.checked = !off && !bottom;
  on.disabled = locked || bottom;
  on.title = bottom ? "Bottom-side part: never placed" : (off ? "Off: not placed. Click to switch on." : "On: placed in runs. Click to switch off.");
  on.setAttribute("aria-label", `Place ${item.ref}`);
  on.addEventListener("change", () => savePart(item.ref, { skip: !on.checked }));
  tr.classList.toggle("part-off", off || bottom);

  const remove = buttonEl("", "btn btn-ghost btn-icon", "i-trash-2");
  remove.title = "Remove from the job";
  remove.setAttribute("aria-label", `Remove ${item.ref} from the job`);
  remove.disabled = locked;
  remove.addEventListener("click", () => removePart(item.ref));
  actions.append(go, remove);

  if (item.status === "failed" || item.status === "placed") {
    const todo = buttonEl("Mark to do", "btn btn-ghost btn-small");
    todo.disabled = locked;
    todo.addEventListener("click", () => jobCall(`Mark ${item.ref} to do`,
      `/api/job/placements/${enc(item.key)}/status`, { status: "pending" }));
    actions.append(todo);
  }

  // Placement in the file (board mm / deg): editable, applies to the part on every board.
  const coord = (name, label, value, digits) => {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.step = "any";
    inp.className = "num part-coord";
    inp.value = part ? Number(value).toFixed(digits).replace(/\.?0+$/, "") : "";
    inp.disabled = locked || !part;
    inp.setAttribute("aria-label", `${label} of ${item.ref}`);
    inp.addEventListener("change", () => {
      const v = parseFloat(inp.value);
      if (!Number.isFinite(v)) { showError(`${label} of ${item.ref} must be a number`); return; }
      savePart(item.ref, { [name]: v });
    });
    return tdWith(inp);
  };
  let placeAt = item.x == null
    ? "–"
    : `${item.x.toFixed(2)}, ${item.y.toFixed(2)}` + (item.nozzle_angle == null ? "" : ` · ${fmtAngle(item.nozzle_angle)}°`);
  if (item.place_depth != null) placeAt += ` · depth ${item.place_depth.toFixed(2)}`;

  tr.append(
    tdWith(on),
    cell(item.board + 1, "mono"),
    cell(item.ref, "mono"),
    cell(item.value),
    cell(item.package),
    coord("x", "X", part ? part.x : 0, 3),
    coord("y", "Y", part ? part.y : 0, 3),
    coord("rotation", "Rotation", part ? part.rotation : 0, 2),
    heightCell(item, part, locked),
    cell(placeAt, "mono place-at"),
    speedCell(item, part, locked),
    tdWith(noz),
    tdWith(feed),
    tdWith(vis),
    tdWith(statusChip(item.status)),
    cell(item.problem || "", "problem"),
    tdWith(actions),
  );
  return tr;
}

// ------------------------------------------------------------------ fiducials

const fiducialsActive = (job) => !!job && job.fiducial_mode !== "off" && job.fiducials.length > 0;

function renderFiducials(job) {
  const locked = jobBusy || runnerBusy();
  const ready = jobReady() && !!job && !!job.origin;
  const fids = job ? job.fiducials : [];
  const mode = job ? job.fiducial_mode : "off";
  for (const b of $$("#fid-mode button")) b.classList.toggle("sel", b.dataset.mode === mode);
  const dia = $("#fid-diameter");
  if (document.activeElement !== dia) dia.value = job ? round3(job.fiducial_diameter) : "";
  $("#fid-table").hidden = fids.length === 0;
  renderFiducialRows(job, locked, ready);
  renderFiducialResult(job);
  updateFiducialControls(job);
}

function renderFiducialRows(job, locked, ready) {
  const fids = job ? job.fiducials : [];
  const sig = JSON.stringify([fids, locked, ready]);
  const tbody = $("#fid-rows");
  if (sig === fidRowsSig || tbody.contains(document.activeElement)) return;
  fidRowsSig = sig;
  tbody.replaceChildren(...fids.map((f, i) => fiducialRow(f, i, locked, ready)));
}

function fidInput(type, value, label, cls = "") {
  const input = elem("input", cls);
  input.type = type;
  if (type === "number") input.step = "any";
  input.value = value;
  input.setAttribute("aria-label", label);
  return input;
}

function fiducialRow(f, i, locked, ready) {
  const tr = elem("tr");
  const nameIn = fidInput("text", f.name, `Name of fiducial ${i + 1}`);
  const xIn = fidInput("number", round3(f.x), `X of ${f.name}`, "num");
  const yIn = fidInput("number", round3(f.y), `Y of ${f.name}`, "num");
  for (const input of [nameIn, xIn, yIn]) input.disabled = locked;
  nameIn.addEventListener("change", () => commitFiducial(i, "name", nameIn));
  xIn.addEventListener("change", () => commitFiducial(i, "x", xIn));
  yIn.addEventListener("change", () => commitFiducial(i, "y", yIn));

  const go = buttonEl("", "btn btn-ghost btn-icon", "i-crosshair");
  go.title = "Go to";
  go.setAttribute("aria-label", `Go to ${f.name}`);
  go.disabled = locked || !ready;
  go.addEventListener("click", () => jobCall(`Go to ${f.name}`,
    `/api/job/fiducials/${enc(f.name)}/goto`, { board: 0 }));

  const del = buttonEl("", "btn btn-ghost btn-icon", "i-trash-2");
  del.title = "Remove";
  del.setAttribute("aria-label", `Remove ${f.name}`);
  del.disabled = locked;
  del.addEventListener("click", () => removeFiducial(i));

  const actions = elem("div", "row-actions");
  actions.append(go, del);
  tr.append(tdWith(nameIn), tdWith(xIn), tdWith(yIn), tdWith(actions));
  return tr;
}

function renderFiducialResult(job) {
  const active = fiducialsActive(job);
  const fit = job ? job.fit : null;
  const fids = job ? job.fiducials : [];

  let status = null;
  if (!job) status = "Import a job to set up fiducials.";
  else if (!fids.length) status = "No fiducials. Add them here or include FID parts in the KiCad file.";
  else if (job.fiducial_mode === "off") status = "Fiducials are off. The board origin and rotation are used as set.";
  const st = $("#fid-status");
  st.hidden = !status;
  st.textContent = status || "";

  $("#fid-measure-label").textContent = fidMeasuring ? "Measuring…" : "Measure fiducials";
  $("#fid-head").hidden = !active;
  $("#fid-missing").hidden = !active || !!fit;
  $("#fid-detail").hidden = !(active && fit);
  if (!active) return;

  const badge = $("#fid-badge");
  badge.className = `badge ${fit ? "success" : "warning"}`;
  badge.textContent = fit ? "Measured" : "Not measured";
  $("#fid-time").textContent = fit ? new Date(fit.measured_at).toLocaleString() : "";
  if (!fit) {
    fidPointsSig = "";
    return;
  }

  const pct = (fit.scale - 1) * 100;
  $("#fid-summary").textContent = `RMS ${fmt(fit.rms)} mm · worst ${fmt(fit.max_error)} mm · `
    + `rotation ${fmt(fit.rotation)}° · scale ${pct >= 0 ? "+" : ""}${fmt(pct, 2)} %`;
  $("#fid-origin").textContent = `Origin (${fmt(fit.origin[0])}, ${fmt(fit.origin[1])}) mm`;

  const sig = JSON.stringify(fit.points);
  if (sig !== fidPointsSig) {
    fidPointsSig = sig;
    $("#fid-points").replaceChildren(...fit.points.map((p) => {
      const tr = elem("tr");
      tr.append(cell(p.board + 1, "mono"), cell(p.name, "mono"), cell(fmt(p.error), "mono"));
      return tr;
    }));
  }
}

function updateFiducialControls(job) {
  const noJob = !job || jobBusy || runnerBusy();
  const ready = jobReady() && !!job && !!job.origin;
  for (const b of $$("#fid-mode button")) b.disabled = noJob;
  $("#fid-diameter").disabled = noJob;
  $("#fid-add").disabled = noJob;
  $("#fid-measure").disabled = noJob || !fiducialsActive(job) || !ready;
  $("#fid-clear").disabled = noJob || !(job && job.fit);
}

// Saves the whole fiducial list (and mode and diameter) with PUT /api/job/fiducials.
function putFiducials(label, fiducials, over = {}) {
  const job = jobData && jobData.job;
  if (!job) return null;
  const body = {
    mode: job.fiducial_mode,
    diameter: job.fiducial_diameter,
    fiducials: fiducials.map((f) => ({ name: f.name, x: f.x, y: f.y })),
    ...over,
  };
  return jobCall(label, "/api/job/fiducials", body, "PUT");
}

function setFiducialMode(mode) {
  const job = jobData && jobData.job;
  if (!job || mode === job.fiducial_mode) return;
  putFiducials(`Set fiducial mode to ${mode}`, job.fiducials, { mode });
}

async function commitDiameter() {
  const job = jobData && jobData.job;
  if (!job) return;
  const input = $("#fid-diameter");
  let d;
  try {
    d = parseNum(input.value.trim(), "Diameter");
    if (d <= 0) throw new Error("Diameter must be more than 0 mm");
  } catch (err) {
    reportError(err);
    input.value = round3(job.fiducial_diameter);
    return;
  }
  if (d === job.fiducial_diameter) return;
  await putFiducials("Set fiducial diameter", job.fiducials, { diameter: d });
}

// Commits one edited cell. Invalid input is reported and the cell goes back to the saved value.
async function commitFiducial(i, field, input) {
  const job = jobData && jobData.job;
  if (!job) return;
  const fids = job.fiducials;
  const cur = fids[i];
  let value;
  try {
    if (field === "name") {
      value = input.value.trim();
      if (!value) throw new Error("Fiducial name can't be empty");
      if (fids.some((f, j) => j !== i && f.name === value)) throw new Error(`Fiducial ${value} already exists`);
    } else {
      value = parseNum(input.value.trim(), field === "x" ? "Fiducial X" : "Fiducial Y");
    }
  } catch (err) {
    reportError(err);
    input.value = field === "name" ? cur.name : round3(cur[field]);
    return;
  }
  await putFiducials(`Update ${cur.name}`, fids.map((f, j) => (j === i ? { ...f, [field]: value } : f)));
}

// New fiducials go at the board origin (0, 0) with the next free FID name.
function addFiducial() {
  const job = jobData && jobData.job;
  if (!job) return;
  const names = new Set(job.fiducials.map((f) => f.name));
  let n = job.fiducials.length + 1;
  while (names.has(`FID${n}`)) n += 1;
  const name = `FID${n}`;
  putFiducials(`Add ${name}`, [...job.fiducials, { name, x: 0, y: 0 }]);
}

function removeFiducial(i) {
  const job = jobData && jobData.job;
  if (!job) return;
  const f = job.fiducials[i];
  putFiducials(`Remove ${f.name}`, job.fiducials.filter((_, j) => j !== i));
}

async function measureFiducials() {
  fidMeasuring = true;
  renderFiducialResult(jobData && jobData.job);
  let res = null;
  try {
    res = await jobCall("Measure fiducials", "/api/job/fiducials/measure");
  } finally {
    fidMeasuring = false;
    renderFiducialResult(jobData && jobData.job);
    updateJobButtons(jobData && jobData.job);
  }
  const fit = res && res.job ? res.job.fit : null;
  if (fit) {
    log(`Fiducials measured: RMS ${fmt(fit.rms)} mm, rotation ${fmt(fit.rotation, 2)}°, `
      + `origin (${fmt(fit.origin[0], 2)}, ${fmt(fit.origin[1], 2)})`);
  }
}

// ------------------------------------------------------------------ controls

function updateJobButtons(job) {
  const s = runStatus().state;
  const busy = jobBusy;
  const runner = runnerBusy();
  $("#job-start").disabled = busy || !job || !(s === "idle" || s === "done");
  $("#job-pause").disabled = busy || s !== "running";
  $("#job-resume").disabled = busy || !(s === "paused" || s === "error");
  $("#job-stop").disabled = busy || !(s === "running" || s === "stepping" || s === "paused" || s === "error");
  $("#job-step").disabled = busy || !job || ["running", "pausing", "stopping"].includes(s)
    || (s === "stepping" && !!(runStatus().step && runStatus().step.running));
  $("#job-reset").disabled = busy || runner || !job;
  $("#job-reset-nozzles").disabled = busy || runner;
  renderJobSpeed();
  $("#job-file").disabled = busy || runner;
  const file = $("#job-file").files[0];
  $("#job-import-opts").hidden = !(file && isJobFile(file.name));
  $("#job-reset-statuses").disabled = busy || runner;
  $("#job-import-btn").disabled = busy || runner || !file;
  $("#job-export").disabled = busy || !job;
  $("#job-delete").disabled = busy || runner || !job;
  $("#parts-enable-shown").disabled = busy || runner || !job;
  $("#parts-disable-shown").disabled = busy || runner || !job;

  // Board, panel and grid edits need a job and no run in progress.
  const noJob = busy || runner || !job;
  for (const el of $$("#job-board input, #job-save-board, #job-panel input, #job-grid, #job-boards input")) {
    el.disabled = noJob;
  }
  for (const el of $$("#ref-mark-size, .mark-part")) el.disabled = noJob;
  for (const el of $$(".mark-actions button")) el.disabled = noJob || !jobReady();
  $("#marks-copy").disabled = noJob || job.boards.length < 2;
  $("#marks-clear").disabled = noJob || !job.board_marks.some((e) => ["m1", "m2"].some((n) => e[n].mx != null));
  updateFiducialControls(job);
}

async function resetNozzles() {
  const held = runStatus().holding || {};
  const parts = [1, 2].filter((n) => held[n]).map((n) => `N${n} (${held[n]})`);
  const ok = await confirmDialog({
    title: "Reset the nozzles?",
    body: "The vacuum switches off on both nozzles and they go up; the head doesn't move. "
      + (parts.length ? `A part still on ${parts.join(" and ")} drops where the head is now. ` : "")
      + "Then the next pick can start again.",
    confirmLabel: "Reset nozzles",
  });
  if (!ok) return;
  await jobCall("Reset nozzles", "/api/head/reset", {});
}

async function removePart(ref) {
  const boards = jobData && jobData.job ? jobData.job.boards.length : 1;
  const ok = await confirmDialog({
    title: `Remove ${ref} from the job?`,
    body: `${ref} is removed${boards > 1 ? ` on all ${boards} boards` : ""}, with its placement status. `
      + "To leave it out only for now, switch it off instead. Import the position file again to get it back.",
    confirmLabel: "Remove part",
  });
  if (!ok) return;
  await jobCall(`Remove ${ref}`, `/api/job/parts/${enc(ref)}`, undefined, "DELETE");
}

// Enable or disable every part in the list as filtered now.
async function setShownParts(enabled) {
  const job = jobData && jobData.job;
  if (!job) return;
  const refs = [...new Set(((jobData.plan) || []).filter(matchesFilter).map((i) => i.ref))]
    .filter((ref) => { const p = currentPart(ref); return p && p.side !== "bottom" && partOff(p) === enabled; });
  if (!refs.length) return;
  await jobCall(`${enabled ? "Enable" : "Disable"} ${refs.length} part(s)`, "/api/job/parts/enable", { refs, enabled });
}

async function savePart(ref, changes) {
  const part = currentPart(ref);
  if (!part) return;
  const body = {
    skip: !!part.skip, nozzle: part.nozzle ?? null, feeder: part.feeder ?? null, vision: part.vision ?? null, ...changes,
  };
  await jobCall(`Update ${ref}`, `/api/job/parts/${enc(ref)}`, body, "PUT");
}

function savePanel(boards) {
  return jobCall("Update panel", "/api/job/panel", {
    boards: boards.map((b) => ({ x: b.x, y: b.y, rotation: b.rotation, skip: !!b.skip })),
  }, "PUT");
}

async function gotoPlacement(ref, key) {
  await jobCall(`Go to ${ref}`, `/api/job/placements/${enc(key)}/goto`);
}

function boardBody() {
  const ox = $("#job-ox").value.trim();
  const oy = $("#job-oy").value.trim();
  const origin = ox === "" && oy === "" ? null : [parseNum(ox, "Origin X"), parseNum(oy, "Origin Y")];
  return { origin, rotation: parseNum($("#job-rot").value.trim(), "Rotation") };
}

async function savePcbThickness() {
  const v = parseFloat($("#job-pcb").value);
  if (!(v > 0 && v <= 10)) { showError("PCB thickness must be above 0 and at most 10 mm"); return; }
  await jobCall("Set PCB thickness", "/api/job/pcb-thickness", { pcb_thickness: v }, "PUT");
}

async function saveBoard() {
  let body;
  try {
    body = boardBody();
  } catch (err) {
    reportError(err);
    return;
  }
  if (await jobCall("Save board", "/api/job/board", body, "PUT")) boardDirty = false;
}


async function makeGrid() {
  let body;
  try {
    body = {
      rows: Math.trunc(parseNum($("#job-grid-rows").value, "Rows")),
      cols: Math.trunc(parseNum($("#job-grid-cols").value, "Columns")),
      step_x: parseNum($("#job-grid-sx").value, "Step X"),
      step_y: parseNum($("#job-grid-sy").value, "Step Y"),
    };
  } catch (err) {
    reportError(err);
    return;
  }
  await jobCall("Make panel grid", "/api/job/panel/grid", body);
}

async function importJob() {
  const input = $("#job-file");
  const file = input.files[0];
  if (!file) return;
  const current = jobData && jobData.job;
  if (current) {
    const ok = await confirmDialog({
      title: `Replace job '${current.name}'?`,
      body: "Importing replaces the current job, including the placement status of every part. This can't be undone.",
      confirmLabel: "Replace job",
    });
    if (!ok) return;
  }
  let text;
  try {
    text = await file.text();
  } catch (err) {
    reportError(err);
    return;
  }
  let res;
  if (isJobFile(file.name)) {
    res = await jobCall(`Import ${file.name}`, "/api/job/import-file",
      { text, reset_statuses: $("#job-reset-statuses").checked });
  } else {
    const name = file.name.replace(/\.[^.]+$/, "");
    res = await jobCall(`Import ${file.name}`, "/api/job/import", { name, filename: file.name, text });
  }
  if (res) {
    input.value = "";
    boardDirty = false;
  }
}

// A job file exported from this app (.yaml) rather than a KiCad position file.
function isJobFile(name) {
  return /\.ya?ml$/i.test(name);
}

// Export the current job: a plain link download, the server names the file job-<name>-<date>.yaml.
function exportJob() {
  const a = document.createElement("a");
  a.href = "/api/job/export";
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
  log("job exported");
}

async function deleteJob() {
  const job = jobData && jobData.job;
  if (!job) return;
  const ok = await confirmDialog({
    title: `Delete job '${job.name}'?`,
    body: "The parts, board setup and placement status are removed. This can't be undone.",
    confirmLabel: "Delete job",
  });
  if (!ok) return;
  await jobCall(`Delete job ${job.name}`, "/api/job", undefined, "DELETE");
}

async function resetStatuses() {
  const ok = await confirmDialog({
    title: "Reset all placement statuses?",
    body: "Placed and failed parts go back to to do. Skipped parts stay skipped.",
    confirmLabel: "Reset statuses",
  });
  if (!ok) return;
  await jobCall("Reset statuses", "/api/job/reset");
}

function initJob() {
  $("#job-file").addEventListener("change", () => updateJobButtons(jobData && jobData.job));
  $("#job-import-btn").addEventListener("click", importJob);
  $("#job-export").addEventListener("click", exportJob);
  $("#job-delete").addEventListener("click", deleteJob);
  $("#job-start").addEventListener("click", () => jobCall("Start job", "/api/job/start"));
  $("#job-pause").addEventListener("click", () => jobCall("Pause job", "/api/job/pause"));
  $("#job-resume").addEventListener("click", () => jobCall("Resume job", "/api/job/resume"));
  $("#job-stop").addEventListener("click", () => jobCall("Stop job", "/api/job/stop"));
  $("#job-step").addEventListener("click", () => jobCall("Job step", "/api/job/step"));
  $("#job-reset").addEventListener("click", resetStatuses);

  for (const id of ["#job-ox", "#job-oy", "#job-rot"]) {
    $(id).addEventListener("input", () => {
      boardDirty = true;
    });
  }
  $("#job-save-board").addEventListener("click", saveBoard);
  $("#job-pcb").addEventListener("change", savePcbThickness);
  $("#marks-copy").addEventListener("click", copyMarksToAll);
  $("#marks-clear").addEventListener("click", clearMarks);
  const markRows = $("#mark-rows");
  markRows.addEventListener("change", (e) => {
    const b = Number(e.target.closest("tr").dataset.board);
    if (e.target.matches("select.mark-part")) markPartChanged(b, e.target.dataset.mark, e.target);
    else saveMarkField(b, e.target.dataset.mark, e.target.dataset.field, e.target);
  });
  markRows.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (btn) captureMark(Number(btn.closest("tr").dataset.board), btn.dataset.mark, btn.dataset.action === "centre");
  });
  $("#job-grid").addEventListener("click", makeGrid);

  for (const b of $$("#fid-mode button")) b.addEventListener("click", () => setFiducialMode(b.dataset.mode));
  $("#fid-diameter").addEventListener("change", commitDiameter);
  $("#fid-add").addEventListener("click", addFiducial);
  $("#fid-measure").addEventListener("click", measureFiducials);
  $("#fid-clear").addEventListener("click", () => jobCall("Clear measurement", "/api/job/fiducials/clear"));

  for (const b of $$("#job-filter button")) {
    b.addEventListener("click", () => {
      jobFilter = b.dataset.filter;
      renderParts(jobData && jobData.job, (jobData && jobData.plan) || []);
    });
  }
  $("#job-reset-nozzles").addEventListener("click", resetNozzles);
  $("#job-speed").addEventListener("change", (e) => setJobSpeed(parseInt(e.target.value, 10)));
  $("#parts-enable-shown").addEventListener("click", () => setShownParts(true));
  $("#parts-disable-shown").addEventListener("click", () => setShownParts(false));
  updateJobButtons(null);
}

initJob();
