"""Package rules, KiCad job, panel, alignment, placement and run endpoints (docs/phase4-design.md, section 7)."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import math
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pnp.api.errors import call as _call
from pnp.api.errors import status_for
from pnp.job.align import AlignError, measure_fiducials
from pnp.job.job import (
    PART_HEIGHT_MAX,
    PCB_THICKNESS_MAX,
    BoardInstance,
    Fiducial,
    Job,
    JobPart,
    JobStore,
    PlanItem,
    clear_fit,
    export_job_yaml,
    fit_rotation,
    grid,
    job_from_pos,
    mark_results,
    parse_job_yaml,
    plan,
    refit_board,
    sync_board_marks,
    to_world,
)
from pnp.job.kicad import PosError, parse_pos
from pnp.job.rules import PackageRule, PackageRules
from pnp.job.runner import JobError, JobRunner
from pnp.machine.machine import MachineError

log = logging.getLogger(__name__)

MAX_BOARDS = 2500


class RuleIn(BaseModel):
    pattern: str
    nozzle: int = 2
    place_depth: float | None = None
    height: float | None = None  # part height (mm) for this package; None = head.part_height
    note: str = ""
    body: list[float] | None = None  # (length, width) mm seen from below; None = no size check
    vision: bool = True


class RulesIn(BaseModel):
    default_nozzle: int = 1
    rules: list[RuleIn] = Field(default_factory=list)


class ImportIn(BaseModel):
    name: str = ""
    filename: str = ""
    text: str


class JobFileIn(BaseModel):
    text: str                        # the exported job file's text
    reset_statuses: bool = True      # True: every part back to "to do"


class BoardSettingsIn(BaseModel):
    origin: list[float] | None = None
    rotation: float = 0.0


class BoardIn(BaseModel):
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    skip: bool = False


class PanelIn(BaseModel):
    boards: list[BoardIn]


class GridIn(BaseModel):
    rows: int
    cols: int
    step_x: float
    step_y: float


class PartIn(BaseModel):
    skip: bool = False
    nozzle: int | None = None
    feeder: str | None = None
    vision: bool | None = None   # None = follow PackageRules.vision_for
    # Placement on the board (file coordinates); None = unchanged
    x: float | None = None
    y: float | None = None
    rotation: float | None = None
    speed: int | None = None     # % of the job speed while carrying the part (10..100); None = unchanged
    height: float | None = None  # part height (mm, 0..30); None = unchanged
    height_auto: bool = False    # True = use the package rule's height (or head.part_height) again


class PcbThicknessIn(BaseModel):
    pcb_thickness: float         # mm, 0 < t <= PCB_THICKNESS_MAX


class PartsEnableIn(BaseModel):
    refs: list[str]          # part references (all boards)
    enabled: bool            # False = left out of runs (JobPart.skip) until enabled again


class ReferencePointIn(BaseModel):
    ref: str | None = None   # part the point was taken from (for re-use), or None for custom X/Y
    x: float                 # board mm
    y: float
    world_x: float           # where the camera was centred when recorded (machine mm)
    world_y: float


class TwoPointIn(BaseModel):
    board: int = 0
    a: ReferencePointIn
    b: ReferencePointIn


class AlignRotationIn(BaseModel):
    ref: str
    board: int = 0


class StatusIn(BaseModel):
    status: str


class FiducialIn(BaseModel):
    name: str
    x: float
    y: float


class FiducialsIn(BaseModel):
    mode: str = "panel"
    diameter: float = 1.0
    fiducials: list[FiducialIn] = Field(default_factory=list)


class FiducialGotoIn(BaseModel):
    board: int = 0


class MarkIn(BaseModel):
    """One mark's fields. Only the fields sent are changed; null clears one. Checked by hand (400, not 422)."""
    x: Any = None      # board mm of the mark in the position file
    y: Any = None
    mx: Any = None     # measured machine mm
    my: Any = None
    ref: Any = None    # part reference the mark was taken from


class MarksIn(BaseModel):
    m1: MarkIn | None = None
    m2: MarkIn | None = None


class MarkCaptureIn(BaseModel):
    centre: bool = False                 # centre the down camera on the mark first
    mark_mm: list[float] | None = None   # diameter range for the centring; None = 0.3-4 mm
    method: str = "auto"                 # blob, circle or auto (as Centre on mark)


