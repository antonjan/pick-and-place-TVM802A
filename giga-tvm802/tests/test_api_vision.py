"""Camera, calibration and head-tool API tests (in-process, simulator + sim cameras).

The tests share one app and build on each other in file order: the down
camera is calibrated before the click test, the nozzle offsets before the
tool test, and so on.
"""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pnp.api.app import create_app
from pnp.config import MachineConfig
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import (
    SIM_NOZZLE_OFFSETS,
    SIM_UP_CAMERA_XY,
    make_down_renderer,
    make_up_renderer,
    sim_down_A,
    sim_up_A,
)
from pnp.sim.tvm_sim import TvmSimulator
from pnp.vision.calibration import move_for_offset
from pnp.vision.calibration_store import Calibration
from pnp.vision.camera import CameraManager, SimCamera

T1 = SIM_NOZZLE_OFFSETS[1]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    cfg = MachineConfig()
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    sim = TvmSimulator(port=0)
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    def pos():
        p = machine.position()
        return (p["x"], p["y"])

    cameras = CameraManager({
        "down": SimCamera("down", make_down_renderer(pos)),
        "up": SimCamera("up", make_up_renderer(pos)),
    })

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    cal_path = tmp_path_factory.mktemp("cal") / "calibration.yaml"
    app = create_app(machine, cameras=cameras, calibration_path=cal_path,
                     extra_startup=startup, extra_shutdown=sim.stop)
    with TestClient(app) as client:
        for _ in range(200):
            s = client.get("/api/state").json()
            if s["connected"] and s["speed_mode"]:
                break
            time.sleep(0.02)
        assert s["connected"]
        yield SimpleNamespace(client=client, machine=machine, cal_path=cal_path)


def head(env):
    p = env.client.get("/api/state").json()["position"]
    return p["x"], p["y"]


def move_head(env, x, y):
    r = env.client.post("/api/move", json={"x": x, "y": y})
    assert r.status_code == 200, r.text


def test_cameras_listed_snapshot_and_stream(env):
    c = env.client
    deadline = time.monotonic() + 2.0
    cams = {}
    while time.monotonic() < deadline:
        cams = {d["name"]: d for d in c.get("/api/cameras").json()}
        if cams.get("down", {}).get("connected") and cams.get("up", {}).get("connected"):
            if cams["down"]["fps"] > 0 and cams["up"]["fps"] > 0:
                break
        time.sleep(0.05)
    assert cams["down"]["connected"] and cams["up"]["connected"]

    r = c.get("/cameras/down/snapshot.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")

    assert c.get("/cameras/nope/snapshot.jpg").status_code == 404

    # The stream is endless: read until the first complete part, then close.
    result = {}

    def read_first_part():
        buf = b""
        with c.stream("GET", "/cameras/down/stream.mjpg") as r:
            result["type"] = r.headers["content-type"]
            for chunk in r.iter_bytes():
                buf += chunk
                if b"--frame" in buf and b"\xff\xd9\r\n" in buf:
                    break
        result["buf"] = buf

    t = threading.Thread(target=read_first_part, daemon=True)
    t.start()
    t.join(15.0)
    assert not t.is_alive(), "stream did not deliver a frame"
    assert result["type"].startswith("multipart/x-mixed-replace")
    assert b"Content-Type: image/jpeg" in result["buf"]
    assert b"\xff\xd8" in result["buf"]


def test_click_before_calibration_is_409(env):
    r = env.client.post("/api/cameras/down/click", json={"u": 10, "v": 10})
    assert r.status_code == 409
    assert "calibrate the down camera first" in r.json()["detail"]


def test_down_scale_calibration(env):
    c = env.client
    assert c.post("/api/home").status_code == 200
    move_head(env, 100.0, 100.0)  # the sim cross mark is at (100, 100)
    r = c.post("/api/calibrate/down/scale", json={"step_mm": 1.0})
    assert r.status_code == 200, r.text
    A = np.asarray(r.json()["A"])
    A_true = sim_down_A()
    assert np.max(np.abs(A - A_true)) <= 0.02 * np.max(np.abs(A_true))

    cal = Calibration.load(env.cal_path)
    assert cal.A("down") is not None
    assert np.allclose(cal.A("down"), A)

    cal_json = c.get("/api/calibration").json()
    assert "down" in cal_json["cameras"]
    assert cal_json["cameras"]["down"]["mm_per_px"] == pytest.approx(
        float(np.mean(1.0 / np.linalg.svd(A, compute_uv=False))), rel=1e-6)


