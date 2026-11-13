"""Feeder, pick-and-place and prick-offset API tests (in-process, simulator).

The module-scoped app builds on its state in file order: feeders are taught,
parts are picked and placed, the drag feeder is fed. Each test says what it relies on.
"""

import contextlib
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pnp.api.app import create_app
from pnp.config import MachineConfig
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import SIM_NOZZLE_OFFSETS, SIM_PRICK_OFFSET
from pnp.sim.tvm_sim import SimConfig, TvmSimulator
from pnp.vision.calibration_store import Calibration

POS_TOL = 0.031  # controller quantum is 0.0305 mm
T1 = SIM_NOZZLE_OFFSETS[1]


@contextlib.contextmanager
def start_app(tmp_path, sim_cfg=None, check_vacuum=False):
    cfg = MachineConfig()
    cfg.head.check_vacuum = check_vacuum
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.head.discard = (20.0, 20.0)
    sim = TvmSimulator(port=0, cfg=sim_cfg)
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    cal_path = tmp_path / "calibration.yaml"
    Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET).save(cal_path)
    app = create_app(machine, calibration_path=cal_path, feeders_path=tmp_path / "feeders.yaml",
                     extra_startup=startup, extra_shutdown=sim.stop)
    with TestClient(app) as client:
        for _ in range(200):
            s = client.get("/api/state").json()
            if s["connected"] and s["speed_mode"]:
                break
            time.sleep(0.02)
        assert s["connected"]
        yield SimpleNamespace(client=client, machine=machine, sim=sim, cal_path=cal_path)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    with start_app(tmp_path_factory.mktemp("feeders")) as e:
        yield e


def head(env):
    p = env.client.get("/api/state").json()["position"]
    return p["x"], p["y"]


def move_head(env, x, y):
    r = env.client.post("/api/move", json={"x": x, "y": y})
    assert r.status_code == 200, r.text


def feeder(env, id):
    return next(f for f in env.client.get("/api/feeders").json() if f["id"] == id)


def close(a, b, tol=POS_TOL):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def test_feeder_crud_and_validation(env):
    c = env.client
    strip = {"name": "0805 10k", "kind": "strip", "pick": [60, 40], "direction": [1, 0],
             "pitch": 4.0, "capacity": 50, "pick_depth": 3.0}
    r = c.put("/api/feeders/F1", json=strip)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "F1"
    got = feeder(env, "F1")
    assert got["name"] == "0805 10k" and got["pick"] == [60.0, 40.0]
    assert got["pick_location"] == [60.0, 40.0] and got["remaining"] == 50

    # Drag feeder needs a prick position.
    assert c.put("/api/feeders/F2", json={"kind": "drag", "pick": [60, 80]}).status_code == 400
    assert c.put("/api/feeders/F2", json={"kind": "bogus"}).status_code == 400
    assert c.put("/api/feeders/F2", json={"pitch": 0}).status_code == 400
    assert c.put("/api/feeders/F2", json={"direction": [0, 0]}).status_code == 400
    assert c.put("/api/feeders/F2", json={"pick": [1, 2, 3]}).status_code == 400
    drag = {"name": "SOT-23", "kind": "drag", "pick": [60, 80], "pitch": 4.0,
            "prick": [56, 84], "pick_depth": 3.0}
    assert c.put("/api/feeders/F2", json=drag).status_code == 200
    assert {f["id"] for f in c.get("/api/feeders").json()} == {"F1", "F2"}

    assert c.delete("/api/feeders/F2").status_code == 200
    assert [f["id"] for f in c.get("/api/feeders").json()] == ["F1"]
    assert c.delete("/api/feeders/F2").status_code == 404
    assert c.post("/api/feeders/F2/teach/pick").status_code == 404
    assert c.put("/api/feeders/F2", json=drag).status_code == 200  # restore for later tests


def test_teach_needs_homing_then_records_camera_position(env):
    c = env.client
    r = c.post("/api/feeders/F1/teach/pick")
    assert r.status_code == 409 and "home the machine first" in r.json()["detail"]

    assert c.post("/api/home").status_code == 200
    move_head(env, 70.0, 45.0)
    r = c.post("/api/feeders/F1/teach/pick")
    assert r.status_code == 200, r.text
    got = feeder(env, "F1")
    assert close(got["pick"], (70.0, 45.0)) and got["count"] == 0

    move_head(env, 56.0, 84.0)
    assert c.post("/api/feeders/F2/teach/prick").status_code == 200
    assert close(feeder(env, "F2")["prick"], (56.0, 84.0))

    # Put F1 back to the seeded pick position for the pick tests.
    move_head(env, 60.0, 40.0)
    assert c.post("/api/feeders/F1/teach/pick").status_code == 200
    assert c.post("/api/feeders/F1/goto").status_code == 200
    assert close(head(env), (60.0, 40.0))


