"""Banks, package rules, job, placement and run endpoints (in-process, simulator).

The module-scoped app builds on its state in file order: banks are generated and loaded,
the rules are edited (and restored), the demo job is imported, aligned and run to done.
"""

import contextlib
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pnp.api.__main__ import sim_demo_boards
from pnp.api.app import create_app
from pnp.config import HeadConfig, MachineConfig
from pnp.job.rules import DEFAULT_RULES
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import (
    SIM_BOARD_ERROR_ROT,
    SIM_BOARD_ERROR_XY,
    SIM_NOZZLE_OFFSETS,
    SIM_PRICK_OFFSET,
    make_down_renderer,
    sim_down_A,
)
from pnp.sim.tvm_sim import TvmSimulator
from pnp.vision.camera import CameraManager, SimCamera
from pnp.vision.calibration_store import Calibration

POS_TOL = 0.031  # controller quantum is 0.0305 mm
DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo.pos"
FAST = HeadConfig(pick_dwell=0.01, place_dwell=0.01, blow_time=0.01, vacuum_settle=0.01, prick_settle=0.01)


@contextlib.contextmanager
def start_app(tmp_path):
    cfg = MachineConfig()
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.head = FAST
    sim = TvmSimulator(port=0)
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    cal_path = tmp_path / "calibration.yaml"
    Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET).save(cal_path)
    app = create_app(machine, calibration_path=cal_path, feeders_path=tmp_path / "feeders.yaml",
                     banks_path=tmp_path / "banks.yaml", rules_path=tmp_path / "packages.yaml",
                     job_path=tmp_path / "job.yaml", extra_startup=startup, extra_shutdown=sim.stop)
    with TestClient(app) as client:
        for _ in range(200):
            s = client.get("/api/state").json()
            if s["connected"] and s["speed_mode"]:
                break
            time.sleep(0.02)
        assert s["connected"]
        yield SimpleNamespace(client=client, machine=machine, sim=sim, app=app, tmp=tmp_path)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    with start_app(tmp_path_factory.mktemp("job")) as e:
        yield e


def close(a, b, tol=POS_TOL):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def lanes_of(c, bank):
    return [f for f in c.get("/api/feeders").json() if f["bank"] == bank]


def set_lane(c, fid, value, package):
    """Load a lane: send the whole feeder back with the value and package changed."""
    f = next(x for x in c.get("/api/feeders").json() if x["id"] == fid)
    body = {k: v for k, v in f.items() if k not in ("id", "pick_location", "remaining")}
    body.update(value=value, package=package)
    r = c.put(f"/api/feeders/{fid}", json=body)
    assert r.status_code == 200, r.text


def job_state(c):
    return c.get("/api/job").json()


def wait_state(c, states, timeout=120.0):
    end = time.monotonic() + timeout
    while True:
        s = c.get("/api/job").json()["status"]
        if s["state"] in states:
            return s
        assert time.monotonic() < end, f"timed out, status {s}"
        time.sleep(0.05)


# ---------------------------------------------------------------- banks

