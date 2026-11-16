"""FastAPI app: REST control + WebSocket state stream + static web UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pnp.api.bank_routes import add_bank_routes
from pnp.api.errors import call as _call
from pnp.api.feeder_routes import add_feeder_routes
from pnp.api.job_routes import add_job_routes
from pnp.api.vision_routes import add_vision_routes
from pnp.config import MachineConfig, load_config
from pnp.machine.machine import Machine
from pnp.vision.camera import CameraManager, UvcCamera, load_cameras_config

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_CALIBRATION = Path(__file__).resolve().parents[2] / "config" / "calibration.yaml"
DEFAULT_FEEDERS = Path(__file__).resolve().parents[2] / "config" / "feeders.yaml"
DEFAULT_BANKS = Path(__file__).resolve().parents[2] / "config" / "banks.yaml"
DEFAULT_PACKAGES = Path(__file__).resolve().parents[2] / "config" / "packages.yaml"
DEFAULT_JOB = Path(__file__).resolve().parents[2] / "config" / "job.yaml"
# Safety controls stay available during a job (the alarm buttons must work while a
# job is waiting on a stuck needle).
ALWAYS_ALLOWED = {"/api/panel", "/api/estop", "/api/reset", "/api/alarm/clear", "/api/alarm/silence", "/api/alarm/abort"}


class MoveReq(BaseModel):
    x: float | None = None
    y: float | None = None
    z: float | None = None
    a1: float | None = None
    a2: float | None = None
    safe: bool = True


class JogReq(BaseModel):
    axis: str
    delta: float


class PanelReq(BaseModel):
    mode: str | None = None  # "dot" or "con"
    step: float | None = None  # X/Y jog step for the front-panel keys (mm)


class NozzleReq(BaseModel):
    nozzle: int
    depth: float = 0.0


class RotateReq(BaseModel):
    nozzle: int
    angle: float


class OutputReq(BaseModel):
    name: str
    on: bool


def create_app(machine: Machine, *, cameras: CameraManager | None = None, calibration_path: Path | None = None,
               config_path: Path | None = None, cameras_path: Path | None = None,
               extra_startup=None, extra_shutdown=None, feeders_path: Path | None = None,
               banks_path: Path | None = None, rules_path: Path | None = None,
               job_path: Path | None = None) -> FastAPI:
    calibration_path = Path(calibration_path) if calibration_path is not None else DEFAULT_CALIBRATION
    feeders_path = Path(feeders_path) if feeders_path is not None else DEFAULT_FEEDERS
    banks_path = Path(banks_path) if banks_path is not None else DEFAULT_BANKS
    rules_path = Path(rules_path) if rules_path is not None else DEFAULT_PACKAGES
    job_path = Path(job_path) if job_path is not None else DEFAULT_JOB

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if extra_startup:
            await extra_startup()
        await machine.start()
        if cameras is not None:
            cameras.start_all()
        yield
        runner = getattr(app.state, "runner", None)
        if runner is not None:
            await runner.shutdown()
        if cameras is not None:
            await asyncio.to_thread(cameras.stop_all)
        await machine.stop()
        if extra_shutdown:
            await extra_shutdown()

    app = FastAPI(title="TVM802 PnP", lifespan=lifespan)
    app.state.machine = machine

    @app.middleware("http")
    async def block_while_job_runs(request, call_next):
        """While a job runs, refuse every change except E-stop and the job's own endpoints,
        so a manual jog, output toggle or calibration can't collide with the runner."""
        runner = getattr(app.state, "runner", None)
        path = request.url.path
        if (runner is not None and runner.busy() and request.method in ("POST", "PUT", "DELETE")
                and path.startswith("/api/") and path not in ALWAYS_ALLOWED
                and not path.startswith("/api/job")):
            return JSONResponse({"detail": "job running: pause or stop it first"}, status_code=409)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate_ui_files(request, call_next):
        """Make the browser check for a newer UI on every load. Without this it
        may keep running a cached app.js after an update (seen 2026-11-13: the
        new Connect button's HTML arrived but its script did not). Unchanged
        files still come from the cache via a cheap 304."""
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    add_vision_routes(app, machine, cameras, calibration_path)
    add_feeder_routes(app, machine, feeders_path)
    add_bank_routes(app, machine, banks_path)
    add_job_routes(app, machine, job_path, rules_path)
    from pnp.machine.panel import FrontPanel
    app.state.panel = FrontPanel(machine, runner=app.state.runner)

    @app.post("/api/panel")
    async def set_panel(req: PanelReq):
        """Front-panel jog mode and step (the UI's X/Y step buttons keep it in sync)."""
        if req.mode is not None:
            if req.mode not in ("dot", "con"):
                raise HTTPException(400, "mode must be dot or con")
            machine.panel["mode"] = req.mode
        if req.step is not None:
            if not req.step > 0:
                raise HTTPException(400, "step must be greater than 0")
            machine.panel["step"] = float(req.step)
        machine._notify()
        return machine.panel

    @app.get("/api/config/defaults")
    async def get_defaults():
        """Defaults tab: every editable machine.yaml value with label, help and default."""
        from pnp.config_editor import describe
        return {"sections": describe(machine.cfg), "path": str(config_path) if config_path else None}

    @app.put("/api/config/defaults")
    async def put_defaults(body: dict):
        """Save changed defaults to machine.yaml (comments kept) and apply them at once."""
        from pnp.config_editor import apply_live, describe, save, validate, warnings_for
        changes = body.get("changes") if isinstance(body, dict) else None
        if not isinstance(changes, dict):
            raise HTTPException(400, "expected {\"changes\": {section: {field: value}}}")
        try:
            new_cfg = validate(machine.cfg, changes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if config_path is None:
            raise HTTPException(409, "no machine.yaml path configured: can't save")
        changed = save(config_path, machine.cfg, new_cfg)
        apply_live(machine.cfg, new_cfg)
        machine._notify()
        return {"changed": changed, "warnings": warnings_for(changed), "sections": describe(machine.cfg)}

    @app.post("/api/config/reload")
    async def reload_config():
        """Re-read machine.yaml (limits, motion, homing, steps) and cameras.yaml
        (orientation/resolution/controls) without restarting the server, so the
        cameras are never re-opened while a dying process still holds them.
        Controller host/port and the HTTP server settings need a restart."""
        result = {"machine": None, "cameras": [], "warnings": []}
        if config_path is not None:
            try:
                new_cfg = load_config(config_path)
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(400, f"machine config: {exc}") from exc
            for section in ("limits", "motion", "homing", "steps"):
                setattr(machine.cfg, section, getattr(new_cfg, section))
            if (new_cfg.controller.host, new_cfg.controller.port) != (machine.cfg.controller.host,
                                                                      machine.cfg.controller.port):
                result["warnings"].append("controller host/port changes need a restart")
            result["machine"] = "reloaded limits, motion, homing, steps"
        if cameras_path is not None and cameras is not None:
            try:
                cam_cfgs = load_cameras_config(cameras_path)
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(400, f"camera config: {exc}") from exc
            for name, cfg in cam_cfgs.items():
                if name not in cameras.names():
                    result["warnings"].append(f"new camera {name!r} needs a restart")
                    continue
                cam = cameras.get(name)
                if not isinstance(cam, UvcCamera) or cam.cfg == cfg:
                    continue
                await asyncio.to_thread(cam.stop)
                if not cam.stopped:
                    result["warnings"].append(f"camera {name} did not stop; replug it")
                    continue
                cam.cfg, cam.device = cfg, cfg.device
                cam.rotate, cam.flip_x, cam.flip_y = cfg.rotate, cfg.flip_x, cfg.flip_y
                cam.start()
                result["cameras"].append(name)
        machine._notify()
        return result

    # ------------------------------------------------------------------ REST

    @app.get("/api/state")
    async def get_state():
        return machine.snapshot()

    @app.post("/api/home")
    async def home():
        return {"position": await _call(machine.home())}

    @app.post("/api/move")
    async def move(req: MoveReq):
        targets = req.model_dump(exclude={"safe"}, exclude_none=True)
        return {"position": await _call(machine.move_to(safe=req.safe, **targets))}

    @app.post("/api/jog")
    async def jog(req: JogReq):
        return {"position": await _call(machine.jog(req.axis, req.delta))}

    @app.post("/api/nozzle/down")
    async def nozzle_down(req: NozzleReq):
        return {"position": await _call(machine.nozzle_down(req.nozzle, req.depth))}

    @app.post("/api/nozzle/up")
    async def nozzle_up():
        return {"position": await _call(machine.nozzles_up())}

    @app.get("/api/debug/raw")
    async def debug_raw():
        """Read-only diagnostics: the controller's raw status reply and a fresh poll
        reply, as hex. Used to find out what the unknown status bits mean."""
        from pnp.tvm import protocol as p_
        state = machine.t.state
        try:
            poll = await machine.t.command(p_.encode_poll())
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            poll = None
            poll_error = str(exc)
        else:
            poll_error = None
        return {
            "state": state.raw.hex(" ") if state is not None else None,
            "state_seq": machine.t.state_seq,
            "poll": poll.hex(" ") if poll else None,
            "poll_error": poll_error,
        }

    @app.post("/api/alarm/clear")
    async def alarm_clear():
        """Popup: continue once the needle sensor reads up (else the alarm stays)."""
        machine.alarm_clear()
        return {"alarm": machine.alarm}

    @app.post("/api/alarm/silence")
    async def alarm_silence():
        await _call(machine.alarm_silence())
        return {"alarm": machine.alarm}

    @app.post("/api/alarm/abort")
    async def alarm_abort():
        """Popup: give up; the waiting action fails with the needle-stuck error."""
        machine.alarm_abort()
        return {"alarm": machine.alarm}

    @app.post("/api/machine/zero-z")
    async def zero_z():
        """Set the current Z as 0 (both nozzle tips level), without moving."""
        return {"position": await _call(machine.zero_z())}

    @app.post("/api/rotate")
    async def rotate(req: RotateReq):
        return {"position": await _call(machine.rotate(req.nozzle, req.angle))}

    @app.post("/api/output")
    async def output(req: OutputReq):
        await _call(machine.set_output(req.name, req.on))
        return {"ok": True}

    @app.post("/api/machine/reconnect")
    async def reconnect():
        """Connect button: drop the controller link and connect again now."""
        await machine.reconnect()
        return {"ok": True}

    @app.post("/api/estop")
    async def estop():
        await machine.estop()
        return {"ok": True}

    @app.post("/api/reset")
    async def reset():
        await machine.reset()
        return {"ok": True}

    # ------------------------------------------------------------------ WebSocket

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        changed = asyncio.Event()
        listener = lambda _snap: changed.set()  # noqa: E731
        machine.on_change(listener)
        last = None
        try:
            while True:
                snap = machine.snapshot()
                snap.pop("time", None)
                text = json.dumps(snap)
                if text != last:
                    await socket.send_text(text)
                    last = text
                changed.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(changed.wait(), 1.0)
                await asyncio.sleep(0.05)  # max ~20 updates/s
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            machine.off_change(listener)

    # ------------------------------------------------------------------ static UI

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def build(cfg: MachineConfig) -> FastAPI:
    return create_app(Machine(cfg))
