"""Fiducial detection, measurement and rigid fit tests (synthetic images, no hardware)."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import cv2
import numpy as np
import pytest

from pnp.sim.sim_vision import sim_down_A
from pnp.vision.fiducials import FiducialError, find_circles, fit_rigid, locate_fiducial

W_PX, H_PX = 640, 480
CENTRE = np.array([W_PX / 2.0, H_PX / 2.0])


def rot(deg: float) -> np.ndarray:
    t = math.radians(deg)
    return np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])


# ---------------------------------------------------------------- synthetic images

def _draw_disc(img: np.ndarray, cu: float, cv_: float, r_px: float, value: int) -> None:
    """Disc centred at continuous pixel (cu, cv_) (index = continuous - 0.5), sub-pixel."""
    cv2.circle(img, (int(round((cu - 0.5) * 16)), int(round((cv_ - 0.5) * 16))),
               int(round(r_px * 16)), value, -1, cv2.LINE_AA, shift=4)


def _noisy(img: np.ndarray, seed: int = 0, sigma: float = 3.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, sigma, img.shape)
    return np.clip(img.astype(float) + noise, 0, 255).astype(np.uint8)


def _disc_image(centres, r_px: float = 25.0, value: int = 215, background: int = 50) -> np.ndarray:
    img = np.full((H_PX, W_PX), background, dtype=np.uint8)
    for cu, cv_ in centres:
        _draw_disc(img, cu, cv_, r_px, value)
    return _noisy(img)


# ---------------------------------------------------------------- find_circles

def test_finds_single_circle():
    img = _disc_image([(320.3, 240.7)])
    circles = find_circles(img, diameter_px=50.0)
    assert len(circles) == 1
    assert circles[0].diameter_px == pytest.approx(50.0, abs=1.0)
    assert circles[0].circularity > 0.9


@pytest.mark.parametrize("cu, cv_", [(320.25, 240.5), (300.7, 200.3), (411.1, 279.6), (150.45, 350.85)])
def test_centroid_is_sub_pixel_accurate(cu, cv_):
    img = _disc_image([(cu, cv_)])
    (c,) = find_circles(img, diameter_px=50.0)
    assert abs(c.u - cu) < 0.1
    assert abs(c.v - cv_) < 0.1


def test_filled_rectangle_of_same_area_is_rejected():
    img = np.full((H_PX, W_PX), 50, dtype=np.uint8)
    # 56 x 35 px: area 1960, about the disc's 1963 px, but elongated (circularity ~0.74)
    cv2.rectangle(img, (300, 222), (355, 256), 215, -1)
    assert find_circles(_noisy(img), diameter_px=50.0) == []


def test_circle_twice_the_expected_size_is_rejected():
    img = _disc_image([(320.0, 240.0)], r_px=50.0)  # diameter 100 px, expected 50
    assert find_circles(img, diameter_px=50.0) == []


def test_circle_cut_by_image_border_is_rejected():
    img = _disc_image([(20.0, 240.0)], r_px=25.0)  # extends past x = 0
    assert find_circles(img, diameter_px=50.0) == []


def test_two_circles_sorted_nearest_centre_first():
    far = (100.0, 100.0)
    near = (330.0, 250.0)  # image centre is (320, 240)
    circles = find_circles(_disc_image([far, near]), diameter_px=50.0)
    assert len(circles) == 2
    assert (circles[0].u, circles[0].v) == pytest.approx(near, abs=0.1)
    assert (circles[1].u, circles[1].v) == pytest.approx(far, abs=0.1)


def test_dark_polarity_finds_dark_disc_on_bright_background():
    img = _disc_image([(300.4, 215.6)], value=30, background=200)
    circles = find_circles(img, diameter_px=50.0, polarity="dark")
    assert len(circles) == 1
    assert abs(circles[0].u - 300.4) < 0.1 and abs(circles[0].v - 215.6) < 0.1


def test_bright_polarity_ignores_dark_holes():
    # A dark dot in a bright area (bed dot, via in copper) must not pass as a bright fiducial.
    img = _disc_image([(300.4, 215.6)], value=30, background=200)
    assert find_circles(img, diameter_px=50.0, polarity="bright") == []


def test_unknown_polarity_rejected():
    with pytest.raises(ValueError):
        find_circles(_disc_image([(320, 240)]), diameter_px=50.0, polarity="sideways")


# ---------------------------------------------------------------- fit_rigid

NOMINAL_3 = [(0.0, 0.0), (40.0, 0.0), (0.0, 28.0)]
NOMINAL_2 = [(2.0, 2.0), (38.0, 2.0)]
ROT = 0.4
T = np.array([150.35, 149.75])


def _measure(points, rotation=ROT, t=T, scale=1.0):
    P = np.asarray(points, dtype=float)
    return [tuple(q) for q in scale * (P @ rot(rotation).T) + t]


@pytest.mark.parametrize("nominal", [NOMINAL_2, NOMINAL_3])
def test_fit_rigid_exact_recovery(nominal):
    fit = fit_rigid(nominal, _measure(nominal))
    assert fit.rotation == pytest.approx(ROT, abs=1e-9)
    assert fit.origin[0] == pytest.approx(T[0], abs=1e-9)
    assert fit.origin[1] == pytest.approx(T[1], abs=1e-9)
    assert fit.rms < 1e-9
    assert fit.max_error < 1e-9
    assert fit.scale == pytest.approx(1.0, abs=1e-9)
    assert len(fit.residuals) == len(nominal)


def test_fit_rigid_with_noise():
    rng = np.random.default_rng(7)
    nominal = [(0.0, 0.0), (40.0, 0.0), (0.0, 28.0), (40.0, 28.0)]
    measured = [tuple(np.add(q, rng.normal(0.0, 0.01, 2))) for q in _measure(nominal)]
    fit = fit_rigid(nominal, measured)
    assert abs(fit.rotation - ROT) < 0.05
    assert 0.003 < fit.rms < 0.03  # noise sigma 0.01 per axis
    assert fit.max_error >= fit.rms


def test_fit_rigid_reports_scale():
    fit = fit_rigid(NOMINAL_3, _measure(NOMINAL_3, scale=1.002))
    assert fit.scale == pytest.approx(1.002, abs=1e-4)


def test_fit_rigid_one_point_uses_rotation_hint():
    nominal = [(10.0, 5.0)]
    measured = _measure(nominal, rotation=1.0)
    fit = fit_rigid(nominal, measured, rotation_hint=1.0)
    assert fit.rotation == pytest.approx(1.0, abs=1e-12)
    expected_t = np.array(measured[0]) - rot(1.0) @ np.array(nominal[0])
    assert fit.origin == pytest.approx(tuple(expected_t), abs=1e-9)
    assert fit.scale == 1.0
    assert fit.rms < 1e-9


def test_fit_rigid_rotation_normalised():
    # 190 deg is the same rotation as -170 deg; the fit reports the (-180, 180] form
    fit = fit_rigid(NOMINAL_3, _measure(NOMINAL_3, rotation=190.0))
    assert fit.rotation == pytest.approx(-170.0, abs=1e-9)
    fit = fit_rigid(NOMINAL_3, _measure(NOMINAL_3, rotation=179.0))
    assert fit.rotation == pytest.approx(179.0, abs=1e-9)


def test_fit_rigid_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        fit_rigid(NOMINAL_3, _measure(NOMINAL_3)[:2])


def test_fit_rigid_rejects_empty():
    with pytest.raises(ValueError):
        fit_rigid([], [])


def test_fit_rigid_rejects_points_too_close():
    nominal = [(0.0, 0.0), (1.0, 0.0)]
    with pytest.raises(ValueError):
        fit_rigid(nominal, _measure(nominal))


# ---------------------------------------------------------------- locate_fiducial

Q = 0.0305  # machine resolution (mm)


class QuantizedMachine:
    """Moves land on a 0.0305 mm grid; move_to sets the position immediately."""

    def __init__(self, x: float, y: float):
        self.x = float(x)
        self.y = float(y)

    async def move_to(self, x=None, y=None, **kw):
        if x is not None:
            self.x = round(float(x) / Q) * Q
        if y is not None:
            self.y = round(float(y) / Q) * Q

    def position(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": 0.0, "a1": 0.0, "a2": 0.0}


@dataclass
class _Frame:
    image: np.ndarray
    t: float
    seq: int


class FiducialCamera:
    """Renders a bright disc at the world point fid (or nothing) for the head's position."""

    def __init__(self, machine: QuantizedMachine, A, fid, diameter_mm: float = 1.0, seed: int = 0):
        self.machine = machine
        self.A = np.asarray(A, dtype=float)
        self.fid = None if fid is None else np.asarray(fid, dtype=float)
        self.radius_px = diameter_mm * float(np.mean(np.linalg.svd(self.A, compute_uv=False))) / 2.0
        self.rng = np.random.default_rng(seed)
        self.frames = 0

    async def fresh_frame(self, after=None, timeout: float = 3.0) -> _Frame:
        self.frames += 1
        img = np.full((H_PX, W_PX), 50, dtype=np.uint8)
        if self.fid is not None:
            p = self.machine.position()
            H = np.array([p["x"], p["y"]])
            # sim convention: pixel = c + M @ (W - H), M = -A
            c = CENTRE - self.A @ (self.fid - H)
            _draw_disc(img, float(c[0]), float(c[1]), self.radius_px, 215)
        img = np.clip(img.astype(float) + self.rng.normal(0.0, 2.0, img.shape), 0, 255).astype(np.uint8)
        return _Frame(image=img, t=time.monotonic(), seq=self.frames)


