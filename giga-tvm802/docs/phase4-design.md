# Phase 4 design: feeder banks, KiCad jobs, panels and blind placement

Goal (from PLAN.md): import a KiCad `.pos`, set the board origin by jogging, and place a full job without vision correction. This phase also adds the user's requests from 2026-11-12:

- **Feeder banks** (the front and back stacks), each with saved, named layouts.
- **Panels**, with bad boards skipped.
- **One nozzle per part:** N1 has the thick tip and N2 the thin tip, with no dual-nozzle cycles.

This is the contract between the phase 4 modules. Read `docs/phase2-design.md` and `docs/phase3-design.md` first; their conventions all still apply:

- **World/machine coordinates** are in mm. The head position `H` is where the **down camera centre** looks. A tool with offset `t` reaches world point `W` when the head is at `W - t`. `HeadOps` already does this.
- **Machine resolution** is 0.0305 mm. **Angles** are in degrees, counter-clockwise positive (X+ toward Y+).
- **Files** are YAML, saved atomically (temp file + `os.replace`), like `FeederStore.save()`.
- **Code style:** Python 3.11+, dataclasses, type hints, short docstrings, no new dependencies.
- **Running things:** on Windows the interpreter is `.venv/Scripts/python`. Run tests with `.venv/Scripts/python -m pytest -q tests/<file>`.
- **Never** run git commit, git push, or any other git command that changes the repository.

---

## 1. Feeder changes (`pnp/job/feeders.py`)

The prick needle feeds by entering a **sprocket hole**, dragging the tape **one part pitch** along `direction`, then lifting. That is the existing `kind="drag"` feeder; bank lanes are drag feeders.

Add two fields to `Feeder`, at the end, after `package`:

```python
    bank: str = ""                   # id of the bank this lane belongs to; "" = loose feeder
    lane: int | None = None          # 1-based lane number within the bank
```

Add these `FeederStore` methods, each saving once (not once per feeder):

```python
    def by_bank(self, bank: str) -> list[Feeder]          # feeders with f.bank == bank, ordered by lane then id
    def replace_bank(self, bank: str, feeders: list[Feeder]) -> None
        # validate all first (nothing changes on a ValueError), every new feeder must have f.bank == bank,
        # ids must not clash with feeders of OTHER banks or loose feeders (ValueError),
        # then delete all feeders of `bank`, add the new ones, save once.
    def delete_bank(self, bank: str) -> None              # remove all feeders of `bank`, save once
```

`feeder_json` in `pnp/api/feeder_routes.py` picks the new fields up automatically. Add `bank: str = ""` and `lane: int | None = None` to `FeederIn`.

## 2. Feeder banks (`pnp/job/banks.py`, new)

A **bank** is a physical stack of lanes, such as `front` or `back`. Its lanes are ordinary drag feeders in the `FeederStore` with `bank` set. A **layout** is a named snapshot of all of the bank's lanes: geometry and loaded parts (value, package, rotation, pick depth, count). When the user swaps a stack, they load its layout.

