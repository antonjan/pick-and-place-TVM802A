"use strict";

// Feeders tab, Banks card: lists banks and their layouts, generates lanes per
// bank, and wraps the /api/banks routes. Loaded after app.js.

const BANK_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const GEN_DIRS = [["1,0", "+X"], ["-1,0", "−X"], ["0,1", "+Y"], ["0,-1", "−Y"]];
const GEN_WIDTHS = [8, 12, 16, 24, 32, 44];
const GEN_PITCHES = [2, 4, 8, 12, 16];

let banks = [];                 // last GET /api/banks
let banksSig = "";
let bankWork = false;           // a bank request is running
const genStates = new Map();    // bank id -> "Generate lanes" form state (kept in memory)
const openGen = new Set();      // bank ids whose "Generate lanes" section is open

const genKey = (id) => `bank-gen:${id}`;
const bankPath = (id, rest = "") => `/api/banks/${enc(id)}${rest}`;
const parseVec = (s) => s.split(",").map(Number);

// ------------------------------------------------------------------ list

async function refreshBanks() {
  let list;
  try {
    list = await getJson("/api/banks");
  } catch (_) {
    return; /* server lost: keep the last list */
  }
  const sig = JSON.stringify(list);
  if (sig === banksSig) return;
  banksSig = sig;
  banks = list;
  renderBanks();
}

function renderBanks() {
  $("#bank-empty").hidden = banks.length > 0;
  $("#bank-list").replaceChildren(...banks.map(bankCard));
  updateBankButtons();
}

function updateBankButtons() {
  const locked = bankWork || !!(headState && headState.busy);
  $("#bank-add").disabled = locked;
  for (const b of $$("#bank-list [data-bank-action]")) {
    const sel = b.closest(".bank").querySelector(".layout-select");
    const needsLayout = b.dataset.bankAction === "load" || b.dataset.bankAction === "delete-layout";
    b.disabled = locked || (needsLayout && sel.dataset.empty === "1");
  }
  for (const s of $$("#bank-list .layout-select")) s.disabled = locked || s.dataset.empty === "1";
  for (const el of $$("#bank-list .gen-form input, #bank-list .gen-form select")) el.disabled = locked;
  for (const el of $$("#bank-list [data-gen-camera]")) el.disabled = locked || !state;
}

function bankCard(b) {
  const art = elem("article", "bank");
  const lanes = b.lanes || [];

  const head = elem("div", "bank-head");
  const title = elem("div", "bank-title");
  title.append(
    elem("strong", "", b.name || b.id),
    elem("span", "bank-id", b.id),
    elem("span", "bank-meta", `${lanes.length} lane${lanes.length === 1 ? "" : "s"}`),
  );
  head.append(title, b.active_layout
    ? elem("span", "badge accent", `Layout ${b.active_layout}`)
    : elem("span", "badge", "No layout loaded"));
  head.append(actionBtn("Delete bank", "delete-bank", "btn btn-ghost btn-destructive", "i-trash-2",
    () => deleteBank(b)));
  art.append(head, layoutBar(b));

  const details = document.createElement("details");
  details.className = "bank-gen";
  details.open = openGen.has(b.id);
  details.addEventListener("toggle", () => (details.open ? openGen.add(b.id) : openGen.delete(b.id)));
  details.append(elem("summary", "", "Generate lanes"));

  if (!genStates.has(b.id)) genStates.set(b.id, initGenState(b));
  const holder = elem("div");
  holder.innerHTML = genFormHtml();
  const form = holder.firstElementChild;
  fillGenForm(b.id, form);
  form.addEventListener("change", () => syncGenForm(b.id, form));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    submitGen(b.id, form);
  });
  form.querySelector("[data-gen-camera]").addEventListener("click", () => useCamera(b.id, form));
  details.append(form);
  art.append(details);
  return art;
}

function actionBtn(label, action, className, icon, onClick) {
  const btn = buttonEl(label, className, icon);
  btn.dataset.bankAction = action;
  btn.addEventListener("click", onClick);
  return btn;
}

