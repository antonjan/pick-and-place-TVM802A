"""Synthetic camera images for `--sim` and the tests.

The constants are the ground truth that calibration is checked against.

Down camera: world point W appears at pixel c + R(angle) @ diag(k, -k) @ (W - H),
where H is the head position. Up camera: a nozzle tip at T = H + t_n appears at
pixel c + diag(-k, -k) @ (T - SIM_UP_CAMERA_XY).

Features are drawn with sub-pixel placement (cv2 `shift`), so the centroids of the
rendered features are accurate to a small fraction of a pixel.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import cv2
import numpy as np

SIM_DOWN_PX_PER_MM = 25.0
SIM_DOWN_ANGLE_DEG = 1.5  # camera rotated slightly
SIM_UP_CAMERA_XY = (150.0, 30.0)  # world position of the up camera
SIM_UP_PX_PER_MM = 30.0
SIM_UP_MIRROR = True  # looking up mirrors X
SIM_NOZZLE_OFFSETS = {1: (-42.0, -3.0), 2: (-58.0, -3.0)}  # t_n in mm
SIM_PRICK_OFFSET = (-50.0, 12.0)  # t_prick in mm (feeder needle tip)
SIM_SIZE = (640, 480)  # (width, height) in px

# Bed pattern and features (world mm).
_DOT_GRID_MM = 4.0
_DOT_OFFSET_MM = 2.0  # dots at 2, 6, 10, ... so the cross centre falls between dots
_DOT_RADIUS_MM = 0.5  # 1 mm dots
_CROSS_CENTRE_MM = (100.0, 100.0)
_CROSS_ARM_MM = 2.0
_CROSS_THICK_PX = 3
_BED_GREY = 200
_DOT_GREY = 40
_TIP_RADIUS_MM = 0.4  # 0.8 mm disc
_UP_BG_GREY = 20
_TIP_GREY = 230
_NOISE_SIGMA = 2.0
_MARGIN_PX = 20

_SHIFT = 4
_SCALE = 1 << _SHIFT


def _rot(angle_deg: float) -> np.ndarray:
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s], [s, c]])


def _down_map() -> np.ndarray:
    """M such that pixel = c + M @ (W - H). Equals -sim_down_A()."""
    k = SIM_DOWN_PX_PER_MM
    return _rot(SIM_DOWN_ANGLE_DEG) @ np.diag([k, -k])


def sim_down_A() -> np.ndarray:
    """px per mm for the down camera: moving the head by d shifts the image content by A @ d."""
    return -_down_map()


def sim_up_A() -> np.ndarray:
    """px per mm for the up camera: moving the head by d moves the tip image by A @ d."""
    k = SIM_UP_PX_PER_MM
    return np.diag([-k, -k])


def _pt(p) -> tuple[int, int]:
    return (int(round(float(p[0]) * _SCALE)), int(round(float(p[1]) * _SCALE)))


def _inside(p, margin: float) -> bool:
    w, h = SIM_SIZE
    return -margin <= p[0] <= w + margin and -margin <= p[1] <= h + margin


def _noise_bank(rng: np.random.Generator, n: int = 4) -> list[tuple[np.ndarray, np.ndarray]]:
    """Precomputed noise frames as (positive, negative) uint8 parts, so frames cost two saturating adds."""
    w, h = SIM_SIZE
    bank = []
    for _ in range(n):
        v = np.rint(rng.standard_normal((h, w, 3), dtype=np.float32) * _NOISE_SIGMA).astype(np.int16)
        bank.append((np.clip(v, 0, 255).astype(np.uint8), np.clip(-v, 0, 255).astype(np.uint8)))
    return bank


def _add_noise(canvas: np.ndarray, bank: list[tuple[np.ndarray, np.ndarray]], rng: np.random.Generator) -> np.ndarray:
    pos, neg = bank[int(rng.integers(len(bank)))]
    return cv2.subtract(cv2.add(canvas, pos), neg)


def make_down_renderer(position: Callable[[], tuple[float, float]]) -> Callable[[], np.ndarray]:
    """Render the bed as seen by the down camera at head position `position()`."""
    M = _down_map()
    Minv = np.linalg.inv(M)
    w, h = SIM_SIZE
    centre = np.array([w / 2, h / 2])
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=float)
    dot_r = _DOT_RADIUS_MM * SIM_DOWN_PX_PER_MM
    cross = np.array(_CROSS_CENTRE_MM, dtype=float)
    arm = _CROSS_ARM_MM
    rng = np.random.default_rng(1)
    bank = _noise_bank(rng)

    def render() -> np.ndarray:
        H = np.asarray(position(), dtype=float)
        # Visible world window: image corners mapped to mm, plus a margin.
        world = H + (corners - centre) @ Minv.T
        pad = _MARGIN_PX / SIM_DOWN_PX_PER_MM
        lo = world.min(axis=0) - pad
        hi = world.max(axis=0) + pad
        canvas = np.full((h, w, 3), _BED_GREY, dtype=np.uint8)

        i0 = math.floor((lo[0] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        i1 = math.ceil((hi[0] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        j0 = math.floor((lo[1] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        j1 = math.ceil((hi[1] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                W = np.array([_DOT_OFFSET_MM + i * _DOT_GRID_MM, _DOT_OFFSET_MM + j * _DOT_GRID_MM])
                p = centre + M @ (W - H)
                if _inside(p, dot_r + 2):
                    cv2.circle(canvas, _pt(p), int(round(dot_r * _SCALE)), (_DOT_GREY,) * 3,
                               -1, cv2.LINE_AA, _SHIFT)

        pc = centre + M @ (cross - H)
        if _inside(pc, arm * SIM_DOWN_PX_PER_MM + 10):
            for d in ((arm, 0.0), (0.0, arm)):
                a = cross - np.array(d)
                b = cross + np.array(d)
                pa = centre + M @ (a - H)
                pb = centre + M @ (b - H)
                cv2.line(canvas, _pt(pa), _pt(pb), (_DOT_GREY,) * 3, _CROSS_THICK_PX, cv2.LINE_AA, _SHIFT)

        return _add_noise(canvas, bank, rng)

    return render


def make_up_renderer(position: Callable[[], tuple[float, float]]) -> Callable[[], np.ndarray]:
    """Render the nozzle tips as seen by the up camera at head position `position()`."""
    M = sim_up_A()
    w, h = SIM_SIZE
    centre = np.array([w / 2, h / 2])
    U = np.array(SIM_UP_CAMERA_XY, dtype=float)
    tip_r = _TIP_RADIUS_MM * SIM_UP_PX_PER_MM
    offsets = [np.array(t, dtype=float) for t in SIM_NOZZLE_OFFSETS.values()]
    rng = np.random.default_rng(2)
    bank = _noise_bank(rng)

    def render() -> np.ndarray:
        H = np.asarray(position(), dtype=float)
        canvas = np.full((h, w, 3), _UP_BG_GREY, dtype=np.uint8)
        for t in offsets:
            T = H + t
            p = centre + M @ (T - U)
            if _inside(p, tip_r + 2):
                cv2.circle(canvas, _pt(p), int(round(tip_r * _SCALE)), (_TIP_GREY,) * 3,
                           -1, cv2.LINE_AA, _SHIFT)
        return _add_noise(canvas, bank, rng)

    return render
