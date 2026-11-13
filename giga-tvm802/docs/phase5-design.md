# Phase 5 design: fiducials

Goal (from PLAN.md): board alignment from 2–3 fiducials, so that the placement error comes from the pick, not from the board position.

Today the user sets the board origin and rotation by hand with the camera (phase 4). In phase 5, the application measures fiducial marks with the down camera and calculates the board position and rotation itself.

This is the contract between the phase 5 modules. Read `docs/phase2-design.md`, `docs/phase3-design.md` and `docs/phase4-design.md` first; their conventions all still apply:

- **World coordinates** are in mm, and the head position `H` is where the **down camera centre** looks. A world point `W` is therefore under the camera crosshair when the head is at `W`.
- **Down camera scale** is `A` (2×2, px per mm) from `calibration.yaml`: moving the head by `d` (mm) shifts the image content by `A @ d` (px). A feature at pixel offset `o` from the image centre (px) is at world point `W = H + move_for_offset(A, o)` (`pnp/vision/calibration.py`). The pixel convention is continuous: the image centre is `(w/2, h/2)`, and blob centroids are moment centroid + 0.5.
- **Machine resolution** is 0.0305 mm. **Angles** are degrees, CCW positive. `rot(a) = [[cos a, -sin a], [sin a, cos a]]`.
- **Files** are YAML, saved atomically. **Code style:** Python 3.11+, dataclasses, type hints, short docstrings, no new dependencies.
- **Windows:** the interpreter is `.venv/Scripts/python`. Run tests with `.venv/Scripts/python -m pytest -q tests/<file>`.
- **Never** run git commit, add, push, stash, checkout or reset.

---

## 1. Fiducials in the job (`pnp/job/job.py`, `pnp/job/kicad.py` unchanged)

```python
@dataclass
class Fiducial:
    name: str          # e.g. "FID1"
    x: float           # board mm
    y: float

FIDUCIAL_REF = re.compile(r"^FID\d*$", re.IGNORECASE)

def is_fiducial(part) -> bool
    # FIDUCIAL_REF matches part.ref, or "fiducial" is in part.package.lower()
```

New `Job` fields, at the end:

```python
    fiducials: list[Fiducial] = field(default_factory=list)   # board coordinates, top side only
    fiducial_mode: str = "off"        # "off" | "panel" | "board"
    fiducial_diameter: float = 1.0    # copper diameter of the fiducial (mm)
    fit: dict | None = None           # last fiducial measurement (section 4), None = not measured
    board_fits: dict[int, list[float]] = field(default_factory=dict)
        # "board" mode only: board index -> [origin_x, origin_y, rotation_deg] (board coords -> world)
```

- `job_from_pos`: top-side parts for which `is_fiducial(part)` is true become `Fiducial`s, not `JobPart`s. Bottom-side fiducials are dropped. `fiducial_mode = "panel"` if at least one fiducial was found, else `"off"`.
- `to_world(job, board, x, y)`: when `board in job.board_fits`, use `world = rot(r_b) @ (x, y) + (ox_b, oy_b)`. Otherwise use the phase 4 formula (board instance → panel → `job.rotation` / `job.origin`). In this case `ValueError` is raised only when the origin is None.
- `world_rotation(job, board, part_rotation)`: with a board fit, `part_rotation + r_b`. Otherwise use the phase 4 formula. Both are normalised to (-180, 180].
- `def clear_fit(job) -> None`: `job.fit = None` and `job.board_fits = {}`. The API calls it whenever the origin, the rotation, the panel or the fiducial list changes.
- `JobStore` saves and loads the new fields. Old `job.yaml` files without them still load. YAML keys must be strings or ints; save `board_fits` with int keys, and accept string keys on load.
- `plan()`: no change in behaviour, apart from using the new `to_world` / `world_rotation`.

Add 3 fiducials to `examples/demo.pos` (top side, `Fiducial` value, `Fiducial_1mm_Mask2mm` package): `FID1` at (2, 2), `FID2` at (38, 2), `FID3` at (2, 28). Update the tests that count the demo parts. The demo still has 11 placeable parts plus 3 fiducials.