function layoutBar(b) {
  const names = Object.keys(b.layouts || {});
  const sel = names.length
    ? makeSelect(names.map((n) => ({ value: n, label: n })), b.active_layout || names[0])
    : makeSelect([{ value: "", label: "No saved layouts" }], "");
  sel.classList.add("layout-select");
  sel.setAttribute("aria-label", "Saved layout");
  sel.dataset.empty = names.length ? "0" : "1";
  if (!names.length) sel.options[0].disabled = true;
  const bar = elem("div", "actions");
  bar.append(
    sel,
    actionBtn("Load layout", "load", "btn", null, () => loadLayout(b.id, sel.value)),
    actionBtn("Save layout", "save-layout", "btn", "i-save", () => saveLayout(b.id)),
    actionBtn("Delete layout", "delete-layout", "btn btn-ghost btn-destructive", "i-trash-2",
      () => deleteLayout(b.id, sel.value)),
  );
  return bar;
}

// ------------------------------------------------------------------ bank and layout actions

// Runs one bank request with the controls locked, then refreshes banks and the feeder table.
async function bankAction(label, fn) {
  if (bankWork) return null;
  bankWork = true;
  updateBankButtons();
  log(`${label}…`);
  let res = null;
  try {
    res = await fn();
    log(`${label}: done`);
  } catch (_) {
    /* api() has logged the error and shown the banner */
  } finally {
    bankWork = false;
    await Promise.all([refreshBanks(), refreshFeeders()]);
    updateBankButtons();
  }
  return res;
}

async function addBank() {
  const v = await promptDialog({
    title: "Add bank",
    confirmLabel: "Add bank",
    fields: [
      { name: "id", label: "Bank id", placeholder: "e.g. back" },
      { name: "name", label: "Name", placeholder: "e.g. Back stack" },
    ],
    validate: (vals) => {
      if (!BANK_ID_RE.test(vals.id)) {
        return "Use 1 to 32 lower-case letters, digits, - or _, starting with a letter or digit.";
      }
      if (banks.some((b) => b.id === vals.id)) return `A bank called '${vals.id}' already exists.`;
      return null;
    },
  });
  if (!v) return;
  await bankAction(`Add bank ${v.id}`, () => api(bankPath(v.id), { name: v.name }, "PUT"));
}

async function deleteBank(b) {
  const ok = await confirmDialog({
    title: `Delete bank '${b.name || b.id}'?`,
    body: "Its lanes and saved layouts are removed, and so are the parts loaded in those lanes. This can't be undone.",
    confirmLabel: "Delete bank",
  });
  if (!ok) return;
  genStates.delete(b.id);
  openGen.delete(b.id);
  try { localStorage.removeItem(genKey(b.id)); } catch (_) { /* storage blocked */ }
  await bankAction(`Delete bank ${b.id}`, () => api(bankPath(b.id), undefined, "DELETE"));
}

async function loadLayout(id, name) {
  if (!name) return;
  await bankAction(`Load layout ${name}`, () => api(bankPath(id, `/layouts/${enc(name)}/load`), {}));
}

async function saveLayout(id) {
  const v = await promptDialog({
    title: "Save layout",
    confirmLabel: "Save layout",
    fields: [{ name: "name", label: "Layout name", placeholder: "e.g. Passives" }],
    validate: (vals) => {
      if (vals.name === "") return "Enter a name for the layout.";
      if (vals.name.length > 40) return "Use 40 characters or fewer.";
      return null;
    },
  });
  if (!v) return;
  const bank = banks.find((x) => x.id === id);
  if (bank && Object.keys(bank.layouts || {}).includes(v.name)) {
    const ok = await confirmDialog({
      title: `Replace layout '${v.name}'?`,
      body: "The lanes saved under that name are overwritten with the lanes now on this bank.",
      confirmLabel: "Replace layout",
    });
    if (!ok) return;
  }
  await bankAction(`Save layout ${v.name}`, () => api(bankPath(id, "/layouts"), { name: v.name }));
}

async function deleteLayout(id, name) {
  if (!name) return;
  const ok = await confirmDialog({
    title: `Delete layout '${name}'?`,
    body: "The saved lane setup is removed from this bank. Lanes already loaded stay as they are. This can't be undone.",
    confirmLabel: "Delete layout",
  });
  if (!ok) return;
  await bankAction(`Delete layout ${name}`, () => api(bankPath(id, `/layouts/${enc(name)}`), undefined, "DELETE"));
}

