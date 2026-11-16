"""TVM802 controller wire protocol: pure encode/decode functions, no I/O.

Everything here is derived from the reverse-engineered Node.js bridge
(TVM802-WEB-API-master/TVM-manager.js). Values are raw motor steps; unit
conversion (mm, degrees) belongs to the machine layer.

All multi-byte integers on the wire are little-endian. Positions are signed
int32 (the Node code read X/Y/A as unsigned, which was a bug).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

DEFAULT_PORT = 701

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


class Cmd(IntEnum):
    """First byte of a frame sent to the controller."""

    REQUEST_STATE = 0x00
    POLL = 0x01
    INIT_05 = 0x05  # sent once on connect, meaning unknown
    SPEED_SETUP = 0x06
    SET_POSITION = 0x07  # overwrite position registers (homing / zeroing)
    MOVE = 0x08  # absolute move to target steps
    STOP = 0x09
    INIT_0B = 0x0B  # sent on connect and before homing, meaning unknown
    OUTPUTS_ON = 0x14
    OUTPUTS_OFF = 0x15
    INIT_48 = 0x48  # sent once on connect, meaning unknown


class Axis(IntFlag):
    """Axis selector bits used in MOVE / SET_POSITION / STOP frames."""

    Z = 0x02  # seesaw: + lowers nozzle 1, - lowers nozzle 2
    X = 0x04
    # Rotation motors, swapped vs the Node bridge (seen on the real machine 2026-11-16:
    # "N1" turned nozzle 2), like the vacuum outputs. Bit, frame slot and state offset of
    # each motor stay together; only which nozzle they belong to changed.
    A2 = 0x08  # nozzle 2 rotation (the Node bridge's "A1")
    Y = 0x10
    A1 = 0x20  # nozzle 1 rotation (the Node bridge's "A2")

    ALL = 0x3E


# Every single axis, in wire-slot order.
AXES: tuple[Axis, ...] = (Axis.Z, Axis.X, Axis.A2, Axis.Y, Axis.A1)

# Slot index of each axis inside a 28-byte MOVE/SET_POSITION frame.
# The int32 for an axis lives at byte 4 + slot*4.
AXIS_SLOT: dict[Axis, int] = {Axis.Z: 1, Axis.X: 2, Axis.A2: 3, Axis.Y: 4, Axis.A1: 5}

# Byte offset of each axis position inside the 44-byte state reply.
STATE_POS_OFFSET: dict[Axis, int] = {Axis.Z: 16, Axis.X: 20, Axis.A2: 24, Axis.Y: 28, Axis.A1: 32}


class Output(IntFlag):
    """Output bits. Bit 0-7 = wire byte 4, bit 8-15 = wire byte 5, etc."""

    BLOW1 = 0x0002
    # Vacuum bits as wired on the real machine (checked 2026-11-13): 0x2000 is
    # nozzle 1 and 0x0004 nozzle 2. The Node bridge had them the other way round.
    VACUUM1 = 0x2000
    PUMP = 0x0008
    PUMP_AUX = 0x0400  # always switched together with PUMP by the vendor sw
    FAST_MODE = 0x0080
    SLOW_MODE = 0x0100
    LEDS = 0x0200  # up-camera ring light
    BUZZER = 0x0800
    BLOW2 = 0x1000
    VACUUM2 = 0x0004
    PRICK = 0x4000  # feeder needle

    PUMP_BOTH = PUMP | PUMP_AUX


class Input(IntFlag):
    """Bits of state-reply byte 8 (limits + nozzle 1 pressure)."""

    LIMIT_RIGHT = 0x01
    LIMIT_LEFT = 0x02
    LIMIT_TOP = 0x04
    LIMIT_BOTTOM = 0x08
    NO_PRESSURE_1 = 0x10  # active low: set = no vacuum on nozzle 1


NO_PRESSURE_2 = 0x10  # bit in state-reply byte 10
# Feeder needle position sensor, state-reply byte 10: set = needle UP, clear = needle
# DOWN. Found on the real machine 2026-11-13: it follows the needle's physical
# position (lags the prick output by the needle's travel time), so it can catch a
# needle that did not go down or is stuck down.
NEEDLE_UP = 0x08

# Front-panel keys (found on the real machine 2026-11-16 with pnp.tvm.keyprobe): one
# bit each in state-reply bytes 9 and 10, active low (set = released, clear = held).
# The controller does nothing with them itself; the app decides what they do.
PANEL_KEYS: dict[str, tuple[int, int]] = {
    "mode": (10, 0x02),   # DotMove / ConMove
    "x-": (10, 0x01),
    "x+": (9, 0x10),
    "y+": (10, 0x04),
    "y-": (9, 0x08),
    "step": (9, 0x20),
    "start": (9, 0x80),   # Start / Stop
    "pause": (9, 0x40),   # Pause / Cont
}

STATE_REPLY_LEN = 44
MOVE_FRAME_LEN = 28

GREETING = b"OK\r\n"  # sent by the controller as soon as a client connects
POLL_REPLY_LEN = 12
ACK_LEN = 2

# The controller is strictly request/response and handles ONE command per TCP
# read: commands sent back-to-back in one packet get dropped (confirmed on the
# real machine 2026-11-12). Every command except 0x48 is answered, and the
# reply starts with the command byte.
#
# Lengths of frames *received from* the controller, keyed by first byte.
# The MOVE (0x08) ack is assumed to be 2 bytes like the other commands.
REPLY_LENGTHS: dict[int, int] = {
    0x00: STATE_REPLY_LEN,
    0x01: POLL_REPLY_LEN,  # 01 00 00 00 <outputs u32> <4 unknown bytes>
    0x05: 4,  # 05 00 03 00 (firmware version?)
    0x06: ACK_LEN,
    0x07: ACK_LEN,
    0x08: ACK_LEN,
    0x09: ACK_LEN,
    0x0B: ACK_LEN,
    0x14: ACK_LEN,
    0x15: ACK_LEN,
    GREETING[0]: len(GREETING),
}

# Commands the controller does not answer.
NO_REPLY: frozenset[int] = frozenset({0x48})

# Known lengths of frames *sent to* the controller (used by the simulator).
COMMAND_LENGTHS: dict[int, int] = {
    Cmd.REQUEST_STATE: 4,
    Cmd.POLL: 12,
    Cmd.INIT_05: 8,
    Cmd.SPEED_SETUP: 76,
    Cmd.SET_POSITION: MOVE_FRAME_LEN,
    Cmd.MOVE: MOVE_FRAME_LEN,
    Cmd.STOP: 4,
    Cmd.INIT_0B: 4,
    Cmd.OUTPUTS_ON: 8,
    Cmd.OUTPUTS_OFF: 8,
    Cmd.INIT_48: 4,
}

# Opaque speed/acceleration block the vendor software sends before every move.
# Looks like 12 small ints followed by 6 speeds (4000, 3000, 5000, 4000, 5000, 4000).
DEFAULT_SPEED_SETUP = bytes(
    [0x06, 0x00, 0x04, 0x00]
    + [0x03, 0, 0, 0, 0x03, 0, 0, 0, 0x03, 0, 0, 0, 0x04, 0, 0, 0,
       0x03, 0, 0, 0, 0x04, 0, 0, 0, 0x03, 0, 0, 0, 0x03, 0, 0, 0,
       0x03, 0, 0, 0, 0x04, 0, 0, 0, 0x03, 0, 0, 0, 0x04, 0, 0, 0]
    + [0xA0, 0x0F, 0, 0, 0xB8, 0x0B, 0, 0, 0x88, 0x13, 0, 0,
       0xA0, 0x0F, 0, 0, 0x88, 0x13, 0, 0, 0xA0, 0x0F, 0, 0]
)


# --------------------------------------------------------------------------- encoders


def _check_int32(value: int) -> int:
    value = int(value)
    if not INT32_MIN <= value <= INT32_MAX:
        raise ValueError(f"step value {value} does not fit in int32")
    return value


def encode_request_state() -> bytes:
    return bytes([Cmd.REQUEST_STATE, 0, 0, 0])


def encode_poll() -> bytes:
    return bytes([0x01, 0x00, 0x00, 0x00, 0xF4, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


def encode_outputs(on: bool, mask: Output | int) -> bytes:
    """Switch the given output bits on (0x14) or off (0x15)."""
    cmd = Cmd.OUTPUTS_ON if on else Cmd.OUTPUTS_OFF
    return bytes([cmd, 0, 0, 0]) + struct.pack("<I", int(mask) & 0xFFFFFFFF)


def _encode_axis_frame(cmd: Cmd, targets: dict[Axis, int]) -> bytes:
    if not targets:
        raise ValueError("no axes given")
    frame = bytearray(MOVE_FRAME_LEN)
    frame[0] = cmd
    selector = 0
    for axis, steps in targets.items():
        axis = Axis(axis)
        if axis not in AXIS_SLOT:
            raise ValueError(f"{axis!r} is not a single axis")
        selector |= axis
        struct.pack_into("<i", frame, 4 + AXIS_SLOT[axis] * 4, _check_int32(steps))
    frame[2] = selector
    return bytes(frame)


def encode_move(targets: dict[Axis, int]) -> bytes:
    """Absolute move of one or more axes to target step counts."""
    return _encode_axis_frame(Cmd.MOVE, targets)


def encode_set_position(targets: dict[Axis, int]) -> bytes:
    """Overwrite the controller's position register(s) without moving."""
    return _encode_axis_frame(Cmd.SET_POSITION, targets)


