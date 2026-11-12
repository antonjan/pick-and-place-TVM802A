"""FastAPI app: REST control + WebSocket state stream + static web UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pnp.api.errors import call as _call
from pnp.api.feeder_routes import add_feeder_routes
from pnp.api.vision_routes import add_vision_routes
from pnp.config import MachineConfig, load_config
from pnp.machine.machine import Machine
from pnp.vision.camera import CameraManager, UvcCamera, load_cameras_config

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_CALIBRATION = Path(__file__).resolve().parents[2] / "config" / "calibration.yaml"
DEFAULT_FEEDERS = Path(__file__).resolve().parents[2] / "config" / "feeders.yaml"


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
               extra_startup=None, extra_shutdown=None, feeders_path: Path | None = None) -> FastAPI:
    calibration_path = Path(calibration_path) if calibration_path is not None else DEFAULT_CALIBRATION
    feeders_path = Path(feeders_path) if feeders_path is not None else DEFAULT_FEEDERS

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if extra_startup:
            await extra_startup()
        await machine.start()
        if cameras is not None:
            cameras.start_all()
        yield
        if cameras is not None:
            await asyncio.to_thread(cameras.stop_all)
        await machine.stop()
        if extra_shutdown:
            await extra_shutdown()

    app = FastAPI(title="TVM802 PnP", lifespan=lifespan)
    app.state.machine = machine
    add_vision_routes(app, machine, cameras, calibration_path)
    add_feeder_routes(app, machine, feeders_path)

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

    @app.post("/api/rotate")
    async def rotate(req: RotateReq):
        return {"position": await _call(machine.rotate(req.nozzle, req.angle))}

    @app.post("/api/output")
    async def output(req: OutputReq):
        await _call(machine.set_output(req.name, req.on))
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