```python
BANK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Distance across the tape from the pocket centre line to the sprocket-hole line (EIA-481 "F"), by tape width.
SPROCKET_ACROSS = {8: 3.5, 12: 5.5, 16: 7.5, 24: 11.5, 32: 14.2, 44: 20.2}

@dataclass
class LaneSpec:
    width: float = 8.0              # tape width (mm): 8, 12, 16, 24, 32, 44
    pitch: float = 4.0              # part pitch (mm): the drag distance per feed
    offset: float | None = None     # mm along lane_axis from the bank origin to this lane's pocket line;
                                    # None = previous lane's offset + (previous width + this width) / 2 + gap
                                    # (lane 1: None = 0)

@dataclass
class GenerateSpec:
    origin: tuple[float, float]               # world mm: pick point (first pocket) of lane 1
    lane_axis: tuple[float, float] = (1.0, 0.0)       # unit vector from lane 1 toward lane 2 (normalised)
    feed_direction: tuple[float, float] = (0.0, -1.0) # unit vector the tape is dragged (normalised)
    gap: float = 0.0                          # extra space between neighbouring tapes (mm)
    sprocket_side: int = -1                   # +1: sprocket holes on the +lane_axis side of the pocket, -1: the other side
    prick_along: float = -2.0                 # needle point offset along feed_direction from the pick point (mm);
                                              # default = the sprocket hole 2 mm (EIA-481 P2) upstream of the pocket
    pick_depth: float = 3.0
    lanes: list[LaneSpec] = field(default_factory=list)   # at least 1, at most 99
    keep_parts: bool = True                   # carry value/package/rotation/pick_depth/count/enabled over from
                                              # existing lanes with the same lane number

def lane_offsets(spec: GenerateSpec) -> list[float]
    # resolves each lane's offset as described in LaneSpec.offset

def generate_lanes(bank_id: str, spec: GenerateSpec, existing: list[Feeder]) -> list[Feeder]
    # For lane i (1-based) with offset o:
    #   pick  = origin + o * lane_axis
    #   prick = pick + sprocket_side * SPROCKET_ACROSS.get(round(width), width / 2 - 0.5) * lane_axis
    #                + prick_along * feed_direction
    #   Feeder(id=f"{bank_id}-{i}", kind="drag", pick=pick, direction=feed_direction, pitch=lane.pitch,
    #          prick=prick, pick_depth=spec.pick_depth, needs_feed=True, enabled=True,
    #          name=f"{width:g} mm lane {i}", bank=bank_id, lane=i)
    #   keep_parts: copy value, package, rotation, pick_depth, count, enabled, name from the `existing`
    #   feeder with the same lane number, if there is one.
    # ValueError: bad bank id, no lanes, more than 99 lanes, width <= 0, pitch <= 0, zero lane_axis or feed_direction.

@dataclass
class Bank:
    id: str
    name: str = ""
    active_layout: str | None = None
    layouts: dict[str, list[dict]] = field(default_factory=dict)  # layout name -> feeder dicts (all Feeder fields)

class BankError(Exception): ...   # unknown bank/layout (message contains "unknown") or a conflict

class BankStore:
    """Banks and their layouts in config/banks.yaml; the lanes themselves live in the FeederStore."""
    def __init__(self, path, feeders: FeederStore)   # missing file -> no banks
    def all(self) -> list[Bank]
    def get(self, id) -> Bank                        # BankError("unknown bank ...")
    def upsert(self, id, name: str = "") -> Bank     # create or rename; ValueError for an id not matching BANK_ID
    def delete(self, id) -> None                     # removes the bank, its layouts and its lanes (feeders.delete_bank)
    def generate(self, id, spec: GenerateSpec) -> list[Feeder]
        # bank must exist; lanes = generate_lanes(id, spec, feeders.by_bank(id)); feeders.replace_bank(id, lanes);
        # active_layout = None (the lanes no longer match a saved layout); save; return lanes
    def save_layout(self, id, name) -> None
        # name: 1..40 chars, stripped, not empty (ValueError); snapshot feeders.by_bank(id) as dicts
        # (same dict form FeederStore writes) into layouts[name] (overwrite allowed); active_layout = name; save
    def load_layout(self, id, name) -> list[Feeder]
        # BankError if unknown; build Feeders from the snapshot (force bank=id), feeders.replace_bank(id, ...),
        # active_layout = name; save; return them
    def delete_layout(self, id, name) -> None        # if it was active, active_layout = None
    def save(self) -> None   # YAML: {"banks": [{"id","name","active_layout","layouts": {name: [feeder dict, ...]}}]}
```

Expose `_feeder_to_dict` and `_feeder_from_dict` from `feeders.py` as public `feeder_to_dict` / `feeder_from_dict`, keeping the old names as aliases, so banks.py can reuse them.

## 3. Package rules: nozzle choice (`pnp/job/rules.py`, new)

N1 = **thick** tip, N2 = **thin** tip. Each part is placed by exactly one nozzle, chosen by its package.

