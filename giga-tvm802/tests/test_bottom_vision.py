"""Bottom vision against the simulator (phase 6): placement correction, rejects, no part, runner and API.

The simulator's SimPartPhysics gives every picked part a random offset and angle error on the
nozzle and records where it really lands, so the corrected placements can be checked against the
targets. The up camera renders the nozzle tips and the held parts (pnp/sim/sim_vision.py).
"""

import asyncio
import contextlib
import dataclasses
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pnp.api.app import create_app
from pnp.config import HeadConfig, MachineConfig
from pnp.job.feeders import Feeder, FeederStore
from pnp.job.head_ops import HeadOps, PickError
from pnp.job.job import Job, JobPart, JobStore, grid, plan
from pnp.job.rules import PackageRules
from pnp.job.runner import JobRunner
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import (
    SIM_NOZZLE_OFFSETS,
    SIM_PRICK_OFFSET,
    SIM_UP_CAMERA_XY,
    SimPartPhysics,
    make_up_renderer,
    sim_up_A,
)
from pnp.sim.tvm_sim import TvmSimulator
from pnp.vision.bottom import inspect_nozzle
from pnp.vision.camera import CameraManager, SimCamera
from pnp.vision.calibration_store import Calibration

POS_TOL = 0.05  # mm, the placement accuracy the phase 6 design asks for
ANGLE_TOL = 0.5  # deg
DISCARD = (20.0, 20.0)
FAST = dict(pick_dwell=0.01, place_dwell=0.01, blow_time=0.01, vacuum_settle=0.01, prick_settle=0.01)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 240))


def calibration() -> Calibration:
    up = {n: (SIM_UP_CAMERA_XY[0] - t[0], SIM_UP_CAMERA_XY[1] - t[1]) for n, t in SIM_NOZZLE_OFFSETS.items()}
    cal = Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET, up_position=up)
    cal.set_camera("up", sim_up_A(), 0.0)
    return cal


def mod180_diff(a: float, b: float) -> float:
    """Signed difference of two line angles (deg), modulo 180."""
    return (a - b + 90.0) % 180.0 - 90.0


def near(a, b, tol=POS_TOL) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def strip_s1(**kw) -> Feeder:
    base = dict(id="S1", kind="strip", pick=(100.0, 50.0), direction=(1.0, 0.0), pitch=4.0,
                capacity=50, pick_depth=3.0, value="10k", package="R_0805_2012Metric")
    base.update(kw)
    return Feeder(**base)


@contextlib.asynccontextmanager
async def rig(tmp_path, part_for_pick=None, *, vision: bool = True):
    """A homed machine, the simulator, the up camera with physics, and the head with the inspector."""
    sim = TvmSimulator(port=0)
    await sim.start()
    cfg = MachineConfig()
    cfg.controller.host, cfg.controller.port = "127.0.0.1", sim.port
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.controller.reconnect_delay = 0.1
    cfg.vision.settle = 0.05
    cfg.vision.bottom_vision = vision
    m = Machine(cfg)
    m.t.delay_before_polling = 0.02
    await m.start()
    await m.t.wait_connected(5)
    await m.refresh()
    await m.home()

    def pos():
        p = m.position()
        return (p["x"], p["y"])

    def angles():
        return {"a1": sim.machine.nozzle_angle(1), "a2": sim.machine.nozzle_angle(2)}

    physics = SimPartPhysics(sim.machine, part_for_pick)
    camera = SimCamera("up", make_up_renderer(pos, physics, angles))
    camera.start()
    cal = calibration()

    async def inspector(nozzle):
        return await inspect_nozzle(m, camera, cal.A("up"), nozzle, cfg.vision)

    feeders = FeederStore(tmp_path / "feeders.yaml")
    head = HeadOps(m, cal, HeadConfig(discard=DISCARD, **FAST), feeders, inspector=inspector)
    jobs = JobStore(tmp_path / "job.yaml")
    rules = PackageRules(tmp_path / "packages.yaml")
    runner = JobRunner(m, head, feeders, jobs, rules, pick_retries=2)
    env = SimpleNamespace(sim=sim, machine=m, camera=camera, physics=physics, head=head, feeders=feeders,
                          jobs=jobs, rules=rules, runner=runner)
    try:
        yield env
        assert sim.machine.burst_violations == 0
    finally:
        camera.stop()
        await runner.shutdown()
        await m.stop()
        await sim.stop()


