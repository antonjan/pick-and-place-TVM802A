# Phase 6 design: bottom vision

Goal (from PLAN.md): per-part offset and rotation correction, mis-pick detection by part size, and SOIC/QFN placed correctly.

After each pick, the head moves the nozzle over the up camera with the LEDs on. The application measures where the part sits on the nozzle (offset from the nozzle axis), how far it is turned from where it should be (angle error), and its size. The place move is then corrected. A missing part, a part of the wrong size, or a part too far off-centre is discarded and the pick is retried.

This is the contract between the phase 6 modules. Read `docs/phase2-design.md` to `docs/phase5-design.md` first; their conventions all apply:

- **World coordinates** are in mm. The head position `H` is the down camera centre. Nozzle n's tip is at `H + t_n`.
- **Up camera:**
  - `calibration.up_position[n]` is the **head** position at which nozzle n's tip is at the up camera image centre.
  - `calibration.A("up")` (2×2, px per mm) works like this: moving the head by `d` moves the tip image by `A_up @ d`.
  - So a point of the part at world offset `q` from the nozzle axis appears at image offset `o = A_up @ q` from the centre, and `q = inv(A_up) @ o`.
  - In the simulator `A_up = diag(-30, -30)` (mirrored), and the up camera is at world `(150, 30)`.
- **Nozzle rotation:**
  - Angles are degrees, CCW positive seen from above.
  - We assume a positive nozzle angle (A axis) turns the part CCW seen from above. This must be checked on the machine; `place()` already relies on it.
  - A part offset `p` (world mm, from the nozzle axis) measured at nozzle angle `a0` becomes `rot(φ) @ p` after the nozzle turns by `φ`.
- **Pixel convention:** continuous (moments centroid + 0.5, image centre `(w/2, h/2)`).
- **Style:** Python 3.11+, dataclasses, type hints, short docstrings, no new dependencies. YAML is saved atomically.
- **Windows:** the interpreter is `.venv/Scripts/python`. Run tests with `.venv/Scripts/python -m pytest -q tests/<file>`.
- **Never** run git commit, add, push, stash, checkout or reset.

---

## 1. Config: `VisionConfig` (`pnp/config.py`, new `vision:` section of `config/machine.yaml`)

```python
@dataclass
class VisionConfig:
    bottom_vision: bool = True            # inspect every part on the up camera before placing
    leds: bool = True                     # switch the "leds" output on while inspecting (off afterwards)
    settle: float = 0.15                  # s after arriving over the up camera before taking the frame
    min_brightness: int = 0               # 0 = Otsu threshold; otherwise a fixed grey threshold
    search_mm: float = 6.0                # only bright regions within this radius of the nozzle axis count
    tip_diameter: tuple[float, float] = (0.8, 0.4)   # N1 (thick), N2 (thin): a lone round blob this size = no part
    size_tolerance: float = 0.3           # measured body vs expected body, relative
    max_offset_mm: float = 1.0            # reject a part further than this from the nozzle axis
    max_angle_deg: float = 15.0           # reject a part turned more than this
```

Add `vision: VisionConfig` to `MachineConfig`, and add a commented `vision:` section to `config/machine.yaml`. The strict key check must keep working. `tip_diameter` loads from a YAML list.

## 2. Measuring a part (`pnp/vision/bottom.py`, new)

```python
@dataclass
class PartView:
    offset: tuple[float, float]   # world mm, part centre minus nozzle axis, at the nozzle angle of the frame
    angle: float                  # deg, world angle of the part's long side (0..180)
    size: tuple[float, float]     # mm (length >= width) of the minimum-area rectangle around the part
    area: float                   # mm^2 of the bright region

def measure_part(img, A_up, *, tip_diameter: float, min_brightness: int = 0,
                 search_mm: float = 6.0) -> PartView | None
```

1. Convert to grey, blur 5×5, then threshold: Otsu when `min_brightness == 0`, otherwise the fixed value.
2. Take the external contours of the bright regions (`RETR_EXTERNAL`). Map every contour point to world mm relative to the nozzle axis: `q = inv(A_up) @ (pixel - centre)`. Use the continuous convention: contour pixel index + 0.5.
3. Keep the contours whose centroid (in mm) is within `search_mm` of the axis. Merge all kept points, which joins body and leads into one outline. Return None if nothing is kept.
4. **No part:** return None if the merged outline is a single round blob that is no bigger than the tip. That is: one contour, circularity ≥ 0.7, and equivalent diameter ≤ 1.3 × `tip_diameter`. This is the bare nozzle tip.
5. Fit `cv2.minAreaRect` to the merged **mm** points (scale them, e.g. ×1000 to float32, then back). Working in mm makes mirroring and non-square pixels correct.
   - `offset` = the rectangle centre.
   - `size` = (long side, short side).
   - `angle` = the direction of the long side in world degrees, normalised to [0, 180).
   - `area` = the summed contour area in mm² (`|det(inv(A))|` × px area).

