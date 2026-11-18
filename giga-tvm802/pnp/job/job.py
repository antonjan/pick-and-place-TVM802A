"""Jobs and panels: a KiCad part list placed on one or more boards, mapped to world coordinates.

A job holds the parts of one board (from a KiCad position file), the panel layout of
board copies, and the board origin and rotation on the machine. `plan()` turns that into
one `PlanItem` per (board, part), with the nozzle, feeder and world position to use.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pnp.config import HeadConfig
    from pnp.job.feeders import Feeder
    from pnp.job.kicad import PosPart
    from pnp.job.rules import PackageRules

PCB_THICKNESS_MAX = 10.0   # mm, the largest board the job accepts
PART_HEIGHT_MAX = 30.0     # mm, the tallest part the job accepts


@dataclass
class JobPart:
    ref: str
    value: str
    package: str
    x: float                     # board mm
    y: float
    rotation: float              # deg
    side: str = "top"
    skip: bool = False           # user override; bottom-side parts are created with skip=True
    nozzle: int | None = None    # user override; None = PackageRules.nozzle_for(package)
    feeder: str | None = None    # user override; None = automatic match
    vision: bool | None = None   # user override; None = PackageRules.vision_for(package)
    speed: int = 100             # % of the job speed for the moves that carry this part (vendor "Speed")
    height: float | None = None  # part height (mm) the nozzle holds it by; None = PackageRules.height_for, else head.part_height


@dataclass
class BoardInstance:
    x: float = 0.0               # panel mm: where this board's (0, 0) sits in the panel
    y: float = 0.0
    rotation: float = 0.0        # deg, this board's rotation within the panel
    skip: bool = False           # bad board: none of its parts are placed


@dataclass
class Fiducial:
    name: str                    # e.g. "FID1"
    x: float                     # board mm
    y: float


FIDUCIAL_REF = re.compile(r"^FID\d*$", re.IGNORECASE)


def is_fiducial(part: Any) -> bool:
    """True for a fiducial mark: ref FID<n>, or a package containing "fiducial"."""
    return bool(FIDUCIAL_REF.match(part.ref)) or "fiducial" in part.package.lower()


@dataclass
class Job:
    name: str
    source: str = ""                         # imported file name
    parts: list[JobPart] = field(default_factory=list)
    boards: list[BoardInstance] = field(default_factory=lambda: [BoardInstance()])
    origin: tuple[float, float] | None = None   # world mm of the panel's (0, 0); None = not set yet
    rotation: float = 0.0                    # deg, panel -> world rotation
    scale: float = 1.0                       # panel -> world scale (within 1 +/- SCALE_LIMIT), see fit_two_points
    status: dict[str, str] = field(default_factory=dict)   # placement key -> "placed" | "failed" | "skipped"
    fiducials: list[Fiducial] = field(default_factory=list)   # board coordinates, top side only
    fiducial_mode: str = "off"        # "off" | "panel" | "board"
    fiducial_diameter: float = 1.0    # copper diameter of the fiducial (mm)
    fit: dict | None = None           # last fiducial measurement, None = not measured
    board_fits: dict[int, list[float]] = field(default_factory=dict)
        # "board" mode only: board index -> [origin_x, origin_y, rotation_deg] or
        # [origin_x, origin_y, rotation_deg, scale] (board coords -> world; scale defaults to 1)
    references: list[dict] = field(default_factory=list)
        # two-point alignment reference points, kept so they can be re-recorded next time:
        # [{"ref": part ref or None, "x": board mm, "y": board mm}, ...] (A then B), on board `reference_board`
    reference_board: int = 0
    pcb_thickness: float = 1.6        # mm, used with the head's bed depth to compute place depths (0 < t <= 10)
    board_marks: list[dict] = field(default_factory=list)
        # one entry per board (see sync_board_marks): {"m1": mark, "m2": mark, "error": str | None}, where
        # mark = {"x", "y": the mark in the position file's board mm, "mx", "my": measured machine mm
        # (None until captured), "ref": part reference it was taken from or None}
    mark_fits: dict[int, list[float]] = field(default_factory=dict)
        # board index -> [origin_x, origin_y, rotation_deg, scale] fitted from that board's two marks
        # (board coords -> world). Not cleared by clear_fit: that forgets the fiducial measurement only.


def placement_key(board: int, ref: str) -> str:
    return f"{board}:{ref}"


def grid(rows: int, cols: int, step_x: float, step_y: float) -> list[BoardInstance]:
    """Boards in a rows x cols grid, row-major from (0, 0), no rotation."""
    if not (1 <= rows <= 50 and 1 <= cols <= 50):
        raise ValueError(f"rows and cols must be 1..50, got {rows} x {cols}")
    return [
        BoardInstance(x=c * step_x, y=r * step_y)
        for r in range(rows)
        for c in range(cols)
    ]


def job_from_pos(name: str, source: str, parts: list[PosPart]) -> Job:
    """A new job from parsed KiCad parts. Bottom-side parts are created with skip=True.

    Top-side fiducials become job fiducials (bottom-side ones are dropped), and the
    fiducial mode is "panel" when at least one is found.
    """
    fiducials = [Fiducial(name=p.ref, x=p.x, y=p.y) for p in parts if p.side == "top" and is_fiducial(p)]
    job = Job(
        name=name,
        source=source,
        parts=[
            JobPart(
                ref=p.ref,
                value=p.value,
                package=p.package,
                x=p.x,
                y=p.y,
                rotation=p.rotation,
                side=p.side,
                skip=p.side == "bottom",
            )
            for p in parts
            if not is_fiducial(p)
        ],
        fiducials=fiducials,
        fiducial_mode="panel" if fiducials else "off",
    )
    sync_board_marks(job)
    return job


def _rot(a_deg: float) -> tuple[float, float]:
    a = math.radians(a_deg)
    return (math.cos(a), math.sin(a))


def _apply_rot(a_deg: float, x: float, y: float) -> tuple[float, float]:
    """rot(a) @ (x, y), with rot(a) = [[cos a, -sin a], [sin a, cos a]]."""
    c, s = _rot(a_deg)
    return (c * x - s * y, s * x + c * y)


def _wrap(a_deg: float) -> float:
    """Normalise an angle to (-180, 180]."""
    r = a_deg % 360.0
    if r > 180.0:
        r -= 360.0
    return r


# A scale correction is only a few tenths of a percent on a real board. The limit is far
# beyond that, so a wrong feature or a typo is caught, and a job file with such a value
# could not be loaded again.
SCALE_LIMIT = 0.1


def check_scale(scale: float) -> None:
    """ValueError unless scale is within 1 +/- SCALE_LIMIT."""
    if not (1.0 - SCALE_LIMIT <= scale <= 1.0 + SCALE_LIMIT):
        raise ValueError(f"scale must be within {1.0 - SCALE_LIMIT:g}..{1.0 + SCALE_LIMIT:g}, got {scale:g}")


def _board_fit(job: Job, board: int) -> list[float] | None:
    """The board's own fit: the fiducial one (`board_fits`), else the one from its marks (`mark_fits`)."""
    fit = job.board_fits.get(board)
    return fit if fit is not None else job.mark_fits.get(board)