def test_pick_hold_place_and_release(env):
    c = env.client
    assert c.post("/api/feeders/F1/pick", json={"nozzle": 1}).status_code == 200
    assert c.get("/api/head").json()["holding"]["1"] == "F1"
    assert feeder(env, "F1")["count"] == 1
    assert close(feeder(env, "F1")["pick_location"], (64.0, 40.0))

    r = c.post("/api/feeders/F1/pick", json={"nozzle": 1})
    assert r.status_code == 409 and "already holds" in r.json()["detail"]

    r = c.post("/api/head/place", json={"nozzle": 1, "x": 120.0, "y": 90.0})
    assert r.status_code == 200, r.text
    assert c.get("/api/head").json()["holding"]["1"] is None
    assert any(e[0] == "release" and e[1] == 1 for e in env.sim.machine.events)

    # A discarded part is also released.
    assert c.post("/api/feeders/F1/pick", json={"nozzle": 2}).status_code == 200
    assert c.get("/api/head").json()["holding"]["2"] == "F1"
    assert c.post("/api/head/discard", json={"nozzle": 2}).status_code == 200
    assert c.get("/api/head").json()["holding"]["2"] is None
    assert c.post("/api/head/discard", json={"nozzle": 3}).status_code == 400

    # Teaching a strip feeder again resets its count.
    move_head(env, 60.0, 40.0)
    assert c.post("/api/feeders/F1/teach/pick").status_code == 200
    assert feeder(env, "F1")["count"] == 0


def test_pick_and_place_at_crosshair(env):
    c = env.client
    move_head(env, 120.0, 90.0)  # the camera crosshair sits over world (120, 90)
    r = c.post("/api/feeders/F1/pick-and-place", json={"nozzle": 1})
    assert r.status_code == 200, r.text
    assert feeder(env, "F1")["count"] == 1
    release = [e for e in env.sim.machine.events if e[0] == "release" and e[1] == 1][-1]
    tip = (release[2] + SIM_NOZZLE_OFFSETS[1][0], release[3] + SIM_NOZZLE_OFFSETS[1][1])
    assert close(tip, (120.0, 90.0))
    assert c.get("/api/head").json()["holding"]["1"] is None

    # Crosshair too far right: the tip would need X 332. Refused before anything is picked.
    move_head(env, 290.0, 90.0)
    before = feeder(env, "F1")["count"]
    r = c.post("/api/feeders/F1/pick-and-place", json={"nozzle": 1})
    assert r.status_code == 400 and "out of reach" in r.json()["detail"]
    assert feeder(env, "F1")["count"] == before
    assert c.get("/api/head").json()["holding"]["1"] is None


def test_drag_feeder_feed_and_pick(env):
    c = env.client
    assert c.post("/api/feeders/F1/feed").status_code == 409  # strip feeders have no feed
    assert feeder(env, "F2")["needs_feed"] is True
    assert c.post("/api/feeders/F2/feed").status_code == 200
    assert feeder(env, "F2")["needs_feed"] is False
    assert c.post("/api/feeders/F2/pick", json={"nozzle": 2}).status_code == 200
    assert feeder(env, "F2")["needs_feed"] is True
    assert c.get("/api/head").json()["holding"]["2"] == "F2"
    assert c.post("/api/head/discard", json={"nozzle": 2}).status_code == 200


def test_prick_offset_from_camera_mark(env):
    c = env.client
    move_head(env, 100.0, 100.0)
    # No camera mark recorded yet: refused.
    assert c.post("/api/calibrate/prick-offset").status_code == 409
    assert c.post("/api/calibrate/nozzle-offset/camera").status_code == 200

    # Needle tip at head + t_prick must sit on the mark (100, 100).
    move_head(env, 100.0 - SIM_PRICK_OFFSET[0], 100.0 - SIM_PRICK_OFFSET[1])
    r = c.post("/api/calibrate/prick-offset")
    assert r.status_code == 200, r.text
    assert close(r.json()["offset"], SIM_PRICK_OFFSET)
    stored = c.get("/api/calibration").json()["prick_offset"]
    assert close(stored, SIM_PRICK_OFFSET)
    assert close(Calibration.load(env.cal_path).prick_offset, SIM_PRICK_OFFSET)


def test_failed_pick_is_422_and_keeps_count(tmp_path):
    with start_app(tmp_path, sim_cfg=SimConfig(pick_success=False), check_vacuum=True) as e:
        c = e.client
        assert c.post("/api/home").status_code == 200
        r = c.put("/api/feeders/F1", json={"kind": "strip", "pick": [60, 40], "pitch": 4.0,
                                           "capacity": 5})
        assert r.status_code == 200
        r = c.post("/api/feeders/F1/pick", json={"nozzle": 1})
        assert r.status_code == 422, r.text
        assert feeder(e, "F1")["count"] == 0
        assert c.get("/api/head").json()["holding"]["1"] is None
        assert c.post("/api/feeders/NOPE/pick", json={"nozzle": 1}).status_code == 404
        assert c.post("/api/feeders/F1/pick", json={"nozzle": 5}).status_code == 400


