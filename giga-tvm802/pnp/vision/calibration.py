"""Camera calibration math and procedures (no FastAPI).

Pixel convention: continuous coordinates, pixel (i, j) covers [i, i+1) x [j, j+1),
so the image centre is (w/2, h/2). Blob centroids returned by find_blob use the
same convention (moments centroid + 0.5).

Scale matrix A (px per mm): moving the head by d (mm) shifts what the camera sees
by s = A @ d (px).
"""

from __future__ import annotations

import time

import cv2
import numpy as np


class CalibrationError(Exception):
    """Calibration measurement or solve failed."""


def to_gray(img) -> np.ndarray:
    """BGR/BGRA/grey image to a single-channel uint8 array."""
    img = np.asarray(img)
    if img.ndim == 2:
        gray = img
    elif img.shape[2] == 4:
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    return gray


def _parabola_offset(left: float, centre: float, right: float) -> float:
    """Sub-sample vertex offset of a parabola through three samples at -1, 0, +1."""
    denom = left - 2.0 * centre + right
    if denom >= 0.0:  # not a maximum
        return 0.0
    return float(np.clip(0.5 * (left - right) / denom, -0.5, 0.5))


def highpass(img, sigma: float = 8.0) -> np.ndarray:
    """Grayscale float32 image minus a heavy blur of itself.

    Removes slow brightness changes that stay put in the image while the head
    moves (on the TVM802: the head's red ring-light glow and vignetting), so that
    template matching follows the board detail instead of the glow. Without it,
    the real machine's scale fit had a 17.4 px residual; with it, 0.21 px.
    """
    g = to_gray(img).astype(np.float32)
    return g - cv2.GaussianBlur(g, (0, 0), sigma, borderType=cv2.BORDER_REFLECT)