def to_world(job: Job, board: int, x: float, y: float) -> tuple[float, float]:
    """Board point (mm) to world mm.

    A board with its own fit maps directly from its board coordinates, with the fit's scale
    (1 when the fit has none): the fiducial fit (`job.board_fits`) first, else the fit from
    its two marks (`job.mark_fits`). Otherwise the panel origin, rotation and `job.scale`
    are used. ValueError if neither is set.

    The scale multiplies the whole panel point, board offset included, before the job
    rotation: the offsets are measured in the same units as the board itself.
    """
    fit = _board_fit(job, board)
    if fit is not None:
        ox, oy, r = fit[0], fit[1], fit[2]
        s = fit[3] if len(fit) > 3 else 1.0
        wx, wy = _apply_rot(r, s * x, s * y)
        return (wx + ox, wy + oy)
    if job.origin is None:
        raise ValueError("board origin not set")
    b = job.boards[board]
    px, py = _apply_rot(b.rotation, x, y)
    px += b.x
    py += b.y
    px *= job.scale
    py *= job.scale
    wx, wy = _apply_rot(job.rotation, px, py)
    return (wx + job.origin[0], wy + job.origin[1])


def world_rotation(job: Job, board: int, part_rotation: float) -> float:
    """World rotation (deg) of a part, in (-180, 180]."""
    fit = _board_fit(job, board)
    if fit is not None:
        return _wrap(part_rotation + fit[2])
    return _wrap(part_rotation + job.boards[board].rotation + job.rotation)


MIN_REFERENCE_SPACING = 3.0  # mm: closer points can't give a usable rotation


