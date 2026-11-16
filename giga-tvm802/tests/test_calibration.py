"""Calibration tests against a synthetic world (no hardware, no pnp.sim)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import functools
import time
from dataclasses import dataclass

import cv2
import numpy as np
import pytest

from pnp.vision.calibration import (
    CalibrationError,
    calibrate_scale,
    centre_on_blob,
    centre_patch,
    find_blob,
    measure_shift,
    move_for_offset,
    solve_scale,
)
from pnp.vision.calibration_store import Calibration

# ---------------------------------------------------------------- synthetic world
#
# Pixel convention is the continuous one used by calibration.py: pixel index i
# covers [i, i+1), so the image centre is (w/2, h/2) and a feature drawn at
# index-coordinate centre p appears at continuous coordinate p + 0.5.
#
# Rendering law: a world point W (mm) appears at c + A @ (H - W), where H is the
# head position. Moving the head by d therefore moves the features by +A @ d,
# which is the design's s = A @ d convention, so calibration must recover A.

W_PX, H_PX = 640, 480
CENTRE = np.array([W_PX / 2.0, H_PX / 2.0])
KC = 25.0                       # canvas pixels per mm (equal to the camera scale)
W0 = np.array([-40.0, -40.0])   # world mm at canvas continuous origin
N = 2000                        # canvas side: 80 mm


def rot(deg: float) -> np.ndarray:
    t = np.radians(deg)
    return np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])


A_DOWN = rot(2.0) @ np.diag([25.0, -25.0])     # rotated, not mirrored, det < 0
A_MIRROR = rot(2.0) @ np.diag([25.0, 25.0])    # mirrored, det > 0


def world_to_index(W) -> np.ndarray:
    """World mm to canvas index coordinates (for drawing)."""
    return (np.asarray(W, dtype=float) - W0) * KC - 0.5


@functools.lru_cache(maxsize=None)
def texture_canvas() -> np.ndarray:
    """Light background with random dark 1 mm dots, fixed seed."""
    rng = np.random.default_rng(1234)
    canvas = np.full((N, N), 200, dtype=np.uint8)
    for _ in range(1500):
        x, y = rng.uniform(0, N, 2)
        cv2.circle(canvas, (int(round(x * 16)), int(round(y * 16))), 12 * 16, 60, -1,
                   cv2.LINE_AA, shift=4)
    return canvas


def disc_canvas(centre_mm, radius_mm: float = 1.0, value: int = 40, background: int = 200) -> np.ndarray:
    """Plain background with one disc at a world position (sub-pixel placement)."""
    canvas = np.full((N, N), background, dtype=np.uint8)
    p = world_to_index(centre_mm)
    cv2.circle(canvas, (int(round(p[0] * 16)), int(round(p[1] * 16))),
               int(round(radius_mm * KC * 16)), value, -1, cv2.LINE_AA, shift=4)
    return canvas


def render_view(canvas: np.ndarray, A: np.ndarray, H) -> np.ndarray:
    """Warp the world canvas into a 640x480 camera view for head position H."""
    A = np.asarray(A, dtype=float)
    B = -A / KC   # canvas index -> pixel index (linear part)
    # pixel_index = centre - 0.5 + A @ (H - W0) + B @ (0.5, 0.5) + B @ canvas_index
    t = CENTRE - 0.5 + A @ (np.asarray(H, dtype=float) - W0) + B @ np.array([0.5, 0.5])
    M = np.hstack([B, t.reshape(2, 1)])
    return cv2.warpAffine(canvas, M, (W_PX, H_PX), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=200)


class FakeMachine:
    """Instant machine: move_to sets the position, every move is recorded."""

    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.x = float(x)
        self.y = float(y)
        self.moves: list[tuple[float, float]] = []

    async def move_to(self, x=None, y=None, **kw):
        if x is not None:
            self.x = float(x)
        if y is not None:
            self.y = float(y)
        self.moves.append((self.x, self.y))
        return self.position()

    def position(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": 0.0, "a1": 0.0, "a2": 0.0}


@dataclass
class _Frame:
    image: np.ndarray
    t: float
    seq: int


class FakeCamera:
    """Renders the world canvas as seen from the FakeMachine's current position."""

    def __init__(self, machine: FakeMachine, A, canvas: np.ndarray):
        self.machine = machine
        self.A = np.asarray(A, dtype=float)
        self.canvas = canvas
        self.seq = 0

    async def fresh_frame(self, after=None, timeout: float = 3.0) -> _Frame:
        self.seq += 1
        p = self.machine.position()
        img = render_view(self.canvas, self.A, (p["x"], p["y"]))
        return _Frame(image=img, t=time.monotonic(), seq=self.seq)