def encode_stop(mask: Axis | int = Axis.ALL) -> bytes:
    return bytes([Cmd.STOP, 0, int(mask) & 0xFF, 0])


def encode_speed_setup(blob: bytes = DEFAULT_SPEED_SETUP) -> bytes:
    if len(blob) != COMMAND_LENGTHS[Cmd.SPEED_SETUP] or blob[0] != Cmd.SPEED_SETUP:
        raise ValueError("speed setup blob must be 76 bytes starting with 0x06")
    return bytes(blob)


# Sequence the Node bridge sends right after connecting.
# NOTE: the 4-byte `15 00 00 00` is shorter than a normal OUTPUTS_OFF frame;
# kept verbatim because that is what is known to work on the real machine.
INIT_SEQUENCE: tuple[bytes, ...] = (
    bytes([0x05, 0, 0, 0, 0, 0, 0, 0]),
    encode_outputs(False, Output.PUMP),
    encode_outputs(True, Output.SLOW_MODE),
    encode_outputs(True, Output.LEDS),
    bytes([0x0B, 0, 0, 0]),
    bytes([0x15, 0, 0, 0]),
    bytes([0x48, 0, 0, 0]),
)

# Sequence the Node bridge sends before driving an axis onto its limit switch.
HOMING_PREP_SEQUENCE: tuple[bytes, ...] = (
    encode_stop(0x3F),  # "stop on limit hit"
    bytes([0x0B, 0, 0, 0]),
    encode_outputs(False, 0x4020),  # prick off + unknown bit 0x20
    encode_outputs(False, 0x742E),  # every pneumatic output off
    DEFAULT_SPEED_SETUP,
)