PLACEMENT_STATUSES = ("pending", "skipped", "placed")


def _job_dict(job: Job) -> dict:
    return {
        "name": job.name,
        "source": job.source,
        "origin": None if job.origin is None else [job.origin[0], job.origin[1]],
        "rotation": job.rotation,
        "scale": job.scale,
        "boards": [dataclasses.asdict(b) for b in job.boards],
        "parts": [dataclasses.asdict(p) for p in job.parts],
        "status": dict(job.status),
        "fiducials": [dataclasses.asdict(f) for f in job.fiducials],
        "fiducial_mode": job.fiducial_mode,
        "fiducial_diameter": float(job.fiducial_diameter),
        "fit": job.fit,
        "board_fits": {str(k): [float(v) for v in vals] for k, vals in job.board_fits.items()},
        "references": [dict(r) for r in job.references],
        "reference_board": int(job.reference_board),
        "pcb_thickness": float(job.pcb_thickness),
        "board_marks": [{"m1": dict(e["m1"]), "m2": dict(e["m2"]), "error": e.get("error")}
                        for e in job.board_marks],
        "mark_fits": {str(k): [float(v) for v in vals] for k, vals in job.mark_fits.items()},
        "mark_results": mark_results(job),
    }


def _panel_point(job: Job, board: int, x: float, y: float) -> tuple[float, float]:
    """Board point to panel mm (board rotation and position, before the job rotation)."""
    b = job.boards[board]
    a = math.radians(b.rotation)
    return (math.cos(a) * x - math.sin(a) * y + b.x, math.sin(a) * x + math.cos(a) * y + b.y)


