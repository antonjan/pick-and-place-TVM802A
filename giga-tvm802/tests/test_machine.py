"""Machine layer tests against the simulator."""

import asyncio

import pytest

from pnp.config import MachineConfig
from pnp.machine.machine import (
    EStopped, HomingFailed, LimitHit, Machine, MotionTimeout, NotHomed, SoftLimitError,
)
from pnp.sim.tvm_sim import STEPS_PER_MM, SimConfig, TvmSimulator
from pnp.tvm.protocol import Axis

Q_MM = 1000 / STEPS_PER_MM  # controller resolution: 0.0305 mm
Q_DEG = 1000 / 4444.44  # 0.225 deg


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def make(sim_cfg: SimConfig | None = None, tweak=None):
    sim = TvmSimulator(port=0, cfg=sim_cfg)
    await sim.start()
    cfg = MachineConfig()
    cfg.controller.host, cfg.controller.port = "127.0.0.1", sim.port
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.controller.reconnect_delay = 0.1
    if tweak:
        tweak(cfg)
    m = Machine(cfg)
    m.t.delay_before_polling = 0.02
    await m.start()
    await m.t.wait_connected(5)
    await m.refresh()
    return sim, m


def scenario(sim_cfg=None, tweak=None):
    """Decorator: run an async test body with a fresh simulator + machine."""
    def deco(fn):
        def wrapper():
            async def body():
                sim, m = await make(sim_cfg, tweak)
                try:
                    await fn(sim, m)
                    assert sim.machine.burst_violations == 0
                finally:
                    await m.stop()
                    await sim.stop()
            run(body())
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def phys_mm(sim, axis):
    return sim.machine.axes[axis].phys / STEPS_PER_MM


@scenario()
async def test_xy_absolute_requires_homing(sim, m):
    with pytest.raises(NotHomed):
        await m.move_to(x=10)
    pos = await m.move_to(z=1.0)  # Z / A don't need homing
    assert pos["z"] == pytest.approx(1.0, abs=Q_MM)


@scenario()
async def test_home_sets_registers(sim, m):
    pos = await m.home()
    assert m.homed
    assert pos["x"] == pytest.approx(9_843_000 / STEPS_PER_MM, abs=Q_MM)
    assert pos["y"] == pytest.approx(11_532_000 / STEPS_PER_MM, abs=Q_MM)
    # register == physical position after homing (sim travel equals home register)
    assert phys_mm(sim, Axis.Y) == pytest.approx(351.5, abs=0.5)


@scenario()
async def test_move_returns_only_after_arrival(sim, m):
    await m.home()
    pos = await m.move_to(x=120.5, y=80.25)
    assert pos["x"] == pytest.approx(120.5, abs=Q_MM)
    assert pos["y"] == pytest.approx(80.25, abs=Q_MM)
    assert not sim.machine.is_moving()


@scenario()
async def test_soft_limits(sim, m):
    await m.home()
    with pytest.raises(SoftLimitError):
        await m.move_to(x=299.5)
    with pytest.raises(SoftLimitError):
        await m.move_to(z=50)
    assert m.last_error and "soft limits" in m.last_error


@scenario()
async def test_xy_move_raises_nozzle_first(sim, m):
    await m.home()
    await m.nozzle_down(1, 3.0)
    assert m.position()["z"] == pytest.approx(3.0, abs=Q_MM)
    pos = await m.move_to(x=100, y=100)
    assert pos["z"] == pytest.approx(0, abs=Q_MM)
    await m.nozzle_down(2, 2.0)
    assert m.position()["z"] == pytest.approx(-2.0, abs=Q_MM)


@scenario()
async def test_jog_clamps_when_homed(sim, m):
    await m.home()
    pos = await m.jog("x", 50)  # already at 300 -> clamped to 299
    assert pos["x"] == pytest.approx(299.0, abs=Q_MM)
    pos = await m.jog("x", -10)
    assert pos["x"] == pytest.approx(289.0, abs=Q_MM)