// Text prompt in the #prompt dialog. Resolves to {field: value} (trimmed), or null when cancelled.
function promptDialog({ title, fields, confirmLabel, validate }) {
  const dlg = $("#prompt");
  const form = elem("form", "dialog-form");
  form.noValidate = true;
  form.append(elem("h2", "dialog-title", title));
  const inputs = {};
  for (const f of fields) {
    const input = elem("input");
    input.name = f.name;
    input.autocomplete = "off";
    input.placeholder = f.placeholder || "";
    inputs[f.name] = input;
    form.append(elem("label", "field", f.label), input);
  }
  const err = elem("p", "dialog-error");
  err.hidden = true;
  const cancel = buttonEl("Cancel", "btn btn-ghost");
  const ok = buttonEl(confirmLabel, "btn");
  ok.type = "submit";
  const actions = elem("div", "dialog-actions");
  actions.append(cancel, ok);
  form.append(err, actions);
  dlg.replaceChildren(form);

  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      if (dlg.open) dlg.close();
      resolve(value);
    };
    dlg.addEventListener("close", () => finish(null), { once: true });
    cancel.addEventListener("click", () => finish(null));
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const values = {};
      for (const f of fields) values[f.name] = inputs[f.name].value.trim();
      const msg = validate ? validate(values) : null;
      if (msg) {
        err.textContent = msg;
        err.hidden = false;
        return;
      }
      finish(values);
    });
    dlg.showModal();
    inputs[fields[0].name].focus();
  });
}

// ------------------------------------------------------------------ generate lanes

function genFormHtml() {
  const dirOpts = GEN_DIRS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  const widthOpts = GEN_WIDTHS.map((w) => `<option value="${w}">${w} mm</option>`).join("");
  const pitchOpts = GEN_PITCHES.map((p) => `<option value="${p}">${p} mm</option>`).join("");
  return `
    <form class="gen-form">
      <div class="form-grid">
        <label>Origin X mm <input name="origin_x" class="num" type="number" step="any" placeholder="–"></label>
        <label>Origin Y mm <input name="origin_y" class="num" type="number" step="any" placeholder="–"></label>
        <label>Lane direction <select name="lane_axis">${dirOpts}</select></label>
        <label>Feed direction <select name="feed_direction">${dirOpts}</select></label>
        <label>Sprocket side <select name="sprocket_side"><option value="1">+ side</option><option value="-1">− side</option></select></label>
        <label>Gap mm <input name="gap" class="num" type="number" step="any" min="0"></label>
        <label>Pick depth mm <input name="pick_depth" class="num" type="number" step="any" min="0"></label>
        <label>Lanes <input name="count" class="num" type="number" step="1" min="1" max="99"></label>
        <label>Default width <select name="default_width">${widthOpts}</select></label>
        <label>Default pitch <select name="default_pitch">${pitchOpts}</select></label>
      </div>
      <div class="actions"><button type="button" class="btn btn-ghost" data-gen-camera>Use camera position</button></div>
      <p class="hint">Lane 1's first pocket sits at the origin. Changing the default width or pitch sets every lane.</p>
      <div class="table-wrap lanes-wrap">
        <table class="feeders">
          <thead><tr><th>Lane</th><th>Width</th><th>Pitch</th></tr></thead>
          <tbody data-gen-lanes></tbody>
        </table>
      </div>
      <div class="actions"><button type="submit" class="btn" data-bank-action="generate">Generate lanes</button></div>
    </form>`;
}

// Form state: strings as typed, plus lanes [{width, pitch}] as numbers.
function initGenState(bank) {
  let st = null;
  try {
    st = JSON.parse(localStorage.getItem(genKey(bank.id)) || "null");
  } catch (_) {
    st = null;
  }
  if (st && Array.isArray(st.lanes) && st.lanes.length) return st;

  const lanes = bank.lanes || [];
  const first = lanes[0];
  const defaultPitch = first ? first.pitch : 4;
  const n = lanes.length || 8;
  return {
    origin_x: first && first.pick ? round3(first.pick[0]) : "",
    origin_y: first && first.pick ? round3(first.pick[1]) : "",
    lane_axis: "1,0",
    feed_direction: "0,-1",
    sprocket_side: "-1",
    gap: "0",
    pick_depth: String(configDefault("head", "pick_depth", 3)),
    default_width: 8,
    default_pitch: defaultPitch,
    lanes: Array.from({ length: n }, (_, i) => ({
      width: 8,
      pitch: lanes[i] && lanes[i].pitch ? lanes[i].pitch : defaultPitch,
    })),
  };
}

function saveGenState(id) {
  try {
    localStorage.setItem(genKey(id), JSON.stringify(genStates.get(id)));
  } catch (_) {
    /* storage blocked: the values last for this visit only */
  }
}