def add_job_routes(app: FastAPI, machine, job_path: Path, rules_path: Path) -> None:
    """Must run after add_feeder_routes and add_bank_routes (uses app.state.feeders and app.state.head)."""
    feeders = app.state.feeders
    head = app.state.head
    jobs = JobStore(job_path)
    rules = PackageRules(rules_path)

    def down_camera():
        """(camera, A) for the fiducial measurement; None means missing or not calibrated."""
        cams = getattr(app.state, "cameras", None)
        camera = cams.get("down") if cams is not None and "down" in cams.names() else None
        return camera, app.state.calibration.A("down")

    async def measure_now() -> None:
        """The runner's alignment step: measure the job and save it. Raises AlignError or MachineError."""
        camera, A = down_camera()
        if camera is None:
            raise AlignError("The down camera is not available.")
        if A is None:
            raise AlignError("Calibrate the down camera first.")
        job = jobs.load()
        if job is None:
            raise AlignError("No job loaded.")
        async with head.exclusive("aligning"):
            measured = await measure_fiducials(job, machine, camera, A, log=log.info)
        jobs.save(measured)

    runner = JobRunner(machine, head, feeders, jobs, rules, pick_retries=2, align=measure_now)
    app.state.jobs = jobs
    app.state.rules = rules
    app.state.runner = runner

    # ------------------------------------------------------------ checks

    def running() -> None:
        if runner.busy():
            raise HTTPException(409, "job running")

    def idle() -> None:
        """Running, or the head busy: refuse edits that move the camera or change placements."""
        running()
        if head.busy:
            raise HTTPException(409, f"head busy ({head.busy})")

    def need_homed() -> None:
        if not machine.homed:
            raise HTTPException(409, "home the machine first")

    def need_job() -> Job:
        job = jobs.load()
        if job is None:
            raise HTTPException(404, "no job loaded: import a KiCad position file first")
        return job

    def need_origin(job: Job) -> None:
        if job.origin is None:
            raise HTTPException(409, "board origin not set")

    def items_of(job: Job) -> list[PlanItem]:
        return plan(job, feeders.all(), rules, head=machine.cfg.head)

    def state_json() -> dict:
        job = jobs.load()
        if job is None:
            return {"job": None, "plan": [], "status": runner.status()}
        return {
            "job": _job_dict(job),
            "plan": [dataclasses.asdict(i) for i in items_of(job)],
            "status": runner.status(),
        }

    def board_index(job: Job, board: int) -> int:
        if not 0 <= board < len(job.boards):
            raise HTTPException(400, f"no board {board}")
        return board

    def refit_marks(job: Job) -> None:
        """Refit every board's mark fit (after the marks or the panel changed)."""
        lim = machine.cfg.limits
        for b in range(len(job.boards)):
            refit_board(job, b, lim.x, lim.y)

    def find_part(job: Job, ref: str) -> JobPart:
        for part in job.parts:
            if part.ref == ref:
                return part
        raise HTTPException(404, f"no part {ref!r} in the job")

    def find_item(job: Job, key: str) -> PlanItem:
        for item in items_of(job):
            if item.key == key:
                return item
        raise HTTPException(404, f"no placement {key!r} in the job")

    def head_xy() -> tuple[float, float]:
        pos = machine.position()
        return float(pos["x"]), float(pos["y"])

    # ------------------------------------------------------------ package rules

    def rules_json() -> dict:
        return {"default_nozzle": rules.default_nozzle,
                "rules": [dataclasses.asdict(r) for r in rules.rules]}

    @app.get("/api/package-rules")
    async def get_rules():
        return rules_json()

    @app.put("/api/package-rules")
    async def put_rules(body: RulesIn):
        running()
        new_rules = [PackageRule(**r.model_dump()) for r in body.rules]
        try:
            rules.set(body.default_nozzle, new_rules)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return rules_json()

    # ------------------------------------------------------------ job

    @app.get("/api/job")
    async def get_job():
        return state_json()

    @app.post("/api/job/import")
    async def import_job(body: ImportIn):
        running()
        try:
            parts = parse_pos(body.text)
        except PosError as exc:
            raise HTTPException(400, str(exc)) from exc
        name = body.name.strip() or Path(body.filename).stem or "job"
        jobs.save(job_from_pos(name, body.filename, parts))
        return state_json()

    @app.get("/api/job/export")
    async def export_job():
        """Download the current job as one YAML file (import it again with /api/job/import-file)."""
        job = need_job()
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", job.name).strip("-") or "job"
        name = f"job-{safe}-{_dt.datetime.now().strftime('%Y-%m-%d-%H%M')}.yaml"
        return Response(export_job_yaml(job), media_type="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/job/import-file")
    async def import_job_file(body: JobFileIn):
        """Load an exported job file. Parts and edits are kept; the fiducial measurement is
        forgotten (it belongs to the board that was on the machine then)."""
        running()
        try:
            job = parse_job_yaml(body.text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if body.reset_statuses:
            job.status = {}
        clear_fit(job)
        jobs.save(job)
        return state_json()

    @app.delete("/api/job")
    async def delete_job():
        running()
        jobs.clear()
        return {"ok": True}

    @app.put("/api/job/board")
    async def put_board(body: BoardSettingsIn):
        running()
        job = need_job()
        if body.origin is None:
            job.origin = None
        else:
            if len(body.origin) != 2:
                raise HTTPException(400, "origin must be [x, y]")
            job.origin = (float(body.origin[0]), float(body.origin[1]))
        job.rotation = float(body.rotation)
        clear_fit(job)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/align/origin")
    async def align_origin():
        idle()
        need_homed()
        job = need_job()
        job.origin = head_xy()
        clear_fit(job)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/align/two-points")
    async def align_two_points(body: TwoPointIn):
        """Board origin AND rotation from two reference points recorded with the camera.

        One board: the panel origin, rotation and scale. Several boards: the points fit the
        selected board alone (job.board_fits, from its board X/Y), and the panel transform
        follows the lowest measured board, so boards not measured yet still get a position."""
        from pnp.job.job import (check_distances, check_reference_point, check_scale, fit_two_points,
                                 panel_from_board_fit, placements_outside)

        idle()
        need_homed()
        job = need_job()
        if not 0 <= body.board < len(job.boards):
            raise HTTPException(400, f"no board {body.board + 1}")
        per_board = len(job.boards) > 1
        if per_board:
            a_panel, b_panel = (body.a.x, body.a.y), (body.b.x, body.b.y)  # the board fit maps board X/Y
        else:
            a_panel = _panel_point(job, body.board, body.a.x, body.a.y)
            b_panel = _panel_point(job, body.board, body.b.x, body.b.y)
        try:
            check_reference_point(job, "Point 1", body.a.x, body.a.y)
            check_reference_point(job, "Point 2", body.b.x, body.b.y)
            result = fit_two_points(a_panel, (body.a.world_x, body.a.world_y),
                                    b_panel, (body.b.world_x, body.b.world_y))
            check_distances(result)
            check_scale(result["scale"])  # the distance check alone allows more on a short A-B
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        old = (job.origin, job.rotation, job.scale, job.board_fits, job.fit)
        if per_board:
            fit = [float(result["origin"][0]), float(result["origin"][1]), float(result["rotation"]),
                   float(result["scale"])]
            job.board_fits = {**job.board_fits, body.board: fit}  # the other boards' fits are kept
            job.fit = None  # the fiducial summary no longer describes the panel
            (ox, oy), job.rotation, job.scale = panel_from_board_fit(job, min(job.board_fits))
            job.origin = (ox, oy)
        else:
            job.origin = (result["origin"][0], result["origin"][1])
            job.rotation = result["rotation"]
            job.scale = result["scale"]
        lim = machine.cfg.limits
        outside = placements_outside(job, lim.x, lim.y)
        if outside:
            # not saved: keep the previous board position and fits
            job.origin, job.rotation, job.scale, job.board_fits, job.fit = old
            first = ", ".join(f"{r} at X {x:.1f}, Y {y:.1f}" for r, x, y in outside[:3])
            raise HTTPException(400, f"with these points {len(outside)} part(s) would land outside the machine "
                                     f"({first}). Check both points' board X/Y and their camera captures.")
        job.references = [{"ref": p.ref or None, "x": p.x, "y": p.y} for p in (body.a, body.b)]
        job.reference_board = body.board
        if not per_board:
            clear_fit(job)
        jobs.save(job)
        alignment = {**result, "board": body.board, "boards_aligned": sorted(job.board_fits)}
        return {**state_json(), "alignment": alignment}

    @app.post("/api/job/align/rotation")
    async def align_rotation(body: AlignRotationIn):
        idle()
        need_homed()
        job = need_job()
        need_origin(job)
        part = find_part(job, body.ref)
        if not 0 <= body.board < len(job.boards):
            raise HTTPException(400, f"no board {body.board}")
        panel = _panel_point(job, body.board, part.x, part.y)
        try:
            job.rotation = fit_rotation(job, panel, head_xy())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        clear_fit(job)
        jobs.save(job)
        return state_json()

    @app.put("/api/job/panel")
    async def put_panel(body: PanelIn):
        running()
        job = need_job()
        if not 1 <= len(body.boards) <= MAX_BOARDS:
            raise HTTPException(400, f"a panel has 1 to {MAX_BOARDS} boards, got {len(body.boards)}")
        job.boards = [BoardInstance(**b.model_dump()) for b in body.boards]
        sync_board_marks(job)
        clear_fit(job)
        refit_marks(job)  # new offsets: the panel follows the first measured board again
        jobs.save(job)
        return state_json()

    @app.post("/api/job/panel/grid")
    async def panel_grid(body: GridIn):
        running()
        job = need_job()
        try:
            job.boards = grid(body.rows, body.cols, body.step_x, body.step_y)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        sync_board_marks(job)
        clear_fit(job)
        refit_marks(job)  # new offsets: the panel follows the first measured board again
        jobs.save(job)
        return state_json()

    @app.put("/api/job/parts/{ref}")
    async def put_part(ref: str, body: PartIn):
        running()
        job = need_job()
        part = find_part(job, ref)
        if body.nozzle is not None and body.nozzle not in (1, 2):
            raise HTTPException(400, "nozzle must be 1 or 2")
        part.skip = body.skip
        part.nozzle = body.nozzle
        part.feeder = body.feeder or None
        part.vision = body.vision
        if body.speed is not None:
            if not 10 <= body.speed <= 100:
                raise HTTPException(400, "speed must be 10..100 %")
            part.speed = int(body.speed)
        if body.height_auto:
            part.height = None
        elif body.height is not None:
            if not math.isfinite(body.height) or not 0 <= body.height <= PART_HEIGHT_MAX:
                raise HTTPException(400, f"height must be 0..{PART_HEIGHT_MAX:g} mm")
            part.height = float(body.height)
        for name in ("x", "y", "rotation"):
            value = getattr(body, name)
            if value is not None:
                if not math.isfinite(value):
                    raise HTTPException(400, f"{name} must be a number")
                setattr(part, name, float(value) % 360.0 if name == "rotation" else float(value))
        jobs.save(job)
        return state_json()

    @app.put("/api/job/pcb-thickness")
    async def put_pcb_thickness(body: PcbThicknessIn):
        """Board thickness for the place depth model (bed depth - thickness - part height)."""
        running()
        job = need_job()
        t = body.pcb_thickness
        if not math.isfinite(t) or not 0 < t <= PCB_THICKNESS_MAX:
            raise HTTPException(400, f"PCB thickness must be above 0 and at most {PCB_THICKNESS_MAX:g} mm")
        job.pcb_thickness = float(t)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/parts/enable")
    async def enable_parts(body: PartsEnableIn):
        """Enable or disable several parts at once (the Parts table's bulk buttons).
        A disabled part stays in the job but is not placed until it is enabled again."""
        running()
        job = need_job()
        wanted = set(body.refs)
        found = {p.ref for p in job.parts} & wanted
        missing = sorted(wanted - found)
        if missing:
            raise HTTPException(404, f"no part(s) {', '.join(missing)} in the job")
        for part in job.parts:
            if part.ref in wanted:
                part.skip = not body.enabled
        jobs.save(job)
        return state_json()

    @app.delete("/api/job/parts/{ref}")
    async def delete_part(ref: str):
        """Remove a part from the job on every board, with its placement statuses. A
        reference point that used the part keeps its board X/Y as a custom point."""
        running()
        job = need_job()
        part = find_part(job, ref)
        job.parts.remove(part)
        job.status = {k: v for k, v in job.status.items() if k.split(":", 1)[-1] != ref}
        for point in job.references:
            if point.get("ref") == ref:
                point["ref"] = None
        for entry in job.board_marks:
            for name in ("m1", "m2"):
                if entry[name]["ref"] == ref:
                    entry[name]["ref"] = None
        jobs.save(job)
        return state_json()

    @app.post("/api/job/placements/{key}/goto")
    async def goto_placement(key: str):
        idle()
        need_homed()
        job = need_job()
        need_origin(job)
        item = find_item(job, key)
        if item.x is None or item.y is None:
            raise HTTPException(409, "board origin not set")
        pos = await _call(machine.move_to(x=item.x, y=item.y))
        return {"position": pos}

    @app.post("/api/job/placements/{key}/status")
    async def set_placement_status(key: str, body: StatusIn):
        running()
        if body.status not in PLACEMENT_STATUSES:
            raise HTTPException(400, f"status must be one of {PLACEMENT_STATUSES}")
        job = need_job()
        find_item(job, key)
        if body.status == "pending":
            job.status.pop(key, None)
        else:
            job.status[key] = body.status
        jobs.save(job)
        return state_json()

    @app.post("/api/job/reset")
    async def reset_job():
        running()
        job = need_job()
        job.status = {}
        jobs.save(job)
        return state_json()

    # ------------------------------------------------------------ board marks

    def mark_number(value: Any, what: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise HTTPException(400, f"{what} must be a number")
        return float(value)

    @app.put("/api/job/marks/{board}")
    async def put_marks(board: int, body: MarksIn):
        """Set one board's two marks (board X/Y, measured machine X/Y, part) and refit that board."""
        running()
        job = need_job()
        b = board_index(job, board)
        for name in ("m1", "m2"):
            given = getattr(body, name)
            if given is None:
                continue
            mark = job.board_marks[b][name]
            for key in given.model_fields_set:
                if key == "ref":
                    if given.ref is not None and not isinstance(given.ref, str):
                        raise HTTPException(400, f"{name} ref must be text")
                    mark["ref"] = given.ref or None
                else:
                    mark[key] = mark_number(getattr(given, key), f"{name} {key}")
        lim = machine.cfg.limits
        refit_board(job, b, lim.x, lim.y)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/marks/design-to-all")
    async def marks_design_to_all():
        """Copy board 1's design X/Y and parts of both marks to every board (their measurements stay)."""
        running()
        job = need_job()
        src = job.board_marks[0]
        for entry in job.board_marks[1:]:
            for name in ("m1", "m2"):
                entry[name]["x"] = src[name]["x"]
                entry[name]["y"] = src[name]["y"]
                entry[name]["ref"] = src[name]["ref"]
        refit_marks(job)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/marks/{board}/{mark}/capture")
    async def capture_mark(board: int, mark: str, body: MarkCaptureIn | None = None):
        """Record the head position as mark m1 or m2 of a board (centring on the mark first when asked)."""
        idle()
        need_homed()
        job = need_job()
        b = board_index(job, board)
        if mark not in ("m1", "m2"):
            raise HTTPException(400, "mark must be m1 or m2")
        body = body or MarkCaptureIn()
        if body.centre:
            camera, A = down_camera()
            if camera is None:
                raise HTTPException(409, "the down camera is not available")
            if A is None:
                raise HTTPException(409, "calibrate the down camera first")
            await app.state.centre_camera("down", polarity="auto", mark_mm=body.mark_mm, method=body.method)
        job = need_job()  # the centring moved the head; re-read the job before changing it
        x, y = head_xy()
        target = job.board_marks[b][mark]
        target["mx"], target["my"] = x, y
        lim = machine.cfg.limits
        refit_board(job, b, lim.x, lim.y)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/marks/clear")
    async def marks_clear():
        """Forget every measured machine position and mark fit. The design X/Y stay."""
        running()
        job = need_job()
        for entry in job.board_marks:
            for name in ("m1", "m2"):
                entry[name]["mx"] = None
                entry[name]["my"] = None
        job.mark_fits = {}
        refit_marks(job)
        jobs.save(job)
        return state_json()

    # ------------------------------------------------------------ fiducials

    @app.put("/api/job/fiducials")
    async def put_fiducials(body: FiducialsIn):
        idle()
        job = need_job()
        if body.mode not in ("off", "panel", "board"):
            raise HTTPException(400, "mode must be off, panel or board")
        if not 0 < body.diameter <= 10:
            raise HTTPException(400, "fiducial diameter must be between 0 and 10 mm")
        names = [f.name.strip() for f in body.fiducials]
        if any(not n for n in names):
            raise HTTPException(400, "every fiducial needs a name")
        if len(set(names)) != len(names):
            raise HTTPException(400, "fiducial names must be unique")
        job.fiducials = [Fiducial(name=n, x=float(f.x), y=float(f.y)) for n, f in zip(names, body.fiducials)]
        job.fiducial_mode = body.mode
        job.fiducial_diameter = float(body.diameter)
        clear_fit(job)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/fiducials/measure")
    async def measure_fiducials_route():
        idle()
        need_homed()
        job = need_job()
        need_origin(job)
        if not job.fiducials:
            raise HTTPException(409, "the job has no fiducials")
        if job.fiducial_mode == "off":
            raise HTTPException(409, "fiducial mode is off")
        camera, A = down_camera()
        if camera is None:
            raise HTTPException(409, "the down camera is not available")
        if A is None:
            raise HTTPException(409, "calibrate the down camera first")
        try:
            async with head.exclusive("aligning"):
                measured = await measure_fiducials(job, machine, camera, A, log=log.info)
        except AlignError as exc:
            raise HTTPException(422, str(exc)) from exc
        except MachineError as exc:
            raise HTTPException(status_for(exc), str(exc)) from exc
        jobs.save(measured)
        return state_json()

    @app.post("/api/job/fiducials/clear")
    async def clear_fiducial_fit():
        idle()
        job = need_job()
        clear_fit(job)
        jobs.save(job)
        return state_json()

    @app.post("/api/job/fiducials/{name}/goto")
    async def goto_fiducial(name: str, body: FiducialGotoIn | None = None):
        idle()
        need_homed()
        job = need_job()
        need_origin(job)
        fid = next((f for f in job.fiducials if f.name == name), None)
        if fid is None:
            raise HTTPException(404, f"no fiducial {name!r} in the job")
        board = body.board if body is not None else 0
        if not 0 <= board < len(job.boards):
            raise HTTPException(400, f"no board {board}")
        x, y = to_world(job, board, fid.x, fid.y)
        pos = await _call(machine.move_to(x=x, y=y))
        return {"position": pos}

    # ------------------------------------------------------------ run

    @app.post("/api/job/start")
    async def start_job():
        try:
            await runner.start()
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.status()

    @app.post("/api/job/resume")
    async def resume_job():
        try:
            await runner.resume()
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.status()

    @app.post("/api/job/step")
    async def step_job():
        """Step mode: run the next stage of the job (the first call starts the job)."""
        try:
            await runner.step()
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.status()

    @app.post("/api/job/pause")
    async def pause_job():
        try:
            runner.pause()
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.status()

    @app.post("/api/job/stop")
    async def stop_job():
        try:
            runner.stop()
        except JobError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.status()
