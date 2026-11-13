"""Bottom vision: measure a part held on a nozzle from the up camera (no FastAPI).

Conventions are those of pnp/vision/calibration.py: continuous pixel coordinates
(image centre is (w/2, h/2), contour points and centroids use index + 0.5) and
A_up in px per mm, so a world offset q (mm, from the nozzle axis) appears at
pixel offset o = A_up @ q and q = inv(A_up) @ o.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import cv2
import numpy as np

from pnp.config import VisionConfig
from pnp.machine.machine import OUTPUTS
from pnp.vision.calibration import to_gray

_MM_SCALE = 1000.0  # mm points are scaled to um before OpenCV geometry (float32 precision)
_UPSAMPLE = 4  # the grey image is upsampled this much before contouring (sub-pixel edges)
_MIN_CONTRAST = 30.0  # grey levels between the bright and dark classes; less = no part in view


class InspectError(Exception):
    """Inspection could not run. The message is for the user."""


@dataclass
class PartView:
    offset: tuple[float, float]  # world mm, part centre minus nozzle axis, at the nozzle angle of the frame
    angle: float  # deg, world angle of the part's long side (0..180)
    size: tuple[float, float]  # mm (length >= width) of the minimum-area rectangle around the part
    area: float  # mm^2 of the bright region


def _threshold(blur: np.ndarray, min_brightness: int) -> float:
    """Grey level separating the part from the background: Otsu, or the fixed value."""
    if min_brightness == 0:
        level, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return float(level)
    return float(min_brightness)


def _is_bare_tip(pts_mm: np.ndarray, tip_diameter: float) -> bool:
    """A single round blob no bigger than the bare nozzle tip."""
    p = (pts_mm * _MM_SCALE).astype(np.float32).reshape(-1, 1, 2)
    area = float(cv2.contourArea(p)) / _MM_SCALE ** 2
    perimeter = float(cv2.arcLength(p, True)) / _MM_SCALE
    if area <= 0.0 or perimeter <= 0.0:
        return False
    circularity = 4.0 * math.pi * area / perimeter ** 2
    eq_diameter = 2.0 * math.sqrt(area / math.pi)
    return circularity >= 0.7 and eq_diameter <= 1.3 * tip_diameter


def measure_part(img, A_up, *, tip_diameter: float, min_brightness: int = 0,
                 search_mm: float = 6.0) -> PartView | None:
    """Measure the part on the nozzle in an up camera frame. None when there is no part.

    Bright regions (Otsu, or a fixed grey level) are found, unless the frame has no real
    contrast (an empty view). Those whose centroid is within search_mm of the nozzle axis
    are kept and their outlines merged. A lone round blob no bigger than the bare tip
    (tip_diameter, mm) counts as no part.

    The blurred grey image is upsampled (linear, _UPSAMPLE x) before thresholding, so the
    outline is found at sub-pixel accuracy. The outline runs through pixel centres of the
    upsampled image, which sits about half an upsampled pixel inside the true edge; the
    size is corrected for that by half an upsampled pixel per side.
    """
    A = np.asarray(A_up, dtype=float)
    A_inv = np.linalg.inv(A)
    det_inv = abs(float(np.linalg.det(A_inv)))
    px_mm = math.sqrt(det_inv)
    k = _UPSAMPLE

    gray = to_gray(img)
    h, w = gray.shape[:2]
    centre = np.array([w / 2.0, h / 2.0])
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    level = _threshold(blur, min_brightness)
    # Pure noise splits into two classes too, so an empty frame needs a contrast check.
    bright, dark = blur[blur > level], blur[blur <= level]
    if bright.size == 0 or dark.size == 0 or float(bright.mean() - dark.mean()) < _MIN_CONTRAST:
        return None

    blur_up = cv2.resize(blur, None, fx=k, fy=k, interpolation=cv2.INTER_LINEAR)
    _, mask_up = cv2.threshold(blur_up, level, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(mask_up, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

    # Upsampled pixel index j has continuous original coordinate (j + 0.5) / k.
    kept: list[tuple[np.ndarray, float]] = []  # (outline in mm, area in original px)
    for cnt in contours:
        m = cv2.moments(cnt)
        if m["m00"] <= 0:
            continue
        c_px = np.array([m["m10"] / m["m00"], m["m01"] / m["m00"]]) + 0.5
        c_px = c_px / k - centre
        if math.hypot(*(A_inv @ c_px)) > search_mm:
            continue
        pts_px = (cnt.reshape(-1, 2).astype(float) + 0.5) / k - centre
        kept.append((pts_px @ A_inv.T, float(cv2.contourArea(cnt)) / (k * k)))
    if not kept:
        return None
    if len(kept) == 1 and _is_bare_tip(kept[0][0], tip_diameter):
        return None

    area = sum(a for _, a in kept) * det_inv
    pts = np.vstack([p for p, _ in kept])
    rect = cv2.minAreaRect((pts * _MM_SCALE).astype(np.float32))
    box = cv2.boxPoints(rect).astype(float) / _MM_SCALE
    e0 = box[1] - box[0]
    e1 = box[2] - box[1]
    long_edge = e0 if np.hypot(*e0) >= np.hypot(*e1) else e1
    edge_mm = (float(np.hypot(*e0)), float(np.hypot(*e1)))
    # Half an upsampled pixel per side, i.e. px_mm / k in total per dimension.
    short_len = min(edge_mm) + px_mm / k
    long_len = max(edge_mm) + px_mm / k
    angle = math.degrees(math.atan2(long_edge[1], long_edge[0])) % 180.0
    cx, cy = rect[0][0] / _MM_SCALE, rect[0][1] / _MM_SCALE
    return PartView(
        offset=(float(cx), float(cy)),
        angle=float(angle),
        size=(float(long_len), float(short_len)),
        area=float(area),
    )


def angle_error(measured: float, expected_axis: float) -> float:
    """Smallest rotation (deg) mapping the expected body axis onto the measured long side.

    Taken modulo 90 deg, since a rectangle looks the same after a quarter turn.
    The result lies in (-45, 45].
    """
    d = (measured - expected_axis) % 90.0
    if d > 45.0:
        d -= 90.0
    return float(d)


async def inspect_nozzle(machine, camera, A_up, nozzle: int, cfg: VisionConfig) -> tuple[PartView | None, float]:
    """Measure the part on nozzle n. The head must already be over the up camera.

    Switches the leds on (when cfg.leds) for the frame and restores them afterwards.
    Returns (view or None, nozzle angle in deg at the frame).
    """
    if A_up is None:
        raise InspectError("calibrate the up camera first")
    if camera is None:
        raise InspectError("no up camera")

    led_turned_on = False
    try:
        if cfg.leds:
            s = machine.state  # a property on Machine: the last decoded controller state
            was_on = bool(s is not None and (s.outputs & OUTPUTS["leds"]))
            led_turned_on = not was_on
            await machine.set_output("leds", True)
        frame = await camera.fresh_frame(after=time.monotonic() + cfg.settle)
        view = measure_part(frame.image, A_up, tip_diameter=cfg.tip_diameter[nozzle - 1],
                            min_brightness=cfg.min_brightness, search_mm=cfg.search_mm)
    finally:
        if led_turned_on:
            await machine.set_output("leds", False)
    angle = float(machine.position()[f"a{nozzle}"])
    return view, angle
