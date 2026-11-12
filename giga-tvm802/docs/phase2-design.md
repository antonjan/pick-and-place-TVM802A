# Phase 2 design: cameras and calibration

This is the contract between the phase 2 modules. Code must follow these names and conventions exactly.

## Hardware (measured 2026-11-12)

| Role | Device (stable path) | Notes |
|---|---|---|
| down (on head, looks at the bed) | `/dev/v4l/by-id/usb-Microsoft_Video_Camera_000F330056215848-video-index0` | Microsoft 045e:0294; hangs on open (faulty camera or cable) |
| up (fixed, looks at the nozzles) | `/dev/v4l/by-id/usb-Microsoft_Video_Camera_000F330011585848-video-index0` | Microsoft 045e:0294; 30 fps at 640x480 |

Both support MJPG at 160x120 … 960x720 (YUYV up to 1280x960). Configured at **MJPG 640x480** (960x720 gave only 4–7 fps). OpenCV 5.0 (`opencv-python-headless`) and numpy are installed in `.venv`.

## Conventions

- **Pixels:** `(u, v)` from the top-left, u to the right, v down. Image centre `c = (w/2, h/2)`. A feature's *offset* is `o = p - c`.
- **Machine coordinates** are mm (see `pnp/machine/machine.py`). Head position `H = (x, y)` = `machine.position()["x"], ["y"]`.
- **Down camera convention:** the down camera's image centre looks at machine point `H`. All other head tools are offsets from it.
- **Scale matrix `A`** (2×2, px per mm, per camera): moving the head by `d = (dx, dy)` mm shifts what the camera sees by `s = A @ d` px. For the down camera the features on the bed move; for the up camera the nozzle tip moves. A general 2×2 matrix covers scale, camera rotation and mirroring.
- **Centring a feature** at offset `o`: move the head by `d = -inv(A) @ o`.
- **Nozzle offset `t_n`** (mm) for nozzle n: nozzle n's tip is at world point `H + t_n`. It is measured by centring the down camera on a mark (head at `H_c`), then jogging nozzle n's tip onto the same mark (head at `H_n`): `t_n = H_c - H_n`. To put nozzle n over world point `W`, move the head to `W - t_n`.
- **Up camera position `U_n`** (mm): head position at which nozzle n's tip is centred in the up camera.
- **Settling:** a frame used for measuring must be captured after the move has finished, plus `settle` seconds (default 0.2 s): `await cam.fresh_frame(after=time.monotonic() + settle)`.

## Module: `pnp/vision/camera.py`

```python
@dataclass
class CameraConfig:
    name: str
    device: str
    width: int = 960
    height: int = 720
    fourcc: str = "MJPG"
    fps: int = 30
    rotate: int = 0          # 0/90/180/270, applied after capture
    flip_x: bool = False
    flip_y: bool = False
    controls: dict[str, int] = field(default_factory=dict)  # v4l2 controls set at start

class Frame(NamedTuple):
    image: np.ndarray   # BGR uint8, already rotated/flipped
    t: float            # time.monotonic() when captured
    seq: int            # increments per frame

class CameraError(Exception): ...

class Camera:  # base class, capture runs in a daemon thread
    name: str
    def start(self) -> None
    def stop(self) -> None
    def latest(self) -> Frame | None
    async def fresh_frame(self, after: float | None = None, timeout: float = 3.0) -> Frame
        # first frame with t > after (default: now); raises CameraError on timeout
    def jpeg(self, quality: int = 80) -> bytes | None   # latest frame as JPEG
    def status(self) -> dict  # {name, device, connected, fps (measured), width, height, error}
    @property size -> tuple[int, int]  # (w, h) of delivered frames

class UvcCamera(Camera):     # OpenCV CAP_V4L2, sets FOURCC/size/fps, BUFFERSIZE=1,
                             # reconnects every 2 s if the device fails or disappears
class SimCamera(Camera):     # SimCamera(name, render: Callable[[], np.ndarray], fps=15)

class CameraManager:
    def __init__(self, cameras: dict[str, Camera])
    def get(self, name: str) -> Camera        # KeyError if unknown
    def names(self) -> list[str]
    def start_all(self) / stop_all(self)

def load_cameras_config(path) -> dict[str, CameraConfig]   # config/cameras.yaml
def build_uvc_manager(path) -> CameraManager
```

## Module: `pnp/sim/sim_vision.py` (synthetic camera images for `--sim` and tests)

The constants are the ground truth that the calibration tests check against:

```python
SIM_DOWN_PX_PER_MM = 25.0
SIM_DOWN_ANGLE_DEG = 1.5            # camera rotated slightly
SIM_UP_CAMERA_XY = (150.0, 30.0)    # world position of the up camera
SIM_UP_PX_PER_MM = 30.0
SIM_UP_MIRROR = True                # looking up mirrors X
SIM_NOZZLE_OFFSETS = {1: (-42.0, -3.0), 2: (-58.0, -3.0)}   # t_n in mm
SIM_SIZE = (640, 480)

def make_down_renderer(position: Callable[[], tuple[float, float]]) -> Callable[[], np.ndarray]
    # light-grey bed with dark 1 mm dots every 4 mm, plus a dark cross mark (2 mm arms) at world (100, 100)
    # and small noise. World point W appears at pixel c + R(angle) @ diag(k, -k) @ (W - H), k = px/mm.
def make_up_renderer(position: Callable[[], tuple[float, float]]) -> Callable[[], np.ndarray]
    # dark background, bright 0.8 mm disc for each nozzle tip at world T = H + t_n,
    # drawn at pixel c + diag(-k, -k) @ (T - SIM_UP_CAMERA_XY), k = SIM_UP_PX_PER_MM
    # (looking up: X appears mirrored compared with the down camera)
def sim_down_A() -> np.ndarray   # = -R(angle) @ diag(k, -k)   (exact A implied by the down renderer)
def sim_up_A() -> np.ndarray     # = diag(-k, -k)
```