@scenario()
async def test_unhomed_jog_limits(sim, m):
    pos = await m.jog("y", 5)
    assert pos["y"] == pytest.approx(5.0, abs=Q_MM)
    with pytest.raises(NotHomed):
        await m.jog("y", 20)


@scenario()
async def test_rotate_shortest_path(sim, m):
    await m.move_to(a1=350)
    pos = await m.rotate(1, 10)
    assert pos["a1"] == pytest.approx(370, abs=Q_DEG)
    pos = await m.rotate(1, -90)
    assert pos["a1"] == pytest.approx(270, abs=Q_DEG)


@scenario(SimConfig(xy_speed=40 * STEPS_PER_MM))
async def test_estop_interrupts_move(sim, m):
    await m.home()
    task = asyncio.create_task(m.move_to(x=100))
    await asyncio.sleep(0.3)
    await m.estop()
    with pytest.raises(EStopped):
        await task
    await asyncio.sleep(0.05)
    assert not sim.machine.is_moving()
    assert 100 < phys_mm(sim, Axis.X) < 299
    with pytest.raises(EStopped):
        await m.move_to(x=280)
    await m.reset()
    assert (await m.move_to(x=280))["x"] == pytest.approx(280, abs=Q_MM)


@scenario(SimConfig(start_x=9_843_000 - 3 * STEPS_PER_MM))
async def test_limit_switch_aborts_move(sim, m):
    with pytest.raises(LimitHit):
        await m.jog("x", 5)  # unhomed, 3 mm from the right switch
    assert not sim.machine.is_moving()


@scenario(SimConfig(xy_speed=5 * STEPS_PER_MM),
          tweak=lambda c: (setattr(c.motion, "xy_speed", 1000), setattr(c.motion, "min_timeout", 0.3)))
async def test_motion_timeout_stops(sim, m):
    with pytest.raises(MotionTimeout):
        await m.jog("x", 10)
    await asyncio.sleep(0.05)
    assert not sim.machine.is_moving()


@scenario(tweak=lambda c: (setattr(c.homing, "approach_mm", 10), setattr(c.homing, "approach_timeout", 0.5)))
async def test_homing_failure_reported(sim, m):
    with pytest.raises(HomingFailed):
        await m.home()
    assert not m.homed
    assert "limit switch" in m.last_error


@scenario()
async def test_pick_detects_part(sim, m):
    await m.set_output("pump", True)
    await m.set_output("vacuum1", True)
    assert not await m.has_part(1)
    await m.nozzle_down(1, 3.0)
    await m.nozzles_up()
    assert await m.has_part(1)
    await m.set_output("vacuum1", False)
    assert not await m.has_part(1)


@scenario()
async def test_disconnect_clears_homed(sim, m):
    await m.home()
    await sim.drop_clients()
    await asyncio.sleep(0.05)
    assert not m.homed
    await m.t.wait_connected(5)


@scenario()
async def test_busy_rejects_second_move(sim, m):
    await m.home()
    first = asyncio.create_task(m.move_to(x=10))
    await asyncio.sleep(0.05)
    with pytest.raises(Exception, match="busy"):
        await m.move_to(y=10)
    await first


@scenario(SimConfig(xy_speed=300 * STEPS_PER_MM))
async def test_homing_wrong_switch_aborts(sim, m):
    # Pretend Y is wired backwards: the approach ends on the Bottom switch.
    sim.machine.axes[Axis.Y].offset = 0
    real_handle = sim.machine.handle

    def flipped(frame):
        import struct
        from pnp.tvm import protocol as p
        if frame[0] == p.Cmd.MOVE and frame[2] & Axis.Y:
            b = bytearray(frame)
            v = struct.unpack_from("<i", b, 4 + 4 * 4)[0]
            struct.pack_into("<i", b, 4 + 4 * 4, -v)
            frame = bytes(b)
        return real_handle(frame)

    sim.machine.handle = flipped
    with pytest.raises(HomingFailed, match="wrong switch"):
        await m.home()
    assert not m.homed