def fit_two_points(a_panel: tuple[float, float], a_world: tuple[float, float],
                   b_panel: tuple[float, float], b_world: tuple[float, float]) -> dict[str, Any]:
    """Job origin and rotation from two reference points.

    a_panel/b_panel are panel mm (board point after the board's own offset and
    rotation), a_world/b_world where the camera saw them. The rotation comes from
    the direction A->B; the origin puts A exactly where it was seen. The distance
    A->B is compared as a check (a large difference = wrong feature or point).

    The scale is measured A->B over board A->B, as the vendor software does. It is part of
    the origin, so point A maps exactly, and point B maps exactly too.
    """
    dpx, dpy = b_panel[0] - a_panel[0], b_panel[1] - a_panel[1]
    dwx, dwy = b_world[0] - a_world[0], b_world[1] - a_world[1]
    board_dist = math.hypot(dpx, dpy)
    measured_dist = math.hypot(dwx, dwy)
    if board_dist < MIN_REFERENCE_SPACING:
        raise ValueError(f"reference points A and B are only {board_dist:.2f} mm apart on the board; "
                         f"use points at least {MIN_REFERENCE_SPACING:g} mm apart (further is more accurate)")
    if measured_dist < MIN_REFERENCE_SPACING:
        raise ValueError(f"the recorded camera positions are only {measured_dist:.2f} mm apart; "
                         "record A and B on two different features")
    rotation = _wrap(math.degrees(math.atan2(dwy, dwx)) - math.degrees(math.atan2(dpy, dpx)))
    scale = measured_dist / board_dist
    rx, ry = _apply_rot(rotation, scale * a_panel[0], scale * a_panel[1])
    origin = (a_world[0] - rx, a_world[1] - ry)
    return {
        "origin": [origin[0], origin[1]],
        "rotation": rotation,
        "scale": scale,
        "board_distance": board_dist,
        "measured_distance": measured_dist,
        "distance_error": measured_dist - board_dist,
    }


# A reference point further than this from every part can't be on the board (or uses
# other coordinates than the position file). Generous: mounting holes and board
# corners sit outside the parts' area, but not by tens of mm.
REFERENCE_MARGIN = 40.0
# Measured A-B distance vs the board's: more than this means a wrong feature or point.
DISTANCE_TOLERANCE_MM = 1.0
DISTANCE_TOLERANCE_REL = 0.02


def parts_extent(job: Job) -> tuple[float, float, float, float] | None:
    """(min_x, max_x, min_y, max_y) of the top-side parts in board coordinates, or None."""
    top = [p for p in job.parts if p.side != "bottom"]
    if not top:
        return None
    xs, ys = [p.x for p in top], [p.y for p in top]
    return min(xs), max(xs), min(ys), max(ys)


def check_reference_point(job: Job, name: str, x: float, y: float) -> None:
    """ValueError when a reference point can't be in the position file's coordinates:
    far away from every part. The message says why it is probably wrong."""
    ext = parts_extent(job)
    if ext is None:
        return
    x0, x1, y0, y1 = ext
    dist = math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))
    if dist <= REFERENCE_MARGIN:
        return
    msg = (f"{name} (X {x:g}, Y {y:g}) is {dist:.0f} mm away from every part in the position file "
           f"(the parts are at X {x0:.1f} to {x1:.1f}, Y {y0:.1f} to {y1:.1f}). A reference point must use "
           "the file's coordinates.")
    if y > 0 and y1 < 0:
        msg += (f" KiCad shows Y as positive downwards, but the position file stores it negated: "
                f"try Y {-y:g}.")
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        msg += (" Here 0, 0 is KiCad's page origin, not the board corner (the file wasn't exported "
                "with the drill/place file origin).")
    msg += " Easiest: pick a part for the point, so its file position is filled in."
    raise ValueError(msg)


def check_distances(result: dict[str, Any]) -> None:
    """ValueError when the measured A-B distance doesn't match the board's."""
    board, err = result["board_distance"], result["distance_error"]
    if abs(err) > max(DISTANCE_TOLERANCE_MM, DISTANCE_TOLERANCE_REL * board):
        raise ValueError(
            f"the two points are {board:.2f} mm apart on the board but {result['measured_distance']:.2f} mm "
            "apart where the camera captured them: one of them is on the wrong feature, or its board X/Y "
            "is wrong")


def placements_outside(job: Job, x_range: tuple[float, float], y_range: tuple[float, float],
                       margin: float = 0.0, boards: Iterable[int] | None = None) -> list[tuple[str, float, float]]:
    """(ref, world x, world y) of every top-side part on a non-skipped board that lands
    outside the machine's X/Y range with the job's current origin and rotation.
    `boards` limits the check to those board indexes (default: all)."""
    out = []
    for b, board in enumerate(job.boards):
        if board.skip or (boards is not None and b not in boards):
            continue
        for p in job.parts:
            if p.side == "bottom":
                continue
            wx, wy = to_world(job, b, p.x, p.y)
            if not (x_range[0] - margin <= wx <= x_range[1] + margin
                    and y_range[0] - margin <= wy <= y_range[1] + margin):
                out.append((p.ref if len(job.boards) == 1 else f"{p.ref} (board {b + 1})", wx, wy))
    return out


def clear_fit(job: Job) -> None:
    """Forget the fiducial measurement (the origin or rotation was changed by hand).

    `job.scale` is kept: it is a separate correction, not part of the measurement.
    """
    job.fit = None
    job.board_fits = {}