A_SIM = sim_down_A()
FID_WORLD = np.array([152.0, 88.5])


def test_locate_fiducial_from_1_2_mm_off():
    start = FID_WORLD + np.array([1.2, 0.0])
    machine = QuantizedMachine(*start)
    camera = FiducialCamera(machine, A_SIM, FID_WORLD)
    x, y = asyncio.run(locate_fiducial(machine, camera, A_SIM, diameter_mm=1.0))
    assert math.hypot(x - FID_WORLD[0], y - FID_WORLD[1]) < 0.02


def test_locate_fiducial_moves_head_towards_fiducial():
    machine = QuantizedMachine(FID_WORLD[0] - 0.8, FID_WORLD[1] + 0.6)
    camera = FiducialCamera(machine, A_SIM, FID_WORLD)
    asyncio.run(locate_fiducial(machine, camera, A_SIM, diameter_mm=1.0))
    assert math.hypot(machine.x - FID_WORLD[0], machine.y - FID_WORLD[1]) < 0.05


def test_locate_fiducial_raises_when_nothing_in_view():
    machine = QuantizedMachine(*FID_WORLD)
    camera = FiducialCamera(machine, A_SIM, None)
    with pytest.raises(FiducialError):
        asyncio.run(locate_fiducial(machine, camera, A_SIM, diameter_mm=1.0))


def test_locate_fiducial_raises_when_fiducial_beyond_search_radius():
    machine = QuantizedMachine(FID_WORLD[0] + 4.0, FID_WORLD[1])  # 4 mm away, search 3 mm
    camera = FiducialCamera(machine, A_SIM, FID_WORLD)
    with pytest.raises(FiducialError):
        asyncio.run(locate_fiducial(machine, camera, A_SIM, diameter_mm=1.0, search_mm=3.0))