# ---------------------------------------------------------------- solve / inverse

SIX_MOVES = [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0), (1.0, 1.0), (-1.0, -1.0)]


def test_solve_scale_recovers_exact_A():
    A = np.array([[24.0, 0.7], [-0.9, -25.2]])
    D = np.array(SIX_MOVES)
    S = D @ A.T
    A_est, rms = solve_scale(list(map(tuple, D)), list(map(tuple, S)))
    assert np.allclose(A_est, A, atol=1e-9)
    assert rms < 1e-9


def test_solve_scale_rejects_collinear_moves():
    moves = [(1.0, 0.0), (2.0, 0.0), (-1.0, 0.0), (3.0, 0.0)]
    shifts = [(25.0, 0.0), (50.0, 0.0), (-25.0, 0.0), (75.0, 0.0)]
    with pytest.raises(CalibrationError):
        solve_scale(moves, shifts)


def test_solve_scale_needs_three_moves():
    with pytest.raises(CalibrationError):
        solve_scale([(1.0, 0.0), (0.0, 1.0)], [(25.0, 0.0), (0.0, 25.0)])


def test_move_for_offset_is_inverse():
    o = np.array([10.0, -4.0])
    for A in (A_DOWN, A_MIRROR):
        d = move_for_offset(A, o)
        assert np.allclose(o + A @ np.array(d), 0.0, atol=1e-9)


# ---------------------------------------------------------------- measure_shift

def _reference_frame() -> np.ndarray:
    return render_view(texture_canvas(), A_DOWN, (0.0, 0.0))


def test_measure_shift_integer_translation():
    ref = _reference_frame()
    template = centre_patch(ref)
    M = np.float64([[1, 0, 7], [0, 1, -3]])
    img = cv2.warpAffine(ref, M, (W_PX, H_PX), borderValue=200)
    su, sv, score = measure_shift(template, img)
    assert abs(su - 7.0) < 0.1 and abs(sv + 3.0) < 0.1
    assert score > 0.9


def test_measure_shift_fractional_translation():
    ref = _reference_frame()
    template = centre_patch(ref)
    M = np.float64([[1, 0, 4.3], [0, 1, -2.6]])
    img = cv2.warpAffine(ref, M, (W_PX, H_PX), flags=cv2.INTER_LINEAR, borderValue=200)
    su, sv, _ = measure_shift(template, img)
    assert abs(su - 4.3) < 0.1 and abs(sv + 2.6) < 0.1


def test_measure_shift_zero_on_same_frame():
    ref = _reference_frame()
    su, sv, _ = measure_shift(centre_patch(ref), ref)
    assert abs(su) < 0.05 and abs(sv) < 0.05


# ---------------------------------------------------------------- calibrate_scale

@pytest.mark.parametrize("A_true, mirrored", [(A_DOWN, False), (A_MIRROR, True)])
def test_calibrate_scale_end_to_end(A_true, mirrored):
    machine = FakeMachine(3.0, -2.0)
    camera = FakeCamera(machine, A_true, texture_canvas())
    result = asyncio.run(calibrate_scale(machine, camera))

    A_est = np.array(result["A"])
    assert np.all(np.abs(A_est - A_true) <= 0.01 * np.abs(A_true).max())
    assert result["rms_px"] < 0.3
    assert result["mirrored"] is mirrored
    assert (machine.x, machine.y) == (3.0, -2.0)
    assert result["mm_per_px"] == pytest.approx(1.0 / 25.0, rel=0.02)
    assert result["angle_deg"] == pytest.approx(2.0, abs=0.1)


def test_calibrate_scale_returns_to_start_even_on_failure():
    machine = FakeMachine(1.0, 1.0)

    class BlankCamera:
        async def fresh_frame(self, after=None, timeout=3.0):
            return _Frame(image=np.full((H_PX, W_PX), 128, np.uint8), t=0.0, seq=0)

    with pytest.raises(CalibrationError):
        asyncio.run(calibrate_scale(machine, BlankCamera()))
    assert (machine.x, machine.y) == (1.0, 1.0)


