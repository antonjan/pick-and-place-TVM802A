"""Simulated part physics: held parts follow the nozzle, land where the physics says, and render on the up camera.

No hardware needed. Uses the real SimMachine (listeners) and crafted events.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from pnp.sim.sim_vision import (
    SIM_NOZZLE_OFFSETS,
    SIM_SIZE,
    SIM_UP_CAMERA_XY,
    SIM_UP_PX_PER_MM,
    SimHeldPart,
    SimPartPhysics,
    _add_noise,
    _noise_bank,
    _pt,
    _SCALE,
    _SHIFT,
    _rot,
    make_up_renderer,
    sim_up_A,
)
from pnp.sim.tvm_sim import STEPS_PER_DEG, SimMachine
from pnp.tvm import protocol as p

W_PX, H_PX = SIM_SIZE
CENTRE = np.array([W_PX / 2, H_PX / 2])
U = np.array(SIM_UP_CAMERA_XY, dtype=float)
M = sim_up_A()  # pixel = centre + M @ (T - U)
T1 = np.array(SIM_NOZZLE_OFFSETS[1], dtype=float)
PART_SIZE = (3.0, 2.0)  # long, short mm: the bare tip lies fully inside the body


def _fake_machine() -> SimpleNamespace:
    return SimpleNamespace(listeners=[])


def _set_held(phys: SimPartPhysics, n: int, offset, angle: float, a_pick: float) -> None:
    phys.held[n] = SimHeldPart(size=PART_SIZE, offset=tuple(offset), angle=angle, a_pick=a_pick)


def _wrap(a: float) -> float:
    """Expected wrap into (-180, 180] for hand-checked values."""
    return 180.0 - ((180.0 - a) % 360.0)


# ---------------------------------------------------------------- listeners on a real SimMachine

def test_listeners_called_with_physical_angles_and_events_unchanged():
    sim = SimMachine()
    seen = []
    sim.listeners.append(lambda ev, ang: seen.append((ev, dict(ang))))
    sim.outputs |= p.Output.VACUUM1
    sim.axes[p.Axis.Z].phys = sim.axes[p.Axis.Z].target = sim.cfg.pick_depth
    sim.step(0.0)
    sim.axes[p.Axis.A1].phys = sim.axes[p.Axis.A1].target = 90 * STEPS_PER_DEG
    sim.outputs &= ~p.Output.VACUUM1
    sim.step(0.0)

    assert [ev[0] for ev, _ in seen] == ["pick", "release"]
    assert seen[0][1]["a1"] == pytest.approx(0.0)
    assert seen[1][1]["a1"] == pytest.approx(90.0, abs=1e-3)
    assert seen[1][1]["a2"] == pytest.approx(0.0)
    assert sim.nozzle_angle(1) == pytest.approx(90.0, abs=1e-3)
    assert sim.events == [ev for ev, _ in seen]


def test_real_machine_pick_then_turn_places_rotated_part():
    sim = SimMachine()
    phys = SimPartPhysics(sim, lambda n, x, y: (PART_SIZE, 10.0), seed=5)
    sim.outputs |= p.Output.VACUUM1
    sim.axes[p.Axis.Z].phys = sim.axes[p.Axis.Z].target = sim.cfg.pick_depth
    sim.step(0.0)  # pick with the head at register (0, 0)
    held = phys.held[1]
    assert held.a_pick == pytest.approx(0.0)

    sim.axes[p.Axis.A1].phys = sim.axes[p.Axis.A1].target = 90 * STEPS_PER_DEG
    sim.outputs &= ~p.Output.VACUUM1
    sim.step(0.0)  # release, nozzle turned 90 deg

    rec = phys.placed[0]
    tip = np.array(SIM_NOZZLE_OFFSETS[1], dtype=float)
    ox, oy = held.offset
    assert rec["centre"] == pytest.approx([tip[0] - oy, tip[1] + ox], abs=1e-6)  # rot(90) @ offset
    assert rec["angle"] == pytest.approx(_wrap(held.angle + 90.0), abs=1e-6)


# ---------------------------------------------------------------- physics from crafted events

def test_pick_then_release_without_turn_lands_at_tip_plus_offset():
    m = _fake_machine()
    phys = SimPartPhysics(m, lambda n, x, y: (PART_SIZE, 30.0), seed=1)
    phys.on_event(("pick", 1, 10.0, 20.0), {"a1": 0.0, "a2": 0.0})
    part = phys.held[1]
    assert abs(part.angle - 30.0) <= 4.0
    assert math.hypot(*part.offset) <= 0.3 + 1e-12
    phys.on_event(("release", 1, 10.0, 20.0, 0.0), {"a1": 0.0, "a2": 0.0})

    rec = phys.placed[0]
    tip = np.array([10.0 - 42.0, 20.0 - 3.0])
    assert rec["centre"] == pytest.approx(list(tip + np.array(part.offset)), abs=1e-9)
    assert rec["angle"] == pytest.approx(part.angle)
    assert rec["nozzle"] == 1 and rec["size"] == PART_SIZE
    assert 1 not in phys.held


def test_turn_of_90_deg_rotates_offset_and_angle_by_hand():
    m = _fake_machine()
    phys = SimPartPhysics(m, None, seed=1)  # registers itself on m
    _set_held(phys, 1, (0.2, 0.0), angle=10.0, a_pick=0.0)
    phys.on_event(("release", 1, 0.0, 0.0, 0.0), {"a1": 90.0, "a2": 0.0})

    # tip = (0 - 42, 0 - 3); rot(90) @ (0.2, 0) = (0, 0.2) -> centre (-42, -2.8); angle 10 + 90
    rec = phys.placed[0]
    assert rec["centre"] == pytest.approx([-42.0, -2.8], abs=1e-12)
    assert rec["angle"] == pytest.approx(100.0)


def test_placed_angle_wraps_into_minus180_180():
    m = _fake_machine()
    phys = SimPartPhysics(m, None, seed=1)
    _set_held(phys, 2, (0.0, 0.0), angle=170.0, a_pick=0.0)
    phys.on_event(("release", 2, 0.0, 0.0, 0.0), {"a1": 0.0, "a2": 40.0})
    assert phys.placed[0]["angle"] == pytest.approx(-150.0)  # 170 + 40 = 210, wrapped


def test_release_without_held_part_records_nothing():
    m = _fake_machine()
    phys = SimPartPhysics(m, None, seed=1)
    phys.on_event(("release", 1, 0.0, 0.0, 0.0), {"a1": 0.0, "a2": 0.0})
    assert phys.placed == []


def test_drop_next_pick_holds_nothing_and_resets():
    m = _fake_machine()
    phys = SimPartPhysics(m, None, seed=1)
    phys.drop_next = True
    phys.on_event(("pick", 1, 0.0, 0.0), {"a1": 0.0, "a2": 0.0})
    assert phys.held == {}
    assert phys.drop_next is False
    phys.on_event(("release", 1, 0.0, 0.0, 0.0), {"a1": 0.0, "a2": 0.0})
    assert phys.placed == []

    phys.on_event(("pick", 1, 0.0, 0.0), {"a1": 0.0, "a2": 0.0})  # the next pick holds a part again
    assert 1 in phys.held


def test_default_part_when_no_feeder_near_tip():
    m = _fake_machine()
    phys = SimPartPhysics(m, lambda n, x, y: None, seed=1)
    phys.on_event(("pick", 2, 0.0, 0.0), {"a1": 0.0, "a2": 0.0})
    assert phys.held[2].size == (2.0, 1.25)
    assert abs(phys.held[2].angle) <= 4.0


def test_view_reports_turn_since_pick():
    m = _fake_machine()
    phys = SimPartPhysics(m, None, seed=1)
    _set_held(phys, 1, (0.0, 0.0), angle=0.0, a_pick=15.0)
    _, phi = phys.view(1, 40.0)
    assert phi == pytest.approx(25.0)
    assert phys.view(2, 0.0) is None


# ---------------------------------------------------------------- renderer

def _frame(physics=None, angles=None) -> np.ndarray:
    pos = tuple(U - T1)  # N1 tip at the image centre; N2 is off screen in this view
    return make_up_renderer(lambda: pos, physics, angles)()


def _bright_centroid(img: np.ndarray) -> np.ndarray:
    """Coverage-weighted centroid of the part (continuous convention). A hard threshold on a rotated
    rectangle samples the pixel lattice unevenly (~0.15 px), so weight by anti-aliased coverage instead."""
    g = img[..., 0].astype(float)
    w = np.where(g > 60, g - 20.0, 0.0)  # background is 20, the part 225; noise sigma 2 is cut off
    ys, xs = np.mgrid[0:g.shape[0], 0:g.shape[1]]
    total = w.sum()
    assert total > 100 * 205
    return np.array([(w * xs).sum() / total + 0.5, (w * ys).sum() / total + 0.5])


def _long_side(img: np.ndarray) -> tuple[float, float]:
    """Direction of the long side (deg, mod 180) and its length (px) of the bright region."""
    ys, xs = np.nonzero(img[..., 0] > 200)
    pts = np.column_stack([xs, ys]).astype(np.float32)
    box = cv2.boxPoints(cv2.minAreaRect(pts))
    edges = [box[(i + 1) % 4] - box[i] for i in range(4)]
    longest = max(edges, key=lambda e: float(np.hypot(*e)))
    return math.degrees(math.atan2(longest[1], longest[0])) % 180.0, float(np.hypot(*longest))


def _mod180_diff(a: float, b: float) -> float:
    return ((a - b + 90.0) % 180.0) - 90.0


@pytest.mark.parametrize("offset, axis, turn", [
    ((0.1, -0.05), 30.0, 0.0),
    ((-0.12, 0.08), 30.0, 20.0),
])
def test_renderer_draws_held_part_at_expected_pixel_and_angle(offset, axis, turn):
    m = _fake_machine()
    phys = SimPartPhysics(m, None, seed=1)
    _set_held(phys, 1, offset, angle=axis, a_pick=0.0)
    img = _frame(phys, lambda: {"a1": turn, "a2": 0.0})

    c_w = U + _rot(turn) @ np.asarray(offset)  # tip is at U
    expected_px = CENTRE + M @ (c_w - U)
    got = _bright_centroid(img)
    assert np.linalg.norm(got - expected_px) < 0.1, (got, expected_px)

    ang, length_px = _long_side(img)
    assert abs(_mod180_diff(ang, axis + turn)) < 0.5, (ang, axis + turn)
    assert abs(length_px - PART_SIZE[0] * SIM_UP_PX_PER_MM) < 1.5


def test_renderer_with_no_held_part_equals_no_physics_frame():
    phys = SimPartPhysics(_fake_machine(), None, seed=1)
    assert np.array_equal(_frame(), _frame(phys, lambda: {"a1": 0.0, "a2": 0.0}))


def test_drop_next_renders_bare_tip():
    phys = SimPartPhysics(_fake_machine(), None, seed=1)
    phys.drop_next = True
    phys.on_event(("pick", 1, U[0] - T1[0], U[1] - T1[1]), {"a1": 0.0, "a2": 0.0})
    assert np.array_equal(_frame(phys, lambda: {"a1": 0.0, "a2": 0.0}), _frame())


def test_renderer_without_physics_draws_n1_tip_as_before():
    # Reference: N1 disc (0.4 mm radius, grey 230) on the 20 grey background, with the renderer's noise
    # sequence. N2 is off screen in this view, so the whole frame must match.
    tip_r = 0.4 * SIM_UP_PX_PER_MM
    canvas = np.full((H_PX, W_PX, 3), 20, dtype=np.uint8)
    cv2.circle(canvas, _pt(CENTRE), int(round(tip_r * _SCALE)), (230,) * 3, -1, cv2.LINE_AA, _SHIFT)
    rng = np.random.default_rng(2)
    expected = _add_noise(canvas, _noise_bank(rng), rng)
    assert np.array_equal(_frame(), expected)
