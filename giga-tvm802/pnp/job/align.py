"""Measure a job's fiducial marks with the down camera and fit the board to them (docs/phase5-design.md, section 4).

Works on a copy of the job: the caller gets the updated job back only on success and saves it.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from collections.abc import Callable

from pnp.job.job import Job, clear_fit, to_world
from pnp.vision.camera import CameraError
from pnp.vision.fiducials import FiducialError, RigidFit, fit_rigid, locate_fiducial


class AlignError(Exception):
    """The fiducials can't be measured or the fit is not trusted. The message is for the user."""


def _rot(a_deg: float, x: float, y: float) -> tuple[float, float]:
    t = math.radians(a_deg)
    c, s = math.cos(t), math.sin(t)
    return (c * x - s * y, s * x + c * y)


def _wrap(a_deg: float) -> float:
    """Normalise an angle to (-180, 180]."""
    r = a_deg % 360.0
    if r > 180.0:
        r -= 360.0
    return r


def _panel_point(job: Job, board: int, x: float, y: float) -> tuple[float, float]:
    """Board point to panel mm (the board instance's rotation and position, before the job rotation)."""
    inst = job.boards[board]
    px, py = _rot(inst.rotation, x, y)
    return (px + inst.x, py + inst.y)


def _check_inputs(job: Job, machine) -> None:
    if not machine.homed:
        raise AlignError("The machine isn't homed. Home it first.")
    if job.origin is None:
        raise AlignError("Set the board origin roughly first, then measure the fiducials.")
    if not job.fiducials:
        raise AlignError("The job has no fiducials.")
    if job.fiducial_mode not in ("panel", "board"):
        raise AlignError("Fiducial mode is off. Set it to panel or board first.")
    if all(inst.skip for inst in job.boards):
        raise AlignError("Every board is skipped, so there is nothing to measure.")


def _boards_to_measure(job: Job) -> list[int]:
    open_boards = [b for b, inst in enumerate(job.boards) if not inst.skip]
    if job.fiducial_mode == "panel":
        return sorted({open_boards[0], open_boards[-1]})
    return open_boards


def _check_fit(fit: RigidFit, labels: list[str], max_rms: float, max_scale_error: float) -> None:
    """Refuse a fit whose residuals or scale say the marks don't match the board."""
    if fit.rms > max_rms:
        worst = max(range(len(fit.residuals)), key=lambda i: fit.residuals[i])
        raise AlignError(
            f"The fiducials don't fit the board: RMS error {fit.rms:.3f} mm is above {max_rms:g} mm. "
            f"The worst point is {labels[worst]} ({fit.residuals[worst]:.3f} mm off). "
            "Check the fiducials and the board origin.")
    if len(labels) >= 2 and abs(fit.scale - 1.0) > max_scale_error:
        raise AlignError(
            f"The fiducials are {abs(fit.scale - 1.0) * 100:.2f} % off the design scale, more than "
            f"{max_scale_error * 100:g} %. Check the fiducial positions and the board size.")


async def _measure_point(machine, camera, A, diameter: float, name: str, board: int,
                         x: float, y: float, settle: float) -> tuple[float, float]:
    """Move the head over the predicted point, then measure the fiducial there (world mm)."""
    await machine.move_to(x=x, y=y)
    try:
        return await locate_fiducial(machine, camera, A, diameter, settle=settle)
    except FiducialError as exc:
        raise AlignError(
            f"Couldn't find {name} on board {board + 1} near X {x:.2f} Y {y:.2f}. "
            "Check the board origin and the fiducial diameter.") from exc
    except CameraError as exc:
        raise AlignError(f"Camera error while measuring {name} on board {board + 1}: {exc}") from exc


