"""Pick and place operations against the simulator (no hardware)."""

import asyncio
import contextlib
import struct

import pytest

from pnp.config import HeadConfig, MachineConfig
from pnp.job.feeders import Feeder, FeederError, FeederStore
from pnp.job.head_ops import HeadOps, NotCalibrated, PickError
from pnp.machine.machine import Machine, MachineError
from pnp.sim.sim_vision import SIM_NOZZLE_OFFSETS, SIM_PRICK_OFFSET
from pnp.sim.tvm_sim import STEPS_PER_MM, SimConfig, TvmSimulator
from pnp.tvm import protocol as p
from pnp.vision.calibration_store import Calibration

POS_TOL = 0.031  # controller quantum is 0.0305 mm
DEPTH_TOL = 0.05


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def calibration() -> Calibration:
    return Calibration(nozzle_offset=dict(SIM_NOZZLE_OFFSETS), prick_offset=SIM_PRICK_OFFSET)


@contextlib.asynccontextmanager
async def rig(tmp_path, sim_cfg=None, head_cfg=None, cal=None):
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
    store = FeederStore(tmp_path / "feeders.yaml")
    head = HeadOps(m, cal if cal is not None else calibration(), head_cfg or HeadConfig(), store)
    try:
        yield sim, m, head, store
        assert sim.machine.burst_violations == 0
    finally:
        await m.stop()
        await sim.stop()


def events(sim, kind):
    return [e for e in sim.machine.events if e[0] == kind]


def tool_xy(event_xy, offset):
    """World position of a tool from the head position in an event."""
    return (event_xy[0] + offset[0], event_xy[1] + offset[1])


def assert_at(actual, expected, tol=POS_TOL):
    assert actual[0] == pytest.approx(expected[0], abs=tol)
    assert actual[1] == pytest.approx(expected[1], abs=tol)


def strip_feeder(**kw) -> Feeder:
    base = dict(id="F1", kind="strip", pick=(100.0, 50.0), direction=(1.0, 0.0), pitch=4.0,
                capacity=10, pick_depth=3.0)
    base.update(kw)
    return Feeder(**base)


# ---------------------------------------------------------------- no hardware needed

def test_head_for_nozzle_and_prick(tmp_path):
    head = HeadOps(None, calibration(), HeadConfig(), FeederStore(tmp_path / "f.yaml"))
    x, y = head.head_for(1, (100.0, 50.0))
    assert (x, y) == pytest.approx((100.0 + 42.0, 50.0 + 3.0))
    x, y = head.head_for("prick", (80.0, 40.0))
    assert (x, y) == pytest.approx((80.0 + 50.0, 40.0 - 12.0))


def test_not_calibrated(tmp_path):
    head = HeadOps(None, Calibration(), HeadConfig(), FeederStore(tmp_path / "f.yaml"))
    with pytest.raises(NotCalibrated):
        head.head_for(1, (0, 0))
    with pytest.raises(NotCalibrated):
        head.head_for("prick", (0, 0))
    assert issubclass(NotCalibrated, MachineError)
    assert issubclass(PickError, MachineError)


# ---------------------------------------------------------------- simulator

def test_pick_from_strip_and_count(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder())
            feeder = store.get("F1")
            await head.pick(1, feeder)
            picks = events(sim, "pick")
            assert len(picks) == 1
            _, n, x, y = picks[0]
            assert n == 1
            assert_at(tool_xy((x, y), SIM_NOZZLE_OFFSETS[1]), (100.0, 50.0))
            assert feeder.count == 1
            assert FeederStore(tmp_path / "feeders.yaml").get("F1").count == 1

            await head.place(1, (120.0, 80.0))
            await head.pick(1, feeder)
            picks = events(sim, "pick")
            assert len(picks) == 2
            assert_at((picks[1][2] - picks[0][2], picks[1][3] - picks[0][3]), (4.0, 0.0))
            assert_at(tool_xy((picks[1][2], picks[1][3]), SIM_NOZZLE_OFFSETS[1]), (104.0, 50.0))
            assert store.get("F1").count == 2
    run(body())


def test_place_releases_at_target_and_blows(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder())
            await head.pick(1, store.get("F1"))
            await head.place(1, (120.0, 80.0))

            releases = events(sim, "release")
            assert len(releases) == 1
            _, n, x, y, z = releases[0]
            assert n == 1
            assert_at(tool_xy((x, y), SIM_NOZZLE_OFFSETS[1]), (120.0, 80.0))
            assert z == pytest.approx(3.0, abs=DEPTH_TOL)
            assert head.holding[1] is None

            blow_on = [
                c for c in sim.machine.commands
                if c[0] == p.Cmd.OUTPUTS_ON and len(c) == 8
                and struct.unpack_from("<I", c, 4)[0] & p.Output.BLOW1
            ]
            assert blow_on
            assert not sim.machine.outputs & p.Output.VACUUM1
    run(body())