# ---------------------------------------------------------------- find_blob

def _blank(value: int) -> np.ndarray:
    return np.full((H_PX, W_PX), value, dtype=np.uint8)


def _disc(img: np.ndarray, cx_idx: float, cy_idx: float, r_px: float, value: int) -> None:
    cv2.circle(img, (int(round(cx_idx * 16)), int(round(cy_idx * 16))), int(round(r_px * 16)),
               value, -1, cv2.LINE_AA, shift=4)


def test_find_blob_dark_disc_auto():
    img = _blank(200)
    _disc(img, 326.8, 235.4, 25.0, 30)
    _disc(img, 60.0, 60.0, 12.0, 30)   # distractor farther from the centre
    u, v = find_blob(img, polarity="auto")
    # index centre + 0.5 -> continuous coordinates
    assert abs(u - (326.8 + 0.5)) < 0.3
    assert abs(v - (235.4 + 0.5)) < 0.3


def test_find_blob_bright_disc_auto():
    img = _blank(40)
    _disc(img, 318.0, 243.0, 25.0, 230)
    u, v = find_blob(img, polarity="auto")
    assert abs(u - 318.5) < 0.3 and abs(v - 243.5) < 0.3


def test_find_blob_none_on_blank_image():
    assert find_blob(_blank(128)) is None


# ---------------------------------------------------------------- centre_on_blob

def test_centre_on_blob_converges_on_off_centre_disc():
    disc_at = np.array([1.5, -1.0])            # world mm
    canvas = disc_canvas(disc_at)
    machine = FakeMachine(0.0, 0.0)
    camera = FakeCamera(machine, A_DOWN, canvas)
    x, y = asyncio.run(centre_on_blob(machine, camera, A_DOWN))
    head = np.array([x, y])
    # residual blob offset in pixels must be under 1 px
    assert np.linalg.norm(A_DOWN @ (head - disc_at)) < 1.0
    assert np.allclose(head, disc_at, atol=0.05)


def test_centre_on_blob_raises_without_blob():
    machine = FakeMachine(0.0, 0.0)
    camera = FakeCamera(machine, A_DOWN, np.full((N, N), 200, np.uint8))
    with pytest.raises(CalibrationError):
        asyncio.run(centre_on_blob(machine, camera, A_DOWN))


# ---------------------------------------------------------------- store

def test_calibration_round_trip(tmp_path):
    path = tmp_path / "sub" / "calibration.yaml"
    cal = Calibration()
    cal.set_camera("down", A_DOWN, 0.12)
    cal.nozzle_offset = {1: (-42.0, -3.0), 2: (-58.0, -3.0)}
    cal.up_position = {1: (150.5, 30.25), 2: (120.0, 30.0)}
    cal.save(path)

    loaded = Calibration.load(path)
    assert set(loaded.cameras) == {"down"}
    assert np.allclose(loaded.A("down"), A_DOWN)
    assert loaded.cameras["down"].rms_px == pytest.approx(0.12)
    assert loaded.cameras["down"].date == cal.cameras["down"].date
    assert loaded.A("up") is None
    assert loaded.nozzle_offset == {1: (-42.0, -3.0), 2: (-58.0, -3.0)}
    assert loaded.up_position == {1: (150.5, 30.25), 2: (120.0, 30.0)}
    assert all(isinstance(k, int) for k in loaded.nozzle_offset)
    assert all(isinstance(k, int) for k in loaded.up_position)
    # no temp files left behind
    assert [p.name for p in path.parent.iterdir()] == ["calibration.yaml"]


def test_calibration_load_missing_file_is_empty(tmp_path):
    cal = Calibration.load(tmp_path / "does_not_exist.yaml")
    assert cal.cameras == {}
    assert cal.nozzle_offset == {}
    assert cal.up_position == {}


class QuantizedMachine(FakeMachine):
    """Like the TVM802: moves land on whole 1000-step quanta (0.0305 mm)."""

    Q = 1000 / 32808

    async def move_to(self, x=None, y=None, **kw):
        q = lambda v, cur: cur + round((v - cur) / self.Q) * self.Q  # noqa: E731
        return await super().move_to(None if x is None else q(x, self.x), None if y is None else q(y, self.y), **kw)