def test_bank_crud_and_errors(env):
    c = env.client
    assert c.get("/api/banks").json() == []

    r = c.put("/api/banks/back", json={"name": "Back"})
    assert r.status_code == 200 and r.json()["name"] == "Back"
    assert c.put("/api/banks/BAD", json={"name": "x"}).status_code == 400
    assert c.delete("/api/banks/nope").status_code == 404
    assert c.post("/api/banks/nope/generate", json={"origin": [1, 2]}).status_code == 404
    assert c.post("/api/banks/nope/layouts", json={"name": "x"}).status_code == 404

    lanes = [{"width": 8.0, "pitch": 4.0} for _ in range(8)]
    gen = {"origin": [40, 300], "lane_axis": [1, 0], "feed_direction": [0, -1], "sprocket_side": -1,
           "lanes": lanes}
    r = c.post("/api/banks/back/generate", json=gen)
    assert r.status_code == 200, r.text
    bank = r.json()
    assert len(bank["lanes"]) == 8
    lane1 = bank["lanes"][0]
    assert lane1["id"] == "back-1" and lane1["bank"] == "back" and lane1["lane"] == 1
    assert close(lane1["pick"], (40, 300)) and close(lane1["prick"], (36.5, 302))
    assert close(bank["lanes"][7]["pick"], (96, 300))

    # Bad generate specs are refused before anything changes.
    assert c.post("/api/banks/back/generate", json={**gen, "lanes": []}).status_code == 400
    assert c.post("/api/banks/back/generate", json={**gen, "lane_axis": [0, 0]}).status_code == 400
    assert c.post("/api/banks/back/generate", json={**gen, "sprocket_side": 2}).status_code == 400
    many = [{"width": 8.0, "pitch": 4.0} for _ in range(100)]
    assert c.post("/api/banks/back/generate", json={**gen, "lanes": many}).status_code == 400
    assert c.post("/api/banks/back/generate", json={**gen, "origin": [1, 2, 3]}).status_code == 422
    assert len(lanes_of(c, "back")) == 8

    # Load the parts, save the layout, and check the active layout bookkeeping.
    set_lane(c, "back-1", "10k", "R_0805_2012Metric")
    set_lane(c, "back-2", "100n", "C_0402_1005Metric")
    set_lane(c, "back-3", "1u", "C_0805_2012Metric")
    set_lane(c, "back-4", "BC847", "SOT-23")
    assert c.post("/api/banks/back/layouts", json={"name": "  "}).status_code == 400
    r = c.post("/api/banks/back/layouts", json={"name": "Passives"})
    assert r.status_code == 200 and r.json()["layouts"] == ["Passives"]
    assert r.json()["active_layout"] == "Passives"

    # Generating again keeps loaded parts (same lane numbers) and clears the active layout.
    r = c.post("/api/banks/back/generate", json={**gen, "gap": 1.0})
    assert r.status_code == 200
    assert r.json()["active_layout"] is None
    assert r.json()["lanes"][0]["value"] == "10k"
    assert r.json()["lanes"][0]["pick"] == [40.0, 300.0]

    r = c.post("/api/banks/back/layouts/Passives/load")
    assert r.status_code == 200 and r.json()["active_layout"] == "Passives"
    assert c.post("/api/banks/back/layouts/Nope/load").status_code == 404
    assert c.delete("/api/banks/back/layouts/Nope").status_code == 404

    # Front stack: one IC lane, saved as "ICs".
    r = c.put("/api/banks/front", json={"name": "Front"})
    assert r.status_code == 200
    gen_front = {"origin": [40, 20], "lane_axis": [1, 0], "feed_direction": [0, 1], "sprocket_side": 1,
                 "lanes": [{"width": 12.0, "pitch": 8.0} for _ in range(4)]}
    assert c.post("/api/banks/front/generate", json=gen_front).status_code == 200
    set_lane(c, "front-1", "NE555", "SOIC-8_3.9x4.9mm_P1.27mm")
    assert c.post("/api/banks/front/layouts", json={"name": "ICs"}).status_code == 200

    names = {b["id"]: b for b in c.get("/api/banks").json()}
    assert set(names) == {"back", "front"}
    assert names["back"]["layouts"] == ["Passives"] and names["front"]["layouts"] == ["ICs"]
    assert c.delete("/api/banks/back/layouts/Passives").status_code == 200
    assert c.get("/api/banks").json()[0]["layouts"] == []
    assert c.post("/api/banks/back/layouts/Passives/load").status_code == 404
    # Put the layout back for the job tests.
    assert c.post("/api/banks/back/layouts", json={"name": "Passives"}).status_code == 200

    # Deleting a bank removes its lanes too; then it is recreated for the job.
    assert c.delete("/api/banks/front").status_code == 200
    assert lanes_of(c, "front") == []
    assert c.post("/api/banks/front/layouts/ICs/load").status_code == 404
    assert c.put("/api/banks/front", json={"name": "Front"}).status_code == 200
    assert c.post("/api/banks/front/generate", json=gen_front).status_code == 200
    set_lane(c, "front-1", "NE555", "SOIC-8_3.9x4.9mm_P1.27mm")


# ---------------------------------------------------------------- package rules

def test_package_rules(env):
    c = env.client
    original = c.get("/api/package-rules").json()
    assert original["default_nozzle"] == 1
    assert len(original["rules"]) == len(DEFAULT_RULES)
    assert original["rules"][0]["pattern"] == "*0201*" and original["rules"][0]["nozzle"] == 2

    assert c.put("/api/package-rules", json={"default_nozzle": 3, "rules": []}).status_code == 400
    bad_nozzle = {"default_nozzle": 1, "rules": [{"pattern": "*0805*", "nozzle": 3}]}
    assert c.put("/api/package-rules", json=bad_nozzle).status_code == 400
    empty = {"default_nozzle": 1, "rules": [{"pattern": " ", "nozzle": 2}]}
    assert c.put("/api/package-rules", json=empty).status_code == 400
    depth = {"default_nozzle": 1, "rules": [{"pattern": "QFN*", "nozzle": 1, "place_depth": -1}]}
    assert c.put("/api/package-rules", json=depth).status_code == 400
    assert c.get("/api/package-rules").json() == original  # nothing changed on a refusal

    new = {"default_nozzle": 2, "rules": [{"pattern": "TSSOP*", "nozzle": 1, "place_depth": 2.5, "note": "tall"}]}
    r = c.put("/api/package-rules", json=new)
    assert r.status_code == 200
    assert r.json()["default_nozzle"] == 2
    assert r.json()["rules"] == [{"pattern": "TSSOP*", "nozzle": 1, "place_depth": 2.5, "note": "tall",
                                  "body": None, "vision": True}]
    assert (env.tmp / "packages.yaml").exists()

    # Put the defaults back for the job tests.
    assert c.put("/api/package-rules", json=original).status_code == 200
    assert c.get("/api/package-rules").json() == original