## 2. Fiducial detection and fitting (`pnp/vision/fiducials.py`, new, no FastAPI, no machine I/O except `locate_fiducial`)

```python
@dataclass
class Circle:
    u: float           # px, continuous convention
    v: float
    diameter_px: float # equivalent diameter, 2 * sqrt(area / pi)
    circularity: float # 4 * pi * area / perimeter**2 (1.0 = perfect circle)

def find_circles(img, diameter_px: float, polarity: str = "bright", tolerance: float = 0.3,
                 min_circularity: float = 0.8) -> list[Circle]
    # Gaussian blur 5x5, Otsu threshold, polarity "bright" (copper on dark mask), "dark" or "auto"
    # (as find_blob). Contours (RETR_LIST, so a pad inside a mask opening is found), keep those with
    # |diameter_px / expected - 1| <= tolerance and circularity >= min_circularity, and not touching
    # the image border. Sorted by distance to the image centre (nearest first).

class FiducialError(Exception): ...   # the message is for the user

async def locate_fiducial(machine, camera, A, diameter_mm: float, *, search_mm: float = 3.0,
                          iterations: int = 3, tol_px: float = 0.5, settle: float = 0.2,
                          resolution_mm: float = 0.0305, polarity: str = "bright") -> tuple[float, float]
    # The head is already near the fiducial. diameter_px = diameter_mm * mean singular value of A.
    # Repeat: take a fresh frame (camera.fresh_frame(after=monotonic()+settle)), find_circles, take
    # the nearest circle. If there is none, or it is further than search_mm from the image centre
    # (convert with move_for_offset), raise FiducialError("no fiducial found within 3 mm").
    # If the offset is <= tol_px, or the move is below resolution_mm / 2 on both axes, return
    # H + move_for_offset(A, o) (the measured world point, which includes the remaining sub-step
    # offset). Otherwise move the head by move_for_offset(A, o) and repeat. After `iterations`
    # moves, return H + move_for_offset(A, o) from the last frame anyway (it is accurate; the
    # extra moves only reduce lens distortion).

@dataclass
class RigidFit:
    rotation: float                  # deg
    origin: tuple[float, float]      # translation t: world = rot(rotation) @ p + t
    scale: float                     # from the similarity fit (1.0 = no scale error); 1.0 for 1 point
    residuals: list[float]           # mm, |rot @ p_i + t - w_i| for each point
    rms: float                       # mm
    max_error: float                 # mm

def fit_rigid(nominal: list[tuple[float, float]], measured: list[tuple[float, float]],
              rotation_hint: float = 0.0) -> RigidFit
    # 1 point:  rotation = rotation_hint, t = w - rot(hint) @ p, scale 1.0.
    # >= 2 points: centre both sets (p' = p - mean(p), q' = w - mean(w)). Rotation angle =
    #   atan2(sum(p'x*q'y - p'y*q'x), sum(p'x*q'x + p'y*q'y)) (2-D Kabsch, no scale);
    #   t = mean(w) - R @ mean(p). Report scale s = sum(q'_i . (R @ p'_i)) / sum |p'_i|^2
    #   (the fit itself does not apply the scale).
    # ValueError: different lengths, no points, or (>= 2 points) the nominal points are less than
    #   5 mm apart (rotation is not measurable).
```

## 3. Simulated board (`pnp/sim/sim_vision.py`)

So that phase 5 can be tested without a machine, the simulated down camera also draws boards at a **true** position that differs a little from the nominal job position.

```python
@dataclass
class SimBoard:
    origin: tuple[float, float]       # world mm of the board's (0, 0)
    rotation: float                   # deg
    size: tuple[float, float]         # board mm (width, height), from (0, 0)
    fiducials: list[tuple[float, float]] = field(default_factory=list)   # board mm
    fiducial_diameter: float = 1.0    # copper (mm); the mask opening is 2x this
    parts: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y, rotation) board mm,
                                      # drawn as 1.0 x 0.5 mm copper pads (rectangles), for the look only

def make_down_renderer(position, boards: list[SimBoard] | None = None)
```