def test_failed_pick_does_not_advance(tmp_path):
    async def body():
        async with rig(tmp_path, sim_cfg=SimConfig(pick_success=False),
                       head_cfg=HeadConfig(check_vacuum=True)) as (sim, m, head, store):
            store.upsert(strip_feeder())
            with pytest.raises(PickError):
                await head.pick(1, store.get("F1"))
            assert store.get("F1").count == 0
            assert FeederStore(tmp_path / "feeders.yaml").get("F1").count == 0
            assert not sim.machine.outputs & p.Output.VACUUM1
            assert head.busy is None
            assert head.holding[1] is None
    run(body())


def test_drag_feeder_feeds_before_pick(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            feeder = Feeder(id="D1", kind="drag", pick=(100.0, 50.0), direction=(1.0, 0.0),
                            pitch=4.0, prick=(80.0, 40.0), pick_depth=3.0)
            store.upsert(feeder)
            await head.pick(1, store.get("D1"))

            downs, ups = events(sim, "prick_down"), events(sim, "prick_up")
            assert len(downs) == 1 and len(ups) == 1
            assert_at(tool_xy(downs[0][1:], SIM_PRICK_OFFSET), (80.0, 40.0))
            drag = (ups[0][1] - downs[0][1], ups[0][2] - downs[0][2])
            assert_at(drag, (4.0, 0.0))
            picks = events(sim, "pick")
            assert len(picks) == 1
            assert_at(tool_xy(picks[0][2:], SIM_NOZZLE_OFFSETS[1]), (100.0, 50.0))
            assert store.get("D1").needs_feed is True
            assert not sim.machine.outputs & p.Output.PRICK

            await head.place(1, (120.0, 80.0))
            await head.pick(1, store.get("D1"))
            assert len(events(sim, "prick_down")) == 2
            assert len(events(sim, "prick_up")) == 2
            assert store.get("D1").count == 2
    run(body())


def test_strip_exhausted_and_disabled(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder(id="S", capacity=1))
            await head.pick(1, store.get("S"))
            await head.place(1, (120.0, 80.0))
            with pytest.raises(FeederError):
                await head.pick(1, store.get("S"))

            store.upsert(strip_feeder(id="OFF", enabled=False))
            with pytest.raises(FeederError):
                await head.pick(1, store.get("OFF"))
    run(body())


def test_concurrent_call_rejected(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder())
            first = asyncio.create_task(head.pick(1, store.get("F1")))
            await asyncio.sleep(0.05)
            with pytest.raises(MachineError, match="head busy"):
                await head.place(1, (120.0, 80.0))
            await first
            assert store.get("F1").count == 1
    run(body())


def test_place_rotates_to_tape_rotation(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder(rotation=90.0))
            await head.pick(1, store.get("F1"))
            await head.place(1, (120.0, 80.0), rotation=0.0)
            assert m.position()["a1"] == pytest.approx(-90.0, abs=0.23)
    run(body())


def test_pick_and_place_at_position(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder())
            await head.pick_and_place(1, store.get("F1"), (150.0, 150.0))
            releases = events(sim, "release")
            assert len(releases) == 1
            assert_at(tool_xy(releases[0][2:4], SIM_NOZZLE_OFFSETS[1]), (150.0, 150.0))
            assert store.get("F1").count == 1
            assert head.busy is None
    run(body())


def test_discard(tmp_path):
    async def body():
        async with rig(tmp_path, head_cfg=HeadConfig(discard=(200.0, 300.0))) as (sim, m, head, store):
            store.upsert(strip_feeder())
            await head.pick(1, store.get("F1"))
            await head.discard(1)
            releases = events(sim, "release")
            assert len(releases) == 1
            assert_at(tool_xy(releases[0][2:4], SIM_NOZZLE_OFFSETS[1]), (200.0, 300.0))
            assert head.holding[1] is None
    run(body())


def test_discard_without_position_rejected(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            with pytest.raises(MachineError, match="discard"):
                await head.discard(1)
    run(body())


def test_pick_refused_while_nozzle_holds_part(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder())
            feeder = store.get("F1")
            await head.pick(1, feeder)
            with pytest.raises(MachineError, match="already holds"):
                await head.pick(1, feeder)
            assert feeder.count == 1
    run(body())


def test_failed_place_keeps_part(tmp_path):
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder())
            await head.pick(1, store.get("F1"))
            with pytest.raises(MachineError):
                await head.place(1, (120.0, 80.0), depth=50.0)  # beyond z1_max: refused before release
            assert not events(sim, "release")  # vacuum stayed on
            assert await m.has_part(1)
            assert head.holding[1] == "F1"
            assert abs(m.position()["z"]) < 0.031  # nozzle lifted clear
    run(body())


def test_wait_for_part_tolerates_slow_vacuum():
    """The sensor may need a few reads before the vacuum has built up."""
    from pnp.config import HeadConfig as _HC

    class SlowSensor:
        def __init__(self, ready_after):
            self.reads = 0
            self.ready_after = ready_after

        async def has_part(self, nozzle):
            self.reads += 1
            return self.reads > self.ready_after

    head = HeadOps.__new__(HeadOps)
    head.cfg = _HC(vacuum_timeout=0.6)
    head.machine = SlowSensor(ready_after=4)          # part seen on the 5th read (~0.2 s)
    assert run(head._wait_for_part(1)) is True
    head.machine = SlowSensor(ready_after=10_000)     # never: gives up after the timeout
    head.cfg = _HC(vacuum_timeout=0.2)
    assert run(head._wait_for_part(1)) is False



def test_vacuum_stays_on_after_pick_by_default(tmp_path):
    """Default (check_vacuum off): the sensor is not consulted and vacuum stays on
    until the place, even if the sensor never reports the part (2026-11-13)."""
    async def body():
        async with rig(tmp_path, sim_cfg=SimConfig(pick_success=False)) as (sim, m, head, store):
            store.upsert(strip_feeder())
            await head.pick(1, store.get("F1"))          # no PickError: sensor not used
            assert head.holding[1] == "F1"
            await asyncio.sleep(1.0)                      # well past the old 0.6 s check
            assert sim.machine.outputs & p.Output.VACUUM1  # still sucking
            await head.place(1, (120.0, 80.0))
            assert not sim.machine.outputs & p.Output.VACUUM1
            assert head.holding[1] is None
    run(body())


def test_error_during_pick_keeps_vacuum_and_marks_holding(tmp_path):
    """A failure after the vacuum went on must not drop a part that may be on the nozzle."""
    async def body():
        async with rig(tmp_path) as (sim, m, head, store):
            store.upsert(strip_feeder(pick_depth=50.0))  # beyond z1_max: the nozzle move fails
            with pytest.raises(MachineError):
                await head.pick(1, store.get("F1"))
            assert sim.machine.outputs & p.Output.VACUUM1  # vacuum kept on
            assert head.holding[1] == "F1"                  # must Place or Discard next
            assert abs(m.position()["z"]) < 0.031           # lifted clear
            with pytest.raises(MachineError, match="already holds"):
                await head.pick(1, store.get("F1"))
    run(body())



def _drag_feeder(**kw):
    base = dict(id="D1", kind="drag", pick=(120.0, 60.0), direction=(1.0, 0.0), pitch=4.0,
                prick=(116.0, 64.0), pick_depth=3.0)
    base.update(kw)
    return Feeder(**base)


def test_feed_does_not_drag_when_needle_does_not_go_down(tmp_path):
    from pnp.machine.machine import NeedleError

    async def body():
        async with rig(tmp_path, sim_cfg=SimConfig(needle_stuck="up")) as (sim, m, head, store):
            store.upsert(_drag_feeder())
            feeder = store.get("D1")
            with pytest.raises(NeedleError, match="did not go down"):
                await head.feed(feeder)
            # the head stayed where the needle was supposed to go in: no drag happened
            hx, hy = head.head_for("prick", feeder.prick)
            assert_at((m.position()["x"], m.position()["y"]), (hx, hy))
            assert store.get("D1").needs_feed is True
            assert not sim.machine.outputs & p.Output.PRICK
    run(body())


def test_feed_waits_on_stuck_needle_and_continues_after_clear(tmp_path):
    from pnp.machine.machine import NeedleDown

    async def body():
        async with rig(tmp_path, sim_cfg=SimConfig(needle_stuck="down")) as (sim, m, head, store):
            store.upsert(_drag_feeder())
            feeder = store.get("D1")
            feeding = asyncio.create_task(head.feed(feeder))
            for _ in range(250):
                if m.alarm is not None:
                    break
                await asyncio.sleep(0.02)
            assert m.alarm is not None and not feeding.done()
            assert store.get("D1").needs_feed is True  # not fed yet
            with pytest.raises(NeedleDown):  # nothing may move while it is stuck
                await m.move_to(x=150.0)
            sim.machine.cfg.needle_stuck = None  # freed by hand
            await asyncio.sleep(0.2)
            m.alarm_clear()
            await asyncio.wait_for(feeding, 5)  # the feed finishes normally
            assert store.get("D1").needs_feed is False
            assert m.alarm is None
    run(body())
