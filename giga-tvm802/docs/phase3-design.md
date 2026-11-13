# Phase 3 design: strip feeders and manual pick and place

Goal (from PLAN.md): teach a strip feeder, then "pick from feeder F with nozzle N and place at the camera crosshair" works repeatably, tape feeding included.

This is the contract between the phase 3 modules. Read `docs/phase2-design.md` first for the conventions; they all still apply:

- **World/machine coordinates** are in mm, and the head position `H` is where the **down camera centre** looks.
- **Nozzle offset `t_n`:** nozzle n's tip is at `H + t_n`. To put nozzle n over world point `W`, move the head to `W - t_n`.
- **Seesaw Z:** `machine.nozzle_down(n, depth)` lowers nozzle n by `depth` mm, and `machine.nozzles_up()` raises both.
- **Machine resolution** is 0.0305 mm (1000-step quanta). `Machine` snaps targets itself.

## 1. New head tool: the prick needle

The TVM802 feeds stock strip feeders with a needle on the head: it pokes into the tape, the head drags the tape by one pitch, and the needle lifts. The needle is the machine output `prick` (on = down) and has no Z axis.

- **Offset:** `t_prick` (mm), with the needle tip at `H + t_prick`. It's measured like a nozzle offset: centre the down camera on a mark (`H_c`), lower the needle and jog it onto the mark (`H_p`), then `t_prick = H_c - H_p`.
- **Storage:** `Calibration.prick_offset: tuple[float, float] | None`, saved in `calibration.yaml` as `prick_offset: [x, y]` (missing → `None`).
- **Simulator truth:** `SIM_PRICK_OFFSET = (-50.0, 12.0)` in `pnp/sim/sim_vision.py`.

## 2. Config: `HeadConfig` (new `head:` section of `config/machine.yaml`)

```python
@dataclass
class HeadConfig:
    pick_dwell: float = 0.15        # s with the nozzle down on the part, vacuum on
    place_dwell: float = 0.10       # s with the nozzle down before releasing
    blow_time: float = 0.10         # s of blow-off after releasing vacuum at place/discard
    vacuum_settle: float = 0.10     # s after raising before reading the vacuum sensor
    prick_settle: float = 0.20      # s after switching the needle down/up
    check_vacuum: bool = True       # verify part presence after pick
    place_depth: float = 3.0        # default nozzle depth for placing (mm)
    discard: tuple[float, ...] = () # world [x, y] of the discard bin; empty = not set
```

Add `head: HeadConfig` to `MachineConfig` in `pnp/config.py`, and add a commented `head:` section to `config/machine.yaml`. The existing strict key checking must keep working.

## 3. Feeders: `pnp/job/feeders.py`

```python
@dataclass
class Feeder:
    id: str                          # unique, e.g. "F1"
    name: str = ""
    kind: str = "strip"              # "strip" | "drag"
    pick: tuple[float, float] = (0.0, 0.0)       # world mm, see below
    direction: tuple[float, float] = (1.0, 0.0)  # unit vector along the tape (normalised on load)
    pitch: float = 4.0               # mm between pockets
    count: int = 0                   # parts taken so far
    capacity: int | None = None      # strip: pockets in the strip (None = unknown)
    rotation: float = 0.0            # part rotation in the tape (deg)
    pick_depth: float = 3.0          # nozzle depth to touch the part (mm)
    prick: tuple[float, float] | None = None     # drag: world mm where the needle enters the tape
    needs_feed: bool = True          # drag: True when the pick pocket is empty
    enabled: bool = True
    value: str = ""                  # part value / package, matched against jobs later
    package: str = ""

    def pick_location(self) -> tuple[float, float]
        # strip: pick + count * pitch * direction (each part sits one pitch further along)
        # drag:  pick (the fixed pick position)
    def remaining(self) -> int | None  # strip with capacity: capacity - count; otherwise None
    def drag_vector(self) -> tuple[float, float]   # pitch * direction
    def after_pick(self) -> None     # count += 1; drag: needs_feed = True
    def after_feed(self) -> None     # drag: needs_feed = False
    def validate(self) -> None       # ValueError: unknown kind, pitch <= 0, zero direction,
                                     # drag without prick, strip exhausted is NOT a validation error

class FeederError(Exception): ...    # e.g. strip empty, feeder disabled, unknown id

class FeederStore:
    def __init__(self, path)                  # config/feeders.yaml; missing file → empty
    def all(self) -> list[Feeder]
    def get(self, id) -> Feeder               # FeederError if unknown
    def upsert(self, feeder) -> None          # validate, replace or append, save
    def delete(self, id) -> None
    def save(self) -> None                    # atomic (temp file + os.replace), YAML {"feeders": [ {...}, ... ]}
```

## 4. Pick and place: `pnp/job/head_ops.py`

