"""High-level TVM802 control: mm/degrees, awaitable motion, homing, I/O, safety.

Every motion method returns only once the controller reports that the axes
have *arrived* (within `tolerance_steps`, stable for `settle_polls` state
replies). This is what the G-code bridge never did.

Coordinate conventions
  x, y   mm, machine coordinates. Home is the max corner.
  z      mm on the seesaw: + = nozzle 1 down, - = nozzle 2 down, 0 = both up.
  a1, a2 degrees, unwrapped (can exceed 360). `rotate()` picks the shortest path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from pnp.config import MachineConfig
from pnp.tvm import protocol as p
from pnp.tvm.protocol import Axis, Output
from pnp.tvm.transport import NotConnected, TvmTransport

log = logging.getLogger(__name__)

AXIS_BY_NAME: dict[str, Axis] = {"x": Axis.X, "y": Axis.Y, "z": Axis.Z, "a1": Axis.A1, "a2": Axis.A2}
NAME_BY_AXIS = {v: k for k, v in AXIS_BY_NAME.items()}

OUTPUTS: dict[str, Output] = {
    "pump": Output.PUMP_BOTH,
    "vacuum1": Output.VACUUM1,
    "vacuum2": Output.VACUUM2,
    "blow1": Output.BLOW1,
    "blow2": Output.BLOW2,
    "prick": Output.PRICK,
    "leds": Output.LEDS,
    "buzzer": Output.BUZZER,
}

LIMIT_BITS = p.Input.LIMIT_RIGHT | p.Input.LIMIT_LEFT | p.Input.LIMIT_TOP | p.Input.LIMIT_BOTTOM
# Switch each axis homes onto (both axes home toward +). X+ = right was
# confirmed on the real machine 2026-11-12; Y+ = Top is from the Node bridge.
HOME_SWITCH = {Axis.X: p.Input.LIMIT_RIGHT, Axis.Y: p.Input.LIMIT_TOP}


class MachineError(Exception):
    pass


class NotReady(MachineError):
    pass


class NotHomed(MachineError):
    pass


class SoftLimitError(MachineError):
    pass


class MotionTimeout(MachineError):
    pass


class LimitHit(MachineError):
    pass


class EStopped(MachineError):
    pass


class HomingFailed(MachineError):
    pass


class Machine:
    def __init__(self, cfg: MachineConfig, transport: TvmTransport | None = None):
        self.cfg = cfg
        c = cfg.controller
        self.t = transport or TvmTransport(
            c.host, c.port, poll_interval=c.poll_interval_idle, reconnect_delay=c.reconnect_delay
        )
        self.t.poll_interval = c.poll_interval_idle
        self.homed = False
        self._stopping = False
        self.estopped = False
        self.busy: str | None = None  # "moving" / "homing" / None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._fast_poll_users = 0
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.t.on_state(lambda _s: self._notify())
        self.t.on_connection(self._on_connection)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.t.start()

    async def stop(self) -> None:
        self._stopping = True
        await self.t.stop()
        self.homed = False

    def on_change(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(cb)

    def off_change(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(cb)

    def _notify(self) -> None:
        if not self._listeners:
            return
        snap = self.snapshot()
        for cb in self._listeners:
            cb(snap)

    def _on_connection(self, up: bool) -> None:
        if not up and self.homed and not self._stopping:
            # The controller may have reset; positions can no longer be trusted.
            log.warning("connection lost: machine marked as not homed")
            self.homed = False
        self._notify()

    # ------------------------------------------------------------------ units

    def _scale(self, axis: Axis) -> float:
        s = self.cfg.steps
        return {Axis.X: s.x, Axis.Y: s.y, Axis.Z: s.z, Axis.A1: s.a, Axis.A2: s.a}[axis]

    def to_steps(self, axis: Axis, value: float) -> int:
        return round(value * self._scale(axis))

    def from_steps(self, axis: Axis, steps: int) -> float:
        return steps / self._scale(axis)

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self.t.connected.is_set()

    @property
    def state(self) -> p.MachineState | None:
        return self.t.state

    def position(self) -> dict[str, float]:
        s = self.t.state
        if s is None:
            return {name: 0.0 for name in AXIS_BY_NAME}
        return {NAME_BY_AXIS[a]: self.from_steps(a, s.positions[a]) for a in AXIS_BY_NAME.values()}

    def snapshot(self) -> dict[str, Any]:
        s = self.t.state
        return {
            "connected": self.connected,
            "homed": self.homed,
            "estopped": self.estopped,
            "busy": self.busy,
            "error": self.last_error,
            "position": self.position(),
            "limits": s.limits if s else {},
            "vacuum": {"1": s.pressure1, "2": s.pressure2} if s else {},
            "outputs": {name: bool(s.outputs & bit) == True and (s.outputs & bit) == bit
                        for name, bit in OUTPUTS.items()} if s else {},
            "speed_mode": s.speed_mode if s else None,
            "soft_limits": {"x": list(self.cfg.limits.x), "y": list(self.cfg.limits.y),
                            "z1_max": self.cfg.limits.z1_max, "z2_max": self.cfg.limits.z2_max},
            "time": time.time(),
        }

    async def refresh(self) -> p.MachineState:
        try:
            return await self.t.request_state()
        except NotConnected as exc:
            raise NotReady("not connected to the machine") from exc
        except asyncio.TimeoutError as exc:
            raise NotReady("machine did not answer a state request") from exc

    # ------------------------------------------------------------------ guards

    def _check_ready(self) -> None:
        if self.estopped:
            raise EStopped("E-stop active: reset before moving")
        if not self.connected:
            raise NotReady("not connected to the machine")

    def _check_soft_limits(self, targets: dict[str, float]) -> None:
        lim = self.cfg.limits
        for name in ("x", "y"):
            if name in targets:
                if not self.homed:
                    raise NotHomed(f"home the machine before absolute {name.upper()} moves")
                lo, hi = getattr(lim, name)
                if not lo <= targets[name] <= hi:
                    raise SoftLimitError(f"{name.upper()}={targets[name]:.3f} outside soft limits [{lo}, {hi}]")
        if "z" in targets:
            z = targets["z"]
            if not -lim.z2_max <= z <= lim.z1_max:
                raise SoftLimitError(f"Z={z:.3f} outside soft limits [-{lim.z2_max}, {lim.z1_max}]")

    @contextlib.contextmanager
    def _fast_poll(self):
        self._fast_poll_users += 1
        self.t.poll_interval = self.cfg.controller.poll_interval_moving
        try:
            yield
        finally:
            self._fast_poll_users -= 1
            if self._fast_poll_users == 0:
                self.t.poll_interval = self.cfg.controller.poll_interval_idle

    @contextlib.asynccontextmanager
    async def _operation(self, name: str):
        """Exclusive access to motion; records the error message for the UI."""
        self._check_ready()
        if self._lock.locked():
            raise NotReady(f"machine busy ({self.busy})")
        async with self._lock:
            self.busy = name
            self.last_error = None
            self._notify()
            try:
                yield
            except MachineError as exc:
                self.last_error = str(exc)
                raise
            finally:
                self.busy = None
                self._notify()

    # ------------------------------------------------------------------ motion primitives (lock held)

    def _estimate_timeout(self, targets: dict[Axis, int], start: p.MachineState) -> float:
        m = self.cfg.motion
        speed = {Axis.X: m.xy_speed, Axis.Y: m.xy_speed, Axis.Z: m.z_speed, Axis.A1: m.a_speed, Axis.A2: m.a_speed}
        t = max(
            abs(self.from_steps(a, v - start.positions[a])) / speed[a] for a, v in targets.items()
        )
        return m.min_timeout + t

    async def _next_state(self) -> p.MachineState:
        try:
            s = await self.t.wait_state(timeout=1.5)
        except asyncio.TimeoutError as exc:
            raise NotReady("lost contact with the machine during motion") from exc
        if self.estopped:
            raise EStopped("E-stop pressed")
        return s

    def _quantize(self, targets: dict[Axis, int], start: p.MachineState) -> dict[Axis, int]:
        """Snap each target so the move *distance* is a whole number of quanta.

        The controller rounds the distance up to a multiple of `step_quantum`,
        so asking for anything else lands somewhere we did not expect.
        """
        q = self.cfg.motion.step_quantum
        if q <= 1:
            return targets
        out = {}
        for axis, target in targets.items():
            here = start.positions[axis]
            out[axis] = here + round((target - here) / q) * q
        return out

    async def _move_steps(self, targets: dict[Axis, int], *, watch_limits: bool = True) -> p.MachineState:
        m = self.cfg.motion
        start = await self.refresh()
        start_limits = start.inputs & LIMIT_BITS
        targets = self._quantize(targets, start)
        deadline = asyncio.get_running_loop().time() + self._estimate_timeout(targets, start)
        await self._send(p.DEFAULT_SPEED_SETUP, p.encode_move(targets))
        log.debug("move %s", {NAME_BY_AXIS[a]: v for a, v in targets.items()})
        stable, last = 0, None
        with self._fast_poll():
            while True:
                s = await self._next_state()
                new_limits = (s.inputs & LIMIT_BITS) & ~start_limits
                if watch_limits and new_limits:
                    await self._send_stop()
                    raise LimitHit(f"limit switch hit during move ({p.Input(new_limits).name})")
                pos = tuple(s.positions[a] for a in targets)
                stable = stable + 1 if pos == last else 0
                last = pos
                arrived = all(abs(s.positions[a] - v) <= m.tolerance_steps for a, v in targets.items())
                if arrived and stable >= m.settle_polls - 1:
                    return s
                if asyncio.get_running_loop().time() > deadline:
                    await self._send_stop()
                    where = {NAME_BY_AXIS[a]: round(self.from_steps(a, s.positions[a]), 3) for a in targets}
                    raise MotionTimeout(f"move did not complete in time; stopped at {where}")

    async def _wait_for(self, pred: Callable[[p.MachineState], bool], timeout: float, what: str) -> p.MachineState:
        deadline = asyncio.get_running_loop().time() + timeout
        with self._fast_poll():
            while True:
                s = await self._next_state()
                if pred(s):
                    return s
                if asyncio.get_running_loop().time() > deadline:
                    await self._send_stop()
                    raise HomingFailed(f"timed out waiting for {what}")

    async def _send(self, *frames: bytes) -> None:
        try:
            await self.t.send(*frames)
        except NotConnected as exc:
            raise NotReady("not connected to the machine") from exc

    async def _send_stop(self, mask: Axis | int = Axis.ALL) -> None:
        with contextlib.suppress(NotConnected):
            await self.t.send(p.encode_stop(mask))

    async def _raise_nozzles(self) -> None:
        z = self.t.state.positions[Axis.Z] if self.t.state else 0
        if abs(self.from_steps(Axis.Z, z)) > self.cfg.motion.safe_z_tolerance:
            await self._move_steps({Axis.Z: 0})

    async def _move_mm(self, targets: dict[str, float], safe: bool) -> dict[str, float]:
        self._check_soft_limits(targets)
        if safe and ("x" in targets or "y" in targets) and "z" not in targets:
            await self._raise_nozzles()
        steps = {AXIS_BY_NAME[n]: self.to_steps(AXIS_BY_NAME[n], v) for n, v in targets.items()}
        await self._move_steps(steps)
        return self.position()

    # ------------------------------------------------------------------ public motion

    async def move_to(self, *, safe: bool = True, **targets: float) -> dict[str, float]:
        """Absolute move. Keys: x, y, z, a1, a2 (mm / degrees).

        With safe=True (default) an X/Y move first raises both nozzles.
        """
        targets = {k: float(v) for k, v in targets.items() if v is not None}
        unknown = set(targets) - set(AXIS_BY_NAME)
        if unknown:
            raise ValueError(f"unknown axis {sorted(unknown)}")
        if not targets:
            raise ValueError("no axis given")
        async with self._operation("moving"):
            return await self._move_mm(targets, safe)

    async def jog(self, axis: str, delta: float) -> dict[str, float]:
        """Relative move of one axis. Clamped to soft limits when homed."""
        if axis not in AXIS_BY_NAME:
            raise ValueError(f"unknown axis {axis}")
        async with self._operation("moving"):
            s = await self.refresh()
            current = self.from_steps(AXIS_BY_NAME[axis], s.positions[AXIS_BY_NAME[axis]])
            target = current + float(delta)
            lim, m = self.cfg.limits, self.cfg.motion
            if axis in ("x", "y"):
                if self.homed:
                    lo, hi = getattr(lim, axis)
                    target = min(max(target, lo), hi)
                else:
                    if not m.allow_unhomed_jog:
                        raise NotHomed("home the machine before jogging X/Y")
                    if abs(delta) > m.max_unhomed_jog:
                        raise NotHomed(f"jogs over {m.max_unhomed_jog} mm need homing first")
                    await self._raise_nozzles()
                    await self._move_steps({AXIS_BY_NAME[axis]: self.to_steps(AXIS_BY_NAME[axis], target)})
                    return self.position()
            elif axis == "z":
                target = min(max(target, -lim.z2_max), lim.z1_max)
            return await self._move_mm({axis: target}, safe=True)

    async def nozzle_down(self, nozzle: int, depth: float) -> dict[str, float]:
        """Lower nozzle 1 or 2 by `depth` mm (the other one goes up)."""
        if nozzle not in (1, 2) or depth < 0:
            raise ValueError("nozzle must be 1 or 2 and depth >= 0")
        return await self.move_to(z=depth if nozzle == 1 else -depth)

    async def nozzles_up(self) -> dict[str, float]:
        return await self.move_to(z=0.0)

    async def rotate(self, nozzle: int, angle: float, *, shortest: bool = True) -> dict[str, float]:
        """Rotate a nozzle to `angle` degrees. With shortest=True the target is
        the equivalent angle closest to the current one (never > 180 deg)."""
        if nozzle not in (1, 2):
            raise ValueError("nozzle must be 1 or 2")
        name = f"a{nozzle}"
        target = float(angle)
        if shortest:
            current = self.position()[name]
            target = current + ((target - current + 180.0) % 360.0 - 180.0)
        return await self.move_to(**{name: target})

    # ------------------------------------------------------------------ homing

    async def home(self) -> dict[str, float]:
        """Port of the Node bridge routine: raise Z, zero A, home Y then X."""
        async with self._operation("homing"):
            self.homed = False
            await self._move_steps({Axis.Z: 0})
            await self._send(p.encode_set_position({Axis.A1: 0, Axis.A2: 0}))
            await self._send(*p.HOMING_PREP_SEQUENCE)
            for axis in (Axis.Y, Axis.X):
                await self._home_axis(axis)
            self.homed = True
            log.info("homing complete")
            return self.position()

    async def _home_axis(self, axis: Axis) -> None:
        h = self.cfg.homing
        bits = HOME_SWITCH[axis]
        name = NAME_BY_AXIS[axis].upper()
        far = self.to_steps(axis, h.approach_mm)
        pressed = lambda s: bool(s.inputs & bits)

        s = await self.refresh()
        if not pressed(s):
            others_at_start = s.inputs & LIMIT_BITS & ~bits
            wrong = lambda s: bool(s.inputs & LIMIT_BITS & ~bits & ~others_at_start)
            await self._send(p.DEFAULT_SPEED_SETUP, p.encode_move({axis: s.positions[axis] + far}))
            s = await self._wait_for(lambda s: pressed(s) or wrong(s), h.approach_timeout, f"{name} limit switch")
            await self._send_stop(axis)
            if wrong(s) and not pressed(s):
                tripped = p.Input(s.inputs & LIMIT_BITS & ~bits & ~others_at_start).name
                raise HomingFailed(f"{name} homing hit the wrong switch ({tripped}): check the axis direction")

        s = await self.refresh()
        back = s.positions[axis] - self.to_steps(axis, h.back_off_mm)
        await self._send(p.encode_move({axis: back}))
        s = await self._wait_for(lambda s: not pressed(s), h.release_timeout, f"{name} switch to release")

        await self._send(p.encode_move({axis: s.positions[axis] + far}))
        await self._wait_for(pressed, h.second_approach_timeout, f"{name} switch (2nd approach)")
        await self._send_stop(axis)
        # Let the axis come to rest before overwriting its register.
        await self._wait_settled(axis)
        register = h.x_register if axis == Axis.X else h.y_register
        await self._send(p.encode_set_position({axis: register}))
        await self._wait_for(lambda s: abs(s.positions[axis] - register) <= self.cfg.motion.tolerance_steps,
                             2.0, f"{name} register update")
        log.info("%s homed, register set to %d", name, register)

    async def _wait_settled(self, axis: Axis) -> None:
        last, stable = None, 0
        with self._fast_poll():
            while stable < self.cfg.motion.settle_polls:
                s = await self._next_state()
                stable = stable + 1 if s.positions[axis] == last else 0
                last = s.positions[axis]

    # ------------------------------------------------------------------ outputs

    async def set_output(self, name: str, on: bool) -> None:
        if name not in OUTPUTS:
            raise ValueError(f"unknown output {name}; known: {sorted(OUTPUTS)}")
        if self.estopped and on and name != "leds":
            raise EStopped("E-stop active")
        await self._send(p.encode_outputs(on, OUTPUTS[name]))

    async def has_part(self, nozzle: int) -> bool:
        s = await self.refresh()
        return s.pressure1 if nozzle == 1 else s.pressure2

    # ------------------------------------------------------------------ safety

    async def estop(self) -> None:
        """Stop every axis and switch off vacuum/pump/blow/prick immediately.

        Does not wait for the motion lock: any running move raises EStopped
        on its next state update.
        """
        self.estopped = True
        self.last_error = "E-stop pressed"
        log.warning("E-STOP")
        with contextlib.suppress(NotConnected):
            await self.t.send(
                p.encode_stop(Axis.ALL),
                p.encode_outputs(False, Output.VACUUM1 | Output.VACUUM2 | Output.PUMP_BOTH
                                 | Output.BLOW1 | Output.BLOW2 | Output.PRICK | Output.BUZZER),
            )
        self._notify()

    async def reset(self) -> None:
        """Clear the E-stop. Homing state is kept (axes were stopped under control)."""
        self.estopped = False
        self.last_error = None
        self._notify()