def measure_shift(template: np.ndarray, img: np.ndarray, min_score: float = 0.6,
                  highpass_sigma: float | None = 8.0) -> tuple[float, float, float]:
    """Where the template's centre lies in img, minus the image centre.

    Returns (su, sv, score). With a template cut from the centre of a reference
    frame, this is the feature shift s (px) between the reference and img.
    Both images are high-pass filtered first (see `highpass`) unless
    highpass_sigma is None.
    """
    if highpass_sigma:
        g_t = highpass(template, highpass_sigma)
        g_i = highpass(img, highpass_sigma)
    else:
        g_t = to_gray(template)
        g_i = to_gray(img)
    th, tw = g_t.shape[:2]
    h, w = g_i.shape[:2]
    if th >= h or tw >= w:
        raise CalibrationError("template is not smaller than the image")

    res = cv2.matchTemplate(g_i, g_t, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if not np.isfinite(max_val):
        raise CalibrationError("template matching failed (flat template?)")
    if max_val < min_score:
        raise CalibrationError(f"template match score {max_val:.3f} below {min_score}")

    x0, y0 = max_loc
    dx = dy = 0.0
    rh, rw = res.shape
    if 0 < x0 < rw - 1:
        dx = _parabola_offset(float(res[y0, x0 - 1]), float(res[y0, x0]), float(res[y0, x0 + 1]))
    if 0 < y0 < rh - 1:
        dy = _parabola_offset(float(res[y0 - 1, x0]), float(res[y0, x0]), float(res[y0 + 1, x0]))

    # template top-left -> template centre; image centre subtracted
    cx = x0 + dx + tw / 2.0
    cy = y0 + dy + th / 2.0
    return float(cx - w / 2.0), float(cy - h / 2.0), float(max_val)


def centre_patch(img, frac: float = 0.25) -> np.ndarray:
    """Square crop of side ~frac*min(w, h) centred on the image.

    The side is made even so that the crop centre lands exactly on the image
    centre for even image sizes (otherwise every measured shift carries a bias).
    """
    h, w = img.shape[:2]
    s = int(round(frac * min(h, w)))
    s -= s % 2
    x0 = (w - s) // 2
    y0 = (h - s) // 2
    return np.ascontiguousarray(img[y0:y0 + s, x0:x0 + s])


def solve_scale(moves_mm, shifts_px) -> tuple[np.ndarray, float]:
    """Least-squares A with s = A @ d.

    Returns (A, rms_px) where rms_px is the RMS of the residual over all
    components. Needs >= 3 moves of rank 2 (not collinear).
    """
    D = np.asarray(moves_mm, dtype=float).reshape(-1, 2)
    S = np.asarray(shifts_px, dtype=float).reshape(-1, 2)
    if len(D) != len(S):
        raise CalibrationError("moves and shifts differ in count")
    if len(D) < 3:
        raise CalibrationError(f"need at least 3 moves, got {len(D)}")
    if np.linalg.matrix_rank(D) < 2:
        raise CalibrationError("calibration moves are collinear")
    X, *_ = np.linalg.lstsq(D, S, rcond=None)  # D @ X ~ S, so X = A.T
    A = X.T
    residual = S - D @ X
    rms = float(np.sqrt(np.mean(residual ** 2)))
    return A, rms


def move_for_offset(A: np.ndarray, offset_px) -> tuple[float, float]:
    """Head move d (mm) that brings a feature at offset o (px) to the centre: d = -inv(A) @ o."""
    A = np.asarray(A, dtype=float)
    o = np.asarray(offset_px, dtype=float).reshape(2)
    d = np.linalg.solve(A, -o)
    return float(d[0]), float(d[1])


def find_blob(img, polarity: str = "auto", min_area: int = 20, max_area: int | None = None, *,
              block: int | None = None, min_circularity: float = 0.0):
    """Centroid (u, v) of the blob nearest the image centre, or None.

    Without `block`: Gaussian blur and one global Otsu threshold. polarity
    "dark" selects pixels darker than the threshold, "bright" brighter, "auto"
    the minority side of the split.

    With `block` (odd, px): a local (adaptive) threshold over block x block
    neighbourhoods, so uneven lighting such as the TVM802 head's red glow does
    not turn a whole dark region into one "blob". Only blobs that do not touch
    the image border and are at least `min_circularity` round
    (4*pi*area/perimeter^2) count. "auto" then considers dark and bright blobs.

    Coordinates use the continuous convention (see module docstring).
    """
    if polarity not in ("dark", "bright", "auto"):
        raise ValueError(f"unknown polarity {polarity!r}")
    gray = to_gray(img)
    h, w = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    if block is None:
        t, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        dark = (blur <= t).astype(np.uint8) * 255
        bright = cv2.bitwise_not(dark)
        if polarity == "auto":
            polarity = "dark" if np.count_nonzero(dark) <= np.count_nonzero(bright) else "bright"
        masks = [dark if polarity == "dark" else bright]
        mode = cv2.RETR_EXTERNAL
    else:
        block = max(3, int(block) | 1)
        masks = []
        if polarity in ("dark", "auto"):
            masks.append(cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                               cv2.THRESH_BINARY_INV, block, 8))
        if polarity in ("bright", "auto"):
            masks.append(cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                               cv2.THRESH_BINARY, block, -8))
        mode = cv2.RETR_LIST  # a hole inside a ring (e.g. a through-hole pad) counts too

    cx0, cy0 = w / 2.0, h / 2.0
    best = None
    best_d2 = float("inf")
    for mask in masks:
        contours, _ = cv2.findContours(mask, mode, cv2.CHAIN_APPROX_NONE)
        for cnt in contours:
            m = cv2.moments(cnt)
            if m["m00"] <= 0:
                continue
            area = m["m00"]
            if area < min_area or (max_area is not None and area > max_area):
                continue
            if block is not None:
                x, y, bw, bh = cv2.boundingRect(cnt)
                if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
                    continue  # cut by the image border: centroid would be wrong
                perimeter = cv2.arcLength(cnt, True)
                if perimeter <= 0 or 4 * np.pi * area / perimeter ** 2 < min_circularity:
                    continue
            u = m["m10"] / m["m00"] + 0.5
            v = m["m01"] / m["m00"] + 0.5
            d2 = (u - cx0) ** 2 + (v - cy0) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = (float(u), float(v))
    return best