function fillGenForm(id, form) {
  const st = genStates.get(id);
  const f = (n) => form.elements.namedItem(n);
  f("origin_x").value = st.origin_x;
  f("origin_y").value = st.origin_y;
  f("lane_axis").value = st.lane_axis;
  f("feed_direction").value = st.feed_direction;
  f("sprocket_side").value = st.sprocket_side;
  f("gap").value = st.gap;
  f("pick_depth").value = st.pick_depth;
  f("count").value = String(st.lanes.length);
  f("default_width").value = String(st.default_width);
  f("default_pitch").value = String(st.default_pitch);
  renderGenLanes(id, form);
}

function renderGenLanes(id, form) {
  const st = genStates.get(id);
  form.querySelector("[data-gen-lanes]").replaceChildren(...st.lanes.map((ln, i) => {
    const w = makeSelect(GEN_WIDTHS.map((v) => ({ value: v, label: `${v} mm` })), ln.width);
    const p = makeSelect(GEN_PITCHES.map((v) => ({ value: v, label: `${v} mm` })), ln.pitch);
    w.setAttribute("aria-label", `Lane ${i + 1} width`);
    p.setAttribute("aria-label", `Lane ${i + 1} pitch`);
    w.addEventListener("change", () => {
      ln.width = Number(w.value);
      saveGenState(id);
    });
    p.addEventListener("change", () => {
      ln.pitch = Number(p.value);
      saveGenState(id);
    });
    const cw = elem("td");
    cw.append(w);
    const cp = elem("td");
    cp.append(p);
    const tr = elem("tr");
    tr.append(elem("td", "mono", String(i + 1)), cw, cp);
    return tr;
  }));
}

// Copies the form into the state. Applies a changed default to every lane and resizes
// the lane list to the count, then re-renders the lane rows if either changed.
function syncGenForm(id, form) {
  const st = genStates.get(id);
  const f = (n) => form.elements.namedItem(n);
  st.origin_x = f("origin_x").value;
  st.origin_y = f("origin_y").value;
  st.lane_axis = f("lane_axis").value;
  st.feed_direction = f("feed_direction").value;
  st.sprocket_side = f("sprocket_side").value;
  st.gap = f("gap").value;
  st.pick_depth = f("pick_depth").value;

  let rowsChanged = false;
  const dw = Number(f("default_width").value);
  const dp = Number(f("default_pitch").value);
  if (dw !== st.default_width || dp !== st.default_pitch) {
    st.default_width = dw;
    st.default_pitch = dp;
    for (const ln of st.lanes) {
      ln.width = dw;
      ln.pitch = dp;
    }
    rowsChanged = true;
  }
  const n = parseInt(f("count").value, 10);
  if (Number.isInteger(n) && n >= 1 && n <= 99 && n !== st.lanes.length) {
    while (st.lanes.length < n) st.lanes.push({ width: st.default_width, pitch: st.default_pitch });
    st.lanes.length = n;
    rowsChanged = true;
  }
  if (rowsChanged) renderGenLanes(id, form);
  saveGenState(id);
}

function useCamera(id, form) {
  if (!state || !state.position) {
    reportError(new Error("No camera position yet. Connect to the machine first."));
    return;
  }
  form.elements.namedItem("origin_x").value = fmt(state.position.x, 3);
  form.elements.namedItem("origin_y").value = fmt(state.position.y, 3);
  syncGenForm(id, form);
}

async function submitGen(id, form) {
  syncGenForm(id, form);
  const st = genStates.get(id);
  const count = parseInt(form.elements.namedItem("count").value, 10);
  let body;
  try {
    if (!(count >= 1 && count <= 99)) throw new Error("Lanes must be a number from 1 to 99");
    body = {
      origin: [parseNum(st.origin_x, "Origin X"), parseNum(st.origin_y, "Origin Y")],
      lane_axis: parseVec(st.lane_axis),
      feed_direction: parseVec(st.feed_direction),
      sprocket_side: Number(st.sprocket_side),
      gap: parseNum(st.gap, "Gap"),
      pick_depth: parseNum(st.pick_depth, "Pick depth"),
      lanes: st.lanes.map((ln) => ({ width: ln.width, pitch: ln.pitch })),
      keep_parts: true,
    };
  } catch (err) {
    reportError(err);
    return;
  }
  await bankAction(`Generate lanes for ${id}`, () => api(bankPath(id, "/generate"), body));
}

function initBanks() {
  $("#bank-add").addEventListener("click", addBank);
  refreshBanks();
}

initBanks();
