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


class NeedleDown(MachineError):
    """X/Y motion refused because the feeder needle (prick) is down."""


class NeedleError(MachineError):
    """The needle sensor did not confirm a switch: jammed up or stuck down."""


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
        # Feeder needle interlock: X/Y moves are refused while the needle is down,
        # except while feeding (needle_moves_allowed) or in the Setup needle step
        # (needle_setup). _needle_cmd is what we last commanded (None = unknown,
        # then the controller's reported output bit decides).
        self._needle_cmd: bool | None = None
        self.needle_setup = False
        self._needle_allow = 0
        # Active alarm shown as a popup in the UI (None = no alarm). While the
        # needle-stuck alarm is up, the operation that raised the needle waits for
        # the user: Clear (continue once the sensor reads up), Abort, or E-stop.
        self.alarm: dict | None = None
        self._alarm_event = asyncio.Event()
        self._alarm_action: str | None = None
        self._stopping = False
        self.estopped = False
        self.busy: str | None = None  # "moving" / "homing" / None
        # Front-panel jog settings (pnp/machine/panel.py), shared with the UI's jog step:
        # mode "dot" = one step per key press, "con" = move while the key is held.
        self.panel: dict[str, Any] = {"mode": "dot", "step": 1.0}
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
        self._needle_cmd = None  # unknown again: trust the controller's report
        if not up:
            self.needle_setup = False
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
            "link_error": None if self.connected else self.t.last_error,
            "homed": self.homed,
            "needle_down": self.needle_is_down(),
            "alarm": dict(self.alarm) if self.alarm else None,
            "needle_sensor": (None if self.needle_sensed_down() is None
                              else ("down" if self.needle_sensed_down() else "up")),
            "needle_setup": self.needle_setup,
            "estopped": self.estopped,
            "busy": self.busy,
            "error": self.last_error,
            "position": self.position(),
            "limits": s.limits if s else {},
            "vacuum": {"1": s.pressure1, "2": s.pressure2} if s else {},
            "outputs": {name: bool(s.outputs & bit) == True and (s.outputs & bit) == bit
                        for name, bit in OUTPUTS.items()} if s else {},
            "speed_mode": s.speed_mode if s else None,
            "panel": {**self.panel, "keys": sorted(s.keys) if s else []},
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

    # ------------------------------------------------------------------ needle interlock

    def needle_sensed_down(self) -> bool | None:
        """What the needle position sensor reports (None before the first state)."""
        s = self.t.state
        return None if s is None else not s.needle_up

    def needle_is_down(self) -> bool:
        """Down if commanded down OR the sensor says so (a needle stuck down after
        being switched up must still block X/Y moves)."""
        s = self.t.state
        if self.needle_sensed_down():
            return True
        if self._needle_cmd is not None:
            return self._needle_cmd
        return bool(s is not None and s.outputs & Output.PRICK)

    async def _wait_needle(self, down: bool) -> None:
        """Wait until the needle sensor reports the wanted position."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, self.cfg.head.needle_timeout)
        while True:
            s = await self.refresh()
            if (not s.needle_up) == down:
                return
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.02)
        if down:
            raise NeedleError("the feeder needle did not go down (the sensor still reports up): "
                              "check it is not jammed")
        raise NeedleError("the feeder needle is stuck DOWN (the sensor still reports down): "
                          "free it by hand, then switch it up again before moving")

    @contextlib.contextmanager
    def needle_moves_allowed(self):
        """Allow X/Y moves with the needle down inside this block (feeding a lane)."""
        self._needle_allow += 1
        try:
            yield
        finally:
            self._needle_allow -= 1

    def _check_needle(self, xy_motion: bool) -> None:
        if xy_motion and self.needle_is_down() and not (self._needle_allow or self.needle_setup):
            if self.needle_sensed_down() and self._needle_cmd is False:
                raise NeedleDown("the needle sensor reports the feeder needle DOWN although it was "
                                 "switched up (stuck?): free it before moving X/Y")
            raise NeedleDown("the feeder needle is down: raise it before moving X/Y")

    async def set_needle_setup(self, down: bool) -> None:
        """Setup tab needle step: lower the needle AND allow jogging with it down
        (to put its tip on a mark). Raising it ends the exception."""
        await self.set_output("prick", down)
        self.needle_setup = down
        self._notify()

    async def _move_steps(self, targets: dict[Axis, int], *, watch_limits: bool = True) -> p.MachineState:
        self._check_needle(Axis.X in targets or Axis.Y in targets)
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

    async def jog_hold(self, axis: str, direction: int, released: asyncio.Event) -> dict[str, float]:
        """Front-panel ConMove: move X or Y while a key is held, stop once `released` is set.

        Homed, the move runs at most to the soft limit; not homed, at most
        motion.max_unhomed_jog. Nozzles are raised first and the needle interlock applies,
        as for any X/Y move. Ends with the axis at rest, so the next move isn't ignored.
        """
        if axis not in ("x", "y") or direction not in (-1, 1):
            raise ValueError("jog_hold: axis must be x or y and direction -1 or 1")
        ax = AXIS_BY_NAME[axis]
        async with self._operation("jogging"):
            m = self.cfg.motion
            if not self.homed and not m.allow_unhomed_jog:
                raise NotHomed("home the machine before jogging X/Y")
            self._check_needle(True)
            await self._raise_nozzles()
            await self._wait_settled(ax)
            start = await self.refresh()
            here = self.from_steps(ax, start.positions[ax])
            if self.homed:
                lo, hi = getattr(self.cfg.limits, axis)
                end = hi if direction > 0 else lo
            else:
                end = here + direction * m.max_unhomed_jog
            if released.is_set() or (end - here) * direction <= 0:
                return self.position()  # tapped and let go already, or at the limit
            target = self._quantize({ax: self.to_steps(ax, end)}, start)
            start_limits = start.inputs & LIMIT_BITS
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._estimate_timeout(target, start)
            await self._send(p.DEFAULT_SPEED_SETUP, p.encode_move(target))
            try:
                with self._fast_poll():
                    while not released.is_set():
                        s = await self._next_state()
                        if (s.inputs & LIMIT_BITS) & ~start_limits:
                            raise LimitHit("limit switch hit during jog")
                        if abs(s.positions[ax] - target[ax]) <= m.tolerance_steps or loop.time() > deadline:
                            break
            finally:
                await self._send_stop(ax)
            await self._wait_settled(ax)
            return self.position()

    async def nozzle_down(self, nozzle: int, depth: float) -> dict[str, float]:
        """Lower nozzle 1 or 2 by `depth` mm (the other one goes up)."""
        if nozzle not in (1, 2) or depth < 0:
            raise ValueError("nozzle must be 1 or 2 and depth >= 0")
        return await self.move_to(z=depth if nozzle == 1 else -depth)

    async def nozzles_up(self) -> dict[str, float]:
        return await self.move_to(z=0.0)

    async def zero_z(self) -> dict[str, float]:
        """Declare the current Z position as 0 (both nozzles level) without moving.

        Z is a seesaw with no home switch: if a nozzle is driven onto its
        mechanical end the motor skips steps and the controller's Z zero drifts.
        Jog until both tips are level by eye, then call this.
        """
        async with self._operation("zeroing Z"):
            before = self.position()["z"]
            await self._send(p.encode_set_position({Axis.Z: 0}))
            await self._wait_for(lambda s: abs(s.positions[Axis.Z]) <= self.cfg.motion.tolerance_steps,
                                 2.0, "Z register update")
            log.info("Z zeroed here (controller had %.3f mm)", before)
            return self.position()

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
        self._check_needle(True)
        async with self._operation("homing"):
            self.homed = False
            await self._move_steps({Axis.Z: 0})
            await self._send(p.encode_set_position({Axis.A1: 0, Axis.A2: 0}))
            await self._send(*p.HOMING_PREP_SEQUENCE)
            for axis in (Axis.Y, Axis.X):
                try:
                    await self._home_axis(axis)
                except MachineError as exc:
                    log.warning("homing %s failed: %s", NAME_BY_AXIS[axis].upper(), exc)
                    raise
            self.homed = True
            log.info("homing complete")
            return self.position()

    async def _home_axis(self, axis: Axis) -> None:
        h = self.cfg.homing
        bits = HOME_SWITCH[axis]
        name = NAME_BY_AXIS[axis].upper()
        far = self.to_steps(axis, h.approach_mm)
        pressed = lambda s: bool(s.inputs & bits)

        # Every move below starts only after the axis has come to rest, and is
        # resent if the controller doesn't start it: right after a stop (or the
        # limit switch stopping the axis) the controller acknowledges a new move
        # but may ignore it, which left homing stuck on the Y switch (2026-11-13).
        s = await self.refresh()
        if not pressed(s):
            others_at_start = s.inputs & LIMIT_BITS & ~bits
            wrong = lambda s: bool(s.inputs & LIMIT_BITS & ~bits & ~others_at_start)
            await self._start_home_move(axis, lambda pos: pos + far, f"{name} approach")
            s = await self._wait_for(lambda s: pressed(s) or wrong(s), h.approach_timeout, f"{name} limit switch")
            await self._send_stop(axis)
            if wrong(s) and not pressed(s):
                tripped = p.Input(s.inputs & LIMIT_BITS & ~bits & ~others_at_start).name
                raise HomingFailed(f"{name} homing hit the wrong switch ({tripped}): check the axis direction")

        back_off = self.to_steps(axis, h.back_off_mm)
        await self._start_home_move(axis, lambda pos: pos - back_off, f"{name} back-off")
        await self._wait_for(lambda s: not pressed(s), h.release_timeout, f"{name} switch to release")

        await self._start_home_move(axis, lambda pos: pos + far, f"{name} 2nd approach")
        await self._wait_for(pressed, h.second_approach_timeout, f"{name} switch (2nd approach)")
        await self._send_stop(axis)
        # Let the axis come to rest before overwriting its register.
        await self._wait_settled(axis)
        register = h.x_register if axis == Axis.X else h.y_register
        await self._send(p.encode_set_position({axis: register}))
        await self._wait_for(lambda s: abs(s.positions[axis] - register) <= self.cfg.motion.tolerance_steps,
                             2.0, f"{name} register update")
        log.info("%s homed, register set to %d", name, register)

    async def _start_home_move(self, axis: Axis, target_from, what: str, attempts: int = 3) -> None:
        """Wait until `axis` is at rest, then move it to target_from(current position)
        and confirm the move really started; resend up to `attempts` times."""
        for attempt in range(1, attempts + 1):
            await self._wait_settled(axis)
            start = (await self.refresh()).positions[axis]
            await self._send(p.DEFAULT_SPEED_SETUP, p.encode_move({axis: target_from(start)}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.6
            with self._fast_poll():
                while loop.time() < deadline:
                    s = await self._next_state()
                    if abs(s.positions[axis] - start) > 2 * max(1, self.cfg.motion.step_quantum):
                        if attempt > 1:
                            log.info("%s started on attempt %d", what, attempt)
                        return
            log.warning("%s: the controller did not start the move (attempt %d of %d)", what, attempt, attempts)
        raise HomingFailed(f"{what}: the controller did not start the move after {attempts} attempts")

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
        if name == "prick":
            self._needle_cmd = on
            if not on:
                self.needle_setup = False
            self._notify()
            # Confirm with the needle position sensor (status byte 10 bit 0x08).
            try:
                await self._wait_needle(down=on)
            except NeedleError:
                if on:  # jammed: don't leave the output on for a needle that isn't down
                    with contextlib.suppress(MachineError):
                        await self._send(p.encode_outputs(False, OUTPUTS[name]))
                    self._needle_cmd = False
                    self._notify()
                    raise
                # Stuck DOWN: sound the buzzer and wait for the user instead of
                # failing; whatever called us (feed, pick, job) then carries on.
                await self._needle_stuck_alarm()

    # ------------------------------------------------------------------ alarm

    async def _needle_stuck_alarm(self) -> None:
        """Buzzer on + popup; return once the user pressed Clear and the needle
        sensor reads up. Abort -> NeedleError, E-stop -> EStopped."""
        self.alarm = {
            "kind": "needle_stuck",
            "title": "Feeder needle stuck",
            "message": "The feeder needle did not come back up. Free it by hand, then press "
                       "Clear and continue.",
            "silenced": False,
            "since": time.time(),
        }
        self._alarm_action = None
        self._alarm_event.clear()
        log.warning("ALARM: feeder needle stuck down; waiting for the operator")
        with contextlib.suppress(MachineError):
            await self._send(p.encode_outputs(True, Output.BUZZER))
        self._notify()
        try:
            while True:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._alarm_event.wait(), 0.25)
                if self.estopped:
                    raise EStopped("E-stop pressed")
                action, self._alarm_action = self._alarm_action, None
                self._alarm_event.clear()
                if action == "abort":
                    raise NeedleError("the feeder needle is stuck DOWN (aborted by the operator)")
                if action == "clear":
                    try:
                        s = await self.refresh()
                    except NotReady:
                        s = None
                    if s is not None and s.needle_up:
                        log.info("needle alarm cleared: needle up, continuing")
                        return
                    self.alarm["message"] = ("The needle sensor still reports DOWN. Free the needle, "
                                             "then press Clear and continue again.")
                    self._notify()
        finally:
            self.alarm = None
            with contextlib.suppress(MachineError, NotConnected):
                await self.t.send(p.encode_outputs(False, Output.BUZZER))
            self._notify()

    def alarm_clear(self) -> None:
        """Popup "Clear and continue": resume once the needle sensor reads up."""
        if self.alarm:
            self._alarm_action = "clear"
            self._alarm_event.set()

    def alarm_abort(self) -> None:
        if self.alarm:
            self._alarm_action = "abort"
            self._alarm_event.set()

    async def alarm_silence(self) -> None:
        """Buzzer off; the alarm (and the wait) stays."""
        if self.alarm:
            await self._send(p.encode_outputs(False, Output.BUZZER))
            self.alarm["silenced"] = True
            self._notify()

    async def has_part(self, nozzle: int) -> bool:
        s = await self.refresh()
        return s.pressure1 if nozzle == 1 else s.pressure2

    # ------------------------------------------------------------------ safety

    async def reconnect(self) -> None:
        """Drop the controller link and connect again now (UI Connect button).
        Homing is lost: the controller may have been power-cycled."""
        self.homed = False
        await self.t.reconnect()
        self._notify()

    async def estop(self) -> None:
        """Stop every axis and switch off vacuum/pump/blow/prick immediately.

        Does not wait for the motion lock: any running move raises EStopped
        on its next state update.
        """
        self.estopped = True
        self.last_error = "E-stop pressed"
        self._needle_cmd = False  # switched off below
        self.needle_setup = False
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
