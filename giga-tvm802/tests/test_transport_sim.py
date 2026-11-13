"""Transport <-> simulator integration tests (real TCP on localhost)."""

import asyncio

from pnp.sim.tvm_sim import STEPS_PER_MM, SimConfig, TvmSimulator
from pnp.tvm import protocol as p
from pnp.tvm.protocol import Axis, Output
from pnp.tvm.transport import NotConnected, TvmTransport


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def setup(cfg=None):
    sim = TvmSimulator(port=0, cfg=cfg)
    await sim.start()
    t = TvmTransport("127.0.0.1", sim.port, poll_interval=0.02, delay_before_polling=0.05,
                     reconnect_delay=0.1)
    await t.start()
    await t.wait_connected(5)
    await t.request_state()  # init sequence has been processed by now
    return sim, t


async def wait_until(t: TvmTransport, pred, timeout=5.0) -> p.MachineState:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        s = await t.wait_state(timeout=2)
        if pred(s):
            return s
        assert loop.time() < end, f"condition not reached; last state {s.positions}"


def test_init_sequence_and_state():
    async def body():
        sim, t = await setup()
        try:
            s = await t.request_state()
            # init: pump off, slow mode on, LEDs on
            assert s.speed_mode == "slow" and s.output(Output.LEDS)
            assert s.positions[Axis.X] == 0  # register reads 0 at power-on
            # every init frame reached the simulator intact
            sent = sim.machine.commands[: len(p.INIT_SEQUENCE)]
            assert sent == list(p.INIT_SEQUENCE)
            assert sim.machine.burst_violations == 0
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_absolute_move_reaches_target():
    async def body():
        sim, t = await setup()
        try:
            target = {Axis.X: 328_000, Axis.Y: -164_000, Axis.A1: 4000}  # whole quanta
            await t.send(p.DEFAULT_SPEED_SETUP, p.encode_move(target))
            s = await wait_until(t, lambda s: all(s.positions[a] == v for a, v in target.items()))
            assert s.positions[Axis.Y] < 0  # signed decode works end-to-end
            assert sim.machine.burst_violations == 0
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_limit_switch_and_set_register_homing():
    async def body():
        sim, t = await setup()
        try:
            await t.send(*p.HOMING_PREP_SEQUENCE, p.encode_move({Axis.Y: 32_808_000}))
            s = await wait_until(t, lambda s: s.limits["top"])
            # back off 5 mm -> switch releases
            back = s.positions[Axis.Y] - 5 * STEPS_PER_MM
            await t.send(p.encode_move({Axis.Y: back}))
            await wait_until(t, lambda s: not s.limits["top"])
            await t.send(p.encode_move({Axis.Y: 32_808_000}))
            await wait_until(t, lambda s: s.limits["top"])
            await t.send(p.encode_set_position({Axis.Y: 11_532_000}))
            s = await wait_until(t, lambda s: s.positions[Axis.Y] == 11_532_000)
            assert s.limits["top"]
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_stop_halts_motion():
    async def body():
        sim, t = await setup()
        try:
            await t.send(p.encode_move({Axis.X: 100 * STEPS_PER_MM}))
            await wait_until(t, lambda s: s.positions[Axis.X] > 5 * STEPS_PER_MM)
            await t.send(p.encode_stop(Axis.ALL))
            a = (await t.request_state()).positions[Axis.X]
            await asyncio.sleep(0.1)
            b = (await t.request_state()).positions[Axis.X]
            assert a == b < 100 * STEPS_PER_MM
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_vacuum_pick_sets_pressure():
    async def body():
        sim, t = await setup()
        try:
            await t.send(p.encode_outputs(True, Output.PUMP_BOTH | Output.VACUUM1))
            s = await t.request_state()
            assert s.output(Output.VACUUM1) and not s.pressure1  # nozzle still up
            await t.send(p.encode_move({Axis.Z: 3 * STEPS_PER_MM}))  # N1 down
            await wait_until(t, lambda s: s.pressure1)
            await t.send(p.encode_move({Axis.Z: 0}))
            s = await wait_until(t, lambda s: s.positions[Axis.Z] == 0)
            assert s.pressure1  # still holding the part
            await t.send(p.encode_outputs(False, Output.VACUUM1))
            await wait_until(t, lambda s: not s.pressure1)
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_failed_pick_has_no_pressure():
    async def body():
        sim, t = await setup(SimConfig(pick_success=False))
        try:
            await t.send(p.encode_outputs(True, Output.VACUUM2), p.encode_move({Axis.Z: -98_000}))
            s = await wait_until(t, lambda s: s.positions[Axis.Z] == -98_000)
            await asyncio.sleep(0.05)
            assert not (await t.request_state()).pressure2
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_reconnects_after_drop():
    async def body():
        sim, t = await setup()
        events = []
        t.on_connection(events.append)
        try:
            await sim.drop_clients()
            await asyncio.sleep(0.05)
            await t.wait_connected(5)
            await t.request_state()
            assert events[:2] == [False, True]
            # init sequence replayed on the new connection
            assert sim.machine.commands.count(p.INIT_SEQUENCE[0]) == 2
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_send_while_disconnected_raises():
    async def body():
        t = TvmTransport("127.0.0.1", 1, reconnect_delay=10)
        try:
            await t.send(p.encode_request_state())
        except NotConnected:
            return
        raise AssertionError("expected NotConnected")

    run(body())