def test_calibrate_scale_uses_actual_positions_with_quantized_moves():
    machine = QuantizedMachine(3.0, -2.0)
    camera = FakeCamera(machine, A_DOWN, texture_canvas())
    result = asyncio.run(calibrate_scale(machine, camera))
    # 1 mm requests land at 1.0059 mm; using nominal moves would give a ~0.6 % error.
    assert np.all(np.abs(np.array(result["A"]) - A_DOWN) <= 0.002 * np.abs(A_DOWN).max())


def test_centre_on_blob_stops_at_machine_resolution():
    A = np.array([[60.0, 0.0], [0.0, -60.0]])  # 60 px/mm: one quantum is 1.8 px
    machine = QuantizedMachine(0.0, 0.0)
    camera = FakeCamera(machine, A, disc_canvas((0.37, -0.21)))
    x, y = asyncio.run(centre_on_blob(machine, camera, A, tol_px=0.5, resolution_mm=QuantizedMachine.Q))
    assert abs(x - 0.37) < QuantizedMachine.Q and abs(y + 0.21) < QuantizedMachine.Q


GLOW_DIR = Path(__file__).parent / "data" / "down_glow"
# Real TVM802 head-camera frames (2026-11-13): a PCB under the head with the head's
# red glow fixed in the image. c0 = start, the others after 1 mm moves.
GLOW_MOVES = {"xp": (1, 0), "xm": (-1, 0), "yp": (0, 1), "ym": (0, -1), "xpyp": (1, 1), "xmym": (-1, -1)}


def _glow_fit(**kw):
    ref = cv2.imread(str(GLOW_DIR / "c0.jpg"))
    template = centre_patch(ref)
    moves, shifts = [], []
    for name, d in GLOW_MOVES.items():
        su, sv, _ = measure_shift(template, cv2.imread(str(GLOW_DIR / f"{name}.jpg")), min_score=0.0, **kw)
        moves.append(d)
        shifts.append((su, sv))
    return solve_scale(moves, shifts)


def test_real_glow_frames_fit_with_highpass():
    A, rms = _glow_fit()
    assert rms < 0.5
    # about 42 px/mm, image rotated ~1 deg; X+ moves the view content left
    assert np.allclose(A, [[-42.2, -0.8], [-0.6, 42.0]], atol=1.5)


def test_real_glow_frames_fail_without_highpass():
    """Documents the bug: plain matching locks onto the glow (rms ~17 px)."""
    _A, rms = _glow_fit(highpass_sigma=None)
    assert rms > 5.0


# The real head camera's calibrated scale (2026-11-13).
A_REAL = np.array([[-42.42, -0.77], [-0.60, 42.29]])


def test_real_frame_mark_found_at_crosshair():
    """J2 through-hole pad under the crosshair, red glow around it. The old
    global threshold picked the whole dark area 74 px off-centre."""
    img = cv2.imread(str(GLOW_DIR / "mark_j2.jpg"))
    h, w = img.shape[:2]
    k = float(np.mean(np.linalg.svd(A_REAL, compute_uv=False)))
    blob = find_blob(img, min_area=np.pi * (0.3 * k / 2) ** 2, max_area=np.pi * (4 * k / 2) ** 2,
                     block=int(2.5 * k) | 1, min_circularity=0.6)
    assert blob is not None
    assert np.hypot(blob[0] - w / 2, blob[1] - h / 2) < 5.0


class _StaticCamera:
    """Shows the same image whatever the head does, like glare fixed to the camera."""

    def __init__(self, img):
        self.img = img
        self.seq = 0

    async def fresh_frame(self, after=None, timeout: float = 3.0):
        self.seq += 1
        return _Frame(self.img, time.monotonic(), self.seq)


def _dot_image(du: float, dv: float, r_px: float = 20) -> np.ndarray:
    img = np.full((480, 640, 3), 200, np.uint8)
    cv2.circle(img, (int(320 + du), int(240 + dv)), int(r_px), (40, 40, 40), -1)
    return img


def test_centre_stops_when_mark_does_not_follow():
    machine = FakeMachine(10.0, 10.0)
    camera = _StaticCamera(_dot_image(30, 0))  # 1.2 mm off, never moves
    with pytest.raises(CalibrationError, match="did not move towards the centre"):
        asyncio.run(centre_on_blob(machine, camera, A_DOWN))
    # it made exactly one correction and then stopped instead of running away
    assert len(machine.moves) == 1


