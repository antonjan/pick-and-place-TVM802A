"""Simulated TVM802 controller.

Speaks the same TCP protocol as the real controller so the whole stack can be
developed without the machine. The physics are deliberately simple:

  * every axis moves toward its target at a constant speed;
  * the controller position *register* is separate from the *physical*
    position (SET_POSITION only changes the register), so homing works the
    same way as on the real machine;
  * X and Y hit limit switches at the ends of travel, stop, and set the
    matching limit bit;
  * vacuum on a lowered nozzle "picks" a part, which makes the pressure
    sensor read true until vacuum is switched off.

Protocol behaviour copied from the real controller: "OK\\r\\n" greeting,
2-byte acks, one command per TCP read (extra bytes in the same read are
dropped). 0x05 / 0x0B / 0x48 and SPEED_SETUP are acknowledged but ignored.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from pnp.tvm import protocol as p

log = logging.getLogger(__name__)

STEPS_PER_MM = 32808
STEPS_PER_DEG = 4444.44


@dataclass
class SimConfig:
    # Physical travel in steps (0 = min end). X/Y max match the Node bridge's
    # home register values, so after homing register == physical position.
    x_travel: int = 9_843_000
    y_travel: int = 11_532_000
    z_travel: int = 15 * STEPS_PER_MM  # seesaw travel each way from centre
    # Speeds in steps per second.
    xy_speed: float = 300 * STEPS_PER_MM
    z_speed: float = 100 * STEPS_PER_MM
    a_speed: float = 720 * STEPS_PER_DEG
    tick: float = 0.002
    # Physical power-on position (steps). Registers start at 0 regardless.
    start_x: int = 150 * STEPS_PER_MM
    start_y: int = 150 * STEPS_PER_MM
    # A nozzle counts as "down on a part" beyond this depth.
    pick_depth: int = 2 * STEPS_PER_MM
    # Set False to simulate failed picks.
    pick_success: bool = True
    # Moves happen in whole multiples of this many steps (distance rounded up),
    # as measured on the real controller.
    step_quantum: int = 1000
    # Feeder needle: seconds the needle takes to reach the commanded position (the
    # real sensor lags the output by about this much), and fault injection for
    # tests: "up" = never goes down, "down" = sticks down once it is down.
    needle_travel: float = 0.06
    needle_stuck: str | None = None
    # Seconds after an axis stops (stop command or limit switch) during which the
    # controller acknowledges but IGNORES new moves for that axis - the suspected
    # cause of homing sometimes sticking on the Y switch (2026-11-13). 0 = off.
    move_ignored_after_stop: float = 0.0


@dataclass
class _AxisSim:
    phys: float
    target: float
    offset: int  # register = phys + offset
    speed: float
    lo: float | None = None
    hi: float | None = None

    @property
    def register(self) -> int:
        return int(round(self.phys)) + self.offset


@dataclass
class SimMachine:
    cfg: SimConfig = field(default_factory=SimConfig)

    def __post_init__(self) -> None:
        c = self.cfg
        self.axes: dict[p.Axis, _AxisSim] = {
            p.Axis.X: _AxisSim(c.start_x, c.start_x, -c.start_x, c.xy_speed, 0, c.x_travel),
            p.Axis.Y: _AxisSim(c.start_y, c.start_y, -c.start_y, c.xy_speed, 0, c.y_travel),
            p.Axis.Z: _AxisSim(0, 0, 0, c.z_speed, -c.z_travel, c.z_travel),
            p.Axis.A1: _AxisSim(0, 0, 0, c.a_speed),
            p.Axis.A2: _AxisSim(0, 0, 0, c.a_speed),
        }
        self.outputs = 0
        self.needle_down = False  # physical needle position (what the sensor reports)
        self.keys: set[str] = set()  # front-panel keys held down (tests / UI simulate presses)
        self.clock = 0.0  # simulated seconds
        self._stop_t: dict = {}  # axis -> clock when it last stopped
        self.ignored_moves = 0
        self._needle_timer = 0.0
        self.part_on = {1: False, 2: False}
        self.commands: list[bytes] = []  # log of every command received
        self.burst_violations = 0  # commands that arrived bundled in one packet
        # Physical events in register mm (see docs/phase3-design.md section 5).
        self.events: list[tuple] = []
        # Called with (event, {"a1": deg, "a2": deg}) right after each pick or release event.
        self.listeners: list[Callable[[tuple, dict[str, float]], None]] = []

    # ------------------------------------------------------------------ physics

    def step(self, dt: float) -> None:
        self.clock += dt
        for axis, ax in self.axes.items():
            delta = ax.target - ax.phys
            if delta == 0:
                continue
            move = ax.speed * dt
            ax.phys = ax.target if abs(delta) <= move else ax.phys + (move if delta > 0 else -move)
            if ax.hi is not None and ax.phys >= ax.hi:
                ax.phys = ax.target = ax.hi  # hit the end switch: motor stops
                self._stop_t[axis] = self.clock
            if ax.lo is not None and ax.phys <= ax.lo:
                ax.phys = ax.target = ax.lo
                self._stop_t[axis] = self.clock
        self._update_needle(dt)
        self._update_parts()

    def _update_needle(self, dt: float) -> None:
        want_down = bool(self.outputs & p.Output.PRICK)
        if want_down == self.needle_down:
            self._needle_timer = 0.0
            return
        if want_down and self.cfg.needle_stuck == "up":
            return  # jammed: never goes down
        if not want_down and self.cfg.needle_stuck == "down":
            return  # stuck in the tape: never comes up
        self._needle_timer += dt
        if self._needle_timer >= self.cfg.needle_travel:
            self.needle_down = want_down
            self._needle_timer = 0.0

    def _nozzle_down(self, n: int) -> bool:
        z = self.axes[p.Axis.Z].phys
        return z >= self.cfg.pick_depth if n == 1 else z <= -self.cfg.pick_depth

    def _head_mm(self) -> tuple[float, float, float]:
        """Controller register positions (x, y, z) in mm."""
        return (self.axes[p.Axis.X].register / STEPS_PER_MM,
                self.axes[p.Axis.Y].register / STEPS_PER_MM,
                self.axes[p.Axis.Z].register / STEPS_PER_MM)

    def nozzle_angle(self, n: int) -> float:
        """Physical angle of nozzle n (A1 or A2) in degrees."""
        axis = p.Axis.A1 if n == 1 else p.Axis.A2
        return self.axes[axis].phys / STEPS_PER_DEG

    def _emit(self, event: tuple) -> None:
        self.events.append(event)
        angles = {"a1": self.nozzle_angle(1), "a2": self.nozzle_angle(2)}
        for listener in list(self.listeners):
            listener(event, angles)

    def _update_parts(self) -> None:
        for n, vac in ((1, p.Output.VACUUM1), (2, p.Output.VACUUM2)):
            if not self.outputs & vac:
                if self.part_on[n]:
                    x, y, z = self._head_mm()
                    self._emit(("release", n, x, y, z))
                self.part_on[n] = False
            elif self._nozzle_down(n) and self.cfg.pick_success:
                if not self.part_on[n]:
                    x, y, _ = self._head_mm()
                    self._emit(("pick", n, x, y))
                self.part_on[n] = True

    @property
    def limits(self) -> int:
        bits = 0
        x, y = self.axes[p.Axis.X], self.axes[p.Axis.Y]
        if x.phys >= x.hi:
            bits |= p.Input.LIMIT_RIGHT
        if x.phys <= x.lo:
            bits |= p.Input.LIMIT_LEFT
        if y.phys >= y.hi:
            bits |= p.Input.LIMIT_TOP
        if y.phys <= y.lo:
            bits |= p.Input.LIMIT_BOTTOM
        return bits

    def is_moving(self) -> bool:
        return any(ax.phys != ax.target for ax in self.axes.values())

    # ------------------------------------------------------------------ commands

    def state_frame(self) -> bytes:
        vac_ok = lambda n, bit: bool(self.outputs & bit) and self.part_on[n]
        return p.encode_state(
            self.outputs,
            {a: ax.register for a, ax in self.axes.items()},
            limits=self.limits,
            pressure1=vac_ok(1, p.Output.VACUUM1),
            pressure2=vac_ok(2, p.Output.VACUUM2),
            needle_up=not self.needle_down,
            keys=self.keys,
        )

    def handle(self, frame: bytes) -> bytes | None:
        """Apply one command frame; return a reply frame or None."""
        self.commands.append(frame)
        cmd = frame[0]
        outputs_before = self.outputs
        if cmd == p.Cmd.REQUEST_STATE:
            return self.state_frame()
        if cmd in (p.Cmd.MOVE, p.Cmd.SET_POSITION):
            mask = frame[2]
            for axis, slot in p.AXIS_SLOT.items():
                if mask & axis:
                    value = struct.unpack_from("<i", frame, 4 + slot * 4)[0]
                    ax = self.axes[axis]
                    if cmd == p.Cmd.MOVE and self.cfg.move_ignored_after_stop > 0 and \
                            self.clock - self._stop_t.get(axis, -1e9) < self.cfg.move_ignored_after_stop:
                        self.ignored_moves += 1  # still busy stopping: acked but ignored
                        continue
                    if cmd == p.Cmd.MOVE:
                        # Real controller: distance rounded UP to whole quanta.
                        q = self.cfg.step_quantum
                        delta = value - ax.register
                        if q > 1 and delta:
                            delta = (1 if delta > 0 else -1) * -(-abs(delta) // q) * q
                        ax.target = ax.register + delta - ax.offset
                        if ax.lo is not None:
                            ax.target = min(max(ax.target, ax.lo), ax.hi)
                    else:
                        ax.offset = value - int(round(ax.phys))
        elif cmd == p.Cmd.STOP:
            mask = frame[2]
            for axis, ax in self.axes.items():
                if mask & axis:
                    ax.target = ax.phys
                    self._stop_t[axis] = self.clock
        elif cmd == p.Cmd.OUTPUTS_ON and len(frame) == 8:
            self.outputs |= struct.unpack_from("<I", frame, 4)[0]
        elif cmd == p.Cmd.OUTPUTS_OFF and len(frame) == 8:
            self.outputs &= ~struct.unpack_from("<I", frame, 4)[0]
        if (outputs_before ^ self.outputs) & p.Output.PRICK:
            x, y, _ = self._head_mm()
            self.events.append(("prick_down" if self.outputs & p.Output.PRICK else "prick_up", x, y))
        # Replies as observed on the real controller (2026-11-12).
        if cmd == p.Cmd.INIT_05:
            return bytes([0x05, 0x00, 0x03, 0x00])
        if cmd == p.Cmd.POLL:
            return bytes([0x01, 0, 0, 0]) + struct.pack("<I", self.outputs & 0xFFFFFFFF) + bytes(4)
        if cmd in p.NO_REPLY:
            return None
        return bytes([cmd, 0x00])


class TvmSimulator:
    """TCP server wrapping a SimMachine."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7001, cfg: SimConfig | None = None):
        self.host = host
        self.port = port
        self.machine = SimMachine(cfg or SimConfig())
        self._server: asyncio.base_events.Server | None = None
        self._physics: asyncio.Task | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        # True = behave like a powered-off controller: keep sockets open but
        # never answer (what a power cycle looks like from the PC).
        self.mute = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # resolves port 0
        self._physics = asyncio.create_task(self._physics_loop())
        log.info("TVM simulator listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._physics:
            self._physics.cancel()
        for w in list(self._clients):
            w.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def drop_clients(self) -> None:
        """Forcefully disconnect every client (for reconnect testing)."""
        for w in list(self._clients):
            w.close()

    async def _physics_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self.machine.cfg.tick)
            now = loop.time()
            self.machine.step(now - last)
            last = now

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Like the real controller: greet, then treat each TCP read as ONE
        command. Anything after the first frame in a read is dropped and
        counted in `machine.burst_violations` (tests assert it stays 0)."""
        self._clients.add(writer)
        log.info("simulator: client connected")
        writer.write(p.GREETING)
        try:
            while data := await reader.read(4096):
                if self.mute:
                    continue
                length = p.COMMAND_LENGTHS.get(data[0])
                if length is None:
                    log.warning("simulator: unknown command %s", data.hex(" "))
                    continue
                # The init sequence's `15 00 00 00` is shorter than a normal 0x15.
                frame = data[:length] if len(data) >= length else data
                if len(data) > len(frame):
                    self.machine.burst_violations += 1
                    log.warning("simulator: dropped %d bytes sent in the same packet as %s",
                                len(data) - len(frame), frame.hex(" "))
                reply = self.machine.handle(frame)
                if reply:
                    writer.write(reply)
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            log.info("simulator: client disconnected")