```python
@dataclass
class PackageRule:
    pattern: str                    # fnmatch pattern, matched case-insensitively against the part's package
    nozzle: int = 2                 # 1 (thick) or 2 (thin)
    place_depth: float | None = None  # None = machine.yaml head.place_depth
    note: str = ""

DEFAULT_RULES: list[PackageRule] = [
    PackageRule("*0201*", 2), PackageRule("*0402*", 2), PackageRule("*0603*", 2), PackageRule("*0805*", 2),
    PackageRule("SOD-*", 2), PackageRule("SOT-23*", 2), PackageRule("SOT-323*", 2), PackageRule("SOT-363*", 2),
    PackageRule("*1206*", 1), PackageRule("*1210*", 1), PackageRule("*2512*", 1),
    PackageRule("SOIC*", 1), PackageRule("SOP*", 1), PackageRule("TSSOP*", 1), PackageRule("MSOP*", 1),
    PackageRule("QFN*", 1), PackageRule("QFP*", 1), PackageRule("LQFP*", 1), PackageRule("TQFP*", 1),
    PackageRule("SOT-223*", 1), PackageRule("TO-252*", 1), PackageRule("D_SMA*", 1), PackageRule("D_SMB*", 1),
    PackageRule("CP_Elec*", 1),
]

class PackageRules:
    """config/packages.yaml: {"default_nozzle": 1, "rules": [{pattern, nozzle, place_depth, note}, ...]}"""
    def __init__(self, path)          # missing file -> default_nozzle 1 and a copy of DEFAULT_RULES (not saved yet)
    default_nozzle: int
    rules: list[PackageRule]
    def match(self, package: str) -> PackageRule | None   # first rule whose pattern matches, else None
    def nozzle_for(self, package: str) -> int             # match(...).nozzle or default_nozzle
    def place_depth_for(self, package: str) -> float | None
    def set(self, default_nozzle: int, rules: list[PackageRule]) -> None   # validate (nozzle in {1,2},
        # non-empty pattern, place_depth None or > 0) then save; ValueError leaves the old values
    def save(self) -> None
```

## 4. KiCad position files (`pnp/job/kicad.py`, new)

```python
@dataclass
class PosPart:
    ref: str
    value: str
    package: str
    x: float          # mm, board coordinates
    y: float          # mm
    rotation: float   # deg, normalised to [0, 360)
    side: str         # "top" | "bottom"

class PosError(ValueError): ...    # message names the line number

def parse_pos(text: str) -> list[PosPart]
```

Accept both KiCad export formats; detect the format from the first non-comment, non-blank line:

- **ASCII** (`.pos`): comment lines start with `#`. A line such as `## Unit = mm, Angle = deg.` or `## Unit = inches, ...` sets the unit; inches are multiplied by 25.4. Data lines are whitespace separated: `Ref Val Package PosX PosY Rot Side`. The value may contain spaces, so take `ref = tokens[0]`, the last five tokens as `package, x, y, rot, side`, and the value as `" ".join(tokens[1:-5])`.
- **CSV** (`.csv`): the header contains `Ref`, `Val`, `Package`, `PosX`, `PosY`, `Rot`, `Side` (case-insensitive, any column order, fields may be quoted). Use the `csv` module. CSV coordinates are mm.

Side is normalised to lower case; anything other than `top`/`bottom` is a `PosError`. A malformed number is a `PosError`, and so is a duplicate ref. An empty file, or one without data lines, gives `PosError("no parts found")`.

## 5. Jobs and panels (`pnp/job/job.py`, new)