def test_package_rules_body_and_vision_round_trip(env):
    c = env.client
    original = c.get("/api/package-rules").json()
    edited = {"default_nozzle": 1, "rules": [{"pattern": "*0603*", "nozzle": 2, "body": [0.8, 1.6],
                                              "vision": False}]}
    r = c.put("/api/package-rules", json=edited)
    assert r.status_code == 200, r.text
    assert r.json()["rules"] == [{"pattern": "*0603*", "nozzle": 2, "place_depth": None, "note": "",
                                  "body": [1.6, 0.8], "vision": False}]  # stored as (length, width)
    assert c.get("/api/package-rules").json()["rules"][0]["vision"] is False
    bad_body = {"default_nozzle": 1, "rules": [{"pattern": "*0603*", "nozzle": 2, "body": [1.6]}]}
    assert c.put("/api/package-rules", json=bad_body).status_code == 400
    assert c.put("/api/package-rules", json=original).status_code == 200
    assert c.get("/api/package-rules").json() == original


# ---------------------------------------------------------------- job import and board

def test_import_and_errors(env):
    c = env.client
    assert job_state(c) == {"job": None, "plan": [],
                            "status": {"state": "idle", "current": None, "message": None,
                                       "placed": 0, "failed": 0, "skipped": 0, "pending": 0, "total": 0,
                                       "last_inspection": {"1": None, "2": None}, "step": None,
                                       "holding": {"1": None, "2": None}}}
    assert c.post("/api/job/import", json={"name": "x", "filename": "x.pos", "text": ""}).status_code == 400
    bad = "R1 10k R_0805 abc 4 0 top\n"
    assert c.post("/api/job/import", json={"name": "x", "filename": "x.pos", "text": bad}).status_code == 400
    assert c.post("/api/job/start").status_code == 409  # no job

    r = c.post("/api/job/import", json={"name": "Demo", "filename": "demo.pos", "text": DEMO.read_text()})
    assert r.status_code == 200, r.text
    state = r.json()
    job = state["job"]
    assert job["name"] == "Demo" and job["source"] == "demo.pos" and job["origin"] is None
    assert len(job["parts"]) == 11 and len(job["boards"]) == 1
    assert len(state["plan"]) == 11
    bottom = next(i for i in state["plan"] if i["ref"] == "R4")
    assert bottom["status"] == "skipped" and bottom["problem"] == "Bottom side"  # bottom parts start skipped
    assert state["status"]["total"] == 10 and state["status"]["pending"] == 0


def test_board_panel_and_parts(env):
    c = env.client
    r = c.put("/api/job/board", json={"origin": [1, 2, 3], "rotation": 0})
    assert r.status_code == 400
    r = c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0})
    assert r.status_code == 200
    assert r.json()["job"]["origin"] == [150.0, 150.0]
    r1 = next(i for i in r.json()["plan"] if i["key"] == "0:R1")
    assert close((r1["x"], r1["y"]), (155.0, 154.0)) and r1["problem"] is None and r1["feeder"] == "back-1"

    assert c.put("/api/job/panel", json={"boards": []}).status_code == 400
    assert c.post("/api/job/panel/grid", json={"rows": 0, "cols": 2, "step_x": 50, "step_y": 0}).status_code == 400
    r = c.post("/api/job/panel/grid", json={"rows": 1, "cols": 2, "step_x": 50, "step_y": 0})
    assert r.status_code == 200 and len(r.json()["job"]["boards"]) == 2
    assert len(r.json()["plan"]) == 22

    assert c.put("/api/job/parts/R1", json={"skip": False, "nozzle": 5}).status_code == 400
    assert c.put("/api/job/parts/NOPE", json={"skip": False}).status_code == 404
    r = c.put("/api/job/parts/C2", json={"skip": False, "nozzle": 1})
    c2 = [i for i in r.json()["plan"] if i["key"] == "0:C2"][0]
    assert c2["nozzle"] == 1
    r = c.put("/api/job/parts/C2", json={"skip": True})
    assert [i for i in r.json()["plan"] if i["key"] == "0:C2"][0]["status"] == "skipped"
    r = c.put("/api/job/parts/C2", json={"skip": False})
    c2 = [i for i in r.json()["plan"] if i["key"] == "0:C2"][0]
    assert c2["nozzle"] == 2 and c2["status"] == "pending"  # C_0805 follows the rule again