def fit_rotation(job: Job, board_xy: tuple[float, float], world_xy: tuple[float, float]) -> float:
    """The job rotation (deg) that maps panel point `board_xy` onto `world_xy`.

    ValueError if the origin is not set, or either vector is shorter than 1 mm.
    """
    if job.origin is None:
        raise ValueError("board origin not set")
    vx = world_xy[0] - job.origin[0]
    vy = world_xy[1] - job.origin[1]
    bx, by = board_xy
    if math.hypot(vx, vy) < 1.0 or math.hypot(bx, by) < 1.0:
        raise ValueError("points must be at least 1 mm from the origin")
    ang = math.degrees(math.atan2(vy, vx)) - math.degrees(math.atan2(by, bx))
    return _wrap(ang)


def panel_from_board_fit(job: Job, board: int) -> tuple[tuple[float, float], float, float]:
    """Panel origin, rotation and scale that make the panel formula reproduce board `board`'s fiducial fit.

    ValueError if the board has no fit in `board_fits`.
    """
    fit = job.board_fits.get(board)
    if fit is None:
        raise ValueError(f"board {board + 1} has no fit")
    return panel_from_fit(job, board, fit)


def panel_from_fit(job: Job, board: int, fit: list[float]) -> tuple[tuple[float, float], float, float]:
    """Panel origin, rotation and scale that make the panel formula reproduce `fit` for board `board`.

    The fit maps board point p to rot(fit_rot) @ (s p) + t. The panel formula maps it to
    O + rot(R) @ (s (rot(inst) p + inst_xy)). These are equal when R = fit_rot - inst_rot and
    O = t - s rot(R) @ inst_xy. Boards without a fit then follow this one through their grid
    offsets.
    """
    inst = job.boards[board]
    s = fit[3] if len(fit) > 3 else 1.0
    rotation = _wrap(fit[2] - inst.rotation)
    rx, ry = _apply_rot(rotation, inst.x, inst.y)
    return (fit[0] - s * rx, fit[1] - s * ry), rotation, s


MARK_COORDS = ("x", "y", "mx", "my")


def blank_mark(x: float | None = None, y: float | None = None, ref: str | None = None) -> dict:
    """One board mark: design X/Y (board mm) and reference, measured machine X/Y not captured yet."""
    return {"x": x, "y": y, "mx": None, "my": None, "ref": ref}


def _new_board_marks(design: dict | None) -> dict:
    """A board's entry: the given design marks (copied, nothing measured) or blank ones."""
    marks = design or {"m1": blank_mark(), "m2": blank_mark()}
    return {"m1": dict(marks["m1"]), "m2": dict(marks["m2"]), "error": None}


def _design_marks(job: Job) -> dict | None:
    """{"m1", "m2"} design marks from the two-point references (before board marks existed), or None."""
    if not job.references:
        return None
    marks = {}
    for name, r in zip(("m1", "m2"), job.references[:2]):
        marks[name] = blank_mark(float(r["x"]), float(r["y"]), r.get("ref"))
    marks.setdefault("m1", blank_mark())
    marks.setdefault("m2", blank_mark())
    return marks


def sync_board_marks(job: Job) -> None:
    """Make `job.board_marks` one entry per board, and drop mark fits of boards that are gone.

    A job with no board marks yet takes the two-point references as every board's design
    marks (the older job format). A new board gets the design X/Y of the first board that
    has one, with no measurement. Boards past the end of the panel lose their entries and fits.
    """
    n = len(job.boards)
    if not job.board_marks:
        design = _design_marks(job)
        job.board_marks = [_new_board_marks(design) for _ in range(n)]
    source = next((e for e in job.board_marks if e["m1"]["x"] is not None or e["m2"]["x"] is not None), None)
    template = None
    if source is not None:
        template = {name: blank_mark(source[name]["x"], source[name]["y"], source[name]["ref"])
                    for name in ("m1", "m2")}
    while len(job.board_marks) < n:
        job.board_marks.append(_new_board_marks(template))
    del job.board_marks[n:]
    for board in [k for k in job.mark_fits if k >= n]:
        del job.mark_fits[board]


def _marks_fit(job: Job, board: int) -> dict[str, Any] | None:
    """fit_two_points for the two marks of `board` (board mm to measured world mm).

    None while a mark is incomplete. ValueError with the reason when the marks can't give a fit.
    """
    entry = job.board_marks[board]
    m1, m2 = entry["m1"], entry["m2"]
    if any(m[k] is None for m in (m1, m2) for k in MARK_COORDS):
        return None
    check_reference_point(job, "Mark 1", m1["x"], m1["y"])
    check_reference_point(job, "Mark 2", m2["x"], m2["y"])
    result = fit_two_points((m1["x"], m1["y"]), (m1["mx"], m1["my"]), (m2["x"], m2["y"]), (m2["mx"], m2["my"]))
    check_distances(result)
    check_scale(result["scale"])
    return result