```python
def angle_error(measured: float, expected_axis: float) -> float
    # Smallest rotation (deg) that maps the expected body axis onto the measured long side,
    # modulo 90 (a rectangle looks the same after 90 deg of turning that we can't tell apart here):
    # wrap (measured - expected_axis) into (-45, 45].

class InspectError(Exception): ...   # user-facing message

async def inspect_nozzle(machine, camera, A_up, nozzle: int, cfg: VisionConfig) -> tuple[PartView | None, float]
    # The head must already be at up_position[nozzle] (the caller moves it).
    # If cfg.leds: set_output("leds", True) and remember the previous state.
    # Take a fresh frame (camera.fresh_frame(after=monotonic()+cfg.settle)), then measure_part with
    # cfg.tip_diameter[nozzle-1], cfg.min_brightness, cfg.search_mm.
    # Restore the leds output in a finally block.
    # Return (view, nozzle_angle_deg), where the angle comes from machine.position()[f"a{nozzle}"].
```

## 3. Package rules and plan (`pnp/job/rules.py`, `pnp/job/job.py`)

Add to `PackageRule`, after `note`:

```python
    body: tuple[float, float] | None = None   # (length, width) mm of the part as the up camera sees it
                                              # (body + leads), None = don't check the size
    vision: bool = True                       # inspect on the up camera before placing
```

Give `DEFAULT_RULES` these bodies (length, width), as seen from below with leads:

| Pattern | Body |
|---|---|
| `*0201*` | 0.6, 0.3 |
| `*0402*` | 1.0, 0.5 |
| `*0603*` | 1.6, 0.8 |
| `*0805*` | 2.0, 1.25 |
| `*1206*` | 3.2, 1.6 |
| `*1210*` | 3.2, 2.5 |
| `*2512*` | 6.3, 3.2 |
| `SOT-23*` | 2.9, 2.4 |
| `SOIC-8*` | 4.9, 6.0 (stored as 6.0, 4.9) |

Add this rule **before** the generic `SOIC*` one: `PackageRule("SOIC-8*", 1, body=(6.0, 4.9))`. Leave every other default rule with `body=None`.

Also:

- **Rule loading:** `body` is accepted as a 2-list. Validate it as two positive numbers, with length ≥ width (swap if needed). `vision` is a bool. Old files without these keys still load.
- **Rule lookups:** add `PackageRules.body_for(package) -> tuple | None` and `vision_for(package) -> bool`. With no matching rule, the body is None and vision is True.
- **`PlanItem`:** add `vision: bool` and `body: list[float] | None`, both from the rules. Add them at the end of the dataclass so existing positional uses stay valid.

## 4. Head operations (`pnp/job/head_ops.py`)

```python
@dataclass
class Inspection:                 # the last inspection of a nozzle, for the API and UI
    nozzle: int
    result: str                   # "ok" | "skipped" | "no part" | "rejected"
    message: str
    offset: list[float] | None    # mm
    angle_error: float | None     # deg
    size: list[float] | None      # mm
    at: str                       # ISO UTC
```

- **Constructor:** `HeadOps(..., inspector=None)`. `inspector` is an async callable `(nozzle) -> tuple[PartView | None, float]`. The app passes one built on `inspect_nozzle` when an up camera exists. The head keeps `self.vision_cfg: VisionConfig` from `machine.cfg.vision`, and `self.last_inspection: dict[int, Inspection]`.
- **Signatures:** `pick_and_place(nozzle, feeder, W, rotation=0.0, depth=None, *, vision=True, body=None)`, and `pick(...)` gets the same keyword-only `vision` and `body`.
- **After a successful pick,** when `vision` and `self.vision_cfg.bottom_vision` and `self.inspector` are set and the calibration has `up_position[nozzle]`, call `_inspect(nozzle, feeder, body)`:
  1. Move the head to `up_position[nozzle]` with `machine.move_to(x=, y=)`, then call `view, a0 = await inspector(nozzle)`.
  2. `expected_axis` = the angle of `feeder.direction` in degrees, plus `feeder.rotation`. Then `err = angle_error(view.angle, expected_axis)`.
  3. Reject the part if any of these is true:
     - `view is None` ("No part on nozzle {n} after picking from {feeder.id}").
     - `body` is set and either side differs from the expected side by more than `size_tolerance` (compare sorted sizes). Example message: "Part on nozzle 2 is 3.10 × 1.60 mm, expected 2.00 × 1.25 mm".
     - `|offset| > max_offset_mm`.
     - `|err| > max_angle_deg`.
  4. On a reject, discard the part with the existing discard steps, but inside the current lock: factor them out into `_discard(nozzle)`. Record `last_inspection` with result "no part" or "rejected", and raise `PickError(message)`. The runner already retries a `PickError`, and re-feeds drag lanes first.
  5. On success, store `self._correction[nozzle] = (offset, err, a0)` and record `last_inspection` with result "ok".
  6. When the inspection is skipped (vision off, no inspector, or no up position), record result "skipped" with the reason and clear `_correction[nozzle]`.