def test_part_vision_override_round_trip(env):
    c = env.client
    c2 = [i for i in c.get("/api/job").json()["plan"] if i["key"] == "0:C2"][0]
    rule_vision = c2["vision"]
    r = c.put("/api/job/parts/C2", json={"skip": False, "nozzle": None, "feeder": None, "vision": not rule_vision})
    assert r.status_code == 200
    c2 = [i for i in r.json()["plan"] if i["key"] == "0:C2"][0]
    assert c2["vision"] is (not rule_vision)
    r = c.put("/api/job/parts/C2", json={"skip": False, "nozzle": None, "feeder": None, "vision": None})
    assert r.status_code == 200
    c2 = [i for i in r.json()["plan"] if i["key"] == "0:C2"][0]
    assert c2["vision"] is rule_vision
    assert c.put("/api/job/parts/C2", json={"skip": False, "vision": "maybe"}).status_code == 422


# ---------------------------------------------------------------- alignment (needs homing)

def test_align_needs_homing(env):
    c = env.client
    assert c.post("/api/job/align/origin").status_code == 409
    assert c.post("/api/job/align/rotation", json={"ref": "R1", "board": 0}).status_code == 409


def test_align_origin_and_rotation_then_goto(env):
    c = env.client
    assert c.post("/api/home").status_code == 200
    assert c.post("/api/move", json={"x": 150.0, "y": 150.0}).status_code == 200
    r = c.post("/api/job/align/origin")
    assert r.status_code == 200
    assert close(r.json()["job"]["origin"], (150.0, 150.0), tol=0.05)

    # Put the camera on R1 of board 0 with the panel turned by 30 degrees.
    import math
    a = math.radians(30.0)
    px, py = 5.0, 4.0
    wx = 150.0 + math.cos(a) * px - math.sin(a) * py
    wy = 150.0 + math.sin(a) * px + math.cos(a) * py
    assert c.post("/api/move", json={"x": wx, "y": wy}).status_code == 200
    assert c.post("/api/job/align/rotation", json={"ref": "NOPE", "board": 0}).status_code == 404
    assert c.post("/api/job/align/rotation", json={"ref": "R1", "board": 9}).status_code == 400
    r = c.post("/api/job/align/rotation", json={"ref": "R1", "board": 0})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["rotation"] == pytest.approx(30.0, abs=0.5)

    # Goto moves the camera over the placement's world position (rotated panel).
    r = c.post("/api/job/placements/0:R1/goto")
    assert r.status_code == 200, r.text
    pos = r.json()["position"]
    r1 = next(i for i in job_state(c)["plan"] if i["key"] == "0:R1")
    assert close((pos["x"], pos["y"]), (r1["x"], r1["y"]))
    assert c.post("/api/job/placements/0:NOPE/goto").status_code == 404

    # Reset the rotation for the placement tests.
    assert c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0}).status_code == 200


def test_status_override_and_reset(env):
    c = env.client
    assert c.post("/api/job/placements/0:R1/status", json={"status": "bogus"}).status_code == 400
    assert c.post("/api/job/placements/0:NOPE/status", json={"status": "skipped"}).status_code == 404
    r = c.post("/api/job/placements/0:R1/status", json={"status": "skipped"})
    assert r.status_code == 200
    assert next(i for i in r.json()["plan"] if i["key"] == "0:R1")["status"] == "skipped"
    r = c.post("/api/job/placements/0:R1/status", json={"status": "pending"})
    assert next(i for i in r.json()["plan"] if i["key"] == "0:R1")["status"] == "pending"
    r = c.post("/api/job/reset")
    assert r.status_code == 200 and r.json()["job"]["status"] == {}

    # Run controls with nothing to run.
    assert c.post("/api/job/pause").status_code == 409
    assert c.post("/api/job/stop").status_code == 409


# ---------------------------------------------------------------- run to done

def test_run_job_to_done_and_block_feeder_edits_meanwhile(env):
    c = env.client
    # No down camera in this app: the fiducials are switched off so the run places parts directly.
    fids = c.get("/api/job").json()["job"]["fiducials"]
    assert c.put("/api/job/fiducials", json={"mode": "off", "fiducials": fids}).status_code == 200
    assert c.post("/api/job/reset").status_code == 200
    assert c.post("/api/job/start").status_code == 200
    # Twenty placements take seconds, so the job is still running for these requests.
    assert c.get("/api/job").json()["status"]["state"] == "running"
    assert c.put("/api/feeders/back-1", json={"name": "x"}).status_code == 409
    assert c.delete("/api/feeders/back-1").status_code == 409
    assert c.post("/api/feeders/back-1/teach/pick").status_code == 409
    assert c.post("/api/feeders/back-1/feed").status_code == 409
    assert c.post("/api/banks/back/generate", json={"origin": [1, 2]}).status_code == 409
    assert c.put("/api/job/board", json={"origin": [1, 2], "rotation": 0}).status_code == 409
    assert c.delete("/api/job").status_code == 409
    assert c.post("/api/job/start").status_code == 409
    # Manual machine control is locked out too; the job's own pause stays allowed.
    assert c.post("/api/jog", json={"axis": "x", "delta": 1}).status_code == 409
    assert c.post("/api/output", json={"name": "pump", "on": False}).status_code == 409
    assert c.post("/api/home").status_code == 409

    s = wait_state(c, ("done", "paused", "error", "idle"))
    assert s["state"] == "done", s
    assert s["placed"] == 20 and s["total"] == 20 and s["failed"] == 0 and s["pending"] == 0
    state = job_state(c)
    assert len(state["job"]["status"]) == 20
    assert all(v == "placed" for v in state["job"]["status"].values())
    releases = [e for e in env.sim.machine.events if e[0] == "release"]
    assert len(releases) >= 20

    # Edits are allowed again once the run is over.
    assert c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0}).status_code == 200
    assert c.post("/api/job/start").status_code == 409  # nothing left to place


