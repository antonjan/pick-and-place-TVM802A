"""Calibration tests against a synthetic world (no hardware, no pnp.sim)."""

from __future__ import annotations

import asyncio
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
