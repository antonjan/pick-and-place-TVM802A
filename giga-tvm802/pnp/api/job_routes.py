"""Package rules, KiCad job, panel, alignment, placement and run endpoints (docs/phase4-design.md, section 7)."""

from __future__ import annotations

import dataclasses
import logging
import math
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from pnp.api.errors import call as _call
from pnp.api.errors import status_for
from pnp.job.align import AlignError, measure_fiducials
from pnp.job.job import (
    BoardInstance,
    Fiducial,
    Job,
    JobPart,
    JobStore,
    PlanItem,
    clear_fit,
    fit_rotation,
    grid,
    job_from_pos,
    plan,
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


PLACEMENT_STATUSES = ("pending", "skipped", "placed")


def _job_dict(job: Job) -> dict:
    return {
        "name": job.name,
        "source": job.source,
        "origin": None if job.origin is None else [job.origin[0], job.origin[1]],
        "rotation": job.rotation,
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
        return plan(job, feeders.all(), rules)

    def state_json() -> dict:
        job = jobs.load()
        if job is None:
            return {"job": None, "plan": [], "status": runner.status()}
        return {
            "job": _job_dict(job),
            "plan": [dataclasses.asdict(i) for i in items_of(job)],
            "status": runner.status(),
        }

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
        """Board origin AND rotation from two reference points recorded with the camera."""
        from pnp.job.job import fit_two_points

        idle()
        need_homed()
        job = need_job()
        if not 0 <= body.board < len(job.boards):
            raise HTTPException(400, f"no board {body.board + 1}")
        a_panel = _panel_point(job, body.board, body.a.x, body.a.y)
        b_panel = _panel_point(job, body.board, body.b.x, body.b.y)
        try:
            result = fit_two_points(a_panel, (body.a.world_x, body.a.world_y),
                                    b_panel, (body.b.world_x, body.b.world_y))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        job.origin = (result["origin"][0], result["origin"][1])
        job.rotation = result["rotation"]
        job.references = [{"ref": p.ref or None, "x": p.x, "y": p.y} for p in (body.a, body.b)]
        job.reference_board = body.board
        clear_fit(job)
        jobs.save(job)
        return {**state_json(), "alignment": result}

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
        clear_fit(job)
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
        clear_fit(job)
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