- **Drawing order:** bed and dots (as now), then each board as a dark solder-mask polygon (grey 50) covering its size, then the mask openings (grey 35 discs, 2× the fiducial diameter), then copper (grey 215): the fiducial discs and the part pads.
- Use the same sub-pixel drawing (`cv2` shift) and noise as the existing renderer. The fiducial centroid must be accurate to better than 0.1 px.
- Skip drawing anything that is not near the visible window, so frame time stays low for a 2-board panel.
- `boards=None` or `[]` gives exactly the current image.
- **Truth helper:** `sim_board_truth(nominal_origin, nominal_rotation, panel_offsets: list[tuple[float, float]], size, fiducials, parts) -> list[SimBoard]` places board i at `rot(TRUE_ROT) @ (panel_offset_i) + true_origin`, with rotation `nominal_rotation + SIM_BOARD_ERROR_ROT`. Here `true_origin = nominal_origin + SIM_BOARD_ERROR_XY`, `SIM_BOARD_ERROR_XY = (0.35, -0.25)` and `SIM_BOARD_ERROR_ROT = 0.4` (deg). Panel offsets are already in panel coordinates, so with `nominal_rotation = 0` the true panel transform is `world = rot(0.4) @ p + true_origin`.

## 4. Measuring a job (`pnp/job/align.py`, new)

```python
class AlignError(Exception): ...   # user-facing message

async def measure_fiducials(job: Job, machine, camera, A, *, settle: float = 0.2,
                            max_rms: float = 0.15, max_scale_error: float = 0.005,
                            log=None) -> Job
```

1. **Checks:** the machine is homed, `job.origin` is set (the rough estimate), `job.fiducials` is not empty, `job.fiducial_mode != "off"`, and at least one board is not skipped. Each failure raises `AlignError` with a clear message.
2. **Boards to measure:** in `"panel"` mode, the first and the last board that is not skipped (one board if they are the same). In `"board"` mode, every board that is not skipped.
3. **Each fiducial on each of those boards:**
   1. Predict the world point with the **current estimate**. Start with `to_world` from the user's origin and rotation, ignoring old fits: call `clear_fit` on a working copy first.
   2. In `"panel"` mode, once 2 or more points are measured, re-fit and use that fit for the next predictions. This keeps big panels on target.
   3. Move the head to the prediction, then call `locate_fiducial`.
   4. On `FiducialError`, raise `AlignError(f"Couldn't find {name} on board {b + 1} near X {x:.2f} Y {y:.2f}. Check the board origin and the fiducial diameter.")`.
