"""Front-panel keys: decoding, jog mode/step, jogging and job control (against the simulator)."""

import asyncio

from pnp.machine.panel import FrontPanel
from pnp.tvm import protocol as p
from pnp.tvm.protocol import Axis
from test_machine import phys_mm, scenario

# State bytes 8-11 as read on the real machine while idle, and with one key held
# (from the keyprobe log of 2026-11-16).
IDLE = bytes.fromhex("00000000e0030000f0ffff03") + bytes(32)


def with_bytes(b9, b10):
    f = bytearray(IDLE)
    f[9], f[10] = b9, b10
    return bytes(f)


def test_decode_real_key_bytes():
    assert p.decode_state(IDLE).keys == frozenset()
    cases = {(0xFF, 0xFD): "mode", (0xFF, 0xFE): "x-", (0xEF, 0xFF): "x+", (0xFF, 0xFB): "y+",
             (0xF7, 0xFF): "y-", (0xDF, 0xFF): "step", (0x7F, 0xFF): "start", (0xBF, 0xFF): "pause"}
    for (b9, b10), key in cases.items():
        s = p.decode_state(with_bytes(b9, b10))
        assert s.keys == {key}
        assert s.needle_up  # byte 10 bit 0x08 is still the needle sensor


def test_encode_state_round_trip():
    assert p.decode_state(p.encode_state(0, {})).keys == frozenset()
    assert p.decode_state(p.encode_state(0, {}, keys={"x+", "start"})).keys == {"x+", "start"}


class FakeRunner:
    def __init__(self, state="idle"):
        self.state = state
        self.calls = []

    def busy(self):
        return self.state in ("running", "pausing", "stopping")

    async def start(self):
        self.calls.append("start")
        self.state = "running"

    async def resume(self):
        self.calls.append("resume")
        self.state = "running"

    def pause(self):
        self.calls.append("pause")
        self.state = "pausing"

    def stop(self):
        self.calls.append("stop")
        self.state = "idle"


async def press(sim, key, hold=0.15):
    sim.machine.keys.add(key)
    await asyncio.sleep(hold)
    sim.machine.keys.discard(key)
    await asyncio.sleep(0.15)


async def jog_done(panel):
    if panel._jog is not None:
        await asyncio.wait_for(panel._jog, 10)


@scenario()
async def test_step_and_mode_keys(sim, m):
    FrontPanel(m)
    await asyncio.sleep(0.15)
    assert m.panel == {"mode": "dot", "step": 1.0}
    await press(sim, "step")  # Step drives the job stepper now; without a runner it does nothing
    assert m.panel["step"] == 1.0
    await press(sim, "mode")
    assert m.panel["mode"] == "con"
    await press(sim, "mode")
    assert m.panel["mode"] == "dot"
    assert m.snapshot()["panel"]["keys"] == []


@scenario()
async def test_key_held_at_connect_is_not_a_press(sim, m):
    sim.machine.keys.add("mode")
    await asyncio.sleep(0.15)
    FrontPanel(m)
    await asyncio.sleep(0.2)
    assert m.panel["mode"] == "dot"
    assert m.snapshot()["panel"]["keys"] == ["mode"]
    sim.machine.keys.discard("mode")
    await asyncio.sleep(0.15)
    await press(sim, "mode")
    assert m.panel["mode"] == "con"


@scenario()
async def test_dot_jog_moves_one_step(sim, m):
    panel = FrontPanel(m)
    await m.home()
    x0, y0 = phys_mm(sim, Axis.X), phys_mm(sim, Axis.Y)
    m.panel["step"] = 1.0
    await press(sim, "x-")
    await jog_done(panel)
    assert abs(phys_mm(sim, Axis.X) - (x0 - 1.0)) < 0.05
    await press(sim, "y-")
    await jog_done(panel)
    assert abs(phys_mm(sim, Axis.Y) - (y0 - 1.0)) < 0.05


@scenario()
async def test_con_jog_moves_while_held_and_stops_on_release(sim, m):
    panel = FrontPanel(m)
    await m.home()
    m.panel["mode"] = "con"
    x0 = phys_mm(sim, Axis.X)
    await press(sim, "x-", hold=0.4)
    await jog_done(panel)
    x1 = phys_mm(sim, Axis.X)
    assert x1 < x0 - 0.5  # it moved
    assert x1 > m.cfg.limits.x[0] + 1  # and stopped well before the soft limit
    await asyncio.sleep(0.2)
    assert phys_mm(sim, Axis.X) == x1  # it really stopped
    # a second hold right after the stop still moves (the controller ignores moves
    # sent too soon after a stop; jog_hold waits for the axis to settle first)
    await press(sim, "x-", hold=0.3)
    await jog_done(panel)
    assert phys_mm(sim, Axis.X) < x1 - 0.3
    assert m.busy is None and m.last_error is None


@scenario()
async def test_con_jog_stops_at_soft_limit(sim, m):
    panel = FrontPanel(m)
    await m.home()
    m.cfg.limits.x = (m.position()["x"] - 2.0, m.cfg.limits.x[1])
    m.panel["mode"] = "con"
    sim.machine.keys.add("x-")
    await asyncio.sleep(0.15)
    await jog_done(panel)  # ends by itself at the limit while the key is still held
    assert abs(m.position()["x"] - m.cfg.limits.x[0]) < 0.05
    sim.machine.keys.discard("x-")
    await asyncio.sleep(0.1)


@scenario()
async def test_jog_refused_while_job_runs(sim, m):
    runner = FakeRunner("running")
    panel = FrontPanel(m, runner=runner)
    await m.home()
    x0 = sim.machine.axes[Axis.X].phys
    await press(sim, "x+")
    assert panel._jog is None
    assert sim.machine.axes[Axis.X].phys == x0
    assert "job is running" in m.last_error


@scenario()
async def test_job_keys(sim, m):
    runner = FakeRunner("idle")
    FrontPanel(m, runner=runner)
    await asyncio.sleep(0.15)
    await press(sim, "start")
    assert runner.calls == ["start"] and runner.state == "running"
    await press(sim, "pause")
    assert runner.calls[-1] == "pause"
    runner.state = "paused"
    await press(sim, "pause")
    assert runner.calls[-1] == "resume"
    await press(sim, "start")
    assert runner.calls[-1] == "stop" and runner.state == "idle"
    await press(sim, "pause")  # nothing to pause: shown, not called
    assert runner.calls[-1] == "stop"
    assert "isn't running" in m.last_error