def test_delete_job(env):
    c = env.client
    assert c.delete("/api/job").status_code == 200
    assert job_state(c)["job"] is None
    assert c.get("/api/job").json()["plan"] == []


# ---------------------------------------------------------------- fiducials (own app with the down camera)

def sim_truth(x, y, offset=(0.0, 0.0)):
    """Where a board point (x, y) of the board at panel offset really is, in world mm."""
    a = math.radians(SIM_BOARD_ERROR_ROT)
    px, py = x + offset[0], y + offset[1]
    return (150.0 + SIM_BOARD_ERROR_XY[0] + math.cos(a) * px - math.sin(a) * py,
            150.0 + SIM_BOARD_ERROR_XY[1] + math.sin(a) * px + math.cos(a) * py)


@contextlib.contextmanager
def start_cam_app(tmp_path):
    cfg = MachineConfig()
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.head = FAST
    sim = TvmSimulator(port=0)
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    def pos():
        p = machine.position()
        return (p["x"], p["y"])

    cameras = CameraManager({"down": SimCamera("down", make_down_renderer(pos, sim_demo_boards()))})

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    cal_path = tmp_path / "calibration.yaml"
    cal = Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET)
    cal.set_camera("down", sim_down_A(), 0.0)
    cal.save(cal_path)
    app = create_app(machine, cameras=cameras, calibration_path=cal_path, feeders_path=tmp_path / "feeders.yaml",
                     banks_path=tmp_path / "banks.yaml", rules_path=tmp_path / "packages.yaml",
                     job_path=tmp_path / "job.yaml", extra_startup=startup, extra_shutdown=sim.stop)
    with TestClient(app) as client:
        for _ in range(200):
            s = client.get("/api/state").json()
            if s["connected"] and s["speed_mode"]:
                break
            time.sleep(0.02)
        assert s["connected"]
        yield SimpleNamespace(client=client, machine=machine, app=app)


@pytest.fixture(scope="module")
def cam(tmp_path_factory):
    with start_cam_app(tmp_path_factory.mktemp("fid")) as e:
        yield e


def approx_pos(p, expected, tol=0.05):
    return abs(p["x"] - expected[0]) <= tol and abs(p["y"] - expected[1]) <= tol


def test_fiducial_routes_import_and_edits_clear_the_fit(cam):
    c = cam.client
    assert c.post("/api/job/import", json={"name": "Demo", "filename": "demo.pos",
                                           "text": DEMO.read_text()}).status_code == 200
    job = c.get("/api/job").json()["job"]
    assert job["fiducial_mode"] == "panel" and len(job["fiducials"]) == 3
    assert job["fit"] is None and job["board_fits"] == {}

    assert c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0}).status_code == 200
    assert c.post("/api/job/panel/grid", json={"rows": 1, "cols": 2, "step_x": 50, "step_y": 0}).status_code == 200

    # Refusals before any measurement.
    assert c.post("/api/job/fiducials/measure").status_code == 409  # not homed
    assert c.put("/api/job/fiducials", json={"mode": "bogus", "fiducials": []}).status_code == 400
    assert c.put("/api/job/fiducials", json={"mode": "panel", "diameter": 0,
                                             "fiducials": []}).status_code == 400
    dup = [{"name": "FID1", "x": 2, "y": 2}, {"name": "FID1", "x": 38, "y": 2}]
    assert c.put("/api/job/fiducials", json={"mode": "panel", "fiducials": dup}).status_code == 400
    assert c.post("/api/job/fiducials/FID1/goto", json={"board": 0}).status_code == 409  # not homed
    assert c.post("/api/job/fiducials/clear").status_code == 200

    # Home, then measure: the fit is returned and the origin and rotation are corrected.
    assert c.post("/api/home").status_code == 200
    r = c.post("/api/job/fiducials/measure")
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["fit"]["mode"] == "panel" and len(job["fit"]["points"]) == 6
    assert job["fit"]["rms"] < 0.05
    assert job["origin"][0] == pytest.approx(150.0 + SIM_BOARD_ERROR_XY[0], abs=0.03)
    assert job["origin"][1] == pytest.approx(150.0 + SIM_BOARD_ERROR_XY[1], abs=0.03)
    assert job["rotation"] == pytest.approx(SIM_BOARD_ERROR_ROT, abs=0.03)

    # Any board, panel or fiducial edit clears the measurement.
    assert c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0}).json()["job"]["fit"] is None
    assert c.post("/api/job/fiducials/measure").status_code == 200
    panel = {"boards": [{"x": 0, "y": 0, "rotation": 0, "skip": False},
                        {"x": 50, "y": 0, "rotation": 0, "skip": False}]}
    assert c.put("/api/job/panel", json=panel).json()["job"]["fit"] is None
    assert c.post("/api/job/fiducials/measure").status_code == 200
    grid_state = c.post("/api/job/panel/grid", json={"rows": 1, "cols": 2, "step_x": 50, "step_y": 0})
    assert grid_state.json()["job"]["fit"] is None
    assert c.post("/api/job/fiducials/measure").status_code == 200
    fids = c.get("/api/job").json()["job"]["fiducials"]
    r = c.put("/api/job/fiducials", json={"mode": "panel", "diameter": 1.0, "fiducials": fids})
    assert r.status_code == 200 and r.json()["job"]["fit"] is None
    assert c.post("/api/job/fiducials/measure").status_code == 200
    assert c.post("/api/job/fiducials/clear").json()["job"]["fit"] is None