def _sync_panel_to_marks(job: Job, fallback: tuple) -> None:
    """Panel origin, rotation and scale from the lowest-index board with a mark fit.

    Without any mark fit the panel is set back to `fallback` (origin, rotation, scale).
    """
    if not job.mark_fits:
        job.origin, job.rotation, job.scale = fallback
        return
    first = min(job.mark_fits)
    (ox, oy), rotation, scale = panel_from_fit(job, first, job.mark_fits[first])
    job.origin, job.rotation, job.scale = (ox, oy), rotation, scale


def refit_board(job: Job, board: int, x_range: tuple[float, float], y_range: tuple[float, float]) -> str | None:
    """Recompute the mark fit of `board` from its two marks, and store the result on the board.

    Returns the error message, or None when the board has a fit or its marks are incomplete.
    On an error the fit is removed but the measurements are kept, so they can be seen and
    captured again. The fit is only kept if the parts of this board, and of the boards that
    follow the panel, stay inside the machine range.
    """
    fallback = (job.origin, job.rotation, job.scale)
    job.mark_fits.pop(board, None)
    error: str | None = None
    try:
        result = _marks_fit(job, board)
    except ValueError as exc:
        result, error = None, str(exc)
    if result is not None:
        job.mark_fits[board] = [float(result["origin"][0]), float(result["origin"][1]),
                                float(result["rotation"]), float(result["scale"])]
        _sync_panel_to_marks(job, fallback)
        followers = [k for k in range(len(job.boards))
                     if k == board or (k not in job.mark_fits and k not in job.board_fits)]
        outside = placements_outside(job, x_range, y_range, boards=followers)
        if outside:
            job.mark_fits.pop(board, None)
            first = ", ".join(f"{r} at X {x:.1f}, Y {y:.1f}" for r, x, y in outside[:3])
            error = (f"with these marks {len(outside)} part(s) would land outside the machine ({first}). "
                     "Check both marks' board X/Y and their captures.")
    _sync_panel_to_marks(job, fallback)
    job.board_marks[board]["error"] = error
    return error


def mark_results(job: Job) -> list[dict]:
    """Per board: whether its mark fit is good, the error if not, and the fit's rotation, scale and distance error."""
    out = []
    for board, entry in enumerate(job.board_marks):
        error = entry.get("error")
        try:
            res = _marks_fit(job, board)
        except ValueError as exc:
            res, error = None, error or str(exc)
        out.append({
            "board": board,
            "ok": res is not None and error is None and board in job.mark_fits,
            "error": error,
            "rotation": None if res is None else float(res["rotation"]),
            "scale": None if res is None else float(res["scale"]),
            "distance_error": None if res is None else float(res["distance_error"]),
        })
    return out


def norm(s: str) -> str:
    """Lower case, stripped, runs of whitespace collapsed to one space."""
    return " ".join(s.lower().split())


def match_feeder(part: JobPart, feeders: list[Feeder]) -> Feeder | None:
    """The feeder to pick a part from, or None.

    A part with an explicit feeder override gets that feeder if it exists and is enabled.
    Otherwise candidates are enabled feeders with the same value, and the same package
    or an empty package. Feeders with parts left win, then lowest (bank, lane, id).
    """
    if part.feeder is not None:
        for f in feeders:
            if f.id == part.feeder and f.enabled:
                return f
        return None

    want_value = norm(part.value)
    want_package = norm(part.package)
    candidates = [
        f
        for f in feeders
        if f.enabled
        and norm(f.value) == want_value
        and (f.package == "" or norm(f.package) == want_package)
    ]
    if not candidates:
        return None

    def rank(f: Feeder) -> tuple:
        remaining = f.remaining()
        has_parts = remaining is None or remaining > 0
        lane = getattr(f, "lane", None)
        return (
            0 if has_parts else 1,
            getattr(f, "bank", ""),
            lane if lane is not None else 10**6,
            f.id,
        )

    return min(candidates, key=rank)


@dataclass
class PlanItem:
    key: str
    board: int
    ref: str
    value: str
    package: str
    x: float | None          # world mm (None when the origin is not set)
    y: float | None
    rotation: float          # world deg
    nozzle: int
    feeder: str | None
    place_depth: float | None
    status: str              # "pending" | "placed" | "failed" | "skipped"
    problem: str | None      # why it can't run: "Part skipped", "Board skipped", "Bottom side",
                             # "No feeder for <value> <package>", "Feeder <id> is empty", "Board origin not set"
    vision: bool = True                  # inspect on the up camera before placing (PackageRules.vision_for)
    body: list[float] | None = None      # (length, width) mm to check against, None = no size check
    nozzle_angle: float | None = None    # deg the nozzle turns to for the place: rotation minus the
                                         # part's rotation in the tape (the feeder's rotation); None = no feeder
    speed: int = 100                     # % of the job speed while carrying the part (JobPart.speed)
    height: float = 0.0                  # part height (mm) used for the place depth (JobPart.height, rule, or head default)