# --------------------------------------------------------------------------- decoders


@dataclass(frozen=True)
class MachineState:
    """Decoded 44-byte state reply. Positions are raw signed steps."""

    outputs: Output
    inputs: Input
    pressure1: bool  # True = vacuum detected on nozzle 1
    pressure2: bool
    positions: dict[Axis, int] = field(default_factory=dict)
    needle_up: bool = True  # feeder needle sensor (byte 10 bit 0x08)
    keys: frozenset[str] = frozenset()  # front-panel keys held down (names from PANEL_KEYS)
    raw: bytes = b""

    @property
    def speed_mode(self) -> str | None:
        fast = bool(self.outputs & Output.FAST_MODE)
        slow = bool(self.outputs & Output.SLOW_MODE)
        if fast and not slow:
            return "fast"
        if slow and not fast:
            return "slow"
        return None

    def output(self, bit: Output) -> bool:
        return bool(self.outputs & bit)

    @property
    def limits(self) -> dict[str, bool]:
        return {
            "right": bool(self.inputs & Input.LIMIT_RIGHT),
            "left": bool(self.inputs & Input.LIMIT_LEFT),
            "top": bool(self.inputs & Input.LIMIT_TOP),
            "bottom": bool(self.inputs & Input.LIMIT_BOTTOM),
        }