async def measure_fiducials(job: Job, machine, camera, A, *, settle: float = 0.2,
                            max_rms: float = 0.15, max_scale_error: float = 0.02,
                            log: Callable[[str], None] | None = None) -> Job:
    """Measure the job's fiducials and return a copy of the job fitted to them.

    Panel mode fits one transform for the panel (job.origin and job.rotation). Board mode fits
    each measured board (job.board_fits) and sets origin and rotation from the first one, so that
    the panel formula reproduces that board. Raises AlignError when the job can't be measured or
    the fit is rejected; the input job is never modified. The head ends over the first fiducial.

    The final fits include a scale (the marks' spacing against the design), so a board that is
    a little larger or smaller than the file is placed correctly. The in-loop refinement stays
    rigid: it only predicts where the head goes next.
    """
    _check_inputs(job, machine)
    say = log or (lambda _msg: None)
    mode = job.fiducial_mode
    boards = _boards_to_measure(job)

    # The user's estimate with any old fit ignored; `base` is the job we return.
    base = copy.deepcopy(job)
    clear_fit(base)
    work = copy.deepcopy(base)  # prediction estimate; panel mode refines it as points arrive

    points: list[dict] = []
    panel_nominal: list[tuple[float, float]] = []
    panel_measured: list[tuple[float, float]] = []
    panel_labels: list[str] = []
    # Board mode: per board, (nominal board points, measured world points, labels).
    per_board: dict[int, tuple[list, list, list]] = {b: ([], [], []) for b in boards}

    for b in boards:
        board_nominal, board_measured, board_labels = per_board[b]
        for fid in job.fiducials:
            expected = to_world(base, b, fid.x, fid.y)
            predicted = to_world(work, b, fid.x, fid.y)
            say(f"Measuring {fid.name} on board {b + 1}")
            mx, my = await _measure_point(machine, camera, A, job.fiducial_diameter, fid.name, b,
                                          predicted[0], predicted[1], settle)
            label = f"{fid.name} on board {b + 1}"
            points.append({
                "board": b,
                "name": fid.name,
                "expected": [float(expected[0]), float(expected[1])],
                "measured": [float(mx), float(my)],
                "error": float(math.hypot(mx - expected[0], my - expected[1])),
            })
            if mode == "panel":
                panel_nominal.append(_panel_point(job, b, fid.x, fid.y))
                panel_measured.append((mx, my))
                panel_labels.append(label)
                if len(panel_measured) >= 2:
                    try:
                        refit = fit_rigid(panel_nominal, panel_measured, rotation_hint=work.rotation)
                    except ValueError:
                        pass  # points too close together for a rotation yet: keep the estimate
                    else:
                        work.origin = refit.origin
                        work.rotation = refit.rotation
            else:
                board_nominal.append((fid.x, fid.y))
                board_measured.append((mx, my))
                board_labels.append(label)

    result = base
    fits: list[RigidFit] = []
    if mode == "panel":
        fit = fit_rigid(panel_nominal, panel_measured, rotation_hint=job.rotation, with_scale=True)
        _check_fit(fit, panel_labels, max_rms, max_scale_error)
        result.origin = (fit.origin[0], fit.origin[1])
        result.rotation = fit.rotation
        result.scale = fit.scale
        fits.append(fit)
    else:
        result.board_fits = {}
        first_fit: RigidFit | None = None
        for b in boards:
            nominal, measured, labels = per_board[b]
            hint = _wrap(job.rotation + job.boards[b].rotation)
            fit = fit_rigid(nominal, measured, rotation_hint=hint, with_scale=True)
            _check_fit(fit, labels, max_rms, max_scale_error)
            result.board_fits[b] = [float(fit.origin[0]), float(fit.origin[1]), float(fit.rotation),
                                    float(fit.scale)]
            fits.append(fit)
            if first_fit is None:
                first_fit = fit
                # Panel formula for the first board, with the job scale s: world = O + rot(R) @ s @
                # (rot(inst) @ p + inst_xy). Choose R, s and O so that this equals the board fit
                # rot(fit.rotation) @ (s p) + t, i.e. R = fit.rotation - inst.rotation, s = fit.scale.
                inst = job.boards[b]
                R = _wrap(fit.rotation - inst.rotation)
                rx, ry = _rot(R, inst.x, inst.y)
                result.rotation = R
                result.scale = fit.scale
                result.origin = (fit.origin[0] - fit.scale * rx, fit.origin[1] - fit.scale * ry)

    scales = [f.scale for f in fits]
    worst_scale = max(scales, key=lambda s: abs(s - 1.0))
    result.fit = {
        "mode": mode,
        "rms": float(max(f.rms for f in fits)),
        "max_error": float(max(f.max_error for f in fits)),
        "scale": float(worst_scale),
        "rotation": float(result.rotation),
        "origin": [float(result.origin[0]), float(result.origin[1])],
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "points": points,
    }
    say(f"Fiducials measured: RMS {result.fit['rms']:.3f} mm, rotation {result.rotation:.3f} deg")

    first = points[0]["measured"]
    await machine.move_to(x=first[0], y=first[1])
    return result