async def until(cond, timeout=120.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out waiting for the runner")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- placement correction

TARGETS = [((120.0, 80.0), 0.0), ((130.0, 90.0), 90.0), ((140.0, 100.0), 37.0)]


def test_vision_puts_the_part_on_target_and_turned(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            env.feeders.upsert(strip_s1())
            feeder = env.feeders.get("S1")
            for (W, rotation) in TARGETS:
                await env.head.pick_and_place(2, feeder, W, rotation, body=(2.0, 1.25))
                placed = env.physics.placed[-1]
                assert near(placed["centre"], W), (placed["centre"], W)
                assert abs(mod180_diff(placed["angle"], rotation)) <= ANGLE_TOL, (placed["angle"], rotation)
                assert env.head.last_inspection[2].result == "ok"
    run(body())


def test_without_vision_the_part_lands_off_target(tmp_path):
    async def body():
        async with rig(tmp_path, vision=False) as env:
            env.feeders.upsert(strip_s1())
            feeder = env.feeders.get("S1")
            off = []
            for W, rotation in TARGETS:
                await env.head.pick_and_place(2, feeder, W, rotation, vision=False)
                placed = env.physics.placed[-1]
                off.append(max(abs(placed["centre"][0] - W[0]), abs(placed["centre"][1] - W[1])))
                assert env.head.last_inspection[2].result == "skipped"
            assert max(off) > 0.1, off
    run(body())


def test_skip_reason_names_a_part_level_vision_off(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            assert env.head._skip_reason(2, False) == "vision off for this part"
    run(body())


def test_bottom_vision_switched_off_in_config_skips(tmp_path):
    async def body():
        async with rig(tmp_path, vision=False) as env:
            env.feeders.upsert(strip_s1())
            await env.head.pick(2, env.feeders.get("S1"))
            insp = env.head.last_inspection[2]
            assert insp.result == "skipped" and "off" in insp.message
            assert env.head.holding[2] == "S1"
    run(body())


# ---------------------------------------------------------------- rejects

def test_wrong_size_part_is_discarded_and_the_pick_retried(tmp_path):
    async def body():
        async with rig(tmp_path, part_for_pick=lambda n, x, y: ((6.0, 3.75), 0.0)) as env:
            env.feeders.upsert(strip_s1())
            feeder = env.feeders.get("S1")
            with pytest.raises(PickError, match="expected 2.00 × 1.25 mm"):
                await env.head.pick_and_place(2, feeder, (120.0, 80.0), 0.0, body=(2.0, 1.25))
            insp = env.head.last_inspection[2]
            assert insp.result == "rejected"
            assert insp.size is not None and insp.size[0] > 5.0
            # The part went into the discard bin: the sim released it at the discard position.
            released = env.physics.placed[-1]
            assert near(released["centre"], DISCARD, tol=0.5)
            assert env.head.holding[2] is None
            assert env.feeders.get("S1").count == 1  # the pick used the strip; the retry is the runner's job
    run(body())


def test_drop_next_means_no_part(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            env.feeders.upsert(strip_s1())
            feeder = env.feeders.get("S1")
            env.physics.drop_next = True
            with pytest.raises(PickError, match="No part on nozzle 2"):
                await env.head.pick(2, feeder)
            assert env.head.last_inspection[2].result == "no part"
            assert env.head.holding[2] is None
            assert env.physics.placed == []  # nothing was placed or dropped on the bed
    run(body())


def test_inspect_an_empty_nozzle_reports_no_part(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            insp = await env.head.inspect(1)
            assert insp.result == "no part"
            assert insp.offset is None and insp.size is None
            assert env.head.last_inspection[1] is insp
            assert env.head.busy is None
    run(body())


def test_inspect_a_held_part_measures_its_offset(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            env.feeders.upsert(strip_s1())
            await env.head.pick(2, env.feeders.get("S1"))
            held = env.physics.held[2]
            insp = await env.head.inspect(2)
            assert insp.result == "ok"
            assert insp.offset[0] == pytest.approx(held.offset[0], abs=0.03)
            assert insp.offset[1] == pytest.approx(held.offset[1], abs=0.03)
            assert insp.size[0] == pytest.approx(2.0, abs=0.1) and insp.size[1] == pytest.approx(1.25, abs=0.1)
            assert env.head.holding[2] == "S1"  # inspecting never releases the part
    run(body())


# ---------------------------------------------------------------- runner

def test_runner_places_every_part_within_tolerance(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            env.feeders.upsert(strip_s1())
            env.jobs.save(Job(name="test", parts=[
                JobPart("R1", "10k", "R_0805_2012Metric", 5.0, 4.0, 0.0),
                JobPart("R2", "10k", "R_0805_2012Metric", 8.0, 4.0, 90.0),
            ], boards=grid(1, 1, 50.0, 0.0), origin=(150.0, 150.0)))
            targets = [i for i in plan(env.jobs.load(), env.feeders.all(), env.rules) if i.status == "pending"]
            assert len(targets) == 2 and all(i.vision and i.body == [2.0, 1.25] for i in targets)

            await env.runner.start()
            await until(lambda: env.runner.state == "done")

            status = env.runner.status()
            assert status["placed"] == 2
            assert status["last_inspection"]["2"]["result"] == "ok"
            assert status["last_inspection"]["1"] is None
            assert len(env.physics.placed) == 2
            for placed, item in zip(env.physics.placed, targets):
                assert near(placed["centre"], (item.x, item.y)), (placed["centre"], item.x, item.y)
                assert abs(mod180_diff(placed["angle"], item.rotation)) <= ANGLE_TOL
    run(body())


def test_runner_status_last_inspection_is_json_safe(tmp_path):
    import json

    async def body():
        async with rig(tmp_path) as env:
            env.feeders.upsert(strip_s1())
            await env.head.pick(2, env.feeders.get("S1"))
            json.dumps(env.runner.status())  # must not raise
    run(body())


# ---------------------------------------------------------------- plan and rules

def test_plan_items_carry_vision_and_body(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    job = Job(name="t", parts=[
        JobPart("U1", "NE555", "SOIC-8_3.9x4.9mm_P1.27mm", 5.0, 4.0, 0.0),
        JobPart("R1", "10k", "R_0805_2012Metric", 8.0, 4.0, 0.0),
        JobPart("X1", "?", "Weird-Package", 9.0, 4.0, 0.0),
    ], boards=grid(1, 1, 50.0, 0.0), origin=(150.0, 150.0))
    items = {i.ref: i for i in plan(job, [], rules)}
    assert items["U1"].body == [6.0, 4.9] and items["U1"].vision is True
    assert items["R1"].body == [2.0, 1.25]
    assert items["X1"].body is None and items["X1"].vision is True


def test_rules_body_and_vision_are_validated(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    with pytest.raises(ValueError):
        rules.set(1, [dataclasses.replace(rules.rules[0], body=(0.0, 1.0))])
    with pytest.raises(ValueError):
        rules.set(1, [dataclasses.replace(rules.rules[0], vision="yes")])
    rules.set(1, [dataclasses.replace(rules.rules[0], body=(1.25, 2.0), vision=False)])
    assert rules.body_for(rules.rules[0].pattern) == (2.0, 1.25)  # length >= width
    assert rules.vision_for(rules.rules[0].pattern) is False
    assert rules.vision_for("no rule matches this") is True
    assert rules.body_for("no rule matches this") is None


# ---------------------------------------------------------------- API

@contextlib.contextmanager
def api_app(tmp_path):
    cfg = MachineConfig()
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.vision.settle = 0.05
    cfg.head = HeadConfig(discard=DISCARD, **FAST)
    sim = TvmSimulator(port=0)
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02
    physics = SimPartPhysics(sim.machine)

    def pos():
        p = machine.position()
        return (p["x"], p["y"])

    def angles():
        return {"a1": sim.machine.nozzle_angle(1), "a2": sim.machine.nozzle_angle(2)}

    cameras = CameraManager({"up": SimCamera("up", make_up_renderer(pos, physics, angles))})

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    cal_path = tmp_path / "calibration.yaml"
    calibration().save(cal_path)
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
        yield SimpleNamespace(client=client, app=app, physics=physics)


def test_api_inspect_flow(tmp_path):
    with api_app(tmp_path) as env:
        c = env.client
        # Refused before homing (soft limits and the up move need a homed machine).
        r = c.post("/api/head/inspect", json={"nozzle": 1})
        assert r.status_code == 409
        assert c.post("/api/home").status_code == 200

        assert c.post("/api/head/inspect", json={"nozzle": 3}).status_code == 400
        head = c.get("/api/head").json()
        assert set(head["last_inspection"]) == {"1", "2"}
        assert head["last_inspection"]["1"] is None

        r = c.post("/api/head/inspect", json={"nozzle": 1})
        assert r.status_code == 200, r.text
        assert r.json()["result"] == "no part"
        assert c.get("/api/head").json()["last_inspection"]["1"]["result"] == "no part"

        c.put("/api/feeders/S1", json={"name": "0805", "kind": "strip", "pick": [100, 50], "direction": [1, 0],
                                       "pitch": 4.0, "capacity": 20, "pick_depth": 3.0,
                                       "value": "10k", "package": "R_0805_2012Metric"})
        r = c.post("/api/feeders/S1/pick", json={"nozzle": 2})
        assert r.status_code == 200, r.text
        r = c.post("/api/head/inspect", json={"nozzle": 2})
        assert r.status_code == 200, r.text
        insp = r.json()
        assert insp["result"] == "ok" and insp["nozzle"] == 2
        assert len(insp["offset"]) == 2 and len(insp["size"]) == 2
        head = c.get("/api/head").json()
        assert head["holding"]["2"] == "S1"
        assert head["last_inspection"]["2"]["result"] == "ok"


def test_api_rules_round_trip_body_and_vision(tmp_path):
    with api_app(tmp_path) as env:
        c = env.client
        before = c.get("/api/package-rules").json()
        rules = before["rules"]
        rules[0]["body"] = [1.25, 2.0]
        rules[0]["vision"] = False
        r = c.put("/api/package-rules", json={"default_nozzle": before["default_nozzle"], "rules": rules})
        assert r.status_code == 200, r.text
        got = c.get("/api/package-rules").json()["rules"][0]
        assert got["body"] == [2.0, 1.25] and got["vision"] is False  # length >= width
        bad = [dict(rules[0], body=[0, 1])]
        assert c.put("/api/package-rules", json={"default_nozzle": 1, "rules": bad}).status_code == 400
        # Old files and new clients: a rule without the keys gets the defaults.
        plain = [{"pattern": "*0603*", "nozzle": 2}]
        c.put("/api/package-rules", json={"default_nozzle": 1, "rules": plain})
        got = c.get("/api/package-rules").json()["rules"][0]
        assert got["body"] is None and got["vision"] is True
        c.put("/api/package-rules", json={"default_nozzle": before["default_nozzle"], "rules": before["rules"]})


def test_part_outline_maps_back_onto_the_part_in_pixels():
    """Detection overlay: the measured rectangle, drawn in camera pixels, sits on the part."""
    import math
    import numpy as np
    from pnp.vision.bottom import PartView, mm_to_px, part_outline_mm
    view = PartView(offset=(0.4, -0.2), angle=30.0, size=(2.0, 1.25), area=2.5)
    corners = part_outline_mm(view)
    assert np.allclose(np.mean(corners, axis=0), view.offset)
    sides = [math.dist(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    assert sorted(round(s, 6) for s in sides) == [1.25, 1.25, 2.0, 2.0]
    A = np.array([[-20.0, 0.5], [0.3, 20.0]])  # px per mm, mirrored in x like the up camera
    px = mm_to_px(corners, A, 640, 480)
    centre = np.mean(px, axis=0)
    assert np.allclose(centre, np.array([320.0, 240.0]) + A @ np.array(view.offset))
