"""Job step mode (front-panel Step key / Job tab Step button): one stage per press."""

import asyncio

import pytest

from pnp.job.job import JobPart, grid, plan
from pnp.job.runner import JobError
from pnp.tvm import protocol as p
from pnp.machine.panel import FrontPanel
from test_runner import add_feeders, assert_at, events, job_of, rig, run, tool_xy, until
from pnp.sim.sim_vision import SIM_NOZZLE_OFFSETS

DRAG_PART = [JobPart("C1", "1u", "C_0805_2012Metric", 12.0, 4.0, 90.0)]   # N2 from drag feeder D1
STRIP_PART = [JobPart("R1", "10k", "R_0805_2012Metric", 5.0, 4.0, 0.0)]   # N2 from strip S1


async def press(env):
    await env.runner.step()
    await until(lambda: env.runner._stage_running is None, 30)


def head_xy(env):
    pos = env.machine.position()
    return pos["x"], pos["y"]


def z(env):
    return env.machine.position()["z"]


def test_drag_part_runs_one_stage_per_press(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(DRAG_PART, boards=grid(1, 1, 50.0, 0.0)))
            item = [i for i in plan(env.jobs.load(), env.feeders.all(), env.rules) if i.status == "pending"][0]
            d1 = env.feeders.get("D1")
            assert d1.needs_feed

            # 1: the job starts in step mode and the needle goes over the lane
            await press(env)
            assert env.runner.state == "stepping" and env.runner.busy()
            assert_at(head_xy(env), env.head.head_for("prick", d1.prick))
            assert not env.machine.needle_is_down()
            st = env.runner.status()["step"]
            assert st["next"] == "needle down and feed the lane" and (st["index"], st["count"]) == (2, 8)

            # 2: needle down and drag one pitch; the needle stays down
            await press(env)
            assert env.machine.needle_is_down()
            px, py = env.head.head_for("prick", d1.prick)
            assert_at(head_xy(env), (px + d1.pitch * d1.direction[0], py + d1.pitch * d1.direction[1]))

            # 3: needle up, the lane is fed
            await press(env)
            assert not env.machine.needle_is_down()
            assert not env.feeders.get("D1").needs_feed

            # 4: nozzle over the part, still up
            await press(env)
            assert_at(head_xy(env), env.head.head_for(item.nozzle, d1.pick_location()))
            assert abs(z(env)) < 0.05

            # 5: nozzle down onto the part
            await press(env)
            assert abs(z(env)) == pytest.approx(d1.pick_depth, abs=0.05)

            # 6: nozzle up with the part
            await press(env)
            assert abs(z(env)) < 0.05
            assert env.head.holding[item.nozzle] == "D1"
            assert not events(env.sim, "release")

            # 7: over the place, nothing released yet
            await press(env)
            assert_at(head_xy(env), env.head.head_for(item.nozzle, (item.x, item.y)))
            assert not events(env.sim, "release")

            # 8: placed; the job is done
            await press(env)
            rel = events(env.sim, "release")
            assert len(rel) == 1
            assert_at(tool_xy(rel[0][2:4], SIM_NOZZLE_OFFSETS[item.nozzle]), (item.x, item.y))
            assert env.jobs.load().status[item.key] == "placed"
            assert env.head.holding[item.nozzle] is None
            assert env.runner.state == "done" and not env.runner.busy()
    run(body())


def test_strip_part_skips_the_feed_and_next_part_follows(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STRIP_PART))  # two boards: R1 twice
            for _ in range(5):
                await press(env)
            assert len(events(env.sim, "release")) == 1
            assert env.runner.state == "stepping"
            assert env.runner.status()["step"]["next"] == "start the next part"
            await press(env)  # next part, first stage: over the strip pocket
            st = env.runner.status()["step"]
            assert (st["index"], st["count"]) == (2, 5)
            for _ in range(4):
                await press(env)
            assert len(events(env.sim, "release")) == 2
            assert env.runner.state == "done"
    run(body())


def test_press_while_a_stage_runs_is_refused(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STRIP_PART))
            await env.runner.step()
            with pytest.raises(JobError, match="still busy"):
                await env.runner.step()
            await until(lambda: env.runner._stage_running is None, 30)
            with pytest.raises(JobError, match="stop it before stepping"):
                env.runner.state = "running"
                await env.runner.step()
            env.runner.state = "stepping"
    run(body())


def test_stop_with_the_needle_down_lifts_it(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(DRAG_PART, boards=grid(1, 1, 50.0, 0.0)))
            await press(env)
            await press(env)
            assert env.machine.needle_is_down()
            env.runner.stop()
            await until(lambda: env.runner.state == "idle", 10)
            assert not env.machine.needle_is_down()
            assert env.runner.message is None
            # the part is still pending; Step starts again from its first stage
            await press(env)
            assert env.runner.status()["step"]["index"] == 2
    run(body())


def test_stop_after_the_pick_turns_the_pump_off_and_says_the_part_dropped(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STRIP_PART))
            await press(env)  # over the pocket (pump on)
            assert env.sim.machine.outputs & p.Output.PUMP_BOTH
            await press(env)  # nozzle down
            env.runner.stop()
            await until(lambda: env.runner.state == "idle", 10)
            assert abs(z(env)) < 0.05
            assert not env.sim.machine.outputs & (p.Output.PUMP_BOTH | p.Output.VACUUM2)
            assert env.head.holding[2] is None
            assert "R1 was released" in env.runner.message
    run(body())


def test_pump_goes_off_when_the_stepped_job_is_done(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STRIP_PART, boards=grid(1, 1, 50.0, 0.0)))
            for _ in range(5):
                await press(env)
            assert env.runner.state == "done"
            await until(lambda: not env.sim.machine.outputs & p.Output.PUMP_BOTH, 5)
    run(body())


def test_panel_step_key_drives_the_stepper(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            add_feeders(env.feeders)
            env.jobs.save(job_of(STRIP_PART))
            FrontPanel(env.machine, runner=env.runner)
            await asyncio.sleep(0.15)
            env.sim.machine.keys.add("step")
            await asyncio.sleep(0.15)
            env.sim.machine.keys.discard("step")
            await until(lambda: env.runner.state == "stepping" and env.runner._stage_running is None, 30)
            assert env.runner.status()["step"]["index"] == 2
            assert env.machine.panel["step"] == 1.0  # Step no longer changes the jog step
            env.sim.machine.keys.add("start")  # Start/Stop ends step mode
            await asyncio.sleep(0.15)
            env.sim.machine.keys.discard("start")
            await until(lambda: env.runner.state == "idle", 10)
    run(body())