## Module: `pnp/vision/calibration.py` (math and procedures; no FastAPI)

```python
class CalibrationError(Exception): ...

def to_gray(img) -> np.ndarray
def measure_shift(template: np.ndarray, img: np.ndarray, min_score: float = 0.6) -> tuple[float, float, float]
    # where the template's centre is found in img, minus the image centre: (su, sv, score).
    # TM_CCOEFF_NORMED on grayscale + sub-pixel parabola refinement. CalibrationError if score < min_score.
def centre_patch(img, frac: float = 0.25) -> np.ndarray   # square crop of side frac*min(w,h) at the centre
def solve_scale(moves_mm: list[tuple[float, float]], shifts_px: list[tuple[float, float]]) -> tuple[np.ndarray, float]
    # least squares A (s = A @ d) and RMS residual in px; needs >= 3 non-collinear moves
def move_for_offset(A: np.ndarray, offset_px: tuple[float, float]) -> tuple[float, float]   # -inv(A) @ o
def find_blob(img, polarity: str = "auto", min_area: int = 20, max_area: int | None = None) -> tuple[float, float] | None
    # centroid (u, v) of the blob nearest the image centre after Otsu threshold; "dark"/"bright"/"auto"

async def calibrate_scale(machine, camera, *, step_mm: float = 1.0, settle: float = 0.2) -> dict
    # head must be homed. Template = centre patch of a frame at the start position.
    # Moves (relative to start): (+s,0), (-s,0), (0,+s), (0,-s), (+s,+s), (-s,-s); measures the shift
    # after each, returns to start, solves A.
    # Returns {"A": [[..],[..]], "rms_px": float, "mm_per_px": float (mean), "angle_deg": float, "mirrored": bool}
    # mirrored = det(A) > 0 (a normal down-looking camera has det(A) < 0 because v points down)
async def centre_on_blob(machine, camera, A, *, polarity="auto", iterations: int = 4, tol_px: float = 1.0,
                         settle: float = 0.2) -> tuple[float, float]
    # iteratively move so the nearest blob sits at the centre; returns the final head (x, y)
```

`machine` is a `pnp.machine.machine.Machine`. Only use `await machine.move_to(x=..., y=...)` and `machine.position()`. `camera` only needs `fresh_frame()`.

## Module: `pnp/vision/calibration_store.py`

```python
@dataclass
class CameraCal:
    A: list[list[float]]     # px per mm
    rms_px: float
    date: str                # ISO timestamp
    def matrix(self) -> np.ndarray

@dataclass
class Calibration:
    cameras: dict[str, CameraCal]              # "down", "up"
    nozzle_offset: dict[int, tuple[float, float]]       # t_n
    up_position: dict[int, tuple[float, float]]         # U_n
    def save(self, path) -> None      # YAML, written atomically (tmp file + rename)
    @classmethod
    def load(cls, path) -> "Calibration"   # a missing file gives an empty Calibration
```

The default path is `config/calibration.yaml`, which is written by the app. `config/cameras.yaml` is edited by hand.

## API/UI (built after the modules above)

- `GET /api/cameras`: status list. `GET /cameras/{name}/stream.mjpg` (multipart, about 10 fps). `GET /cameras/{name}/snapshot.jpg`.
- `POST /api/cameras/{name}/click {u, v}` (full-resolution pixel): with that camera calibrated, move the head by `move_for_offset(A, (u,v) - c)`.
- `GET /api/calibration`
- `POST /api/calibrate/{name}/scale {step_mm}`: runs `calibrate_scale` and saves.
- `POST /api/calibrate/{name}/centre {polarity}`: runs `centre_on_blob`.
- `POST /api/calibrate/nozzle-offset/camera`: remembers the current head position as `H_c`.
- `POST /api/calibrate/nozzle-offset/{n}`: `t_n = H_c - H_now`, then saves.
- `POST /api/calibrate/up-position/{n}`: `U_n = H_now`, then saves.
- `POST /api/tools/nozzle-to-camera/{n}`: moves the head so nozzle n sits where the camera centre was looking (`H - t_n`). `POST /api/tools/camera-to-nozzle/{n}` does the reverse.
- UI, Machine tab: the down and up live views with a crosshair, click-to-move once calibrated, and an LED toggle next to the up view.
- UI, Setup tab: calibration wizard (1. down scale, 2. nozzle offsets, 3. up scale, 4. up positions) showing the current values.
- `python -m pnp.api --sim` uses `SimCamera`s fed by `sim_vision`. Real mode loads `config/cameras.yaml`. `--no-cameras` disables cameras.