- **`_place(nozzle, W, rotation, depth)` with a correction `(p, err, a0)`:**
  - The nozzle command is `cmd = rotation - tape - err`. This is the old `rotation - tape`, corrected by the measured error.
  - `φ = cmd - a0` is how far the nozzle will turn from the inspection angle.
  - Move the nozzle over `W - rot(φ) @ p`, so the part centre lands on `W`.
  - Clear `_correction[nozzle]` when the part is released, and on discard.
  - Without a correction, `_place` behaves exactly as today.
- **Public helper:** `async def inspect(self, nozzle) -> Inspection` runs a manual inspection of whatever is on the nozzle. It moves over the up camera and measures, but never rejects or discards. It uses the lock with busy state "inspecting", and doesn't need a held part.

## 5. Simulator (`pnp/sim/tvm_sim.py`, `pnp/sim/sim_vision.py`)

- **`tvm_sim.SimMachine`:**
  - Add `listeners: list[Callable[[tuple, dict], None]]` (default empty). Call each listener right after a pick or release event is appended, with `(event, {"a1": deg, "a2": deg})`: the current **physical** nozzle angles in degrees (`phys / STEPS_PER_DEG`).
  - Add `def nozzle_angle(self, n) -> float` (physical, degrees).
  - The events themselves stay unchanged, so existing tests keep working.
- **`sim_vision`:**

```python
SIM_TIP_DIAMETERS = {1: 0.8, 2: 0.4}   # mm, N1 thick, N2 thin (match VisionConfig.tip_diameter)

@dataclass
class SimHeldPart:
    size: tuple[float, float]     # mm (length, width)
    offset: tuple[float, float]   # world mm from the nozzle axis, at nozzle angle a_pick
    angle: float                  # world deg of the long side, at nozzle angle a_pick
    a_pick: float                 # nozzle angle at the pick

class SimPartPhysics:
    """Gives each picked part a random position on the nozzle and records where it truly lands."""
    def __init__(self, sim_machine, part_for_pick=None, *, seed: int = 3,
                 max_offset: float = 0.3, max_angle: float = 4.0)
        # part_for_pick(nozzle, tip_x, tip_y) -> (size, axis_deg) | None; None -> ((2.0, 1.25), 0.0)
        # registers itself in sim_machine.listeners
    held: dict[int, SimHeldPart]
    placed: list[dict]   # {"nozzle", "centre": [x, y] world mm of the part centre, "angle": world deg, "size"}
    def on_event(self, event, angles)
        # pick (n, head_x, head_y): tip = head + SIM_NOZZLE_OFFSETS[n]; random offset (uniform in a disc of
        #   radius max_offset), random error in [-max_angle, max_angle]; angle = axis_deg + error;
        #   a_pick = angles[f"a{n}"]; held[n] = SimHeldPart(...)
        # release (n, head_x, head_y, z): φ = angles[a_n] - a_pick; centre = tip + rot(φ) @ offset;
        #   angle = held.angle + φ; append to placed; delete held[n]
    def view(self, n, nozzle_angle) -> tuple[SimHeldPart, float] | None   # for the renderer
```

- **`make_up_renderer(position, physics=None, angles=None)`:**
  - `angles()` returns `{"a1", "a2"}` in degrees.
  - Tips use `SIM_TIP_DIAMETERS`. The default call (both None) must give the same image as today for N1. N2's tip is smaller, which is fine. `tests/test_calibration.py` and `tests/test_api_vision.py` must still pass.
  - For each nozzle with a held part: `φ = a_now - a_pick`. The part centre is at `tip + rot(φ) @ offset`, and its long side at `angle + φ`. Draw it as a filled rotated rectangle `size` (grey 225) over the tip, with the mapping `centre_px + sim_up_A() @ (point - SIM_UP_CAMERA_XY)`, a -0.5 px shift (continuous convention, as the phase 5 board) and sub-pixel `cv2` shift.

