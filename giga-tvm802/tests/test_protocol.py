"""Protocol encode/decode tests.

Expected bytes are copied from the working Node bridge (TVM-manager.js) and the
hand-written frames in its backup.js, which were captured from the vendor sw.
"""

import pytest

from pnp.tvm import protocol as p
from pnp.tvm.protocol import Axis, Output


def h(s: str) -> bytes:
    return bytes.fromhex(s)


@pytest.mark.parametrize(
    "on, mask, expected",
    [
        (True, Output.BUZZER, "14 00 00 00 00 08 00 00"),
        (False, Output.BUZZER, "15 00 00 00 00 08 00 00"),
        (True, Output.PRICK, "14 00 00 00 00 40 00 00"),
        (True, Output.PUMP_BOTH, "14 00 00 00 08 04 00 00"),
        (False, Output.PUMP_BOTH, "15 00 00 00 08 04 00 00"),
        (True, Output.BLOW1, "14 00 00 00 02 00 00 00"),
        (True, Output.BLOW2, "14 00 00 00 00 10 00 00"),
        # swapped vs the Node bridge: checked on the real machine 2026-11-13
        (True, Output.VACUUM1, "14 00 00 00 00 20 00 00"),
        (True, Output.VACUUM2, "14 00 00 00 04 00 00 00"),
        (True, Output.LEDS, "14 00 00 00 00 02 00 00"),
    ],
)
def test_outputs_match_node_bridge(on, mask, expected):
    assert p.encode_outputs(on, mask) == h(expected)


def test_stop_frames():
    assert p.encode_stop(Axis.X) == h("09 00 04 00")
    assert p.encode_stop(Axis.Y) == h("09 00 10 00")
    assert p.encode_stop(Axis.Z) == h("09 00 02 00")
    assert p.encode_stop(Axis.A1) == h("09 00 08 00")
    assert p.encode_stop(0x3F) == h("09 00 3f 00")


def test_move_y_far_matches_vendor_capture():
    # backup.js GoHomeYDirCommand: Y to 32 808 000 steps (1000 mm)
    expected = bytes([0x08, 0, 0x10, 0] + [0] * 16 + [0x40, 0x9C, 0xF4, 0x01, 0, 0, 0, 0])
    assert p.encode_move({Axis.Y: 32_808_000}) == expected


def test_set_y_home_register_matches_vendor_capture():
    # backup.js SetYHome: register Y = 11 532 000
    expected = bytes([0x07, 0, 0x10, 0] + [0] * 16 + [0xE0, 0xF6, 0xAF, 0x00, 0, 0, 0, 0])
    assert p.encode_set_position({Axis.Y: 11_532_000}) == expected


def test_multi_axis_move_layout():
    frame = p.encode_move({Axis.X: 1, Axis.Z: -2, Axis.A2: 3})
    assert len(frame) == 28
    assert frame[2] == Axis.X | Axis.Z | Axis.A2
    assert frame[8:12] == (-2).to_bytes(4, "little", signed=True)  # Z slot 1
    assert frame[12:16] == (1).to_bytes(4, "little")  # X slot 2
    assert frame[24:28] == (3).to_bytes(4, "little")  # A2 slot 5
    assert frame[16:24] == bytes(8)


def test_move_rejects_bad_input():
    with pytest.raises(ValueError):
        p.encode_move({})
    with pytest.raises(ValueError):
        p.encode_move({Axis.X: 2**31})
    with pytest.raises(ValueError):
        p.encode_move({Axis.ALL: 0})


def test_speed_setup_default_is_76_bytes():
    assert len(p.DEFAULT_SPEED_SETUP) == 76
    assert p.encode_speed_setup() == p.DEFAULT_SPEED_SETUP


def test_state_roundtrip_signed_positions():
    pos = {Axis.X: -5, Axis.Y: 11_532_000, Axis.Z: -32808, Axis.A1: 1_600_000, Axis.A2: -1}
    frame = p.encode_state(int(Output.VACUUM1 | Output.SLOW_MODE), pos,
                           limits=p.Input.LIMIT_TOP, pressure1=True, pressure2=False)
    s = p.decode_state(frame)
    assert s.positions == pos
    assert s.pressure1 is True and s.pressure2 is False
    assert s.limits == {"right": False, "left": False, "top": True, "bottom": False}
    assert s.output(Output.VACUUM1) and not s.output(Output.VACUUM2)
    assert s.speed_mode == "slow"


def test_state_offsets_match_node_bridge():
    # Node reads Z at hex chars 32..40 (bytes 16..19), X 40..48, A1 48..56, Y 56..64, A2 64..72
    frame = bytearray(44)
    frame[16:20] = (1).to_bytes(4, "little")
    frame[20:24] = (2).to_bytes(4, "little")
    frame[24:28] = (3).to_bytes(4, "little")
    frame[28:32] = (4).to_bytes(4, "little")
    frame[32:36] = (5).to_bytes(4, "little")
    frame[8] = 0x10  # no pressure on N1
    s = p.decode_state(bytes(frame))
    assert s.positions == {Axis.Z: 1, Axis.X: 2, Axis.A1: 3, Axis.Y: 4, Axis.A2: 5}
    assert s.pressure1 is False and s.pressure2 is True


def test_decode_rejects_wrong_frame():
    with pytest.raises(ValueError):
        p.decode_state(bytes(43))
    with pytest.raises(ValueError):
        p.decode_state(b"\x01" + bytes(43))


class TestReassembler:
    state = p.encode_state(0, {})

    def test_split_frame(self):
        r = p.FrameReassembler()
        assert r.feed(self.state[:10]) == []
        assert r.pending == 10
        frames = r.feed(self.state[10:])
        assert len(frames) == 1 and frames[0].data == self.state and frames[0].known

    def test_merged_frames(self):
        r = p.FrameReassembler()
        frames = r.feed(self.state * 3 + self.state[:5])
        assert len(frames) == 3 and r.pending == 5

    def test_unknown_type_flushes_buffer(self):
        r = p.FrameReassembler()
        frames = r.feed(self.state + b"\x77\x02\x03")
        assert [f.known for f in frames] == [True, False]
        assert frames[1].data == b"\x77\x02\x03" and r.pending == 0

    def test_real_controller_capture(self):
        # Bytes received from the real TVM802 on 2026-11-12 when the init
        # sequence was sent in one burst.
        r = p.FrameReassembler()
        frames = r.feed(bytes.fromhex("4f4b0d0a 05000300 1500 1400 0b00".replace(" ", "")))
        assert [f.data.hex() for f in frames] == ["4f4b0d0a", "05000300", "1500", "1400", "0b00"]
        poll = bytes.fromhex("01000000e003000000000000")
        assert r.feed(poll)[0].data == poll

    def test_custom_lengths(self):
        r = p.FrameReassembler({0x00: 44, 0x01: 4})
        frames = r.feed(b"\x01abc" + self.state)
        assert [f.type for f in frames] == [0x01, 0x00]


def test_needle_sensor_bit_real_frames():
    """Byte 10 bit 0x08 = needle UP (captured on the real machine 2026-11-13)."""
    up = bytes.fromhex("00000000c0030000f0ffff03" + "00" * 32)
    down = bytes.fromhex("00000000c0430000f0fff703" + "00" * 32)
    assert p.decode_state(up).needle_up is True
    assert p.decode_state(down).needle_up is False
    # pressure2 shares byte 10 and must be unaffected
    assert p.decode_state(down).pressure2 is False
    frame = p.encode_state(0, {}, needle_up=False, pressure2=True)
    s = p.decode_state(frame)
    assert s.needle_up is False and s.pressure2 is True
