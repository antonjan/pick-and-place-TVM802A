"use strict";

// Job tab: import, board and panel setup, the parts list and the run controls.
// Polls GET /api/job every 500 ms while the tab is on screen. Loaded after app.js.

const RUN_BUSY = ["running", "pausing", "stopping"];
const RUN_LABEL = {
  idle: "Idle", running: "Running", pausing: "Pausing", paused: "Paused",
  stopping: "Stopping", done: "Done", error: "Error",
};
const RUN_BADGE = { running: "accent", paused: "warning", error: "danger", done: "success" };

let jobData = null;        // last GET /api/job
let jobFeederIds = [];     // feeder ids for the Feeder selects
let jobFilter = "all";     // all | todo | problem
let jobBusy = false;       // a job request is running
let jobFetching = false;   // a GET /api/job is in flight
let jobTimer = null;
let boardDirty = false;    // board fields were edited and not saved yet
let partsSig = "";
let boardRowsSig = "";
let refsSig = "";
// Two-point alignment: point 1 (origin) and point 2, each a board X/Y and a machine
// X/Y (captured from the camera, editable).
let refsLoadedFor = null;  // job name whose saved reference points were put in the fields

function refRow(k) {
  return document.querySelector(`.ref-row[data-refpt="${k}"]`);
}

function refMachine(k) {
  const row = refRow(k);
  const x = parseFloat(row.querySelector(".ref-mx").value);
  const y = parseFloat(row.querySelector(".ref-my").value);
  return Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null;
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
  renderRefPoints(job);
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
  if (boardDirty) return;
  $("#job-ox").value = job && job.origin ? round3(job.origin[0]) : "";
  $("#job-oy").value = job && job.origin ? round3(job.origin[1]) : "";
  $("#job-rot").value = job ? round3(job.rotation) : "";
}

