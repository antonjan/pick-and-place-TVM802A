"use strict";

// Setup tab, Nozzle rules card: edits /api/package-rules. Loaded after app.js.

let rulesState = null;   // {default_nozzle, rules: [{pattern, nozzle, body: [length, width] (text), place_depth (text), vision, note}]}
let rulesDirty = false;  // unsaved edits: switching tabs does not reload over them

function setRulesState(data) {
  rulesState = {
    default_nozzle: data.default_nozzle,
    rules: data.rules.map((r) => ({
      pattern: r.pattern,
      nozzle: r.nozzle,
      body: r.body ? [String(r.body[0]), String(r.body[1])] : ["", ""],
      place_depth: r.place_depth == null ? "" : String(r.place_depth),
      vision: r.vision !== false,
      note: r.note || "",
    })),
  };
  rulesDirty = false;
  renderRules();
}

async function loadRules() {
  if (rulesDirty) return;
  try {
    setRulesState(await getJson("/api/package-rules"));
  } catch (_) {
    log("Couldn't load the nozzle rules.", true);
  }
}

function renderRules() {
  for (const b of $$("#rules-default button")) {
    const on = Number(b.dataset.default) === rulesState.default_nozzle;
    b.classList.toggle("sel", on);
    b.setAttribute("aria-checked", String(on));
  }
  $("#rules-rows").replaceChildren(...rulesState.rules.map(ruleRow));
  $("#rules-empty").hidden = rulesState.rules.length > 0;
}

function ruleRow(rule) {
  const pattern = elem("input");
  pattern.type = "text";
  pattern.value = rule.pattern;
  pattern.placeholder = "*0402*";
  pattern.autocomplete = "off";
  pattern.setAttribute("aria-label", "Package pattern");
  pattern.addEventListener("input", () => {
    rule.pattern = pattern.value;
    rulesDirty = true;
  });

  const nozzle = makeSelect([{ value: 1, label: "N1 thick" }, { value: 2, label: "N2 thin" }], rule.nozzle);
  nozzle.setAttribute("aria-label", "Nozzle");
  nozzle.addEventListener("change", () => {
    rule.nozzle = Number(nozzle.value);
    rulesDirty = true;
  });

  const bodyLen = elem("input", "num");
  bodyLen.type = "number";
  bodyLen.step = "any";
  bodyLen.min = "0";
  bodyLen.placeholder = "Length";
  bodyLen.value = rule.body[0];
  bodyLen.setAttribute("aria-label", "Body length mm");
  bodyLen.addEventListener("input", () => {
    rule.body[0] = bodyLen.value;
    rulesDirty = true;
  });

  const bodyWid = elem("input", "num");
  bodyWid.type = "number";
  bodyWid.step = "any";
  bodyWid.min = "0";
  bodyWid.placeholder = "Width";
  bodyWid.value = rule.body[1];
  bodyWid.setAttribute("aria-label", "Body width mm");
  bodyWid.addEventListener("input", () => {
    rule.body[1] = bodyWid.value;
    rulesDirty = true;
  });
  const body = elem("span", "body-pair");
  body.append(bodyLen, elem("span", "", "×"), bodyWid);

  const depth = elem("input", "num");
  depth.type = "number";
  depth.step = "any";
  depth.min = "0";
  depth.placeholder = "Default";
  depth.value = rule.place_depth;
  depth.setAttribute("aria-label", "Place depth mm");
  depth.addEventListener("input", () => {
    rule.place_depth = depth.value;
    rulesDirty = true;
  });

  const vision = elem("input");
  vision.type = "checkbox";
  vision.checked = rule.vision;
  vision.setAttribute("aria-label", "Inspect on the up camera");
  vision.addEventListener("change", () => {
    rule.vision = vision.checked;
    rulesDirty = true;
  });

  const note = elem("input");
  note.type = "text";
  note.value = rule.note;
  note.autocomplete = "off";
  note.setAttribute("aria-label", "Note");
  note.addEventListener("input", () => {
    rule.note = note.value;
    rulesDirty = true;
  });

  const remove = buttonEl("", "btn btn-ghost btn-icon", "i-trash-2");
  remove.title = "Remove rule";
  remove.setAttribute("aria-label", "Remove rule");
  remove.addEventListener("click", () => {
    rulesState.rules = rulesState.rules.filter((r) => r !== rule);
    rulesDirty = true;
    renderRules();
  });

  const tr = elem("tr");
  tr.append(...[pattern, nozzle, body, depth, vision, note, remove].map((node) => {
    const td = elem("td");
    td.append(node);
    return td;
  }));
  return tr;
}

function addRule() {
  if (!rulesState) return;
  rulesState.rules.push({ pattern: "", nozzle: 1, body: ["", ""], place_depth: "", vision: true, note: "" });
  rulesDirty = true;
  renderRules();
  const first = $("#rules-rows tr:last-child input");
  if (first) first.focus();
}

function rulesBody() {
  const rules = rulesState.rules.map((r, i) => {
    const pattern = r.pattern.trim();
    if (!pattern) throw new Error(`Rule ${i + 1} needs a package pattern`);
    let body = null;
    const [len, wid] = r.body.map((t) => t.trim());
    if (len !== "" || wid !== "") {
      if (len === "" || wid === "") throw new Error(`Body for rule ${i + 1} needs both length and width, or neither`);
      const size = [parseNum(len, `Body length for rule ${i + 1}`), parseNum(wid, `Body width for rule ${i + 1}`)];
      if (size[0] <= 0 || size[1] <= 0) throw new Error(`Body for rule ${i + 1} must be more than 0`);
      body = size;
    }
    let placeDepth = null;
    if (r.place_depth.trim() !== "") {
      placeDepth = parseNum(r.place_depth, `Place depth for rule ${i + 1}`);
      if (placeDepth <= 0) throw new Error(`Place depth for rule ${i + 1} must be more than 0`);
    }
    return {
      pattern,
      nozzle: r.nozzle,
      body,
      place_depth: placeDepth,
      vision: r.vision,
      note: r.note.trim(),
    };
  });
  return { default_nozzle: rulesState.default_nozzle, rules };
}

async function saveRules() {
  if (!rulesState) return;
  let body;
  try {
    body = rulesBody();
  } catch (err) {
    reportError(err);
    return;
  }
  const btn = $("#rules-save");
  btn.disabled = true;
  try {
    setRulesState(await api("/api/package-rules", body, "PUT"));
    log("Nozzle rules saved");
  } catch (_) {
    /* api() has logged the error and shown the banner */
  } finally {
    btn.disabled = false;
  }
}

function initRules() {
  for (const b of $$("#rules-default button")) {
    b.addEventListener("click", () => {
      if (!rulesState) return;
      rulesState.default_nozzle = Number(b.dataset.default);
      rulesDirty = true;
      renderRules();
    });
  }
  $("#rule-add").addEventListener("click", addRule);
  $("#rules-save").addEventListener("click", saveRules);
}

initRules();
