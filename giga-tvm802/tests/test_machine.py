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


# ---------------------------------------------------------------- feeder needle interlock

from pnp.machine.machine import NeedleDown  # noqa: E402
from pnp.tvm.protocol import Output  # noqa: E402


@scenario()
async def test_needle_down_blocks_xy_moves(sim, m):
    await m.home()
    await m.move_to(x=100, y=100)
    await m.set_output("prick", True)
    assert m.needle_is_down() and m.snapshot()["needle_down"]
    with pytest.raises(NeedleDown):
        await m.move_to(x=110)
    with pytest.raises(NeedleDown):
        await m.jog("y", 1.0)
    with pytest.raises(NeedleDown):
        await m.home()
    # Z and rotation don't move the needle across the bed: still allowed
    await m.move_to(z=1.0)
    await m.rotate(1, 45)
    await m.nozzles_up()
    await m.set_output("prick", False)
    pos = await m.move_to(x=110)
    assert pos["x"] == pytest.approx(110, abs=Q_MM)


@scenario()
async def test_needle_moves_allowed_for_feeding(sim, m):
    await m.home()
    await m.move_to(x=100, y=100)
    await m.set_output("prick", True)
    with m.needle_moves_allowed():
        pos = await m.move_to(x=104)
    assert pos["x"] == pytest.approx(104, abs=Q_MM)
    with pytest.raises(NeedleDown):  # the exception ends with the block
        await m.move_to(x=108)
    await m.set_output("prick", False)


@scenario()
async def test_needle_setup_mode_allows_jogging_until_raised(sim, m):
    await m.home()
    await m.move_to(x=100, y=100)
    await m.set_needle_setup(True)
    assert m.needle_setup and m.needle_is_down()
    pos = await m.jog("x", 0.5)
    assert pos["x"] == pytest.approx(100.5, abs=Q_MM)
    await m.set_output("prick", False)  # raising it any way ends setup mode
    assert not m.needle_setup and not m.needle_is_down()
    await m.set_output("prick", True)  # lowered outside Setup: blocked again
    with pytest.raises(NeedleDown):
        await m.jog("x", 0.5)
    await m.set_output("prick", False)


@scenario()
async def test_needle_reported_down_at_connect_blocks(sim, m):
    """Needle left down before the app connected: trust the controller's report."""
    await m.home()
    sim.machine.outputs |= int(Output.PRICK)
    m._needle_cmd = None
    await m.refresh()
    assert m.needle_is_down()
    with pytest.raises(NeedleDown):
        await m.move_to(x=50)


@scenario()
async def test_estop_clears_needle_state(sim, m):
    await m.home()
    await m.set_needle_setup(True)
    await m.estop()
    assert not m.needle_setup
    await asyncio.sleep(0.2)  # the needle takes a moment to rise; the sensor follows it
    await m.refresh()
    assert not m.needle_is_down()
    await m.reset()


@scenario()
async def test_zero_z_sets_current_height_as_zero(sim, m):
    """Seesaw Z has no home switch: after lost steps, re-zero where both tips are level."""
    await m.move_to(z=1.5)  # pretend the tips are level here
    phys_before = sim.machine.axes[Axis.Z].phys
    pos = await m.zero_z()
    assert pos["z"] == pytest.approx(0.0, abs=Q_MM)
    assert sim.machine.axes[Axis.Z].phys == phys_before  # nothing moved
    pos = await m.move_to(z=1.0)  # 1 mm below the NEW zero
    assert pos["z"] == pytest.approx(1.0, abs=Q_MM)
    moved = (sim.machine.axes[Axis.Z].phys - phys_before) / STEPS_PER_MM
    assert moved == pytest.approx(1.0, abs=Q_MM)



# ---------------------------------------------------------------- needle position sensor

from pnp.machine.machine import NeedleError  # noqa: E402


