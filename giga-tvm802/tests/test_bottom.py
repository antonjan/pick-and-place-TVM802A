"""Bottom vision: measuring parts on the up camera, plus the vision config section.

Images are drawn (anti-aliased, like a real camera) from world geometry (mm from the nozzle axis) through the simulator's
up-camera mapping, so the expected values are known exactly.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pnp.config import VisionConfig, load_config
from pnp.machine.machine import OUTPUTS
from pnp.sim.sim_vision import sim_up_A
from pnp.vision.bottom import InspectError, angle_error, inspect_nozzle, measure_part

W, H = 640, 480
_SHIFT = 4
_SCALE = 1 << _SHIFT
_BG = 20
_FG = 225


def _to_px(q, A) -> np.ndarray:
    """World mm (N, 2) from the nozzle axis -> pixel-index coordinates (continuous minus 0.5)."""
    centre = np.array([W / 2.0, H / 2.0])
    return centre - 0.5 + (np.asarray(q, dtype=float) @ np.asarray(A, dtype=float).T)


def _rot(deg: float) -> np.ndarray:
    t = math.radians(deg)
    return np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])


def rect_poly(cx, cy, length, width, ang, lx=0.0, ly=0.0) -> np.ndarray:
    """Corners (mm) of a rectangle whose local x side is `length`, rotated by `ang`.

    The centre is (cx, cy) + R(ang) @ (lx, ly).
    """
    R = _rot(ang)
    c = np.array([cx, cy], dtype=float) + R @ np.array([lx, ly], dtype=float)
    corners = np.array([[length / 2, width / 2], [-length / 2, width / 2],
                        [-length / 2, -width / 2], [length / 2, -width / 2]])
    return corners @ R.T + c


_SS = 4  # supersampling per axis for anti-aliased edges
_SUB = (np.arange(_SS) + 0.5) / _SS - 0.5  # sub-sample offsets inside a pixel


def _bbox(v: np.ndarray, margin: int = 2) -> tuple[int, int, int, int]:
    x0 = max(int(math.floor(v[:, 0].min())) - margin, 0)
    x1 = min(int(math.ceil(v[:, 0].max())) + margin, W)
    y0 = max(int(math.floor(v[:, 1].min())) - margin, 0)
    y1 = min(int(math.ceil(v[:, 1].max())) + margin, H)
    return x0, x1, y0, y1


def _blend(canvas: np.ndarray, x0: int, x1: int, y0: int, y1: int, inside_fn, value: float) -> None:
    """Anti-alias a shape onto canvas: coverage by supersampling, then a linear blend."""
    gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
    cov = np.zeros(gx.shape)
    for oy in _SUB:
        for ox in _SUB:
            cov += inside_fn(gx + ox, gy + oy)
    cov /= _SS * _SS
    canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1.0 - cov) + value * cov


def _draw_poly(canvas, poly_mm, A, value=_FG):
    """Anti-aliased convex polygon (world mm from the axis)."""
    v = _to_px(poly_mm, A) + 0.5  # continuous vertices
    if np.sum(v[:, 0] * np.roll(v[:, 1], -1) - np.roll(v[:, 0], -1) * v[:, 1]) < 0:
        v = v[::-1]

    def inside(px, py):
        ok = np.ones(px.shape, dtype=bool)
        for a, b in zip(v, np.roll(v, -1, axis=0)):
            ok &= (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0]) >= 0
        return ok.astype(float)

    _blend(canvas, *_bbox(v), inside, value)


def _draw_disc(canvas, centre_mm, diameter_mm, A, value=_FG):
    c = np.asarray(_to_px(np.asarray(centre_mm, dtype=float).reshape(1, 2), A)[0]) + 0.5
    r = diameter_mm / 2 * float(np.sqrt(abs(np.linalg.det(A))))
    ring = np.array([[c[0] - r, c[1] - r], [c[0] + r, c[1] + r]])

    def inside(px, py):
        return (np.hypot(px - c[0], py - c[1]) <= r).astype(float)

    _blend(canvas, *_bbox(ring), inside, value)


def _frame(shapes, seed: int = 1) -> np.ndarray:
    canvas = np.full((H, W), float(_BG))
    for kind, args in shapes:
        if kind == "poly":
            _draw_poly(canvas, args, sim_up_A())
        else:
            _draw_disc(canvas, args[0], args[1], sim_up_A())
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 2.0, canvas.shape)
    grey = np.clip(canvas + noise, 0, 255).astype(np.uint8)
    return np.dstack([grey, grey, grey])


def _soic_polys(cx, cy, ang) -> list[np.ndarray]:
    """SOIC-8 body 4.9 x 3.9 with 8 leads (0.4 x 1.05) on two rows; envelope 4.9 x 6.0.

    The long side is the local y axis, so the envelope's long side is at ang + 90.
    """
    polys = [rect_poly(cx, cy, 4.9, 3.9, ang)]
    for x in (-1.905, -0.635, 0.635, 1.905):
        for side in (-1, 1):
            polys.append(rect_poly(cx, cy, 0.4, 1.05, ang, lx=x, ly=side * 2.475))
    return polys


def test_rotated_rectangle_is_measured():
    img = _frame([("poly", rect_poly(0.21, -0.13, 2.0, 1.25, 23.0))])
    view = measure_part(img, sim_up_A(), tip_diameter=0.8)
    assert view is not None
    assert view.offset[0] == pytest.approx(0.21, abs=0.02)
    assert view.offset[1] == pytest.approx(-0.13, abs=0.02)
    assert view.size[0] == pytest.approx(2.0, abs=0.02)
    assert view.size[1] == pytest.approx(1.25, abs=0.02)
    assert abs(angle_error(view.angle, 23.0)) < 0.3
    assert view.area == pytest.approx(2.0 * 1.25, rel=0.1)


def test_soic_outline_gives_envelope():
    img = _frame([("poly", p) for p in _soic_polys(0.0, 0.0, 12.0)])
    view = measure_part(img, sim_up_A(), tip_diameter=0.8)
    assert view is not None
    assert view.size[0] == pytest.approx(6.0, abs=0.05)
    assert view.size[1] == pytest.approx(4.9, abs=0.05)
    assert abs(angle_error(view.angle, 90.0 + 12.0)) < 0.3


def test_bare_tip_is_no_part():
    img = _frame([("disc", ((0.0, 0.0), 0.8))])
    assert measure_part(img, sim_up_A(), tip_diameter=0.8) is None


def test_empty_image_is_no_part():
    img = _frame([])
    assert measure_part(img, sim_up_A(), tip_diameter=0.8) is None


def test_part_over_thin_tip_is_measured():
    # N2 tip (0.4 mm) under a 0402 (1.0 x 0.5 mm): the part hides the tip and is measured.
    img = _frame([("disc", ((0.0, 0.0), 0.4)),
                  ("poly", rect_poly(0.1, 0.05, 1.0, 0.5, 0.0))])
    view = measure_part(img, sim_up_A(), tip_diameter=0.4)
    assert view is not None
    assert view.size[0] == pytest.approx(1.0, abs=0.05)
    assert view.size[1] == pytest.approx(0.5, abs=0.05)


def test_part_outside_search_radius_is_ignored():
    img = _frame([("poly", rect_poly(7.0, 0.0, 2.0, 1.25, 0.0))])
    assert measure_part(img, sim_up_A(), tip_diameter=0.8, search_mm=6.0) is None


def test_angle_error_wraps():
    assert angle_error(89.0, 0.0) == pytest.approx(-1.0)
    assert angle_error(44.0, 0.0) == pytest.approx(44.0)
    assert angle_error(46.0, 0.0) == pytest.approx(-44.0)
    assert angle_error(5.0, 175.0) == pytest.approx(10.0)  # 5 deg is 10 deg past 175 (mod 180)


class FakeMachine:
    def __init__(self, outputs: int = 0):
        self.outputs = outputs
        self.calls: list[tuple[str, bool]] = []
        self.pos = {"x": 0.0, "y": 0.0, "a1": 12.5, "a2": -3.0}

    @property
    def state(self):  # a property, like Machine.state
        return SimpleNamespace(outputs=self.outputs)

    async def set_output(self, name: str, on: bool) -> None:
        self.calls.append((name, on))

    def position(self):
        return dict(self.pos)


class FakeCamera:
    def __init__(self, image=None, error: Exception | None = None):
        self.image = image
        self.error = error

    async def fresh_frame(self, after=None, timeout: float = 3.0):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(image=self.image, t=0.0, seq=1)


def test_inspect_nozzle_toggles_leds_and_returns_angle():
    img = _frame([("poly", rect_poly(0.21, -0.13, 2.0, 1.25, 23.0))])
    m = FakeMachine()
    view, angle = asyncio.run(inspect_nozzle(m, FakeCamera(img), sim_up_A(), 1, VisionConfig()))
    assert view is not None
    assert view.offset[0] == pytest.approx(0.21, abs=0.02)
    assert angle == pytest.approx(12.5)
    assert m.calls == [("leds", True), ("leds", False)]


def test_inspect_nozzle_keeps_leds_that_were_already_on():
    img = _frame([])
    m = FakeMachine(outputs=OUTPUTS["leds"])
    view, _ = asyncio.run(inspect_nozzle(m, FakeCamera(img), sim_up_A(), 2, VisionConfig()))
    assert view is None
    assert m.calls == [("leds", True)]


def test_inspect_nozzle_restores_leds_after_error():
    m = FakeMachine()
    with pytest.raises(RuntimeError):
        asyncio.run(inspect_nozzle(m, FakeCamera(error=RuntimeError("no frame")), sim_up_A(), 1, VisionConfig()))
    assert m.calls == [("leds", True), ("leds", False)]


def test_inspect_nozzle_without_leds_leaves_output_alone():
    img = _frame([])
    m = FakeMachine()
    asyncio.run(inspect_nozzle(m, FakeCamera(img), sim_up_A(), 1, VisionConfig(leds=False)))
    assert m.calls == []


def test_inspect_nozzle_needs_calibration():
    with pytest.raises(InspectError):
        asyncio.run(inspect_nozzle(FakeMachine(), FakeCamera(_frame([])), None, 1, VisionConfig()))


def test_vision_section_loads(tmp_path: Path):
    p = tmp_path / "machine.yaml"
    p.write_text("vision:\n  tip_diameter: [0.9, 0.5]\n  search_mm: 5\n  leds: false\n")
    cfg = load_config(p)
    assert cfg.vision.tip_diameter == (0.9, 0.5)
    assert isinstance(cfg.vision.tip_diameter, tuple)
    assert cfg.vision.search_mm == 5.0
    assert cfg.vision.leds is False
    assert cfg.vision.bottom_vision is True  # default kept


def test_unknown_vision_key_rejected(tmp_path: Path):
    p = tmp_path / "machine.yaml"
    p.write_text("vision:\n  tip_diametr: [1.0, 1.0]\n")
    with pytest.raises(ValueError, match="tip_diametr"):
        load_config(p)


def test_repo_machine_yaml_loads_with_defaults():
    path = Path(__file__).resolve().parent.parent / "config" / "machine.yaml"
    cfg = load_config(path)
    assert cfg.vision == VisionConfig()
