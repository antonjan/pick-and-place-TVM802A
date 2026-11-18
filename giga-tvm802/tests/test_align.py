"""Fiducial measurement against the simulator: pnp/job/align.py and the runner's alignment step (phase 5)."""

import asyncio
import contextlib
import copy
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from pnp.api.__main__ import sim_demo_boards
from pnp.config import HeadConfig, MachineConfig
from pnp.job.align import AlignError, measure_fiducials
from pnp.job.feeders import Feeder, FeederStore
from pnp.job.head_ops import HeadOps
from pnp.job.job import Fiducial, Job, JobPart, JobStore, grid, job_from_pos, plan, to_world, world_rotation
from pnp.job.kicad import parse_pos
from pnp.job.rules import PackageRules
from pnp.job.runner import JobRunner
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
from pnp.vision.calibration_store import Calibration
from pnp.vision.camera import SimCamera

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo.pos"
FAST = HeadConfig(pick_dwell=0.01, place_dwell=0.01, blow_time=0.01, vacuum_settle=0.01, prick_settle=0.01)
TRUE_ORIGIN = (150.0 + SIM_BOARD_ERROR_XY[0], 150.0 + SIM_BOARD_ERROR_XY[1])
TOL = 0.03  # mm and deg, the accuracy the phase 5 design asks for


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 240))


def demo_job(boards: int = 2) -> Job:
    job = job_from_pos("Demo", "demo.pos", parse_pos(DEMO.read_text(encoding="utf-8")))
    job.boards = grid(1, boards, 50.0, 0.0)
    job.origin = (150.0, 150.0)
    return job


def truth_job(job: Job) -> Job:
    """The job as the simulator's board really is: the true origin and the 0.4 degree error."""
    t = copy.deepcopy(job)
    t.origin = TRUE_ORIGIN
    t.rotation = SIM_BOARD_ERROR_ROT
    t.fit = None
    t.board_fits = {}
    return t


def events(sim, kind):
    return [e for e in sim.machine.events if e[0] == kind]


def tool_xy(event_xy, offset):
    return (event_xy[0] + offset[0], event_xy[1] + offset[1])


def assert_near(actual, expected, tol=TOL):
    assert actual[0] == pytest.approx(expected[0], abs=tol)
    assert actual[1] == pytest.approx(expected[1], abs=tol)


@contextlib.asynccontextmanager
async def rig(tmp_path):
    """A homed machine, the simulator, and a started down camera that renders the demo board."""
    sim = TvmSimulator(port=0)
    await sim.start()
    cfg = MachineConfig()
    cfg.controller.host, cfg.controller.port = "127.0.0.1", sim.port
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.controller.reconnect_delay = 0.1
    m = Machine(cfg)
    m.t.delay_before_polling = 0.02
    await m.start()
    await m.t.wait_connected(5)
    await m.refresh()
    await m.home()

    def pos():
        p = m.position()
        return (p["x"], p["y"])

    camera = SimCamera("down", make_down_renderer(pos, sim_demo_boards()))
    camera.start()
    cal = Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET)
    cal.set_camera("down", sim_down_A(), 0.0)
    feeders = FeederStore(tmp_path / "feeders.yaml")
    head = HeadOps(m, cal, FAST, feeders)
    jobs = JobStore(tmp_path / "job.yaml")
    rules = PackageRules(tmp_path / "packages.yaml")
    env = SimpleNamespace(sim=sim, machine=m, camera=camera, head=head, feeders=feeders, jobs=jobs, rules=rules)
    try:
        yield env
    finally:
        camera.stop()
        await m.stop()
        await sim.stop()


def sim_A():
    return sim_down_A()


# ---------------------------------------------------------------- measure_fiducials

