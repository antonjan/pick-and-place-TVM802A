"""Job runner against the simulator: a full run, pause, stop, failed picks, empty lanes and start checks."""

import dataclasses
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from pnp.config import HeadConfig, MachineConfig
from pnp.job.feeders import Feeder, FeederStore
from pnp.job.head_ops import HeadOps
from pnp.job.job import Job, JobPart, JobStore, grid, plan
from pnp.job.rules import PackageRules
from pnp.job.runner import JobError, JobRunner
from pnp.tvm import protocol as p
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import SIM_NOZZLE_OFFSETS, SIM_PRICK_OFFSET
from pnp.sim.tvm_sim import SimConfig, TvmSimulator
from pnp.vision.calibration_store import Calibration

POS_TOL = 0.031  # controller quantum is 0.0305 mm
FAST = HeadConfig(pick_dwell=0.01, place_dwell=0.01, blow_time=0.01, vacuum_settle=0.01, prick_settle=0.01)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 120))


def calibration() -> Calibration:
    return Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET)


async def until(cond, timeout=60.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out waiting for the runner")
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def rig(tmp_path, sim_cfg=None, head_cfg=None):
    sim = TvmSimulator(port=0, cfg=sim_cfg)
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
    feeders = FeederStore(tmp_path / "feeders.yaml")
    head = HeadOps(m, calibration(), head_cfg or FAST, feeders)
    jobs = JobStore(tmp_path / "job.yaml")
    rules = PackageRules(tmp_path / "packages.yaml")
    runner = JobRunner(m, head, feeders, jobs, rules, pick_retries=2)
    env = SimpleNamespace(sim=sim, machine=m, head=head, feeders=feeders, jobs=jobs, rules=rules, runner=runner)
    try:
        yield env
        assert sim.machine.burst_violations == 0
    finally:
        await runner.shutdown()
        await m.stop()
        await sim.stop()


def events(sim, kind):
    return [e for e in sim.machine.events if e[0] == kind]


def tool_xy(event_xy, offset):
    return (event_xy[0] + offset[0], event_xy[1] + offset[1])


def assert_at(actual, expected, tol=POS_TOL):
    assert actual[0] == pytest.approx(expected[0], abs=tol)
    assert actual[1] == pytest.approx(expected[1], abs=tol)


def add_feeders(feeders: FeederStore, *, strip_capacity: int = 10) -> None:
    feeders.upsert(Feeder(id="S1", name="10k", kind="strip", pick=(100.0, 50.0), direction=(1.0, 0.0),
                          pitch=4.0, capacity=strip_capacity, pick_depth=3.0,
                          value="10k", package="R_0805_2012Metric"))
    feeders.upsert(Feeder(id="D1", name="1u", kind="drag", pick=(60.0, 80.0), direction=(1.0, 0.0),
                          pitch=4.0, prick=(56.0, 84.0), pick_depth=3.0,
                          value="1u", package="C_0805_2012Metric"))
    feeders.upsert(Feeder(id="S2", name="NE555", kind="strip", pick=(100.0, 100.0), direction=(1.0, 0.0),
                          pitch=8.0, capacity=10, pick_depth=3.0,
                          value="NE555", package="SOIC-8_3.9x4.9mm_P1.27mm"))


def job_of(parts, boards=None, origin=(150.0, 150.0)) -> Job:
    return Job(name="test", parts=parts, boards=boards or grid(1, 2, 50.0, 0.0), origin=origin)


STANDARD_PARTS = [
    JobPart("R1", "10k", "R_0805_2012Metric", 5.0, 4.0, 0.0),   # N2 from the strip S1
    JobPart("C1", "1u", "C_0805_2012Metric", 12.0, 4.0, 90.0),  # N2 from the drag feeder D1
    JobPart("U1", "NE555", "SOIC-8_3.9x4.9mm_P1.27mm", 20.0, 4.0, 0.0),  # N1 from the strip S2
]


# ---------------------------------------------------------------- full run

def test_full_run_places_every_part_at_the_right_position(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS))
            expected = [i for i in plan(env.jobs.load(), env.feeders.all(), env.rules) if i.status == "pending"]
            assert len(expected) == 6

            await env.runner.start()
            assert env.runner.busy()
            await until(lambda: env.runner.state == "done")

            status = env.runner.status()
            assert status["state"] == "done"
            assert (status["placed"], status["pending"], status["failed"], status["total"]) == (6, 0, 0, 6)
            assert status["message"] is None and status["current"] is None
            assert all(v == "placed" for v in env.jobs.load().status.values())
            assert env.feeders.get("S1").count == 2 and env.feeders.get("S2").count == 2
            assert env.feeders.get("D1").count == 2

            releases = events(env.sim, "release")
            assert len(releases) == 6
            for item, rel in zip(expected, releases):
                assert rel[1] == item.nozzle
                assert_at(tool_xy(rel[2:4], SIM_NOZZLE_OFFSETS[item.nozzle]), (item.x, item.y))
            assert not env.runner.busy()
    run(body())


