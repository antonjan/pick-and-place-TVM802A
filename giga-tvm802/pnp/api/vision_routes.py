"""Camera, calibration and head-tool endpoints (see docs/phase2-design.md, API/UI)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from pnp.api.errors import call, status_for
from pnp.machine.machine import MachineError
from pnp.vision import calibration as cal_math
from pnp.vision.calibration import CalibrationError
from pnp.vision.calibration_store import Calibration
from pnp.vision.camera import Camera, CameraError, CameraManager

log = logging.getLogger(__name__)

STREAM_PERIOD = 0.1  # s between checks for a new frame
STREAM_JPEG_QUALITY = 75
SNAPSHOT_JPEG_QUALITY = 85


class NeedleReq(BaseModel):
    down: bool


class ClickReq(BaseModel):
    u: float
    v: float


class ScaleReq(BaseModel):
    step_mm: float = 1.0


class CentreReq(BaseModel):
    polarity: str = "auto"


async def _run(coro):
    """Run a vision/calibration coroutine, mapping its errors to HTTP status codes."""
    try:
        return await coro
    except CalibrationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CameraError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MachineError as exc:
        raise HTTPException(status_for(exc), str(exc)) from exc


def sharpness(img) -> float:
    """Focus score: variance of the Laplacian over the central third of the image.
    Blurred/featureless views score in the single digits; a focused PCB scores
    in the hundreds. Only meaningful relative to the same scene and lighting."""
    gray = cal_math.to_gray(img)
    h, w = gray.shape[:2]
    centre = gray[h // 3: 2 * h // 3, w // 3: 2 * w // 3]
    return float(cv2.Laplacian(centre, cv2.CV_64F).var())


def add_vision_routes(app: FastAPI, machine, cameras: CameraManager | None, calibration_path: Path) -> None:
    calibration_path = Path(calibration_path)
    app.state.calibration = Calibration.load(calibration_path)
    app.state.calibration_path = calibration_path
    app.state.cameras = cameras  # CameraManager or None (fiducial measurement uses "down")
    app.state.calibration_lock = asyncio.Lock()
    app.state.camera_mark = None  # (x, y) of the down camera centre, in memory only

    def cam(name: str) -> Camera:
        if cameras is None or name not in cameras.names():
            raise HTTPException(404, f"unknown camera {name!r}")
        return cameras.get(name)

    def save() -> None:
        app.state.calibration.save(app.state.calibration_path)

    def head() -> tuple[float, float]:
        if not machine.connected:
            raise HTTPException(409, "not connected to the machine")
        pos = machine.position()
        return float(pos["x"]), float(pos["y"])

    def homed_head() -> tuple[float, float]:
        """Head position for values that get stored: only meaningful after homing."""
        if not machine.homed:
            raise HTTPException(409, "home the machine before recording calibration positions")
        return head()

    def require_cal(name: str):
        A = app.state.calibration.A(name)
        if A is None:
            raise HTTPException(409, f"calibrate the {name} camera first")
        return A

    def nozzle_no(n: int) -> int:
        if n not in (1, 2):
            raise HTTPException(400, "nozzle must be 1 or 2")
        return n

    @contextlib.asynccontextmanager
    async def exclusive():
        lock: asyncio.Lock = app.state.calibration_lock
        if lock.locked():
            raise HTTPException(409, "calibration already running")
        async with lock:
            yield

    # ------------------------------------------------------------ cameras

    @app.get("/api/cameras")
    async def list_cameras():
        if cameras is None:
            return []
        out = []
        for name in cameras.names():
            camera = cameras.get(name)
            st = camera.status()
            frame = camera.latest()
            st["sharpness"] = None if frame is None else round(sharpness(frame.image), 1)
            out.append(st)
        return out

    @app.get("/cameras/{name}/snapshot.jpg")
    async def snapshot(name: str):
        data = await asyncio.to_thread(cam(name).jpeg, SNAPSHOT_JPEG_QUALITY)
        if data is None:
            raise HTTPException(503, f"{name}: no frame yet")
        return Response(content=data, media_type="image/jpeg")

    @app.get("/cameras/{name}/stream.mjpg")
    async def stream(name: str, request: Request):
        camera = cam(name)

        async def frames() -> AsyncIterator[bytes]:
            last_seq = None
            while True:
                if await request.is_disconnected():
                    break
                frame = camera.latest()
                if frame is not None and frame.seq != last_seq:
                    last_seq = frame.seq
                    jpeg = await asyncio.to_thread(camera.jpeg, STREAM_JPEG_QUALITY)
                    if jpeg:
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                               b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                               + jpeg + b"\r\n")
                await asyncio.sleep(STREAM_PERIOD)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/api/cameras/{name}/click")
    async def click(name: str, req: ClickReq):
        camera = cam(name)
        A = require_cal(name)
        w, h = camera.size
        if w == 0 or h == 0:
            raise HTTPException(503, f"{name}: no frame yet")
        dx, dy = cal_math.move_for_offset(A, (req.u - w / 2.0, req.v - h / 2.0))
        x, y = head()
        return {"position": await call(machine.move_to(x=x + dx, y=y + dy))}

    # ------------------------------------------------------------ calibration state

    @app.get("/api/calibration")
    async def get_calibration():
        c: Calibration = app.state.calibration
        cams = {}
        for name, entry in c.cameras.items():
            s = np.linalg.svd(entry.matrix(), compute_uv=False)
            mm_per_px = float(np.mean(1.0 / s)) if np.all(s > 0) else None
            cams[name] = {"A": entry.A, "rms_px": entry.rms_px, "date": entry.date, "mm_per_px": mm_per_px}
        mark = app.state.camera_mark
        return {
            "cameras": cams,
            "nozzle_offset": {str(n): list(v) for n, v in c.nozzle_offset.items()},
            "up_position": {str(n): list(v) for n, v in c.up_position.items()},
            "camera_mark": list(mark) if mark is not None else None,
            "prick_offset": list(c.prick_offset) if c.prick_offset is not None else None,
        }

    # ------------------------------------------------------------ calibration runs

    @app.post("/api/calibrate/{name}/scale")
    async def calibrate_scale(name: str, req: ScaleReq):
        camera = cam(name)
        async with exclusive():
            result = await _run(cal_math.calibrate_scale(machine, camera, step_mm=req.step_mm))
            app.state.calibration.set_camera(name, result["A"], result["rms_px"])
            save()
        log.info("%s camera scale calibrated: rms %.3f px", name, result["rms_px"])
        return result

    @app.post("/api/calibrate/{name}/centre")
    async def calibrate_centre(name: str, req: CentreReq):
        camera = cam(name)
        A = require_cal(name)
        resolution = machine.cfg.motion.step_quantum / machine.cfg.steps.x
        async with exclusive():
            await _run(cal_math.centre_on_blob(
                machine, camera, A, polarity=req.polarity, resolution_mm=resolution))
        return {"position": machine.position()}

    @app.post("/api/calibrate/nozzle-offset/camera")
    async def mark_camera():
        app.state.camera_mark = homed_head()
        return {"camera_mark": list(app.state.camera_mark)}

    @app.post("/api/calibrate/nozzle-offset/{n}")
    async def nozzle_offset(n: int):
        nozzle_no(n)
        mark = app.state.camera_mark
        if mark is None:
            raise HTTPException(409, "record the camera mark first (camera is on mark)")
        x, y = homed_head()
        t = (mark[0] - x, mark[1] - y)
        app.state.calibration.nozzle_offset[n] = t
        save()
        return {"nozzle": n, "offset": list(t)}

    @app.post("/api/calibrate/needle")
    async def setup_needle(req: NeedleReq):
        """Setup needle step: lower/raise the feeder needle. While lowered this
        way, X/Y jogging is allowed so the tip can be put on the camera mark;
        everywhere else moves are refused with the needle down."""
        await call(machine.set_needle_setup(req.down))
        return {"needle_down": machine.needle_is_down(), "needle_setup": machine.needle_setup}

    @app.post("/api/calibrate/prick-offset")
    async def prick_offset():
        mark = app.state.camera_mark
        if mark is None:
            raise HTTPException(409, "record the camera mark first (camera is on mark)")
        x, y = homed_head()
        t = (mark[0] - x, mark[1] - y)
        app.state.calibration.prick_offset = t
        save()
        return {"offset": list(t)}

    @app.post("/api/calibrate/up-position/{n}")
    async def up_position(n: int):
        nozzle_no(n)
        x, y = homed_head()
        app.state.calibration.up_position[n] = (x, y)
        save()
        return {"nozzle": n, "position": [x, y]}

    # ------------------------------------------------------------ head tools

    def offset_of(n: int) -> tuple[float, float]:
        t = app.state.calibration.nozzle_offset.get(nozzle_no(n))
        if t is None:
            raise HTTPException(409, f"record the nozzle {n} offset first")
        return t

    @app.post("/api/tools/nozzle-to-camera/{n}")
    async def nozzle_to_camera(n: int):
        t = offset_of(n)
        x, y = head()
        return {"position": await call(machine.move_to(x=x - t[0], y=y - t[1]))}

    @app.post("/api/tools/camera-to-nozzle/{n}")
    async def camera_to_nozzle(n: int):
        t = offset_of(n)
        x, y = head()
        return {"position": await call(machine.move_to(x=x + t[0], y=y + t[1]))}