async def calibrate_scale(machine, camera, *, step_mm: float = 1.0, settle: float = 0.2) -> dict:
    """Measure the down-camera scale matrix A by jogging the head around the start point.

    The head must be homed. The template is the centre patch of a frame taken at
    the start position. Moves are relative to the start: (+s,0), (-s,0), (0,+s),
    (0,-s), (+s,+s), (-s,-s). The head returns to the start afterwards.

    Returned dict:
      A           2x2 list, px per mm, s = A @ d
      rms_px      RMS residual of the least-squares fit (px)
      mm_per_px   mean of 1/singular values of A (mm per px, isotropic average)
      angle_deg   atan2(A[1,0], A[0,0]) in degrees: the image direction (v down)
                  that the machine +X axis appears along
      mirrored    True when det(A) > 0 (a down-looking camera normally gives det < 0)
    """
    start = machine.position()
    sx, sy = float(start["x"]), float(start["y"])
    s = float(step_mm)
    moves = [(s, 0.0), (-s, 0.0), (0.0, s), (0.0, -s), (s, s), (-s, -s)]
    moves_mm: list[tuple[float, float]] = []
    shifts: list[tuple[float, float]] = []
    try:
        ref = await camera.fresh_frame(after=time.monotonic() + settle)
        template = centre_patch(ref.image)
        for dx, dy in moves:
            await machine.move_to(x=sx + dx, y=sy + dy)
            frame = await camera.fresh_frame(after=time.monotonic() + settle)
            su, sv, _score = measure_shift(template, frame.image)
            # Use where the head really is: the controller moves in whole
            # 1000-step quanta, so a 1 mm request lands at 1.0059 mm.
            pos = machine.position()
            moves_mm.append((float(pos["x"]) - sx, float(pos["y"]) - sy))
            shifts.append((su, sv))
    finally:
        await machine.move_to(x=sx, y=sy)

    A, rms = solve_scale(moves_mm, shifts)
    if rms > 2.0:
        raise CalibrationError(f"scale fit residual {rms:.2f} px is too large")
    sv_vals = np.linalg.svd(A, compute_uv=False)
    return {
        "A": A.tolist(),
        "rms_px": rms,
        "mm_per_px": float(np.mean(1.0 / sv_vals)),
        "angle_deg": float(np.degrees(np.arctan2(A[1, 0], A[0, 0]))),
        "mirrored": bool(np.linalg.det(A) > 0),
    }


async def centre_on_blob(machine, camera, A, *, polarity: str = "auto", iterations: int = 4,
                         tol_px: float = 1.0, settle: float = 0.2,
                         resolution_mm: float = 0.0,
                         mark_mm: tuple[float, float] = (0.3, 4.0),
                         max_move_mm: float = 2.0) -> tuple[float, float]:
    """Jog the head until the nearest round mark sits within tol_px of the image centre.

    Only round blobs between mark_mm[0] and mark_mm[1] across count, found with a
    local threshold (see find_blob), so uneven lighting can't pose as a mark.

    Safety (2026-11-13: on the real machine the old version locked onto the dark
    area inside the head's glow, which moves with the camera, and kept driving
    the head away):
      * no single correction may exceed max_move_mm;
      * after each move the mark must be clearly closer to the centre,
        otherwise the loop stops: the "mark" does not move with the board.

    resolution_mm: smallest move the machine can make (TVM802: 0.0305 mm). The
    loop also counts as converged when the remaining correction is below half
    of it on both axes, since a smaller move cannot be executed.

    Returns the final head (x, y). Raises CalibrationError if no mark is seen,
    a correction is too large, the mark does not follow, or the loop does not
    converge within `iterations` moves.
    """
    A = np.asarray(A, dtype=float)
    k = float(np.mean(np.linalg.svd(A, compute_uv=False)))  # px per mm
    min_area = np.pi * (mark_mm[0] * k / 2) ** 2
    max_area = np.pi * (mark_mm[1] * k / 2) ** 2
    block = max(31, int(2.5 * k) | 1)
    prev = None
    for i in range(iterations + 1):
        frame = await camera.fresh_frame(after=time.monotonic() + settle)
        blob = find_blob(frame.image, polarity=polarity, min_area=min_area, max_area=max_area,
                         block=block, min_circularity=0.6)
        if blob is None:
            raise CalibrationError(
                f"no round mark {mark_mm[0]:g}-{mark_mm[1]:g} mm across found near the crosshair")
        h, w = frame.image.shape[:2]
        o = (blob[0] - w / 2.0, blob[1] - h / 2.0)
        dist = float(np.hypot(*o))
        if dist <= tol_px:
            pos = machine.position()
            return float(pos["x"]), float(pos["y"])
        if prev is not None and dist > 0.7 * prev and prev > 3 * tol_px:
            raise CalibrationError(
                f"stopped: the mark did not move towards the centre ({prev:.1f} px -> {dist:.1f} px). "
                "It may be a reflection or glare that moves with the camera")
        dx, dy = move_for_offset(A, o)
        if resolution_mm > 0 and abs(dx) < resolution_mm / 2 and abs(dy) < resolution_mm / 2:
            pos = machine.position()
            return float(pos["x"]), float(pos["y"])
        if i == iterations:
            break
        if float(np.hypot(dx, dy)) > max_move_mm:
            raise CalibrationError(
                f"the nearest mark is {np.hypot(dx, dy):.1f} mm away (limit {max_move_mm:g} mm): "
                "jog closer or click on it first")
        prev = dist
        pos = machine.position()
        await machine.move_to(x=float(pos["x"]) + dx, y=float(pos["y"]) + dy)
    raise CalibrationError(f"did not centre on the mark within {iterations} moves")