4. **Fit:**
   - `"panel"`: the nominal points are panel coordinates (board instance transform applied to the fiducial's board coords). Fit with `fit_rigid(..., rotation_hint=job.rotation)`. On success, set `job.origin = fit.origin` and `job.rotation = fit.rotation`, and clear `board_fits`.
   - `"board"`: one `fit_rigid` per board, using the board coordinates as nominal. Set `job.board_fits[b] = [ox, oy, rotation]`. Also set `job.origin` and `job.rotation` from the first measured board's fit composed with its panel offset, so manual Go to stays sensible.
   - Reject the fit with an `AlignError` if `rms > max_rms` (tell the RMS and the worst fiducial) or, with 2 or more points, `|scale - 1| > max_scale_error`. In that case, leave `job` unchanged.
5. **Record** `job.fit = {"mode", "rms", "max_error", "scale", "rotation", "origin", "measured_at" (ISO UTC), "points": [{"board", "name", "expected": [x, y], "measured": [x, y], "error": mm}]}`. Here `expected` is the prediction from the user's original origin and rotation, so the error shows how far off the manual setting was. Return the job; the caller saves it.
6. Move the head back over the first fiducial measured, so the user sees the result.

## 5. Runner, API and simulator wiring

- **`JobRunner`** gets an optional `align: Callable[[], Awaitable[None]] | None` constructor argument (keyword, default None). When `start()` or `resume()` is called, and the job has `fiducial_mode != "off"`, fiducials, and `fit is None`, the loop first sets `current = "fiducials"` and awaits `align()`. On `AlignError`, `FiducialError` or `MachineError`, the state becomes `error` with the message, and nothing is placed. After alignment, the loop re-reads the job and places parts as before.
- **`app.state.cameras`:** `add_vision_routes` stores the `CameraManager` (or None) there.
- **New routes** in `pnp/api/job_routes.py`. Mutations return 409 while the runner is busy or `head.busy`.

  | Method and path | Body | Returns |
  |---|---|---|
  | `PUT /api/job/fiducials` | `{mode, diameter, fiducials: [{name, x, y}]}` | same as `GET /api/job`; clears the fit |
  | `POST /api/job/fiducials/measure` | | same as `GET /api/job`. 409: not homed, no origin, no fiducials, mode off, down camera not calibrated or missing. 422: `AlignError` (message as detail) |
  | `POST /api/job/fiducials/clear` | | same as `GET /api/job`; clears the fit |
  | `POST /api/job/fiducials/{name}/goto` | `{board: 0}` | `{position}`; moves the camera to the predicted fiducial point |

  `PUT /api/job/board`, `POST /api/job/align/origin`, `POST /api/job/align/rotation`, `PUT /api/job/panel` and `POST /api/job/panel/grid` must call `clear_fit`. `GET /api/job` includes the new job fields: `fiducials`, `fiducial_mode`, `fiducial_diameter`, `fit` and `board_fits`, with string keys in JSON.
- **The measurement** uses `app.state.cameras.get("down")` and `app.state.calibration.A("down")`, and holds the head lock while it runs. Use `head._exclusive("aligning")`, or add a public `HeadOps.exclusive(what)` alias that returns the same context manager, so no pick can run at the same time.
- **`--sim` seeding** in `pnp/api/__main__.py`: build the demo job as now, with fiducials from `examples/demo.pos` and mode "panel". Then pass `boards=sim_board_truth(...)` (2 boards, panel offsets (0, 0) and (50, 0), size (40, 30), the demo fiducials and part positions) to `make_down_renderer`. The nominal job origin stays (150, 150) with rotation 0, so the simulator board is truly at (150.35, 149.75) rotated by 0.4°.
- **Tests:**
  - `tests/test_fiducials.py`: unit tests for `find_circles` (synthetic cv2 images: a circle, a rectangle of similar area that is rejected, a wrong size that is rejected, a circle cut by the border that is rejected, and sub-pixel accuracy better than 0.1 px) and for `fit_rigid` (exact recovery of a known rotation and translation with 2 and 3 points, noise, a scale error reported, 1 point using the hint, and errors for bad inputs).
  - `tests/test_sim_board.py`: the renderer draws fiducials whose measured centroid matches the truth, and `boards=None` gives the old image.
  - `tests/test_align.py`: against the simulator with a `SimCamera` and the board renderer, through `measure_fiducials`.
    - `"panel"` mode recovers the true origin within 0.03 mm and the rotation within 0.03°.
    - `"board"` mode recovers each board.
    - A missing fiducial gives an `AlignError` and leaves the job unchanged.
    - The runner aligns first and then places, and the sim release events land within 0.06 mm of the **true** pad positions (they were about 0.4 mm off without fiducials).
  - API tests are added to `tests/test_api_job.py`.

## 6. UI (`pnp/web`)

Add a **Fiducials** card to the Job tab, after Board. Follow the Graphite rules already in `styles.css` and the phase 4 UI: tokens only, existing classes, sentence-case copy, verb-first buttons, and no second primary button (the Job tab's primary stays "Start job").

- **Mode:** a segmented control with Off / Panel / Each board, plus the fiducial diameter in mm (`input.num`). Both save with `PUT /api/job/fiducials`.
- **Fiducial table:** name, X, Y (editable), a Go to icon button (`POST /api/job/fiducials/{name}/goto` with board 0), and a remove button. An "Add fiducial" button.
- **Measure fiducials** (secondary button), with a hint: "Set the board origin roughly first. The camera then finds each fiducial and corrects the origin and rotation."
- **Result**, when `fit` is set:
  - a success badge "Measured" with the time;
  - RMS, worst error, rotation, origin, and scale as ppm or %;
  - a small table per point: board, name, error in mm;
  - a "Clear measurement" ghost button.

  Show a warning badge "Not measured" when fiducials exist, the mode is not off and `fit` is null, with the hint "The job measures them before the first part."
- The run card shows `current === "fiducials"` as "Measuring fiducials".