def test_fiducial_goto_predicts_the_fiducial(cam):
    c = cam.client
    # Measured: the goto lands on the true fiducial, not on the rough estimate.
    assert c.post("/api/job/fiducials/measure").status_code == 200
    r = c.post("/api/job/fiducials/FID1/goto", json={"board": 0})
    assert r.status_code == 200, r.text
    assert approx_pos(r.json()["position"], sim_truth(2.0, 2.0))
    r = c.post("/api/job/fiducials/FID3/goto", json={"board": 1})
    assert approx_pos(r.json()["position"], sim_truth(2.0, 28.0, offset=(50.0, 0.0)))
    assert c.post("/api/job/fiducials/NOPE/goto", json={"board": 0}).status_code == 404
    assert c.post("/api/job/fiducials/FID1/goto", json={"board": 9}).status_code == 400

    # Clearing keeps the origin and rotation; setting the rough origin again gives the unmeasured prediction.
    assert c.post("/api/job/fiducials/clear").status_code == 200
    r = c.post("/api/job/fiducials/FID1/goto", json={"board": 0})
    assert approx_pos(r.json()["position"], sim_truth(2.0, 2.0))
    assert c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0}).status_code == 200
    r = c.post("/api/job/fiducials/FID1/goto", json={"board": 0})
    assert approx_pos(r.json()["position"], (152.0, 152.0))


def test_fiducial_measure_refusals_and_failures(cam):
    c = cam.client
    fids = c.get("/api/job").json()["job"]["fiducials"]

    # Mode off: nothing to measure.
    assert c.put("/api/job/fiducials", json={"mode": "off", "fiducials": fids}).status_code == 200
    assert c.post("/api/job/fiducials/measure").status_code == 409
    assert c.put("/api/job/fiducials", json={"mode": "panel", "fiducials": fids}).status_code == 200

    # No origin: the rough estimate is needed.
    assert c.put("/api/job/board", json={"origin": None, "rotation": 0}).status_code == 200
    assert c.post("/api/job/fiducials/measure").status_code == 409
    assert c.put("/api/job/board", json={"origin": [150, 150], "rotation": 0}).status_code == 200

    # A fiducial with no copper under it: 422, and no fit is kept.
    assert c.post("/api/job/fiducials/measure").status_code == 200
    bad = fids + [{"name": "FIDX", "x": 20.0, "y": 15.0}]
    assert c.put("/api/job/fiducials", json={"mode": "panel", "fiducials": bad}).json()["job"]["fit"] is None
    r = c.post("/api/job/fiducials/measure")
    assert r.status_code == 422
    assert "FIDX" in r.json()["detail"]
    assert c.get("/api/job").json()["job"]["fit"] is None
    assert c.put("/api/job/fiducials", json={"mode": "panel", "fiducials": fids}).status_code == 200


def test_fiducial_board_mode_keys_are_strings(cam):
    c = cam.client
    fids = c.get("/api/job").json()["job"]["fiducials"]
    assert c.put("/api/job/fiducials", json={"mode": "board", "fiducials": fids}).status_code == 200
    r = c.post("/api/job/fiducials/measure")
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["fit"]["mode"] == "board"
    assert sorted(job["board_fits"]) == ["0", "1"]
    assert job["board_fits"]["1"][2] == pytest.approx(SIM_BOARD_ERROR_ROT, abs=0.03)
    assert c.put("/api/job/fiducials", json={"mode": "panel", "fiducials": fids}).status_code == 200