def test_command_returns_ack_and_poll_reply():
    async def body():
        sim, t = await setup()
        try:
            assert await t.command(p.encode_stop(Axis.X)) == b"\x09\x00"
            assert await t.command(bytes([0x48, 0, 0, 0])) is None
            poll = await t.command(p.encode_poll())
            assert len(poll) == 12 and poll[0] == 0x01
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_simulator_drops_bursts_like_real_controller():
    async def body():
        sim = TvmSimulator(port=0)
        await sim.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", sim.port)
            assert await r.readexactly(4) == p.GREETING
            w.write(p.encode_outputs(True, Output.LEDS) + p.encode_outputs(True, Output.BUZZER))
            await w.drain()
            assert await r.readexactly(2) == b"\x14\x00"  # only one ack
            await asyncio.sleep(0.05)
            assert sim.machine.burst_violations == 1
            assert sim.machine.outputs & Output.LEDS and not sim.machine.outputs & Output.BUZZER
            w.close()
        finally:
            await sim.stop()

    run(body())


def test_silent_controller_is_detected_and_reconnected():
    """Power cycle: the controller stops answering without closing the socket.
    The transport must notice, drop the dead link and connect again by itself."""
    async def body():
        sim, t = await setup()
        events = []
        t.on_connection(events.append)
        try:
            sim.mute = True  # "power off"
            for _ in range(100):
                if not t.connected.is_set():
                    break
                await asyncio.sleep(0.05)
            assert not t.connected.is_set(), "dead link not detected"
            assert "stopped answering" in (t.last_error or "")
            sim.mute = False  # "power back on"
            await t.wait_connected(5)
            await t.request_state()
            assert events[:2] == [False, True]
            assert t.last_error is None
        finally:
            await t.stop()
            await sim.stop()

    run(body())


def test_reconnect_drops_link_and_connects_now():
    async def body():
        sim, t = await setup()
        t.reconnect_delay = 30  # prove reconnect() does not wait for the retry delay
        events = []
        t.on_connection(events.append)
        try:
            inits_before = sim.machine.commands.count(p.INIT_SEQUENCE[0])
            await t.reconnect()
            await asyncio.wait_for(t.wait_connected(), 5)
            for _ in range(50):
                if events[:2] == [False, True]:
                    break
                await asyncio.sleep(0.05)
            assert events[:2] == [False, True]
            await t.request_state()
            assert sim.machine.commands.count(p.INIT_SEQUENCE[0]) == inits_before + 1
        finally:
            await t.stop()
            await sim.stop()

    run(body())