```python
@dataclass
class JobPart:
    ref: str
    value: str
    package: str
    x: float                 # board mm
    y: float
    rotation: float          # deg
    side: str = "top"
    skip: bool = False       # user override; bottom-side parts are created with skip=True
    nozzle: int | None = None    # user override; None = PackageRules.nozzle_for(package)
    feeder: str | None = None    # user override; None = automatic match

@dataclass
class BoardInstance:
    x: float = 0.0           # panel mm: where this board's (0, 0) sits in the panel
    y: float = 0.0
    rotation: float = 0.0    # deg, this board's rotation within the panel
    skip: bool = False       # bad board: none of its parts are placed

@dataclass
class Job:
    name: str
    source: str = ""                         # imported file name
    parts: list[JobPart] = field(default_factory=list)
    boards: list[BoardInstance] = field(default_factory=lambda: [BoardInstance()])
    origin: tuple[float, float] | None = None   # world mm of the panel's (0, 0); None = not set yet
    rotation: float = 0.0                    # deg, panel -> world rotation
    status: dict[str, str] = field(default_factory=dict)   # placement key -> "placed" | "failed" | "skipped"

def placement_key(board: int, ref: str) -> str          # f"{board}:{ref}"   (board is the 0-based index)
def grid(rows: int, cols: int, step_x: float, step_y: float) -> list[BoardInstance]
    # row-major from (0, 0): board r*cols + c sits at (c*step_x, r*step_y), rotation 0
    # ValueError: rows/cols < 1 or > 50
def job_from_pos(name: str, source: str, parts: list[PosPart]) -> Job   # bottom-side parts get skip=True

def to_world(job: Job, board: int, x: float, y: float) -> tuple[float, float]
    # p = rot(board.rotation) @ (x, y) + (board.x, board.y)   (panel coords)
    # w = rot(job.rotation) @ p + job.origin                  (ValueError if origin is None)
def world_rotation(job: Job, board: int, part_rotation: float) -> float
    # part_rotation + boards[board].rotation + job.rotation, normalised to (-180, 180]
def fit_rotation(job: Job, board_xy: tuple[float, float], world_xy: tuple[float, float]) -> float
    # the job.rotation that maps panel point board_xy onto world_xy, given job.origin
    # (angle of (world_xy - origin) minus angle of board_xy); ValueError if origin is None
    # or either vector is shorter than 1 mm

def norm(s: str) -> str     # lower case, stripped, runs of whitespace collapsed to one space

def match_feeder(part: JobPart, feeders: list[Feeder]) -> Feeder | None
    # part.feeder set -> that feeder if present and enabled, else None
    # otherwise candidates = enabled feeders with norm(f.value) == norm(part.value) and
    #   (f.package == "" or norm(f.package) == norm(part.package));
    # prefer feeders with parts left (remaining() is None or > 0), then lowest (bank, lane, id).

@dataclass
class PlanItem:
    key: str
    board: int
    ref: str
    value: str
    package: str
    x: float | None          # world mm (None when the origin is not set)
    y: float | None
    rotation: float          # world deg
    nozzle: int
    feeder: str | None
    place_depth: float | None
    status: str              # "pending" | "placed" | "failed" | "skipped"
    problem: str | None      # why it can't run: "Part skipped", "Board skipped", "Bottom side",
                             # "No feeder for <value> <package>", "Feeder <id> is empty", "Board origin not set"

def plan(job: Job, feeders: list[Feeder], rules: PackageRules) -> list[PlanItem]
    # One item per (board, part). Order: board index, then nozzle, then feeder id ("" for none), then ref in
    # natural order (C2 before C10).
    # status: job.status.get(key, "pending"), but "skipped" when part.skip or board.skip.

class JobStore:
    """The current job in config/job.yaml (one job at a time)."""
    def __init__(self, path)
    def load(self) -> Job | None
    def save(self, job: Job) -> None
    def clear(self) -> None          # delete the file
```

## 6. Job runner (`pnp/job/runner.py`, new)

```python
class JobRunner:
    def __init__(self, machine, head: HeadOps, feeders: FeederStore, jobs: JobStore, rules: PackageRules,
                 pick_retries: int = 2)
    state: str        # "idle" | "running" | "pausing" | "paused" | "stopping" | "done" | "error"
    current: str | None    # key of the placement in progress
    message: str | None    # last problem, in user words, e.g.
                           # "Couldn't pick R5 from back-3 after 3 tries. Check the lane, then resume or skip it."
    def status(self) -> dict  # {"state", "current", "message", "placed", "failed", "skipped", "pending", "total"}
                              # counts over plan(...) items; "pending" counts runnable items not yet done
    async def start(self) -> None   # JobError if running/pausing/stopping, no job, not homed, origin not set,
                                    # or no runnable item; otherwise launch the loop as an asyncio task
    def pause(self) -> None         # running -> pausing: finish the current part, then paused
    async def resume(self) -> None  # paused/error/done -> start again from the first pending item
    def stop(self) -> None          # running/pausing/paused -> stopping (finish the current part) -> idle
    def busy(self) -> bool          # state in ("running", "pausing", "stopping")

class JobError(Exception): ...
```