@scenario()
async def test_needle_switch_waits_for_sensor(sim, m):
    await m.set_output("prick", True)
    assert sim.machine.needle_down  # returned only once the sensor reported it
    assert m.snapshot()["needle_sensor"] == "down"
    await m.set_output("prick", False)
    assert not sim.machine.needle_down
    assert m.snapshot()["needle_sensor"] == "up"


@scenario(SimConfig(needle_stuck="up"))
async def test_needle_that_does_not_go_down_is_reported_and_switched_off(sim, m):
    with pytest.raises(NeedleError, match="did not go down"):
        await m.set_output("prick", True)
    assert not sim.machine.outputs & int(Output.PRICK)  # output switched off again
    assert not m.needle_is_down()


async def _until(cond, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.02)


@scenario(SimConfig(needle_stuck="down"))
async def test_needle_stuck_alarm_buzzes_waits_and_continues_after_clear(sim, m):
    await m.home()
    await m.move_to(x=100, y=100)
    await m.set_output("prick", True)
    raising = asyncio.create_task(m.set_output("prick", False))  # waits on the alarm
    await _until(lambda: m.alarm is not None)
    assert m.snapshot()["alarm"]["kind"] == "needle_stuck"
    await _until(lambda: bool(sim.machine.outputs & int(Output.BUZZER)))
    assert not raising.done()
    # while waiting, nothing may move: the sensor says down
    with pytest.raises(NeedleDown, match="sensor"):
        await m.move_to(x=110)
    # silence: buzzer off, still waiting
    await m.alarm_silence()
    assert not sim.machine.outputs & int(Output.BUZZER)
    assert m.alarm["silenced"] and not raising.done()
    # Clear while still stuck: alarm stays and says so
    m.alarm_clear()
    await asyncio.sleep(0.5)
    assert not raising.done() and "still reports DOWN" in m.alarm["message"]
    # freed by hand, then Clear: the waiting action carries on without an error
    sim.machine.cfg.needle_stuck = None
    await asyncio.sleep(0.2)
    m.alarm_clear()
    await asyncio.wait_for(raising, 5)
    assert m.alarm is None and not m.needle_is_down()
    assert not sim.machine.outputs & int(Output.BUZZER)
    assert (await m.move_to(x=110))["x"] == pytest.approx(110, abs=Q_MM)


@scenario(SimConfig(needle_stuck="down"))
async def test_needle_stuck_alarm_abort_raises(sim, m):
    await m.set_output("prick", True)
    raising = asyncio.create_task(m.set_output("prick", False))
    await _until(lambda: m.alarm is not None)
    m.alarm_abort()
    with pytest.raises(NeedleError, match="aborted"):
        await asyncio.wait_for(raising, 5)
    assert m.alarm is None
    assert not sim.machine.outputs & int(Output.BUZZER)


@scenario(SimConfig(needle_stuck="down"))
async def test_needle_stuck_alarm_ends_on_estop(sim, m):
    await m.set_output("prick", True)
    raising = asyncio.create_task(m.set_output("prick", False))
    await _until(lambda: m.alarm is not None)
    await m.estop()
    with pytest.raises(EStopped):
        await asyncio.wait_for(raising, 5)
    assert m.alarm is None
    assert not sim.machine.outputs & int(Output.BUZZER)
    await m.reset()


@scenario(SimConfig(move_ignored_after_stop=0.3))
async def test_homing_survives_moves_ignored_right_after_a_stop(sim, m):
    """Real controller (suspected): right after an axis stops on its limit switch, a
    new move is acknowledged but ignored. Homing used to wait 15 s for the switch to
    release and fail; it must now notice the move didn't start and resend it."""
    pos = await m.home()
    assert m.homed
    assert pos["y"] == pytest.approx(11_532_000 / STEPS_PER_MM, abs=1e-4)
    assert pos["x"] == pytest.approx(9_843_000 / STEPS_PER_MM, abs=1e-4)
    assert sim.machine.ignored_moves >= 1  # the situation really occurred and was recovered