def decode_state(frame: bytes) -> MachineState:
    if len(frame) != STATE_REPLY_LEN or frame[0] != 0x00:
        raise ValueError(f"not a state reply: {frame.hex()}")
    outputs = Output(struct.unpack_from("<I", frame, 4)[0])
    inputs = Input(frame[8])
    positions = {axis: struct.unpack_from("<i", frame, off)[0] for axis, off in STATE_POS_OFFSET.items()}
    return MachineState(
        outputs=outputs,
        inputs=inputs,
        pressure1=not (frame[8] & Input.NO_PRESSURE_1),
        pressure2=not (frame[10] & NO_PRESSURE_2),
        positions=positions,
        needle_up=bool(frame[10] & NEEDLE_UP),
        keys=frozenset(k for k, (i, bit) in PANEL_KEYS.items() if not frame[i] & bit),
        raw=bytes(frame),
    )


def encode_state(
    outputs: int,
    positions: dict[Axis, int],
    limits: int = 0,
    pressure1: bool = False,
    pressure2: bool = False,
    needle_up: bool = True,
    keys: frozenset[str] | set[str] = frozenset(),
) -> bytes:
    """Build a state reply (used by the simulator and tests)."""
    frame = bytearray(STATE_REPLY_LEN)
    struct.pack_into("<I", frame, 4, outputs & 0xFFFFFFFF)
    frame[8] = (limits & 0x0F) | (0 if pressure1 else Input.NO_PRESSURE_1)
    frame[10] = (0 if pressure2 else NO_PRESSURE_2) | (NEEDLE_UP if needle_up else 0)
    for name, (i, bit) in PANEL_KEYS.items():  # keys are active low: released = bit set
        if name not in keys:
            frame[i] |= bit
    for axis, off in STATE_POS_OFFSET.items():
        struct.pack_into("<i", frame, off, _check_int32(positions.get(axis, 0)))
    return bytes(frame)


@dataclass(frozen=True)
class Frame:
    type: int
    data: bytes
    known: bool  # False = length unknown, data is "whatever was buffered"


class FrameReassembler:
    """Cuts a TCP byte stream into frames.

    TCP may split one reply across reads or merge several replies into one
    read; the Node bridge dropped both cases. Frames whose type has a known
    length are cut exactly. For an unknown type there is no way to tell where
    it ends, so everything buffered is emitted as one `known=False` frame
    (logged by the transport so new reply types can be identified).
    """

    def __init__(self, lengths: dict[int, int] | None = None):
        self.lengths = dict(REPLY_LENGTHS if lengths is None else lengths)
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        frames: list[Frame] = []
        while self._buf:
            ftype = self._buf[0]
            length = self.lengths.get(ftype)
            if length is None:
                frames.append(Frame(ftype, bytes(self._buf), known=False))
                self._buf.clear()
                break
            if len(self._buf) < length:
                break
            frames.append(Frame(ftype, bytes(self._buf[:length]), known=True))
            del self._buf[:length]
        return frames

    @property
    def pending(self) -> int:
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()