def test_centre_refuses_large_jump():
    machine = FakeMachine(10.0, 10.0)
    camera = _StaticCamera(_dot_image(150, 0))  # 6 mm off at 25 px/mm
    with pytest.raises(CalibrationError, match="mm away"):
        asyncio.run(centre_on_blob(machine, camera, A_DOWN))
    assert machine.moves == []


def test_centre_on_a_mounting_hole_with_the_large_mark_size():
    """Job tab reference points: 5 and 7 mm holes centre with mark_mm=(3, 8)."""
    for radius, at in ((2.5, (1.2, 0.8)), (3.5, (-1.5, 2.0))):
        hole_at = np.array(at)
        canvas = disc_canvas(hole_at, radius_mm=radius)
        machine = FakeMachine(0.0, 0.0)
        camera = FakeCamera(machine, A_DOWN, canvas)
        x, y = asyncio.run(centre_on_blob(machine, camera, A_DOWN, mark_mm=(3.0, 8.0), max_move_mm=8.0))
        assert np.allclose((x, y), hole_at, atol=0.05), (radius, x, y)


# Real down-camera frames (2026-11-16): a 3.25 mm corner hole of the board (dark inside,
# on dark solder mask, only a faint edge ring) with "USB" silkscreen above it. Centring
# jumped to the letters: the threshold search can't separate the hole at all in the
# faint frame. The Job tab's Centre on mark uses method "auto" (circle edge first).
FRAMES = {  # hole centre (continuous px) found in the unshifted frame
    "down.jpg": (220.73, 318.39),          # brighter edge ring
    "corner_faint.jpg": (225.13, 282.55),  # faint edge ring
}
CUTOUT_DIR = Path(__file__).parent / "data" / "cutout_usb"
CUTOUT_K = 41.42  # px per mm of the down camera that took them


def _shifted(gray, crosshair):
    """The frame moved so that `crosshair` (u, v) sits at the image centre."""
    h, w = gray.shape
    M = np.float32([[1, 0, w / 2 - crosshair[0]], [0, 1, h / 2 - crosshair[1]]])
    return cv2.warpAffine(gray, M, (w, h), borderMode=cv2.BORDER_REPLICATE), (w / 2 - crosshair[0], h / 2 - crosshair[1])


@pytest.mark.parametrize("frame", sorted(FRAMES))
@pytest.mark.parametrize("offset", [(0, 0), (0, -60), (0, -85), (-30, -20), (25, 25)])
@pytest.mark.parametrize("mark_mm", [(0.3, 4.0), (1.0, 4.0), (3.0, 8.0)])
def test_real_corner_hole_is_found_not_the_text(frame, offset, mark_mm):
    from pnp.vision.calibration import find_mark
    gray = cv2.cvtColor(cv2.imread(str(CUTOUT_DIR / frame)), cv2.COLOR_BGR2GRAY)
    centre = FRAMES[frame]
    img, (sx, sy) = _shifted(gray, (centre[0] + offset[0], centre[1] + offset[1]))
    found = find_mark(img, CUTOUT_K, mark_mm, method="auto")
    assert found is not None
    # the same hole wherever the crosshair starts, to within 2 px (0.05 mm)
    assert abs(found[0] - sx - centre[0]) < 2.0 and abs(found[1] - sy - centre[1]) < 2.0, found


def test_circle_method_finds_nothing_on_a_plain_frame():
    from pnp.vision.calibration import find_mark
    assert find_mark(np.full((480, 640), 120, np.uint8), CUTOUT_K, (1.0, 4.0), method="circle") is None
    with pytest.raises(ValueError):
        find_mark(np.zeros((10, 10), np.uint8), CUTOUT_K, method="nope")


def test_find_mark_reports_the_hole_radius_for_the_overlay():
    from pnp.vision.calibration import find_mark
    gray = cv2.cvtColor(cv2.imread(str(CUTOUT_DIR / "corner_faint.jpg")), cv2.COLOR_BGR2GRAY)
    u, v, r = find_mark(gray, CUTOUT_K, (1.0, 4.0), method="auto", with_radius=True)
    assert abs(u - FRAMES["corner_faint.jpg"][0]) < 0.5 and abs(v - FRAMES["corner_faint.jpg"][1]) < 0.5
    assert 2 * r / CUTOUT_K == pytest.approx(3.25, abs=0.25)  # the board's 3.25 mm holes