## 6. App wiring, API, runner (`pnp/api/*`, `pnp/job/runner.py`)

- **`app.state.head` is built with an inspector** when cameras exist with an "up" camera: `inspector = lambda n: inspect_nozzle(machine, cameras.get("up"), calibration.A("up"), n, machine.cfg.vision)`.
  - If `A("up")` is None at call time, raise `InspectError("calibrate the up camera first")`. The head turns that into a "skipped" record, so placement continues blind with a logged warning.
  - Read the calibration at call time, because it can change.
- **Runner:**
  - Passes `vision=item.vision, body=item.body` to `pick_and_place`.
  - `status()` adds `"last_inspection": {nozzle: dict or None}`, copied from `head.last_inspection` as JSON-safe dicts.
- **API:**
  - `GET /api/head` adds `"last_inspection"`.
  - `POST /api/head/inspect {nozzle}` runs `head.inspect`. It returns 409 if the head is busy or the machine isn't homed, and 409 if there's no up camera or no up position for that nozzle.
  - `GET/PUT /api/package-rules` pass `body` and `vision` through.
  - `PlanItem` JSON includes `vision` and `body`.
- **`--sim` (`__main__.py`):**
  - Seed `up_position[n] = SIM_UP_CAMERA_XY - t_n` for both nozzles in the sim calibration, when missing.
  - Create `SimPartPhysics(sim.machine, part_for_pick)`. `part_for_pick` finds the enabled feeder whose pick location is within 1 mm of the tip, then returns `(rules.body_for(feeder.package) or (2.0, 1.25), angle(feeder.direction) + feeder.rotation)`. It uses the app's feeder store and rules, and returns None when nothing is near.
  - Pass `physics` and `angles` to `make_up_renderer`.

## 7. Tests

- **`tests/test_bottom.py`** (synthetic cv2 images + `sim_up_A()`):
  - A rotated 2.0×1.25 rectangle at a known offset and angle gives offset, size and angle within 0.02 mm, 0.02 mm and 0.3°.
  - A SOIC-like body with leads (body rectangle plus 8 lead rectangles) gives the 6.0×4.9 envelope.
  - A bare tip gives None. An empty image gives None.
  - `angle_error` wraps correctly (e.g. 89° vs 0° → −1°; 44° → 44°; 46° → −44°).
  - The mirror case works: the A of the simulator.
- **`tests/test_sim_parts.py`:**
  - Physics: a pick then a release with a nozzle turn records the true centre and angle correctly (check by hand).
  - The renderer draws a held part where the physics says.
  - Without physics the image is unchanged for N1.
- **`tests/test_head_ops.py`** and/or a new `tests/test_bottom_vision.py` (simulator, with the up camera renderer and the physics):
  - With vision, the true part centres land within 0.05 mm of the targets and their angles within 0.5° (part tape axis + placement rotation). This holds even for a non-zero placement rotation such as 90° and 37°.
  - Without vision, at least one part is more than 0.1 mm off.
  - A wrong size (`part_for_pick` returns a 3× body) is discarded: the sim records a release at the discard position, then `PickError`.
  - No part (physics returns a "no part" flag): add `SimPartPhysics.drop_next: bool` for tests. The next pick then holds nothing visible, though the vacuum still says "part". The result is `PickError` "No part on nozzle", recorded as "no part".
  - `inspect()` works on an empty nozzle and reports "no part" without raising.
- **API tests** in `tests/test_api_job.py` or `tests/test_api_feeders.py`:
  - `POST /api/head/inspect`.
  - `last_inspection` in `GET /api/head`.
  - The rules round trip with `body` and `vision`.
- **Runner end to end:** the demo panel with vision places every part within 0.05 mm of truth.

## 8. UI (`pnp/web`)

Follow the Graphite rules in `styles.css`. Use tokens only, sentence-case copy, and no new primary buttons.

- **Settings → Nozzle rules:**
  - Add a **Body mm** column with two `input.num` fields (length × width; empty = no check).
  - Add a **Vision** column (checkbox, default on).
  - Both are saved with the rules.
- **Feeders tab → Actions → Head:** add an **Inspect part** button (`POST /api/head/inspect` with the selected nozzle). Below the "Holding" line, show the last inspection of each nozzle: result badge (ok = success, skipped = neutral, no part / rejected = danger), offset in mm, angle error in °, and size `L × W mm`.
- **Job tab → Run card:** a line "Last part: offset 0.12 mm, angle −2.1°, 2.00 × 1.25 mm" from `status.last_inspection`, for the nozzle that placed last (or both). Use a neutral badge "Vision off" when the result is "skipped".
- **Machine tab:** no change. The up camera view already shows the part.