def test_pause_after_first_part_then_resume_finishes(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS))
            await env.runner.start()
            await until(lambda: env.runner.status()["placed"] >= 1)
            env.runner.pause()
            assert env.runner.state in ("pausing", "paused")
            await until(lambda: env.runner.state == "paused")
            placed = env.runner.status()["placed"]
            assert 1 <= placed < 6
            assert env.runner.status()["pending"] == 6 - placed
            assert env.runner.busy() is False

            await env.runner.resume()
            await until(lambda: env.runner.state == "done")
            assert env.runner.status()["placed"] == 6
    run(body())


def test_pause_only_while_running(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            with pytest.raises(JobError):
                env.runner.pause()
    run(body())


def test_stop_leaves_remaining_parts_pending(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS))
            await env.runner.start()
            await until(lambda: env.runner.status()["placed"] >= 1)
            env.runner.stop()
            await until(lambda: env.runner.state == "idle")
            status = env.runner.status()
            assert status["state"] == "idle"
            assert 1 <= status["placed"] < 6
            assert status["placed"] + status["pending"] == 6
            assert not env.runner.busy()
            assert env.machine.homed
            await until(lambda: not env.sim.machine.outputs & p.Output.PUMP_BOTH, 5)  # pump off on stop
    run(body())


def test_pump_goes_off_when_the_job_is_done_or_stopped_while_paused(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS[:1], boards=grid(1, 1, 50.0, 0.0)))
            await env.runner.start()
            await until(lambda: env.runner.state == "done")
            await until(lambda: not env.sim.machine.outputs & p.Output.PUMP_BOTH, 5)
            # paused -> Stop: idle at once, pump off
            await env.machine.set_output("pump", True)
            env.runner.state = "paused"
            env.runner.stop()
            assert env.runner.state == "idle"
            await until(lambda: not env.sim.machine.outputs & p.Output.PUMP_BOTH, 5)
    run(body())


def test_failed_picks_mark_the_part_and_pause(tmp_path):
    async def body():
        async with rig(tmp_path, sim_cfg=SimConfig(pick_success=False),
                       head_cfg=dataclasses.replace(FAST, check_vacuum=True)) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS, boards=grid(1, 1, 0, 0)))
            await env.runner.start()
            await until(lambda: env.runner.state == "paused")
            status = env.runner.status()
            assert status["failed"] == 1 and status["placed"] == 0
            assert "Couldn't pick U1 from S2 after 3 tries" in status["message"]
            assert env.jobs.load().status == {"0:U1": "failed"}
            assert env.feeders.get("S2").count == 0
            assert not events(env.sim, "release")
            assert env.head.holding == {1: None, 2: None}
    run(body())


def test_empty_strip_pauses_without_marking(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders, strip_capacity=1)
            parts = [JobPart("R1", "10k", "R_0805_2012Metric", 5.0, 4.0, 0.0)]
            env.jobs.save(job_of(parts))  # two boards, one pocket on the strip
            await env.runner.start()
            await until(lambda: env.runner.state == "paused")
            status = env.runner.status()
            assert status["placed"] == 1 and status["pending"] == 0
            assert status["failed"] == 0
            assert "1:R1" not in env.jobs.load().status
            assert "empty" in status["message"]
            assert env.feeders.get("S1").remaining() == 0
    run(body())


def test_start_refusals(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            with pytest.raises(JobError, match="no job"):
                await env.runner.start()

            env.jobs.save(job_of(STANDARD_PARTS, origin=None))
            with pytest.raises(JobError, match="origin"):
                await env.runner.start()

            env.jobs.save(job_of(STANDARD_PARTS))
            env.machine.homed = False
            with pytest.raises(JobError, match="homed"):
                await env.runner.start()
            env.machine.homed = True

            assert env.runner.state == "idle"
            assert env.runner.status()["total"] == 6
    run(body())


def test_start_refused_when_first_part_has_no_feeder(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of([JobPart("X1", "22u", "C_1206", 5.0, 4.0, 0.0)]))
            with pytest.raises(JobError, match="X1 can't be placed"):
                await env.runner.start()
            assert env.runner.state == "idle"
    run(body())


def test_runner_is_not_busy_after_error_and_can_resume(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS, boards=grid(1, 1, 0, 0)))
            await env.runner.start()
            env.machine.homed = False  # the loop checks homing before each part; the task has not run yet
            await until(lambda: env.runner.state == "error")
            assert "isn't homed" in env.runner.message
            assert not env.runner.busy()
            env.machine.homed = True
            env.runner.stop()  # error -> idle
            assert env.runner.state == "idle"
            await env.runner.start()
            await until(lambda: env.runner.state == "done")
            assert env.runner.status()["placed"] == 3
    run(body())



def test_job_waits_on_stuck_needle_and_continues_after_clear(tmp_path):
    """Needle stuck after a drag mid-job: alarm, job waits; after Clear it finishes."""
    async def body():
        async with rig(tmp_path, sim_cfg=SimConfig(needle_stuck="down")) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STANDARD_PARTS, boards=grid(1, 1, 0, 0)))
            await env.runner.start()
            await until(lambda: env.machine.alarm is not None)
            assert env.runner.busy()  # still running, waiting on the needle
            env.sim.machine.cfg.needle_stuck = None  # freed by hand
            await asyncio.sleep(0.2)
            env.machine.alarm_clear()
            await until(lambda: env.runner.state == "done")
            status = env.runner.status()
            assert status["placed"] == 3 and status["failed"] == 0
    run(body())