The loop, for each `PlanItem` that is `pending` with no `problem`, in plan order:

1. Between items, honour pause and stop.
2. Re-check that the machine is homed. If it isn't, go to `error` with "The machine isn't homed. Home it, then resume."
3. Call `head.pick_and_place(item.nozzle, feeder, (item.x, item.y), item.rotation, item.place_depth)`.
   - On success, `job.status[key] = "placed"`, then save the job.
   - On `PickError`, retry up to `pick_retries` more times. Before each retry of a drag feeder, set `feeder.needs_feed = True` (the pocket was probably empty) and save the feeders. If every attempt fails, mark the item `"failed"`, save, set `message`, and go to `paused`.
   - On `FeederError` (empty or disabled), don't mark the item. Set `message` and go to `paused`.
   - On any other `MachineError` (E-stop, limits, not calibrated, timeout), set `message = str(exc)` and go to `error`.
4. Re-read the plan after every item, so edits made while paused (skip, nozzle or feeder overrides) take effect.

When no runnable items remain, the state becomes `done`. The task must never die silently: catch everything, log it, and set `error`.

## 7. HTTP API

New router files, `pnp/api/bank_routes.py` and `pnp/api/job_routes.py`, wired into `create_app` after `add_feeder_routes`. They use `app.state.feeders` and `app.state.head`. New `create_app` keyword arguments are `banks_path`, `rules_path` and `job_path`, with defaults `config/banks.yaml`, `config/packages.yaml` and `config/job.yaml`. `pnp/api/__main__.py` passes temp files in `--sim` mode, like it already does for feeders.

**Errors:** unknown → 404, conflict or busy → 409, bad input → 400. Every mutating endpoint below returns **409 while the runner is busy**, and bank and feeder edits also return 409 while `head.busy` is set. The runner must also block the feeder endpoints that move or edit feeders: `PUT/DELETE /api/feeders/{id}`, teach, feed, pick and pick-and-place return 409 "job running" while `runner.busy()` is true. Do this through a small check added in `feeder_routes.py` that reads `app.state.runner` if it exists.

### Banks
| Method and path | Body | Returns |
|---|---|---|
| `GET /api/banks` | | `[{id, name, active_layout, layouts: [names], lanes: [feeder_json...]}]` |
| `PUT /api/banks/{id}` | `{name}` | the bank (created if new) |
| `DELETE /api/banks/{id}` | | `{ok: true}`; removes its lanes too |
| `POST /api/banks/{id}/generate` | `GenerateSpec` as JSON, lists for the vectors | the bank |
| `POST /api/banks/{id}/layouts` | `{name}` | the bank; saves the current lanes under that name |
| `POST /api/banks/{id}/layouts/{name}/load` | | the bank |
| `DELETE /api/banks/{id}/layouts/{name}` | | the bank |

### Package rules
| `GET /api/package-rules` | | `{default_nozzle, rules: [...]}` |
|---|---|---|
| `PUT /api/package-rules` | `{default_nozzle, rules: [...]}` | the same |

### Job
| Method and path | Body | Returns |
|---|---|---|
| `GET /api/job` | | `{job: {name, source, origin, rotation, boards, parts}` or `null`, `plan: [PlanItem...]`, `status: runner.status()}` |
| `POST /api/job/import` | `{name, filename, text}` | same as GET; replaces the current job (400 on `PosError`) |
| `DELETE /api/job` | | `{ok: true}` |
| `PUT /api/job/board` | `{origin: [x,y] or null, rotation}` | same as GET |
| `POST /api/job/align/origin` | | same as GET; origin = current head XY (409 if not homed) |
| `POST /api/job/align/rotation` | `{ref, board: 0}` | same as GET; the camera is centred on that part: rotation = `fit_rotation` (409 if not homed or no origin) |
| `PUT /api/job/panel` | `{boards: [{x, y, rotation, skip}]}` | same as GET (at least 1 board, at most 2500) |
| `POST /api/job/panel/grid` | `{rows, cols, step_x, step_y}` | same as GET |
| `PUT /api/job/parts/{ref}` | `{skip, nozzle, feeder}` (all three, null = no override) | same as GET |
| `POST /api/job/placements/{key}/goto` | | `{position}`: moves the camera over that placement (409 if not homed or no origin) |
| `POST /api/job/placements/{key}/status` | `{status: "pending" or "skipped" or "placed"}` | same as GET |
| `POST /api/job/reset` | | same as GET; clears all statuses |
| `POST /api/job/start`, `/pause`, `/resume`, `/stop` | | `runner.status()` (409 with the `JobError` message) |

