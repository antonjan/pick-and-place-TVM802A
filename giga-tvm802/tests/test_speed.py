"""Speed levels (vendor SucfaceMount WriteSpeed), job / jog speed and per-part speed."""

import struct

import pytest

from pnp.job.job import JobPart, grid
from pnp.machine.panel import FrontPanel
from pnp.tvm import protocol as p
from pnp.tvm.protocol import Axis, Output
from test_machine import scenario
from test_runner import add_feeders, job_of, rig, run, until

# The block this app (and the Node bridge) always sent: the vendor's level 4 at 100 %.
OLD_BLOCK = bytes(
    [0x06, 0x00, 0x04, 0x00]
    + [0x03, 0, 0, 0, 0x03, 0, 0, 0, 0x03, 0, 0, 0, 0x04, 0, 0, 0,
       0x03, 0, 0, 0, 0x04, 0, 0, 0, 0x03, 0, 0, 0, 0x03, 0, 0, 0,
       0x03, 0, 0, 0, 0x04, 0, 0, 0, 0x03, 0, 0, 0, 0x04, 0, 0, 0]
    + [0xA0, 0x0F, 0, 0, 0xB8, 0x0B, 0, 0, 0x88, 0x13, 0, 0,
       0xA0, 0x0F, 0, 0, 0x88, 0x13, 0, 0, 0xA0, 0x0F, 0, 0]
)


def words(frame):
    return [struct.unpack_from("<i", frame, 4 + 4 * i)[0] for i in range(18)]


def test_level_4_at_100_percent_is_the_old_block():
    assert p.encode_speed(4, 100) == OLD_BLOCK == p.DEFAULT_SPEED_SETUP


def test_encode_speed_matches_the_vendor_tables():
    f = p.encode_speed(7, 50)
    assert len(f) == 76 and f[:4] == bytes([0x06, 0, 7, 0])
    w = words(f)
    assert w[:6] == [p.SPEED_ACC[i][7] for i in range(6)]
    assert w[6:12] == [p.SPEED_DEC[i][7] for i in range(6)]
    assert w[12:] == [v // 2 for v in (5000, 7000, 15000, 9000, 15000, 9000)]
    assert p.speed_factor(Axis.X, 7) == pytest.approx(3.0)
    assert p.speed_factor(Axis.Y, 2) == pytest.approx(1500 / 5000)  # Y = slot 4
    assert p.speed_factor(Axis.Z, 2) == pytest.approx(1000 / 3000)  # Z = slot 1
    for bad in ((12, 100), (-1, 100), (4, 0), (4, 101)):
        with pytest.raises(ValueError):
            p.encode_speed(*bad)


def speed_frames(sim):
    return [c for c in sim.machine.commands if c and c[0] == 0x06]


@scenario()
async def test_manual_moves_use_the_jog_level_and_speed_blocks_override_it(sim, m):
    await m.home()
    sim.machine.commands.clear()
    await m.jog("x", -1.0)
    assert speed_frames(sim)[-1] == p.encode_speed(4, 100)  # jog level default 4
    m.cfg.motion.jog_speed_level = 2
    await m.jog("x", -1.0)
    assert speed_frames(sim)[-1][2] == 2
    with m.speed(level=7):
        with m.speed(percent=40):
            assert m.move_speed() == (7, 40)
            await m.jog("x", -1.0)
        assert m.move_speed() == (7, 100)
    assert speed_frames(sim)[-1] == p.encode_speed(7, 40)
    assert m.move_speed() == (2, 100)


@scenario()
async def test_slower_speed_gets_a_longer_time_limit(sim, m):
    await m.home()
    s = await m.refresh()
    target = {Axis.X: s.positions[Axis.X] - m.to_steps(Axis.X, 100.0)}
    base = m._estimate_timeout(target, s)
    with m.speed(level=2):  # X at 0.3x
        slow = m._estimate_timeout(target, s)
    with m.speed(level=9):  # faster: the limit is not shortened
        fast = m._estimate_timeout(target, s)
    extra = base - m.cfg.motion.min_timeout
    assert slow - m.cfg.motion.min_timeout == pytest.approx(extra / 0.3)
    assert fast == pytest.approx(base)


def test_job_moves_use_the_job_level_and_carrying_moves_the_part_speed(tmp_path):
    async def body():
        async with rig(tmp_path) as env:
            env.machine.cfg.motion.job_speed_level = 6
            add_feeders(env.feeders)
            env.jobs.save(job_of([JobPart("R1", "10k", "R_0805_2012Metric", 5.0, 4.0, 0.0, speed=40)],
                                 boards=grid(1, 1, 50.0, 0.0)))
            env.sim.machine.commands.clear()
            await env.runner.start()
            await until(lambda: env.runner.state == "done")
            levels = {(f[2], words(f)[12 + 2]) for f in speed_frames(env.sim)}
            vx6 = p.SPEED_VEL[2][6]
            assert (6, vx6) in levels                 # moves without a part at the job speed
            assert (6, vx6 * 40 // 100) in levels     # moves with the part at 40 %
            assert all(level == 6 for level, _ in levels)
            # manual moves afterwards are back at the jog speed
            env.sim.machine.commands.clear()
            await env.machine.jog("x", -1.0)
            assert speed_frames(env.sim)[-1][2] == env.machine.cfg.motion.jog_speed_level
    run(body())


@scenario()
async def test_jog_mode_drives_the_front_panel_speed_lights(sim, m):
    import asyncio
    FrontPanel(m)
    await asyncio.sleep(0.15)
    sim.machine.keys.add("mode")
    await asyncio.sleep(0.15)
    sim.machine.keys.discard("mode")
    await asyncio.sleep(0.3)
    assert m.panel["mode"] == "con"
    assert sim.machine.outputs & Output.FAST_MODE and not sim.machine.outputs & Output.SLOW_MODE
    await m.set_panel_leds("dot")
    await asyncio.sleep(0.1)
    assert sim.machine.outputs & Output.SLOW_MODE and not sim.machine.outputs & Output.FAST_MODE