def test_align_two_points_sets_origin_and_rotation(env):
    """Record two features with the camera; origin and rotation follow from them."""
    import math
    c = env.client
    r = c.post("/api/job/import", json={"name": "Demo", "filename": "demo.pos", "text": DEMO.read_text()})
    assert r.status_code == 200, r.text
    assert c.post("/api/home").status_code == 200
    true_origin, true_rot = (147.5, 152.25), 1.2
    a_board, b_board = (5.0, 4.0), (40.0, 30.0)
    rot = math.radians(true_rot)

    def seen(p):  # where the camera finds board point p (board 0 has no panel offset)
        return (true_origin[0] + math.cos(rot) * p[0] - math.sin(rot) * p[1],
                true_origin[1] + math.sin(rot) * p[0] + math.cos(rot) * p[1])

    recorded = []
    for p in (a_board, b_board):  # jog the camera onto each feature, read the head position
        w = seen(p)
        assert c.post("/api/move", json={"x": w[0], "y": w[1]}).status_code == 200
        pos = c.get("/api/state").json()["position"]
        recorded.append((pos["x"], pos["y"]))
    body = {"board": 0,
            "a": {"ref": "R1", "x": a_board[0], "y": a_board[1], "world_x": recorded[0][0], "world_y": recorded[0][1]},
            "b": {"ref": None, "x": b_board[0], "y": b_board[1], "world_x": recorded[1][0], "world_y": recorded[1][1]}}
    r = c.post("/api/job/align/two-points", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["job"]["rotation"] == pytest.approx(true_rot, abs=0.1)  # within the 0.03 mm move steps
    assert close(out["job"]["origin"], true_origin, tol=0.1)
    assert abs(out["alignment"]["distance_error"]) < 0.06
    assert out["job"]["references"] == [{"ref": "R1", "x": 5.0, "y": 4.0}, {"ref": None, "x": 40.0, "y": 30.0}]

    too_close = dict(body, b=dict(body["b"], x=5.5, y=4.0))
    r = c.post("/api/job/align/two-points", json=too_close)
    assert r.status_code == 400 and "apart" in r.json()["detail"]
    assert c.post("/api/job/align/two-points", json=dict(body, board=9)).status_code == 400



def test_disable_enable_and_remove_parts(tmp_path):
    with start_app(tmp_path) as e:
        c = e.client
        assert c.post("/api/job/import", json={"name": "Demo", "filename": "demo.pos",
                                                "text": DEMO.read_text()}).status_code == 200
        assert c.put("/api/job/panel", json={"boards": [{"x": 0, "y": 0}, {"x": 50, "y": 0}]}).status_code == 200

        def parts(s):
            return {p["ref"]: p for p in s["job"]["parts"]}

        def items(s, ref):
            return [i for i in s["plan"] if i["ref"] == ref]

        # disable two parts at once: kept in the job, skipped on every board
        r = c.post("/api/job/parts/enable", json={"refs": ["R1", "C1"], "enabled": False})
        assert r.status_code == 200
        s = r.json()
        assert parts(s)["R1"]["skip"] and parts(s)["C1"]["skip"] and not parts(s)["C2"]["skip"]
        assert [i["status"] for i in items(s, "R1")] == ["skipped", "skipped"]
        # enable again
        s = c.post("/api/job/parts/enable", json={"refs": ["R1"], "enabled": True}).json()
        assert not parts(s)["R1"]["skip"] and parts(s)["C1"]["skip"]
        assert [i["status"] for i in items(s, "R1")] == ["pending", "pending"]
        assert c.post("/api/job/parts/enable", json={"refs": ["NOPE"], "enabled": True}).status_code == 404

        # remove a part: gone from the job, its statuses and reference point ref with it
        job_path = e.tmp / "job.yaml"
        from pnp.job.job import JobStore
        store = JobStore(job_path)
        job = store.load()
        job.status = {"0:R2": "placed", "1:R2": "failed", "0:R1": "placed"}
        job.references = [{"ref": "R2", "x": 5.0, "y": 12.0}, {"ref": None, "x": 0.0, "y": 0.0}]
        store.save(job)
        n = len(parts(job_state(c)))
        r = c.delete("/api/job/parts/R2")
        assert r.status_code == 200
        s = r.json()
        assert "R2" not in parts(s) and len(parts(s)) == n - 1
        assert items(s, "R2") == []
        job = store.load()
        assert job.status == {"0:R1": "placed"}
        assert job.references[0] == {"ref": None, "x": 5.0, "y": 12.0}
        assert c.delete("/api/job/parts/R2").status_code == 404


PAGE_CSV = """Ref,Val,Package,PosX,PosY,Rot,Side
"C3","10uF","C_0805_2012Metric",132.483570,-47.774766,-90.000000,top
"C21","100nF","C_0805_2012Metric",117.000000,-99.599504,-90.000000,top
"C16","8pF","C_0805_2012Metric",108.937500,-109.000000,180.000000,top
"""


def test_two_points_in_other_coordinates_are_refused_and_board_kept(tmp_path):
    """The machine case of 2026-11-16: Point 1 = 0,0 and Point 2 with KiCad's on-screen
    (positive) Y, against a file in page coordinates. Refused; the board isn't changed."""
    with start_app(tmp_path) as e:
        c = e.client
        assert c.post("/api/job/import", json={"name": "page", "filename": "p.csv", "text": PAGE_CSV}).status_code == 200
        assert c.post("/api/home").status_code == 200
        assert c.put("/api/job/board", json={"origin": [-80, 160], "rotation": 0}).status_code == 200

        def two(a, b, wa=(-40.326, -39.655), wb=(26.9, 50.3)):
            return c.post("/api/job/align/two-points", json={
                "board": 0,
                "a": {"ref": None, "x": a[0], "y": a[1], "world_x": wa[0], "world_y": wa[1]},
                "b": {"ref": None, "x": b[0], "y": b[1], "world_x": wb[0], "world_y": wb[1]}})

        r = two((0, 0), (67.056, 90.0938))
        assert r.status_code == 400 and "page origin" in r.json()["detail"]
        # file coordinates, but camera captures that put the board off the bed
        r = two((92.0, -41.747), (150.0, -120.75), wa=(-40.0, -60.0), wb=(18.0, -139.0))
        assert r.status_code == 400 and "outside the machine" in r.json()["detail"]
        assert job_state(c)["job"]["origin"] == [-80.0, 160.0]  # unchanged
        # good: the two board corners where the camera saw them on the bed
        r = two((92.0, -41.747), (150.0, -120.75), wa=(12.0, 118.253), wb=(70.0, 39.25))
        assert r.status_code == 200, r.text
        assert r.json()["job"]["rotation"] == pytest.approx(0.0, abs=0.05)


def test_edit_part_placement_and_nozzle_angle_follows_feeder_offset(tmp_path):
    with start_app(tmp_path) as e:
        c = e.client
        assert c.post("/api/job/import", json={"name": "Demo", "filename": "demo.pos",
                                                "text": DEMO.read_text()}).status_code == 200
        f9 = {"kind": "strip", "pick": [100, 50], "direction": [1, 0], "pitch": 4,
              "value": "10k", "package": "R_0805_2012Metric", "rotation": 0}
        r = c.put("/api/feeders/F9", json=f9)
        assert r.status_code == 200, r.text
        s = job_state(c)
        r1 = next(i for i in s["plan"] if i["ref"] == "R1")
        assert r1["feeder"] == "F9" and r1["nozzle_angle"] == pytest.approx(r1["rotation"])
        # rotation offset -90 on the feeder (stored as +90 in the tape): the nozzle turns 90 deg more clockwise
        part = next(p for p in s["job"]["parts"] if p["ref"] == "R1")
        assert c.put("/api/feeders/F9", json={**f9, "rotation": 90.0}).status_code == 200
        r1 = next(i for i in job_state(c)["plan"] if i["ref"] == "R1")
        assert r1["nozzle_angle"] == pytest.approx(r1["rotation"] - 90.0)

        # edit R1's placement in the file coordinates
        body = {"skip": False, "nozzle": None, "feeder": None, "vision": None, "x": 6.5, "y": -3.25, "rotation": -90}
        r = c.put("/api/job/parts/R1", json=body)
        assert r.status_code == 200, r.text
        part = next(p for p in r.json()["job"]["parts"] if p["ref"] == "R1")
        assert (part["x"], part["y"], part["rotation"]) == (6.5, -3.25, 270.0)
        # fields left out stay as they are
        r = c.put("/api/job/parts/R1", json={"skip": False, "nozzle": None, "feeder": None, "vision": None, "x": 7.0})
        part = next(p for p in r.json()["job"]["parts"] if p["ref"] == "R1")
        assert (part["x"], part["y"], part["rotation"]) == (7.0, -3.25, 270.0)


def test_detection_overlay_endpoint(cam):
    c = cam.client
    r = c.get("/api/vision/detect/down?mark_min=0.3&mark_max=4&method=auto")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["camera"] == "down" and d["width"] > 0 and d["height"] > 0
    zones = [s for s in d["shapes"] if s["kind"] == "zone"]
    assert [z["label"] for z in zones] == ["Ø 0.3 mm", "Ø 4 mm"]
    assert isinstance(d["found"], bool)
    assert c.get("/api/vision/detect/down?mark_min=5&mark_max=3").status_code == 400
    assert c.get("/api/vision/detect/down?method=nope").status_code == 400
    assert c.get("/api/vision/detect/up").status_code == 404  # this app has no up camera
