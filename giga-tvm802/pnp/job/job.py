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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pnp.job.feeders import Feeder
    from pnp.job.kicad import PosPart
    from pnp.job.rules import PackageRules


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
    status: dict[str, str] = field(default_factory=dict)   # placement key -> "placed" | "failed" | "skipped"
    fiducials: list[Fiducial] = field(default_factory=list)   # board coordinates, top side only
    fiducial_mode: str = "off"        # "off" | "panel" | "board"
    fiducial_diameter: float = 1.0    # copper diameter of the fiducial (mm)
    fit: dict | None = None           # last fiducial measurement, None = not measured
    board_fits: dict[int, list[float]] = field(default_factory=dict)
        # "board" mode only: board index -> [origin_x, origin_y, rotation_deg] (board coords -> world)
    references: list[dict] = field(default_factory=list)
        # two-point alignment reference points, kept so they can be re-recorded next time:
        # [{"ref": part ref or None, "x": board mm, "y": board mm}, ...] (A then B), on board `reference_board`
    reference_board: int = 0


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
    return Job(
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


def to_world(job: Job, board: int, x: float, y: float) -> tuple[float, float]:
    """Board point (mm) to world mm.

    A board with a board fit (`job.board_fits`) maps directly from its board coordinates.
    Otherwise the panel origin is used. ValueError if neither is set.
    """
    fit = job.board_fits.get(board)
    if fit is not None:
        ox, oy, r = fit
        wx, wy = _apply_rot(r, x, y)
        return (wx + ox, wy + oy)
    if job.origin is None:
        raise ValueError("board origin not set")
    b = job.boards[board]
    px, py = _apply_rot(b.rotation, x, y)
    px += b.x
    py += b.y
    wx, wy = _apply_rot(job.rotation, px, py)
    return (wx + job.origin[0], wy + job.origin[1])


def world_rotation(job: Job, board: int, part_rotation: float) -> float:
    """World rotation (deg) of a part, in (-180, 180]."""
    fit = job.board_fits.get(board)
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
    rx, ry = _apply_rot(rotation, a_panel[0], a_panel[1])
    origin = (a_world[0] - rx, a_world[1] - ry)
    return {
        "origin": [origin[0], origin[1]],
        "rotation": rotation,
        "board_distance": board_dist,
        "measured_distance": measured_dist,
        "distance_error": measured_dist - board_dist,
    }


def clear_fit(job: Job) -> None:
    """Forget the fiducial measurement (the origin or rotation was changed by hand)."""
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


def _natural_key(ref: str) -> list[Any]:
    """Sort key where digit runs compare as numbers: C2 < C10 < R1."""
    parts = re.split(r"(\d+)", ref)
    # re.split with a capturing group puts text at even indices and digits at odd ones.
    return [int(t) if i % 2 else t for i, t in enumerate(parts)]


def plan(job: Job, feeders: list[Feeder], rules: PackageRules) -> list[PlanItem]:
    """One item per (board, part), in placement order.

    Order: board index, nozzle, feeder id ("" for none), then natural ref order.
    Status is the job's saved status, except "skipped" for skipped parts, boards and
    bottom-side parts. Problems are only reported for items that are pending or skipped.
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
            place_depth = rules.place_depth_for(part.package)
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
    }


def _fiducial_from(data: Any, i: int) -> Fiducial:
    f = _build(Fiducial, data, f"fiducial {i}")
    try:
        return Fiducial(name=str(f.name), x=float(f.x), y=float(f.y))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"fiducial {i}: x and y must be numbers") from exc


def _board_fits_from(data: Any) -> dict[int, list[float]]:
    raw = data or {}
    if not isinstance(raw, dict):
        raise ValueError("job board_fits must be a mapping")
    out: dict[int, list[float]] = {}
    for key, vals in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"board_fits key {key!r} is not a board index") from exc
        try:
            ox, oy, rot = vals
            out[index] = [float(ox), float(oy), float(rot)]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"board_fits[{key!r}] must be [x, y, rotation]") from exc
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

    return Job(
        name=str(data["name"]),
        source=str(data.get("source") or ""),
        parts=parts,
        boards=boards,
        origin=origin,
        rotation=float(data.get("rotation") or 0.0),
        status=dict(status),
        fiducials=fiducials,
        fiducial_mode=fiducial_mode,
        fiducial_diameter=1.0 if data.get("fiducial_diameter") is None else float(data["fiducial_diameter"]),
        fit=fit,
        board_fits=_board_fits_from(data.get("board_fits")),
        references=_references_from(data.get("references")),
        reference_board=int(data.get("reference_board") or 0),
    )


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