Keys contain a colon (`0:R1`); the UI URL-encodes them.

## 8. Simulator seeding (`pnp/api/__main__.py`, `--sim` only)

Create these only when the file is missing:

- **Banks `front` and `back`.** `back`: origin (40, 300), lane_axis (1, 0), feed_direction (0, -1), sprocket_side -1, 8 lanes of 8 mm × 4 mm pitch. Load lanes 1–4 with `10k`/`R_0805_2012Metric`, `100n`/`C_0402_1005Metric`, `1u`/`C_0805_2012Metric`, `BC847`/`SOT-23`, then save the layout as `"Passives"`. `front`: origin (40, 20), lane_axis (1, 0), feed_direction (0, 1), sprocket_side +1, 4 lanes of 12 mm × 8 mm pitch, with lane 1 loaded `NE555`/`SOIC-8_3.9x4.9mm_P1.27mm`; save it as `"ICs"`.
- **Example job** from `examples/demo.pos` (add this file: an ASCII KiCad pos with about 10 top-side parts using those values and packages, plus 1 bottom-side part, on a 40 × 30 mm board). Use a 2 × 1 panel with step_x 50, and origin (150, 150).

## 9. Tests

Add focused pytest files: `tests/test_banks.py`, `tests/test_rules.py`, `tests/test_kicad.py`, `tests/test_job.py`, `tests/test_runner.py` (against the simulator, like `tests/test_head_ops.py`), and `tests/test_api_job.py` (like `tests/test_api_feeders.py`). The whole suite must pass except `tests/test_camera.py::test_device_holders_finds_other_process`, which is Linux-only.

## 10. UI (`pnp/web`)

Styling follows the Graphite system already used in `styles.css`; reuse its classes (`.card`, `.btn`, `.btn-primary`, `.btn-ghost`, `.segmented`, `.badge`/`.chip`, `.form-grid`, `.actions`, `.hint`, `.value`, `table.feeders`, `.empty`, `.dialog` via `confirmDialog`). Use no new colours or sizes; tokens only. Keep one primary button per tab. Copy is sentence case, verb-first buttons, no full stops on labels. Icons come from the Lucide sprite in `index.html`; add symbols in the same format if needed.

- **Feeders tab: "Banks" card** (wide), above the feeder table. Per bank:
  - name, active layout badge and lane count;
  - a layout `<select>` with Load layout, Save layout as… (name prompt via a small dialog) and Delete layout;
  - a "Generate lanes" form: origin X/Y with a Use camera position button, lane direction and feed direction as selects of ±X/±Y, sprocket side, gap, lane count, a default width/pitch, and a per-lane width/pitch list;
  - Add bank and Delete bank buttons.

  The feeder table gains a Bank column. Bank lanes are edited like any feeder.
- **Job tab** (enable the button; place it before Log):
  - **Import:** file input for `.pos`/`.csv` → `POST /api/job/import`.
  - **Board:** origin X/Y, Set origin from camera, rotation, Align rotation on part (a ref select, then a button), Panel (rows, cols, step X/Y) with a board list (skip toggles).
  - **Parts table:** columns Board, Ref, Value, Package, Nozzle (select: Auto (N1/N2 shown), N1 thick, N2 thin), Feeder (select: Auto, plus feeder ids), Status chip, Problem, and actions Go to and Skip/Unskip. Filter: All / To do / Problems.
  - **Run controls:** Start job (the tab's one primary button), Pause, Resume, Stop, Reset statuses, a progress bar (`placed / total`), and the runner message as a banner.

  Poll `GET /api/job` every 500 ms while the Job tab is visible.
- **Setup tab:** a "Nozzle rules" card editing `/api/package-rules`: default nozzle segmented N1/N2, and a rules table (pattern, nozzle, place depth, note) with add, remove and Save rules.
