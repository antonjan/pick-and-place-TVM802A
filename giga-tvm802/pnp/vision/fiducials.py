"""Fiducial detection, measurement and rigid board fitting (no FastAPI).

Conventions are those of pnp/vision/calibration.py: continuous pixel coordinates
(image centre is (w/2, h/2), centroids are moments centroid + 0.5), A in px per mm,
and move_for_offset(A, o) is the head move that brings a feature at offset o to
the image centre.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import cv2
import numpy as np

from pnp.vision.calibration import move_for_offset, to_gray


class FiducialError(Exception):
    """A fiducial could not be found or measured. The message is for the user."""


@dataclass
class Circle:
    u: float            # px, continuous convention
    v: float
    diameter_px: float  # equivalent diameter, 2 * sqrt(area / pi)
    circularity: float  # 4 * pi * area / perimeter**2 (1.0 = perfect circle)


def _rot(deg: float) -> np.ndarray:
    t = math.radians(deg)
    return np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])


def _normalise_deg(deg: float) -> float:
    """Map an angle in degrees to (-180, 180]."""
    return 180.0 - ((180.0 - deg) % 360.0)


def find_circles(img, diameter_px: float, polarity: str = "bright", tolerance: float = 0.3,
                 min_circularity: float = 0.8) -> list[Circle]:
    """Circles of the expected diameter, nearest the image centre first.

    Gaussian blur, Otsu threshold. polarity "bright" selects pixels brighter than
    the threshold (copper on a dark mask), "dark" the darker ones, "auto" the
    minority side. Contours touching the image border are rejected, as are
    contours whose equivalent diameter differs from diameter_px by more than
    `tolerance` (relative) or whose circularity is below `min_circularity`.
    Near-duplicate circles (within 1 px) keep the one closest to the expected size.
    """
    if polarity not in ("dark", "bright", "auto"):
        raise ValueError(f"unknown polarity {polarity!r}")
    if diameter_px <= 0:
        raise ValueError("diameter_px must be positive")
    gray = to_gray(img)
    h, w = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    t, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = (blur <= t).astype(np.uint8) * 255
    bright = cv2.bitwise_not(dark)

    if polarity == "auto":
        polarity = "dark" if np.count_nonzero(dark) <= np.count_nonzero(bright) else "bright"
    mask = dark if polarity == "dark" else bright

    # RETR_CCOMP: outer boundaries of the selected-polarity regions are top level (parent -1),
    # holes in them are second level. Only outer boundaries count: with "bright", a dark hole
    # in a bright area (a bed dot, a via in copper) must never pass as a fiducial. A pad inside
    # a mask opening is its own region, so it is still top level.
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    candidates: list[tuple[float, Circle]] = []  # (|diameter error|, circle)
    for i, cnt in enumerate(contours):
        if hierarchy is not None and hierarchy[0][i][3] != -1:
            continue  # a hole, not a region of the wanted polarity
        area = float(cv2.contourArea(cnt))
        perimeter = float(cv2.arcLength(cnt, True))
        if area <= 0.0 or perimeter <= 0.0:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if x == 0 or y == 0 or x + bw == w or y + bh == h:
            continue  # cut by the image border: the true size and centre are unknown
        diameter = 2.0 * math.sqrt(area / math.pi)
        err = abs(diameter / diameter_px - 1.0)
        if err > tolerance:
            continue
        circularity = 4.0 * math.pi * area / perimeter ** 2
        if circularity < min_circularity:
            continue
        m = cv2.moments(cnt)
        if m["m00"] <= 0:
            continue
        u = m["m10"] / m["m00"] + 0.5
        v = m["m01"] / m["m00"] + 0.5
        candidates.append((err, Circle(u=float(u), v=float(v), diameter_px=diameter,
                                       circularity=float(circularity))))

    # Drop duplicates: keep the candidate closest to the expected size.
    candidates.sort(key=lambda c: c[0])
    kept: list[Circle] = []
    for _, circ in candidates:
        if any(math.hypot(circ.u - k.u, circ.v - k.v) <= 1.0 for k in kept):
            continue
        kept.append(circ)

    cx0, cy0 = w / 2.0, h / 2.0
    kept.sort(key=lambda c: (c.u - cx0) ** 2 + (c.v - cy0) ** 2)
    return kept


async def locate_fiducial(machine, camera, A, diameter_mm: float, *, search_mm: float = 3.0,
                          iterations: int = 3, tol_px: float = 0.5, settle: float = 0.2,
                          resolution_mm: float = 0.0305, polarity: str = "bright") -> tuple[float, float]:
    """World (x, y) of the fiducial the head is near. Moves the head to centre it.

    diameter_px is diameter_mm times the mean singular value of A. Each round takes
    a fresh frame, finds the circle nearest the image centre and converts its offset
    to a head move. Raises FiducialError when no circle is found or the nearest one
    needs a move longer than search_mm. Returns H + move_for_offset(A, o) from the
    last frame, which includes the sub-step residual.
    """
    A = np.asarray(A, dtype=float)
    diameter_px = float(diameter_mm) * float(np.mean(np.linalg.svd(A, compute_uv=False)))

    for i in range(iterations + 1):
        frame = await camera.fresh_frame(after=time.monotonic() + settle)
        h, w = frame.image.shape[:2]
        circles = find_circles(frame.image, diameter_px, polarity=polarity)
        if not circles:
            raise FiducialError(f"no fiducial found within {search_mm:g} mm")
        c = circles[0]
        o = (c.u - w / 2.0, c.v - h / 2.0)
        dx, dy = move_for_offset(A, o)
        if math.hypot(dx, dy) > search_mm:
            raise FiducialError(f"no fiducial found within {search_mm:g} mm")

        pos = machine.position()
        hx, hy = float(pos["x"]), float(pos["y"])
        converged = math.hypot(*o) <= tol_px or (
            resolution_mm > 0 and abs(dx) < resolution_mm / 2 and abs(dy) < resolution_mm / 2)
        if converged or i == iterations:
            return hx + dx, hy + dy
        await machine.move_to(x=hx + dx, y=hy + dy)
    raise FiducialError("fiducial measurement did not converge")  # pragma: no cover


@dataclass
class RigidFit:
    rotation: float                  # deg, in (-180, 180]
    origin: tuple[float, float]      # translation t: world = scale * rot(rotation) @ p + t
    scale: float                     # from the similarity fit (1.0 = no scale error); 1.0 for 1 point
    residuals: list[float]           # mm, |rot @ p_i + t - w_i| for each point
    rms: float                       # mm
    max_error: float                 # mm


def fit_rigid(nominal: list[tuple[float, float]], measured: list[tuple[float, float]],
              rotation_hint: float = 0.0, *, with_scale: bool = False) -> RigidFit:
    """Rigid transform (rotation and translation) mapping nominal points onto measured ones.

    One point: the rotation is rotation_hint. Two or more: 2-D Kabsch fit. The scale comes
    from a similarity fit. With with_scale=False (the default) it is only reported, and the
    transform is rigid. With with_scale=True the transform is world = scale * rot @ p + t,
    so the residuals are those of the scaled transform (a 1-point fit has scale 1).
    """
    P = np.asarray(nominal, dtype=float).reshape(-1, 2)
    Q = np.asarray(measured, dtype=float).reshape(-1, 2)
    if len(P) != len(Q):
        raise ValueError(f"{len(P)} nominal points but {len(Q)} measured points")
    if len(P) == 0:
        raise ValueError("no points to fit")

    if len(P) == 1:
        angle = float(rotation_hint)
        R = _rot(angle)
        scale = 1.0
        t = Q[0] - R @ P[0]
    else:
        spread = max(float(np.hypot(*(P[i] - P[j]))) for i in range(len(P)) for j in range(i + 1, len(P)))
        if spread < 5.0:
            raise ValueError("nominal points are less than 5 mm apart; rotation cannot be measured")
        pc = P.mean(axis=0)
        qc = Q.mean(axis=0)
        pp = P - pc
        qq = Q - qc
        s_cross = float(np.sum(pp[:, 0] * qq[:, 1] - pp[:, 1] * qq[:, 0]))
        s_dot = float(np.sum(pp[:, 0] * qq[:, 0] + pp[:, 1] * qq[:, 1]))
        angle = math.degrees(math.atan2(s_cross, s_dot))
        R = _rot(angle)
        rotated = pp @ R.T
        scale = float(np.sum(qq * rotated) / np.sum(pp * pp))
        applied = scale if with_scale else 1.0
        t = qc - applied * (R @ pc)

    applied = scale if with_scale else 1.0
    residuals = np.hypot(*(applied * (P @ R.T) + t - Q).T)
    rms = float(np.sqrt(np.mean(residuals ** 2)))
    return RigidFit(
        rotation=_normalise_deg(angle),
        origin=(float(t[0]), float(t[1])),
        scale=float(scale),
        residuals=[float(r) for r in residuals],
        rms=rms,
        max_error=float(np.max(residuals)),
    )
