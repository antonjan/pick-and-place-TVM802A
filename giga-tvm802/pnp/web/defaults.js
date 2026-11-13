// Defaults tab: every editable value of config/machine.yaml, grouped in cards.
// Saving writes the changed values to machine.yaml (comments kept) and applies
// them to the running machine at once.

let defaultsData = null;   // sections as last loaded from the server
let defaultsDirty = false;

function machineDefault(section, key, fallback) {
  // Used by other tabs (e.g. the pick depth of a new feeder).
  const s = defaultsData && defaultsData.find((x) => x.key === section);
  const f = s && s.fields.find((x) => x.key === key);
  return f ? f.value : fallback;
}

function fmtDefault(f) {
  if (f.type === "bool") return f.default ? "on" : "off";
  if (f.type === "pair" || f.type === "optional_pair") return f.default.length ? f.default.join(", ") : "empty";
  return String(f.default);
}

function sameValue(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function defField(section, f) {
  const wrap = document.createElement("div");
  wrap.className = "def-field";
  wrap.dataset.section = section;
  wrap.dataset.key = f.key;
  wrap.dataset.type = f.type;

  const head = document.createElement("div");
  head.className = "def-label";
  const name = document.createElement("span");
  name.textContent = f.label + (f.unit ? ` (${f.unit})` : "");
  head.append(name);
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "btn btn-ghost btn-small def-reset";
  reset.title = `Back to the built-in default (${fmtDefault(f)})`;
  reset.innerHTML = '<svg class="icon"><use href="#i-rotate-ccw"/></svg>Default';
  reset.addEventListener("click", () => { setFieldValue(wrap, f.default); defChanged(); });
  head.append(reset);
  wrap.append(head);

  const inputs = document.createElement("div");
  inputs.className = "def-inputs";
  if (f.type === "bool") {
    const lab = document.createElement("label");
    lab.className = "def-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.addEventListener("change", defChanged);
    lab.append(cb, document.createTextNode(" On"));
    inputs.append(lab);
  } else if (f.type === "pair" || f.type === "optional_pair") {
    for (const ph of ["X", "Y"].map((a, i) => (f.key === "x" || f.key === "y") ? ["min", "max"][i] : (f.key === "tip_diameter" ? ["N1", "N2"][i] : a))) {
      const inp = document.createElement("input");
      inp.type = "number";
      inp.step = "any";
      inp.className = "num";
      inp.placeholder = ph;
      inp.setAttribute("aria-label", `${f.label} ${ph}`);
      inp.addEventListener("input", defChanged);
      inputs.append(inp);
    }
  } else {
    const inp = document.createElement("input");
    inp.type = f.type === "str" ? "text" : "number";
    if (f.type !== "str") inp.step = f.type === "int" ? "1" : "any";
    inp.className = f.type === "str" ? "" : "num";
    inp.setAttribute("aria-label", f.label);
    inp.addEventListener("input", defChanged);
    inputs.append(inp);
  }
  wrap.append(inputs);

  const help = document.createElement("p");
  help.className = "def-help";
  help.textContent = (f.help ? f.help + " " : "") + `Built-in default: ${fmtDefault(f)}.`;
  wrap.append(help);

  setFieldValue(wrap, f.value);
  return wrap;
}

function setFieldValue(wrap, v) {
  const t = wrap.dataset.type;
  const inputs = wrap.querySelectorAll("input");
  if (t === "bool") inputs[0].checked = !!v;
  else if (t === "pair" || t === "optional_pair") inputs.forEach((inp, i) => { inp.value = v && v.length ? v[i] : ""; });
  else inputs[0].value = v;
}

// The field's value as JSON for the server, or throws with a readable message.
function readFieldValue(wrap, f) {
  const inputs = [...wrap.querySelectorAll("input")];
  if (f.type === "bool") return inputs[0].checked;
  if (f.type === "str") return inputs[0].value.trim();
  if (f.type === "pair" || f.type === "optional_pair") {
    const raw = inputs.map((i) => i.value.trim());
    if (f.type === "optional_pair" && raw.every((r) => r === "")) return [];
    const nums = raw.map(Number);
    if (raw.some((r) => r === "") || nums.some((n) => !Number.isFinite(n))) {
      throw new Error(`${f.label}: enter both values${f.type === "optional_pair" ? " (or leave both empty)" : ""}`);
    }
    return nums;
  }
  const n = Number(inputs[0].value);
  if (inputs[0].value.trim() === "" || !Number.isFinite(n)) throw new Error(`${f.label}: enter a number`);
  if (f.type === "int" && !Number.isInteger(n)) throw new Error(`${f.label}: enter a whole number`);
  return n;
}

function eachDefField(fn) {
  if (!defaultsData) return;
  for (const s of defaultsData) {
    for (const f of s.fields) {
      const wrap = document.querySelector(`.def-field[data-section="${s.key}"][data-key="${f.key}"]`);
      if (wrap) fn(s, f, wrap);
    }
  }
}

// Mark edited and non-default fields; enable Save/Revert when something changed.
function defChanged() {
  let dirty = false;
  eachDefField((s, f, wrap) => {
    let v;
    try { v = readFieldValue(wrap, f); } catch (_) { v = undefined; }
    const edited = v === undefined || !sameValue(v, f.value);
    wrap.classList.toggle("edited", edited);
    wrap.querySelector(".def-reset").hidden = v !== undefined && sameValue(v, f.default);
    dirty = dirty || edited;
  });
  defaultsDirty = dirty;
  $("#defaults-save").disabled = !dirty;
  $("#defaults-revert").disabled = !dirty;
  $("#defaults-status").textContent = dirty ? "Unsaved changes" : "Saved values";
  $("#defaults-status").classList.toggle("dirty", dirty);
}

function renderDefaults() {
  const basic = $("#defaults-basic");
  const adv = $("#defaults-advanced-body");
  basic.replaceChildren();
  adv.replaceChildren();
  for (const s of defaultsData) {
    for (const [advanced, host] of [[false, basic], [true, adv]]) {
      const fields = s.fields.filter((f) => f.advanced === advanced);
      if (!fields.length) continue;
      const card = document.createElement("section");
      card.className = advanced ? "def-group" : "card def-group";
      const h = document.createElement(advanced ? "h3" : "h2");
      h.textContent = s.title + (advanced && !s.advanced ? " (advanced)" : "");
      const grid = document.createElement("div");
      grid.className = "def-grid";
      for (const f of fields) grid.append(defField(s.key, f));
      card.append(h, grid);
      host.append(card);
    }
  }
  defChanged();
}

async function loadDefaults(force = false) {
  if (defaultsDirty && !force) return;  // don't throw away edits when the tab is re-opened
  const res = await fetch("/api/config/defaults");
  if (!res.ok) { log(`defaults: HTTP ${res.status}`, true); return; }
  const data = await res.json();
  defaultsData = data.sections;
  $("#defaults-path").textContent = data.path || "(no file)";
  renderDefaults();
}

async function saveDefaults() {
  const changes = {};
  try {
    eachDefField((s, f, wrap) => {
      const v = readFieldValue(wrap, f);
      if (!sameValue(v, f.value)) (changes[s.key] ||= {})[f.key] = v;
    });
  } catch (e) {
    showError(e.message);
    return;
  }
  if (!Object.keys(changes).length) return;
  const risky = ["steps", "homing", "controller"].filter((k) => changes[k]);
  if (risky.length && !(await askConfirm(
      `You changed advanced values (${risky.join(", ")}). Wrong values here can make the machine move the wrong distance or lose its connection. Save them?`,
      "Save"))) return;
  let res;
  try {
    res = await api("/api/config/defaults", { changes }, "PUT");
  } catch (_) {
    return;  // api() shows the error
  }
  defaultsData = res.sections;
  defaultsDirty = false;
  renderDefaults();
  log(`defaults saved: ${res.changed.join(", ") || "nothing changed"}`);
  for (const w of res.warnings) log(w, true);
  $("#defaults-result").hidden = !res.warnings.length;
  $("#defaults-result").textContent = res.warnings.join(" ");
}

function initDefaults() {
  $("#defaults-save").addEventListener("click", saveDefaults);
  $("#defaults-revert").addEventListener("click", () => { defaultsDirty = false; renderDefaults(); });
  loadDefaults();  // other tabs use machineDefault() (e.g. a new feeder's pick depth)
}

initDefaults();