def test_click_moves_head_by_offset(env):
    c = env.client
    move_head(env, 100.0, 100.0)
    A = Calibration.load(env.cal_path).A("down")
    w, h = 640, 480
    r = c.post("/api/cameras/down/click", json={"u": w / 2 + 50, "v": h / 2})
    assert r.status_code == 200, r.text
    dx, dy = move_for_offset(A, (50, 0))
    x, y = head(env)
    assert abs(x - (100.0 + dx)) < 0.05
    assert abs(y - (100.0 + dy)) < 0.05


def test_nozzle_offset_flow(env):
    c = env.client
    move_head(env, 100.0, 100.0)
    assert c.post("/api/calibrate/nozzle-offset/camera").status_code == 200
    mark = c.get("/api/calibration").json()["camera_mark"]
    assert mark is not None and abs(mark[0] - 100.0) < 0.05 and abs(mark[1] - 100.0) < 0.05

    # Jog nozzle 1 onto the mark: the head is where the nozzle tip is at the mark.
    move_head(env, 100.0 - T1[0], 100.0 - T1[1])
    r = c.post("/api/calibrate/nozzle-offset/1")
    assert r.status_code == 200, r.text
    stored = np.asarray(c.get("/api/calibration").json()["nozzle_offset"]["1"])
    assert np.max(np.abs(stored - np.asarray(T1))) < 0.05

    move_head(env, 100.0, 100.0)
    r = c.post("/api/tools/nozzle-to-camera/1")
    assert r.status_code == 200, r.text
    x, y = head(env)
    assert abs(x - (100.0 - stored[0])) < 0.05
    assert abs(y - (100.0 - stored[1])) < 0.05


def test_up_camera_calibration(env):
    c = env.client
    # Nozzle 1 tip (head + t_1) over the up camera centre.
    target = (SIM_UP_CAMERA_XY[0] - T1[0], SIM_UP_CAMERA_XY[1] - T1[1])
    move_head(env, *target)

    r = c.post("/api/calibrate/up/scale", json={"step_mm": 1.0})
    assert r.status_code == 200, r.text
    A = np.asarray(r.json()["A"])
    A_true = sim_up_A()
    assert np.max(np.abs(A - A_true)) <= 0.03 * np.max(np.abs(A_true))

    move_head(env, target[0] + 1.0, target[1] - 0.5)  # start the centring off target
    r = c.post("/api/calibrate/up/centre", json={"polarity": "bright"})
    assert r.status_code == 200, r.text

    r = c.post("/api/calibrate/up-position/1")
    assert r.status_code == 200, r.text
    up = c.get("/api/calibration").json()["up_position"]["1"]
    assert abs(up[0] - target[0]) < 0.05 and abs(up[1] - target[1]) < 0.05


def test_error_mapping(env):
    c = env.client
    assert c.post("/api/cameras/nope/click", json={"u": 1, "v": 1}).status_code == 404
    assert c.post("/api/calibrate/nope/scale", json={}).status_code == 404
    assert c.post("/api/calibrate/nozzle-offset/3").status_code == 400
    assert c.post("/api/tools/camera-to-nozzle/2").status_code == 409  # offset 2 not recorded
    assert c.get("/cameras/nope/stream.mjpg").status_code == 404


def test_config_reload_updates_limits_without_restart(tmp_path):
    """POST /api/config/reload re-reads machine.yaml limits in place."""
    from fastapi.testclient import TestClient

    from pnp.api.app import create_app
    from pnp.config import MachineConfig
    from pnp.machine.machine import Machine
    from pnp.sim.tvm_sim import TvmSimulator

    cfg_file = tmp_path / "machine.yaml"
    cfg_file.write_text("limits:\n  x: [-10.0, 299.0]\n")
    sim = TvmSimulator(port=0)
    machine = Machine(MachineConfig())

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    app = create_app(machine, calibration_path=tmp_path / "cal.yaml", config_path=cfg_file,
                     extra_startup=startup, extra_shutdown=sim.stop)
    with TestClient(app) as c:
        assert c.post("/api/config/reload").status_code == 200
        assert c.get("/api/state").json()["soft_limits"]["x"] == [-10.0, 299.0]
        cfg_file.write_text("limits:\n  x: [-113.0, 299.0]\n")
        r = c.post("/api/config/reload")
        assert r.status_code == 200 and r.json()["machine"]
        assert c.get("/api/state").json()["soft_limits"]["x"] == [-113.0, 299.0]
        cfg_file.write_text("limits:\n  nope: 1\n")
        assert c.post("/api/config/reload").status_code == 400