function renderRefPoints(job) {
  const parts = job ? job.parts : [];
  const refs = [...new Set(parts.map((p) => p.ref))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const sig = JSON.stringify([refs, job ? job.boards.length : 0]);
  if (sig !== refsSig) {
    refsSig = sig;
    for (const row of $$(".ref-row")) {
      const sel = row.querySelector(".ref-part");
      const prev = sel.value;
      sel.replaceChildren(optionEl("", "Custom"), ...refs.map((r) => optionEl(r, r)));
      if (refs.includes(prev)) sel.value = prev;
    }
    const bsel = $("#ref-board");
    const prevB = bsel.value;
    const n = job ? job.boards.length : 1;
    bsel.replaceChildren(...Array.from({ length: n }, (_, k) => optionEl(String(k), `Board ${k + 1}`)));
    if (prevB !== "" && Number(prevB) < n) bsel.value = prevB;
  }
  // Put the job's saved reference points in the fields once per job.
  if (job && refsLoadedFor !== job.name) {
    refsLoadedFor = job.name;
    const saved = (job.references || []).slice(0, 2);
    for (const [k, r] of [["a", saved[0]], ["b", saved[1]]]) {
      const row = refRow(k);
      // Point 1 defaults to the board origin (0, 0); point 2 starts empty.
      const def = k === "a" ? { ref: null, x: 0, y: 0 } : { ref: null, x: "", y: "" };
      const p = r || def;
      row.querySelector(".ref-part").value = p.ref && refs.includes(p.ref) ? p.ref : "";
      row.querySelector(".ref-x").value = p.x;
      row.querySelector(".ref-y").value = p.y;
      row.querySelector(".ref-mx").value = "";  // machine positions are captured fresh each time
      row.querySelector(".ref-my").value = "";
    }
    if (job.reference_board != null && job.reference_board < (job.boards || []).length) {
      $("#ref-board").value = String(job.reference_board);
    }
  }
}

function refPartChanged(row) {
  const ref = row.querySelector(".ref-part").value;
  const part = ref && jobData && jobData.job ? jobData.job.parts.find((p) => p.ref === ref) : null;
  if (part) {
    row.querySelector(".ref-x").value = part.x;
    row.querySelector(".ref-y").value = part.y;
  }
}

function recordRef(row) {
  if (!state) return;
  row.querySelector(".ref-mx").value = state.position.x.toFixed(3);
  row.querySelector(".ref-my").value = state.position.y.toFixed(3);
  updateJobButtons(jobData && jobData.job);
  const n = row.dataset.refpt === "a" ? "1" : "2";
  log(`point ${n}: camera at X ${state.position.x.toFixed(3)}, Y ${state.position.y.toFixed(3)}`);
}

async function applyTwoPoints() {
  const pts = {};
  for (const row of $$(".ref-row")) {
    const k = row.dataset.refpt;
    const name = k === "a" ? "Point 1" : "Point 2";
    const x = parseFloat(row.querySelector(".ref-x").value);
    const y = parseFloat(row.querySelector(".ref-y").value);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      showError(`${name}: enter its board X and Y (or pick a part).`);
      return;
    }
    const m = refMachine(k);
    if (!m) {
      showError(`${name}: centre the camera on it and press Capture camera (or type the machine X and Y).`);
      return;
    }
    pts[k] = { ref: row.querySelector(".ref-part").value || null, x, y, world_x: m[0], world_y: m[1] };
  }
  const res = await jobCall("Set board from points 1 and 2", "/api/job/align/two-points",
                            { board: Number($("#ref-board").value || 0), a: pts.a, b: pts.b });
  if (res && res.alignment) {
    boardDirty = false;
    const al = res.alignment;
    const err = al.distance_error;
    const out = $("#ref-result");
    out.hidden = false;
    out.textContent = `Origin (${al.origin[0].toFixed(3)}, ${al.origin[1].toFixed(3)}) · rotation ${al.rotation.toFixed(3)}° · ` +
      `point 1 to 2: ${al.board_distance.toFixed(3)} mm on the board, ${al.measured_distance.toFixed(3)} mm measured ` +
      `(${err >= 0 ? "+" : ""}${err.toFixed(3)} mm)` +
      (Math.abs(err) > 0.3 ? " — large difference: check you centred on the right features" : "");
    log(`board aligned: rotation ${al.rotation.toFixed(3)}°, distance check ${err >= 0 ? "+" : ""}${err.toFixed(3)} mm`,
        Math.abs(err) > 0.3);
  }
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
  return true;
}

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
  const overrides = job ? job.parts.map((p) => [p.ref, p.skip, p.nozzle ?? null, p.feeder ?? null, p.vision ?? null]) : [];
  const sig = JSON.stringify([rows, jobFeederIds, overrides, locked, ready]);
  const tbody = $("#job-rows");
  if (sig === partsSig) return;
  // Keep an open or focused select: a rebuild would close it. The next poll retries.
  if (tbody.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
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

  const skipped = !!part && !!part.skip;
  const skip = buttonEl(skipped ? "Unskip" : "Skip", "btn btn-ghost btn-small");
  skip.disabled = locked;
  skip.addEventListener("click", () => savePart(item.ref, { skip: !skipped }));
  actions.append(go, skip);

  if (item.status === "failed" || item.status === "placed") {
    const todo = buttonEl("Mark to do", "btn btn-ghost btn-small");
    todo.disabled = locked;
    todo.addEventListener("click", () => jobCall(`Mark ${item.ref} to do`,
      `/api/job/placements/${enc(item.key)}/status`, { status: "pending" }));
    actions.append(todo);
  }

  tr.append(
    cell(item.board + 1, "mono"),
    cell(item.ref, "mono"),
    cell(item.value),
    cell(item.package),
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
  $("#job-stop").disabled = busy || !(s === "running" || s === "paused" || s === "error");
  $("#job-reset").disabled = busy || runner || !job;
  $("#job-file").disabled = busy || runner;
  $("#job-import-btn").disabled = busy || runner || !$("#job-file").files.length;
  $("#job-delete").disabled = busy || runner || !job;

  // Board, panel and grid edits need a job and no run in progress.
  const noJob = busy || runner || !job;
  for (const el of $$("#job-board input, #job-save-board, #job-panel input, #job-grid, #job-boards input")) {
    el.disabled = noJob;
  }
  for (const el of $$(".ref-row select, #ref-board")) el.disabled = noJob;
  for (const el of $$(".ref-record")) el.disabled = noJob || !jobReady();
  $("#ref-apply").disabled = noJob || !jobReady() || !refMachine("a") || !refMachine("b");
  updateFiducialControls(job);
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
  const name = file.name.replace(/\.[^.]+$/, "");
  const res = await jobCall(`Import ${file.name}`, "/api/job/import", { name, filename: file.name, text });
  if (res) {
    input.value = "";
    boardDirty = false;
  }
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
  $("#job-delete").addEventListener("click", deleteJob);
  $("#job-start").addEventListener("click", () => jobCall("Start job", "/api/job/start"));
  $("#job-pause").addEventListener("click", () => jobCall("Pause job", "/api/job/pause"));
  $("#job-resume").addEventListener("click", () => jobCall("Resume job", "/api/job/resume"));
  $("#job-stop").addEventListener("click", () => jobCall("Stop job", "/api/job/stop"));
  $("#job-reset").addEventListener("click", resetStatuses);

  for (const id of ["#job-ox", "#job-oy", "#job-rot"]) {
    $(id).addEventListener("input", () => {
      boardDirty = true;
    });
  }
  $("#job-save-board").addEventListener("click", saveBoard);
  for (const row of $$(".ref-row")) {
    row.querySelector(".ref-part").addEventListener("change", () => refPartChanged(row));
    row.querySelector(".ref-record").addEventListener("click", () => recordRef(row));
    for (const f of row.querySelectorAll(".ref-mx, .ref-my")) {
      f.addEventListener("input", () => updateJobButtons(jobData && jobData.job));
    }
  }
  $("#ref-apply").addEventListener("click", applyTwoPoints);
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
  updateJobButtons(null);
}

initJob();