def _natural_key(ref: str) -> list[Any]:
    """Sort key where digit runs compare as numbers: C2 < C10 < R1."""
    parts = re.split(r"(\d+)", ref)
    # re.split with a capturing group puts text at even indices and digits at odd ones.
    return [int(t) if i % 2 else t for i, t in enumerate(parts)]


def part_height(part: JobPart, rules: PackageRules, head: HeadConfig | None) -> float:
    """The height (mm) a part is held by: the part's own, else its package rule's, else head.part_height.

    Without a head config the default HeadConfig.part_height is used.
    """
    if part.height is not None:
        return float(part.height)
    height_for = getattr(rules, "height_for", None)
    rule_height = height_for(part.package) if height_for is not None else None
    if rule_height is not None:
        return float(rule_height)
    if head is None:
        from pnp.config import HeadConfig

        return HeadConfig().part_height
    return float(head.part_height)


def plan(job: Job, feeders: list[Feeder], rules: PackageRules, *, head: HeadConfig | None = None) -> list[PlanItem]:
    """One item per (board, part), in placement order.

    Order: board index, nozzle, feeder id ("" for none), then natural ref order.
    Status is the job's saved status, except "skipped" for skipped parts, boards and
    bottom-side parts. Problems are only reported for items that are pending or skipped.

    Place depth: the matching rule's place_depth if it has one; else, when `head` has a
    bed depth, bed_depth - pcb_thickness - part height (the vendor software's model);
    else None, and the runner uses head.place_depth.
    """
    items: list[tuple[tuple, PlanItem]] = []
    origin_set = job.origin is not None
    # Duck typing: rules objects without the vision/body lookups get the defaults.
    vision_for = getattr(rules, "vision_for", None)
    body_for = getattr(rules, "body_for", None)

    for b_index, board in enumerate(job.boards):
        for part in job.parts:
            key = placement_key(b_index, part.ref)
            nozzle = part.nozzle if part.nozzle is not None else rules.nozzle_for(part.package)
            height = part_height(part, rules, head)
            place_depth = rules.place_depth_for(part.package)
            computed_depth = False
            if place_depth is None and head is not None and head.bed_depth > 0:
                place_depth = head.bed_depth - job.pcb_thickness - height
                computed_depth = True
            if part.vision is not None:
                vision = part.vision
            else:
                vision = bool(vision_for(part.package)) if vision_for is not None else True
            body_rule = body_for(part.package) if body_for is not None else None
            body = [float(side) for side in body_rule] if body_rule is not None else None
            feeder = match_feeder(part, feeders)
            feeder_id = feeder.id if feeder is not None else None

            if origin_set:
                x, y = to_world(job, b_index, part.x, part.y)
            else:
                x = y = None
            rotation = world_rotation(job, b_index, part.rotation)

            forced_skip = part.skip or board.skip or part.side == "bottom"
            status = "skipped" if forced_skip else job.status.get(key, "pending")

            problem: str | None = None
            if status not in ("placed", "failed"):
                if part.side == "bottom":
                    problem = "Bottom side"
                elif part.skip:
                    problem = "Part skipped"
                elif board.skip:
                    problem = "Board skipped"
                elif feeder is None:
                    problem = f"No feeder for {part.value} {part.package}"
                elif feeder.remaining() == 0:
                    problem = f"Feeder {feeder.id} is empty"
                elif not origin_set:
                    problem = "Board origin not set"
                elif computed_depth and place_depth is not None and place_depth <= 0:
                    problem = "Place depth below 0: check bed depth, PCB thickness and part height"

            item = PlanItem(
                key=key,
                board=b_index,
                ref=part.ref,
                value=part.value,
                package=part.package,
                x=x,
                y=y,
                rotation=rotation,
                nozzle=nozzle,
                feeder=feeder_id,
                place_depth=place_depth,
                status=status,
                problem=problem,
                vision=vision,
                body=body,
                nozzle_angle=(rotation - feeder.rotation) if feeder is not None else None,
                speed=part.speed,
                height=height,
            )
            sort_key = (b_index, nozzle, feeder_id or "", _natural_key(part.ref))
            items.append((sort_key, item))

    items.sort(key=lambda pair: pair[0])
    return [item for _, item in items]