def test_panel_mode_recovers_the_true_board(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            job = demo_job()
            before = copy.deepcopy(job)
            new = await measure_fiducials(job, env.machine, env.camera, sim_A())

            assert job == before  # the input is never touched
            assert new.origin[0] == pytest.approx(TRUE_ORIGIN[0], abs=TOL)
            assert new.origin[1] == pytest.approx(TRUE_ORIGIN[1], abs=TOL)
            assert new.rotation == pytest.approx(SIM_BOARD_ERROR_ROT, abs=TOL)

            fit = new.fit
            assert fit["mode"] == "panel"
            assert fit["rms"] < 0.05
            assert fit["scale"] == pytest.approx(1.0, abs=0.001)
            assert fit["rotation"] == pytest.approx(new.rotation)
            assert fit["origin"] == pytest.approx(list(new.origin))
            assert fit["measured_at"].endswith("+00:00")
            assert new.board_fits == {}

            # First and last board, three fiducials each; the measured points are the true pads.
            pts = fit["points"]
            assert [(p["board"], p["name"]) for p in pts] == [
                (0, "FID1"), (0, "FID2"), (0, "FID3"), (1, "FID1"), (1, "FID2"), (1, "FID3")]
            truth = truth_job(job)
            for p in pts:
                f = next(f for f in job.fiducials if f.name == p["name"])
                assert_near(p["measured"], to_world(truth, p["board"], f.x, f.y))
                assert_near(p["expected"], to_world(job, p["board"], f.x, f.y), tol=1e-9)
                assert 0.15 < p["error"] < 0.6  # the uncorrected manual setting is 0.2 to 0.5 mm off
            mean_error = sum(p["error"] for p in pts) / len(pts)
            assert 0.3 < mean_error < 0.45

            # The head ends over the first fiducial measured.
            pos = env.machine.position()
            assert pos["x"] == pytest.approx(pts[0]["measured"][0], abs=0.05)
            assert pos["y"] == pytest.approx(pts[0]["measured"][1], abs=0.05)
    run(body())


def test_board_mode_recovers_each_board(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            job = demo_job()
            job.fiducial_mode = "board"
            new = await measure_fiducials(job, env.machine, env.camera, sim_A())

            truth = truth_job(job)
            assert sorted(new.board_fits) == [0, 1]
            for b in (0, 1):
                ox, oy, r, s = new.board_fits[b]
                assert_near((ox, oy), to_world(truth, b, 0.0, 0.0))  # board 1: rot(0.4) @ (50, 0) + origin
                assert r == pytest.approx(SIM_BOARD_ERROR_ROT, abs=TOL)
                assert s == pytest.approx(1.0, abs=1e-3)  # the simulated boards have the design size
            assert new.fit["mode"] == "board" and len(new.fit["points"]) == 6

            # A part on board 1 lands where the truth puts it.
            assert_near(to_world(new, 1, 5.0, 4.0), to_world(truth, 1, 5.0, 4.0))
            assert world_rotation(new, 1, 0.0) == pytest.approx(SIM_BOARD_ERROR_ROT, abs=TOL)

            # The job origin and rotation reproduce board 0's fit through the panel formula.
            plain = copy.deepcopy(new)
            plain.board_fits = {}
            assert new.rotation == pytest.approx(SIM_BOARD_ERROR_ROT, abs=TOL)
            assert new.origin[0] == pytest.approx(TRUE_ORIGIN[0], abs=TOL)
            assert new.origin[1] == pytest.approx(TRUE_ORIGIN[1], abs=TOL)
            for x, y in ((0.0, 0.0), (5.0, 4.0), (38.0, 28.0)):
                assert to_world(plain, 0, x, y) == pytest.approx(to_world(new, 0, x, y), abs=1e-6)
    run(body())


def test_board_mode_skips_skipped_boards(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            job = demo_job()
            job.fiducial_mode = "board"
            job.boards[0].skip = True
            new = await measure_fiducials(job, env.machine, env.camera, sim_A())
            assert sorted(new.board_fits) == [1]
            assert {p["board"] for p in new.fit["points"]} == {1}
            assert new.rotation == pytest.approx(SIM_BOARD_ERROR_ROT, abs=TOL)
    run(body())


def test_missing_fiducial_raises_and_leaves_the_job_unchanged(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            job = demo_job()
            job.fiducials = [Fiducial("FID1", 2.0, 2.0), Fiducial("FIDX", 20.0, 15.0)]  # no copper at FIDX
            before = copy.deepcopy(job)
            with pytest.raises(AlignError, match="Couldn't find FIDX on board 1"):
                await measure_fiducials(job, env.machine, env.camera, sim_A())
            assert job == before
    run(body())


def test_checks_refuse_before_measuring(tmp_path):
    async def body():
        job = demo_job()
        with pytest.raises(AlignError, match="homed"):
            await measure_fiducials(job, SimpleNamespace(homed=False), None, sim_A())
        homed = SimpleNamespace(homed=True)
        no_origin = demo_job()
        no_origin.origin = None
        with pytest.raises(AlignError, match="origin"):
            await measure_fiducials(no_origin, homed, None, sim_A())
        off = demo_job()
        off.fiducial_mode = "off"
        with pytest.raises(AlignError, match="Fiducial mode is off"):
            await measure_fiducials(off, homed, None, sim_A())
        none = demo_job()
        none.fiducials = []
        with pytest.raises(AlignError, match="no fiducials"):
            await measure_fiducials(none, homed, None, sim_A())
        skipped = demo_job()
        for b in skipped.boards:
            b.skip = True
        with pytest.raises(AlignError, match="skipped"):
            await measure_fiducials(skipped, homed, None, sim_A())
    run(body())


# ---------------------------------------------------------------- runner

DEMO_FEEDERS = [
    # value, package, pick point (strip feeders, one per part type)
    ("10k", "R_0805_2012Metric", (100.0, 50.0)),
    ("100n", "C_0402_1005Metric", (100.0, 70.0)),
    ("1u", "C_0805_2012Metric", (100.0, 90.0)),
    ("BC847", "SOT-23", (100.0, 110.0)),
    ("NE555", "SOIC-8_3.9x4.9mm_P1.27mm", (100.0, 130.0)),
]


def add_demo_feeders(feeders: FeederStore) -> None:
    for i, (value, package, pick) in enumerate(DEMO_FEEDERS, 1):
        feeders.upsert(Feeder(id=f"S{i}", name=value, kind="strip", pick=pick, direction=(1.0, 0.0),
                              pitch=8.0, capacity=10, pick_depth=3.0, value=value, package=package))


async def until(cond, timeout=200.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out waiting for the runner")
        await asyncio.sleep(0.01)


def make_runner(env, align):
    return JobRunner(env.machine, env.head, env.feeders, env.jobs, env.rules, pick_retries=2, align=align)


def aligner(env):
    async def align():
        env.jobs.save(await measure_fiducials(env.jobs.load(), env.machine, env.camera, sim_A()))
    return align


def test_runner_aligns_then_places_on_the_true_positions(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_demo_feeders(env.feeders)
            job = demo_job()
            env.jobs.save(job)
            order = [i for i in plan(job, env.feeders.all(), env.rules) if i.status == "pending"]
            assert len(order) == 20 and all(i.problem is None for i in order)
            runner = make_runner(env, aligner(env))

            await runner.start()
            await until(lambda: runner.state in ("done", "error", "paused"))
            assert runner.state == "done", runner.message
            assert runner.status()["placed"] == 20

            saved = env.jobs.load()
            assert saved.fit is not None and saved.fit["mode"] == "panel"
            assert saved.rotation == pytest.approx(SIM_BOARD_ERROR_ROT, abs=TOL)

            truth = truth_job(job)
            releases = events(env.sim, "release")
            assert len(releases) == 20
            worst = 0.0
            for item, rel in zip(order, releases):
                assert rel[1] == item.nozzle
                part = next(p for p in job.parts if p.ref == item.ref)
                tx, ty = to_world(truth, item.board, part.x, part.y)
                tool = tool_xy(rel[2:4], SIM_NOZZLE_OFFSETS[item.nozzle])
                worst = max(worst, math.hypot(tool[0] - tx, tool[1] - ty))
            assert worst < 0.06
    run(body())


def test_without_fiducials_the_placements_are_off(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_demo_feeders(env.feeders)
            job = demo_job()
            job.fiducial_mode = "off"
            env.jobs.save(job)
            order = [i for i in plan(job, env.feeders.all(), env.rules) if i.status == "pending"]
            runner = make_runner(env, aligner(env))  # mode off: the align step must not run

            await runner.start()
            await until(lambda: runner.state in ("done", "error", "paused"))
            assert runner.state == "done", runner.message
            assert env.jobs.load().fit is None

            truth = truth_job(job)
            releases = events(env.sim, "release")
            errors = []
            for item, rel in zip(order, releases):
                part = next(p for p in job.parts if p.ref == item.ref)
                tx, ty = to_world(truth, item.board, part.x, part.y)
                tool = tool_xy(rel[2:4], SIM_NOZZLE_OFFSETS[item.nozzle])
                errors.append(math.hypot(tool[0] - tx, tool[1] - ty))
            assert max(errors) > 0.2
    run(body())


def test_runner_stops_in_error_when_alignment_fails(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_demo_feeders(env.feeders)
            job = demo_job(boards=1)
            job.parts = [p for p in job.parts if p.ref == "R1"]
            env.jobs.save(job)

            async def align():
                raise AlignError("no fiducial here")

            runner = make_runner(env, align)
            await runner.start()
            await until(lambda: runner.state != "running")
            status = runner.status()
            assert status["state"] == "error"
            assert status["message"] == "no fiducial here"
            assert status["current"] is None
            assert status["placed"] == 0 and not events(env.sim, "release")
            assert env.jobs.load().status == {}
    run(body())


def test_runner_passes_a_fitted_job_straight_to_placing(tmp_path):
    """A job that already has a fit is not measured again when it starts."""
    async def body():
        async with rig(tmp_path) as env:
            add_demo_feeders(env.feeders)
            job = demo_job(boards=1)
            job.parts = [p for p in job.parts if p.ref == "R1"]
            job.fit = {"mode": "panel"}
            env.jobs.save(job)
            calls = []

            async def align():
                calls.append(1)

            runner = make_runner(env, align)
            await runner.start()
            await until(lambda: runner.state != "running")
            assert runner.state == "done", runner.message
            assert calls == []
    run(body())