```python
class NotCalibrated(MachineError): ...   # missing nozzle/prick offset (HTTP 409 via status_for)
class PickError(MachineError): ...       # no vacuum after picking (HTTP 422)

class HeadOps:
    def __init__(self, machine, calibration: Calibration, cfg: HeadConfig, feeders: FeederStore)
    busy: str | None                      # "picking" / "placing" / "feeding" / None
    def head_for(self, tool: int | str, W) -> tuple[float, float]
        # tool 1/2 → W - t_n ; tool "prick" → W - t_prick ; NotCalibrated if missing
    async def move_tool_over(self, tool, W) -> None   # machine.move_to(x=, y=) (safe: nozzles go up first)
    async def feed(self, feeder) -> None
        # drag only: move needle over feeder.prick, prick on, sleep prick_settle,
        # move the head by feeder.drag_vector() (relative), prick off, sleep prick_settle,
        # feeder.after_feed(), store.save(). Always switch prick off in a finally block.
    async def pick(self, nozzle: int, feeder) -> None
        # FeederError if disabled or (strip) remaining() == 0
        # drag and needs_feed → feed() first
        # pump on; rotate nozzle to 0; move nozzle over feeder.pick_location();
        # vacuum<n> on; nozzle_down(n, feeder.pick_depth); sleep pick_dwell; nozzles_up();
        # sleep vacuum_settle; if check_vacuum and not await machine.has_part(n):
        #     vacuum off, raise PickError (the feeder is NOT advanced)
        # feeder.after_pick(); store.save()
        # remember which feeder's part is on the nozzle: self.holding[n] = feeder.id
    async def place(self, nozzle: int, W, rotation: float = 0.0, depth: float | None = None) -> None
        # rotate nozzle to (rotation - tape rotation of the held part, 0 if unknown) (machine.rotate);
        # move nozzle over W; nozzle_down(n, depth or cfg.place_depth); sleep place_dwell;
        # vacuum<n> off; blow<n> on; sleep blow_time; blow<n> off; nozzles_up(); holding[n] = None
    async def discard(self, nozzle: int) -> None
        # NotCalibrated-like MachineError if cfg.discard is empty; move nozzle over discard,
        # vacuum off, blow, nozzles up, holding[n] = None
    async def pick_and_place(self, nozzle, feeder, W, rotation=0.0, depth=None) -> None
```

- **Exclusivity:** each public operation holds an `asyncio.Lock`. A second call while busy raises `MachineError("head busy (...)")`.
- **Safety:** on any exception after vacuum went on during pick, switch that nozzle's vacuum off and raise the nozzles before re-raising. On E-stop the machine already switches everything off.

## 5. Simulator support (`pnp/sim/tvm_sim.py`)

`SimMachine.events: list[tuple]` records what the head physically did, in **mm of register (machine) coordinates**:

- `("pick", n, x, y)`: when a part gets attached to nozzle n (vacuum on and nozzle down). `x`, `y` are the head position.
- `("release", n, x, y, z)`: when vacuum on nozzle n switches off while a part is attached.
- `("prick_down", x, y)` and `("prick_up", x, y)`: when the prick output changes.

Tests compute where the tool was with the head position plus the offsets in `SIM_NOZZLE_OFFSETS` / `SIM_PRICK_OFFSET`. Use `STEPS_PER_MM` from `tvm_sim` to convert. Existing simulator behaviour must not change.

## 6. API and UI (second step, built after sections 1–5)

- `GET /api/feeders`, `PUT /api/feeders/{id}` (create/replace, body = feeder fields), `DELETE /api/feeders/{id}`
- `POST /api/feeders/{id}/teach/pick`: `pick =` current head XY (the down camera centre). Strip feeders also get `count = 0`.
- `POST /api/feeders/{id}/teach/prick`: `prick =` current head XY.
- `POST /api/feeders/{id}/goto`: moves the down camera over `pick_location()` (to check visually).
- `POST /api/feeders/{id}/feed`, `POST /api/feeders/{id}/pick {nozzle}`
- `POST /api/head/place {nozzle, x?, y?, rotation=0, depth?}`: x/y default to the current camera centre.
- `POST /api/head/discard {nozzle}`
- `POST /api/feeders/{id}/pick-and-place {nozzle, rotation=0, depth?}`: places at the current camera centre (the crosshair). This is the phase 3 acceptance test.
- `POST /api/calibrate/prick-offset`: `t_prick = camera_mark - H` (uses the phase 2 camera mark).
- UI: a new **Feeders** tab with a feeder table (id, name, kind, pitch, count/remaining, pick position) and an add/edit form. Each feeder gets the buttons Teach pick, Teach prick, Go to, Feed, Pick N1/N2, and Pick & place at crosshair, next to a live down view with crosshair. The Setup tab gets a "Prick needle offset" step.
- `--sim` seeds an empty sim calibration with the simulator truth (nozzle offsets, prick offset, camera scales from `sim_down_A`/`sim_up_A`), so a pick and place demo works immediately.