def _plain(value: Any) -> Any:
    """Tuples become lists, so the value is plain YAML."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "name": job.name,
        "source": job.source,
        "origin": None if job.origin is None else [float(job.origin[0]), float(job.origin[1])],
        "rotation": job.rotation,
        "scale": float(job.scale),
        "boards": [dataclasses.asdict(b) for b in job.boards],
        "parts": [dataclasses.asdict(p) for p in job.parts],
        "status": dict(job.status),
        "fiducials": [dataclasses.asdict(f) for f in job.fiducials],
        "fiducial_mode": job.fiducial_mode,
        "fiducial_diameter": float(job.fiducial_diameter),
        "fit": _plain(job.fit),
        "board_fits": {int(k): [float(v) for v in vals] for k, vals in job.board_fits.items()},
        "references": [dict(r) for r in job.references],
        "reference_board": int(job.reference_board),
        "pcb_thickness": float(job.pcb_thickness),
        "board_marks": [_board_marks_plain(e) for e in job.board_marks],
        "mark_fits": {int(k): [float(v) for v in vals] for k, vals in job.mark_fits.items()},
    }


def _board_marks_plain(entry: dict) -> dict:
    def mark(m: dict) -> dict:
        out = {k: None if m[k] is None else float(m[k]) for k in MARK_COORDS}
        out["ref"] = m["ref"]
        return out
    return {"m1": mark(entry["m1"]), "m2": mark(entry["m2"]), "error": entry.get("error")}


def _fiducial_from(data: Any, i: int) -> Fiducial:
    f = _build(Fiducial, data, f"fiducial {i}")
    try:
        return Fiducial(name=str(f.name), x=float(f.x), y=float(f.y))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"fiducial {i}: x and y must be numbers") from exc


def _board_fits_from(data: Any, name: str = "board_fits") -> dict[int, list[float]]:
    """Board fits as stored: [x, y, rotation] (older files) or [x, y, rotation, scale]."""
    raw = data or {}
    if not isinstance(raw, dict):
        raise ValueError(f"job {name} must be a mapping")
    out: dict[int, list[float]] = {}
    for key, vals in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} key {key!r} is not a board index") from exc
        what = f"{name}[{key!r}] must be [x, y, rotation] or [x, y, rotation, scale]"
        try:
            nums = [float(v) for v in vals]
        except (TypeError, ValueError) as exc:
            raise ValueError(what) from exc
        if len(nums) not in (3, 4):
            raise ValueError(what)
        if len(nums) == 4:
            check_scale(nums[3])
        out[index] = nums
    return out


def _mark_from(data: Any, what: str) -> dict:
    if data is None:
        return blank_mark()
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be a mapping")
    unknown = set(data) - set(MARK_COORDS) - {"ref"}
    if unknown:
        raise ValueError(f"{what}: unknown key(s) {sorted(unknown)}")
    out: dict[str, Any] = {k: None if data.get(k) is None else _number(data[k], f"{what} {k}") for k in MARK_COORDS}
    ref = data.get("ref")
    out["ref"] = None if ref in (None, "") else str(ref)
    return out


def _board_marks_from(data: Any) -> list[dict]:
    out = []
    for i, entry in enumerate(data or []):
        what = f"board_marks {i}"
        if not isinstance(entry, dict):
            raise ValueError(f"{what} must be a mapping")
        unknown = set(entry) - {"m1", "m2", "error"}
        if unknown:
            raise ValueError(f"{what}: unknown key(s) {sorted(unknown)}")
        err = entry.get("error")
        out.append({
            "m1": _mark_from(entry.get("m1"), f"{what} m1"),
            "m2": _mark_from(entry.get("m2"), f"{what} m2"),
            "error": None if err in (None, "") else str(err),
        })
    return out


def _build(cls, data: Any, what: str):
    """Build a dataclass from a mapping, rejecting unknown keys."""
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be a mapping, got {data!r}")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{what}: unknown key(s) {sorted(unknown)}")
    try:
        return cls(**data)
    except TypeError as exc:
        raise ValueError(f"{what}: {exc}") from exc


def _job_from_dict(data: Any) -> Job:
    if not isinstance(data, dict):
        raise ValueError(f"job file must be a mapping, got {data!r}")
    known = {f.name for f in dataclasses.fields(Job)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"job: unknown key(s) {sorted(unknown)}")
    if "name" not in data:
        raise ValueError("job without a name")

    parts = [_build(JobPart, p, f"part {i}") for i, p in enumerate(data.get("parts") or [])]
    for i, part in enumerate(parts):
        if part.vision not in (None, True, False):
            raise ValueError(f"part {i}: vision must be true, false or null, got {part.vision!r}")
        if not 10 <= part.speed <= 100:
            raise ValueError(f"part {i}: speed must be 10..100 %, got {part.speed!r}")
        if part.height is not None:
            part.height = _number(part.height, f"part {i}: height")
            if not 0 <= part.height <= PART_HEIGHT_MAX:
                raise ValueError(f"part {i}: height must be 0..{PART_HEIGHT_MAX:g} mm, got {part.height!r}")
    if "boards" in data:
        boards = [_build(BoardInstance, b, f"board {i}") for i, b in enumerate(data["boards"] or [])]
        if not boards:
            raise ValueError("job needs at least one board")
    else:
        boards = [BoardInstance()]

    origin = data.get("origin")
    if origin is not None:
        try:
            ox, oy = origin
            origin = (float(ox), float(oy))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"origin must be [x, y], got {origin!r}") from exc

    status = data.get("status") or {}
    if not isinstance(status, dict):
        raise ValueError("job status must be a mapping")

    fiducials = [_fiducial_from(f, i) for i, f in enumerate(data.get("fiducials") or [])]
    fiducial_mode = str(data.get("fiducial_mode") or "off")
    if fiducial_mode not in ("off", "panel", "board"):
        raise ValueError(f"fiducial_mode must be off, panel or board, got {fiducial_mode!r}")

    fit = data.get("fit")
    if fit is not None and not isinstance(fit, dict):
        raise ValueError("job fit must be a mapping")

    # Files written before the scale correction have no key: 1.0 (no correction).
    scale = 1.0 if data.get("scale") is None else _number(data["scale"], "scale")
    check_scale(scale)

    # Files written before the bed depth model have no key: 1.6 mm, a standard board.
    pcb_thickness = 1.6 if data.get("pcb_thickness") is None else _number(data["pcb_thickness"], "pcb_thickness")
    if not 0 < pcb_thickness <= PCB_THICKNESS_MAX:
        raise ValueError(f"pcb_thickness must be above 0 and at most {PCB_THICKNESS_MAX:g} mm, got {pcb_thickness!r}")

    job = Job(
        name=str(data["name"]),
        source=str(data.get("source") or ""),
        parts=parts,
        boards=boards,
        origin=origin,
        rotation=float(data.get("rotation") or 0.0),
        scale=scale,
        status=dict(status),
        fiducials=fiducials,
        fiducial_mode=fiducial_mode,
        fiducial_diameter=1.0 if data.get("fiducial_diameter") is None else float(data["fiducial_diameter"]),
        fit=fit,
        board_fits=_board_fits_from(data.get("board_fits")),
        references=_references_from(data.get("references")),
        reference_board=int(data.get("reference_board") or 0),
        pcb_thickness=pcb_thickness,
        board_marks=_board_marks_from(data.get("board_marks")),
        mark_fits=_board_fits_from(data.get("mark_fits"), "mark_fits"),
    )
    sync_board_marks(job)  # older files: no board marks yet (taken from the references)
    return job


def _number(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a number, got {value!r}") from exc


def _references_from(data: Any) -> list[dict]:
    out: list[dict] = []
    for i, r in enumerate(data or []):
        if not isinstance(r, dict):
            raise ValueError(f"reference {i}: must be a mapping")
        try:
            ref = r.get("ref")
            out.append({"ref": None if ref in (None, "") else str(ref), "x": float(r["x"]), "y": float(r["y"])})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"reference {i}: needs numeric x and y") from exc
    return out


JOB_FILE_FORMAT = "tvm802-job"
JOB_FILE_VERSION = 1


def export_job_yaml(job: Job) -> str:
    """The job as a YAML document for download: the same fields as config/job.yaml, under a header."""
    sync_board_marks(job)  # as JobStore.save does
    return yaml.safe_dump({"format": JOB_FILE_FORMAT, "version": JOB_FILE_VERSION, "job": _job_to_dict(job)},
                          sort_keys=False)


def parse_job_yaml(text: str) -> Job:
    """A job from an exported file. Raises ValueError naming the first problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not a valid YAML file: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != JOB_FILE_FORMAT:
        raise ValueError("not a job file exported from this app (format: tvm802-job missing)")
    if data.get("version") != JOB_FILE_VERSION:
        raise ValueError(f"unsupported job file version {data.get('version')!r} (expected {JOB_FILE_VERSION})")
    if data.get("job") is None:
        raise ValueError("job file has no job")
    try:
        return _job_from_dict(data["job"])
    except (TypeError, KeyError, AttributeError) as exc:  # odd shapes the checks in _job_from_dict don't reach
        raise ValueError(f"job file is not valid: {exc}") from exc


class JobStore:
    """The current job in config/job.yaml (one job at a time)."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def load(self) -> Job | None:
        if not self.path.exists():
            return None
        with self.path.open() as f:
            data = yaml.safe_load(f)
        if data is None:
            return None
        return _job_from_dict(data)

    def save(self, job: Job) -> None:
        sync_board_marks(job)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(_job_to_dict(job), f, sort_keys=False)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