def test_head_inspect_needs_an_up_camera(tmp_path):
    """No cameras in this app: no inspector, so picks are recorded as skipped and inspect is a 409."""
    with start_app(tmp_path) as e:
        c = e.client
        assert c.post("/api/head/inspect", json={"nozzle": 1}).status_code == 409  # not homed yet
        assert c.post("/api/home").status_code == 200
        assert c.post("/api/head/inspect", json={"nozzle": 1}).status_code == 409
        assert c.post("/api/head/inspect", json={"nozzle": 7}).status_code == 400
        head = c.get("/api/head").json()
        assert set(head["last_inspection"]) == {"1", "2"}
        assert head["last_inspection"]["1"] is None and head["last_inspection"]["2"] is None


def test_pick_and_place_at_entered_coordinates(env):
    """Feeders tab 'Place target': explicit X/Y/rotation instead of the crosshair."""
    c = env.client
    move_head(env, 60.0, 60.0)  # crosshair elsewhere: must be ignored
    r = c.post("/api/feeders/F1/pick-and-place", json={"nozzle": 1, "x": 130.0, "y": 95.0, "rotation": 90.0})
    assert r.status_code == 200, r.text
    release = [e for e in env.sim.machine.events if e[0] == "release" and e[1] == 1][-1]
    tip = (release[2] + SIM_NOZZLE_OFFSETS[1][0], release[3] + SIM_NOZZLE_OFFSETS[1][1])
    # bottom vision may shift the head by the measured pick offset (<= 0.3 mm in the sim)
    assert abs(tip[0] - 130.0) < 0.5 and abs(tip[1] - 95.0) < 0.5
    a1 = c.get("/api/state").json()["position"]["a1"]
    assert abs(((a1 - 90.0) + 180) % 360 - 180) < 5.0  # 90 deg, minus the corrected pick error

    # Coordinates the nozzle can't reach are refused before anything is picked.
    before = feeder(env, "F1")["count"]
    r = c.post("/api/feeders/F1/pick-and-place", json={"nozzle": 1, "x": 400.0, "y": 95.0})
    assert r.status_code == 400 and "out of reach" in r.json()["detail"]
    assert feeder(env, "F1")["count"] == before


def test_manual_pick_vision_switch(env):
    """Feeders tab 'Vision check' off: the part is not inspected on the up camera."""
    c = env.client
    assert c.post("/api/home").status_code == 200
    r = c.put("/api/feeders/FV", json={"kind": "strip", "pick": [60, 40], "pitch": 4.0, "capacity": 20})
    assert r.status_code == 200, r.text
    r = c.post("/api/feeders/FV/pick", json={"nozzle": 1, "vision": False})
    assert r.status_code == 200, r.text
    last = c.get("/api/head").json()["last_inspection"]["1"]
    assert last is not None and last["result"] == "skipped" and "vision off" in last["message"]
    assert c.post("/api/head/place", json={"nozzle": 1}).status_code == 200
    # default (switch on): vision is requested; this test app has no cameras, so it is
    # skipped for that reason instead - not because vision was switched off
    r = c.post("/api/feeders/FV/pick", json={"nozzle": 1})
    assert r.status_code == 200, r.text
    assert "vision off" not in c.get("/api/head").json()["last_inspection"]["1"]["message"]
    assert c.post("/api/head/place", json={"nozzle": 1}).status_code == 200


def test_export_and_import_endpoints(tmp_path):
    with start_app(tmp_path) as e:
        c = e.client
        assert c.put("/api/feeders/X1", json={"kind": "strip", "pick": [60, 40], "pitch": 4.0,
                                              "value": "1k", "count": 3}).status_code == 200
        r = c.get("/api/feeders/export")
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith('attachment; filename="feeders-')
        exported = r.text
        assert "tvm802-feeders" in exported and "X1" in exported

        assert c.delete("/api/feeders/X1").status_code == 200
        assert all(f["id"] != "X1" for f in c.get("/api/feeders").json())

        r = c.post("/api/feeders/import", json={"content": exported, "mode": "merge", "reset_counts": True})
        assert r.status_code == 200, r.text
        x1 = next(f for f in c.get("/api/feeders").json() if f["id"] == "X1")
        assert x1["value"] == "1k" and x1["count"] == 0

        r = c.post("/api/feeders/import", json={"content": "not: a feeder file"})
        assert r.status_code == 400 and "not a feeder configuration" in r.json()["detail"]
