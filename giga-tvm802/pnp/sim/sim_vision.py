"""Synthetic camera images for `--sim` and the tests.

The constants are the ground truth that calibration is checked against.

Down camera: world point W appears at pixel c + R(angle) @ diag(k, -k) @ (W - H),
where H is the head position. Up camera: a nozzle tip at T = H + t_n appears at
pixel c + diag(-k, -k) @ (T - SIM_UP_CAMERA_XY).

Features are drawn with sub-pixel placement (cv2 `shift`), so the centroids of the
rendered features are accurate to a small fraction of a pixel.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

SIM_DOWN_PX_PER_MM = 25.0
SIM_DOWN_ANGLE_DEG = 1.5  # camera rotated slightly
SIM_UP_CAMERA_XY = (150.0, 30.0)  # world position of the up camera
SIM_UP_PX_PER_MM = 30.0
SIM_UP_MIRROR = True  # looking up mirrors X
SIM_NOZZLE_OFFSETS = {1: (-42.0, -3.0), 2: (-58.0, -3.0)}  # t_n in mm
SIM_PRICK_OFFSET = (-50.0, 12.0)  # t_prick in mm (feeder needle tip)
SIM_SIZE = (640, 480)  # (width, height) in px
SIM_BOARD_ERROR_XY = (0.35, -0.25)  # true board origin minus the nominal job origin (mm)
SIM_BOARD_ERROR_ROT = 0.4  # true panel rotation minus the nominal job rotation (deg)

# Bed pattern and features (world mm).
_DOT_GRID_MM = 4.0
_DOT_OFFSET_MM = 2.0  # dots at 2, 6, 10, ... so the cross centre falls between dots
_DOT_RADIUS_MM = 0.5  # 1 mm dots
_CROSS_CENTRE_MM = (100.0, 100.0)
_CROSS_ARM_MM = 2.0
_CROSS_THICK_PX = 3
_BED_GREY = 200
_DOT_GREY = 40
_TIP_RADIUS_MM = 0.4  # 0.8 mm disc
_UP_BG_GREY = 20
_TIP_GREY = 230
_TIP_COVERED_GREY = 70  # nozzle face under a held part (in its shadow)
_BOARD_MASK_GREY = 50
_BOARD_OPENING_GREY = 35
_BOARD_COPPER_GREY = 215
_PAD_HALF_MM = (0.5, 0.25)  # part pads are 1.0 x 0.5 mm
_NOISE_SIGMA = 2.0
_MARGIN_PX = 20

_SHIFT = 4
_SCALE = 1 << _SHIFT


def _rot(angle_deg: float) -> np.ndarray:
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s], [s, c]])


def _down_map() -> np.ndarray:
    """M such that pixel = c + M @ (W - H). Equals -sim_down_A()."""
    k = SIM_DOWN_PX_PER_MM
    return _rot(SIM_DOWN_ANGLE_DEG) @ np.diag([k, -k])


def sim_down_A() -> np.ndarray:
    """px per mm for the down camera: moving the head by d shifts the image content by A @ d."""
    return -_down_map()


def sim_up_A() -> np.ndarray:
    """px per mm for the up camera: moving the head by d moves the tip image by A @ d."""
    k = SIM_UP_PX_PER_MM
    return np.diag([-k, -k])


def _pt(p) -> tuple[int, int]:
    return (int(round(float(p[0]) * _SCALE)), int(round(float(p[1]) * _SCALE)))


def _inside(p, margin: float) -> bool:
    w, h = SIM_SIZE
    return -margin <= p[0] <= w + margin and -margin <= p[1] <= h + margin


def _noise_bank(rng: np.random.Generator, n: int = 4) -> list[tuple[np.ndarray, np.ndarray]]:
    """Precomputed noise frames as (positive, negative) uint8 parts, so frames cost two saturating adds."""
    w, h = SIM_SIZE
    bank = []
    for _ in range(n):
        v = np.rint(rng.standard_normal((h, w, 3), dtype=np.float32) * _NOISE_SIGMA).astype(np.int16)
        bank.append((np.clip(v, 0, 255).astype(np.uint8), np.clip(-v, 0, 255).astype(np.uint8)))
    return bank


def _add_noise(canvas: np.ndarray, bank: list[tuple[np.ndarray, np.ndarray]], rng: np.random.Generator) -> np.ndarray:
    pos, neg = bank[int(rng.integers(len(bank)))]
    return cv2.subtract(cv2.add(canvas, pos), neg)


@dataclass
class SimBoard:
    """A board on the bed, in world mm; its features are drawn for the down camera only."""

    origin: tuple[float, float]  # world mm of the board's (0, 0)
    rotation: float  # deg
    size: tuple[float, float]  # board mm (width, height), from (0, 0)
    fiducials: list[tuple[float, float]] = field(default_factory=list)  # board mm
    fiducial_diameter: float = 1.0  # copper (mm); the mask opening is 2x this
    parts: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y, rotation) board mm


def sim_board_truth(nominal_origin, nominal_rotation: float, panel_offsets, size, fiducials, parts) -> list[SimBoard]:
    """Boards at their true positions: the nominal job origin and rotation plus the simulated errors.

    Board i sits at rot(true rotation) @ panel_offsets[i] + true origin, and turns with the panel.
    """
    true_rot = float(nominal_rotation) + SIM_BOARD_ERROR_ROT
    true_origin = np.asarray(nominal_origin, dtype=float) + np.asarray(SIM_BOARD_ERROR_XY, dtype=float)
    R = _rot(true_rot)
    boards = []
    for offset in panel_offsets:
        origin = R @ np.asarray(offset, dtype=float) + true_origin
        boards.append(SimBoard(
            origin=(float(origin[0]), float(origin[1])),
            rotation=true_rot,
            size=tuple(size),
            fiducials=[tuple(f) for f in fiducials],
            fiducial_diameter=1.0,
            parts=[tuple(p) for p in parts],
        ))
    return boards


def _fill_poly(canvas: np.ndarray, pts_px, grey: int) -> None:
    poly = np.array([_pt(p) for p in pts_px], dtype=np.int32)
    cv2.fillConvexPoly(canvas, poly, (grey,) * 3, cv2.LINE_AA, _SHIFT)


def _draw_board(canvas: np.ndarray, board: SimBoard, H: np.ndarray, M: np.ndarray,
                centre: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> None:
    """Draw one board: solder mask, mask openings, then copper fiducials and part pads."""
    R = _rot(board.rotation)
    origin = np.asarray(board.origin, dtype=float)
    sw, sh = board.size
    rect = np.array([[0, 0], [sw, 0], [sw, sh], [0, sh]], dtype=float)
    world = rect @ R.T + origin
    if np.any(world.max(axis=0) < lo) or np.any(world.min(axis=0) > hi):
        return  # the board is outside the visible window

    def to_px(W):
        # cv2 draws in index coordinates (pixel i centred at i), but the vision code measures in
        # continuous coordinates (pixel i centred at i + 0.5, image centre at w/2). Shift by -0.5 px
        # so a measured fiducial lands exactly on centre + M @ (W - H): the board is ground truth
        # for absolute positions, unlike the calibration patterns, where only differences count.
        return centre + M @ (W - H) - 0.5

    _fill_poly(canvas, [to_px(c) for c in world], _BOARD_MASK_GREY)

    # Radii in px: the mask opening is 2x the copper diameter, so its radius is the copper diameter.
    copper_r = float(board.fiducial_diameter) / 2 * SIM_DOWN_PX_PER_MM
    opening_r = 2 * copper_r
    for f in board.fiducials:
        p = to_px(R @ np.asarray(f, dtype=float) + origin)
        if _inside(p, opening_r + 2):
            cv2.circle(canvas, _pt(p), int(round(opening_r * _SCALE)), (_BOARD_OPENING_GREY,) * 3,
                       -1, cv2.LINE_AA, _SHIFT)
    for f in board.fiducials:
        p = to_px(R @ np.asarray(f, dtype=float) + origin)
        if _inside(p, copper_r + 2):
            cv2.circle(canvas, _pt(p), int(round(copper_r * _SCALE)), (_BOARD_COPPER_GREY,) * 3,
                       -1, cv2.LINE_AA, _SHIFT)

    hx, hy = _PAD_HALF_MM
    local = np.array([[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy]])
    for x, y, r in board.parts:
        c = R @ np.array([x, y], dtype=float) + origin
        if not _inside(to_px(c), 16):
            continue
        pad = local @ _rot(board.rotation + r).T + c
        _fill_poly(canvas, [to_px(q) for q in pad], _BOARD_COPPER_GREY)


def make_down_renderer(position: Callable[[], tuple[float, float]],
                       boards: list[SimBoard] | None = None) -> Callable[[], np.ndarray]:
    """Render the bed as seen by the down camera at head position `position()`.

    `boards` are drawn on top of the bed (before the sensor noise).
    """
    M = _down_map()
    Minv = np.linalg.inv(M)
    w, h = SIM_SIZE
    centre = np.array([w / 2, h / 2])
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=float)
    dot_r = _DOT_RADIUS_MM * SIM_DOWN_PX_PER_MM
    cross = np.array(_CROSS_CENTRE_MM, dtype=float)
    arm = _CROSS_ARM_MM
    rng = np.random.default_rng(1)
    bank = _noise_bank(rng)
    board_list = list(boards or [])

    def render() -> np.ndarray:
        H = np.asarray(position(), dtype=float)
        # Visible world window: image corners mapped to mm, plus a margin.
        world = H + (corners - centre) @ Minv.T
        pad = _MARGIN_PX / SIM_DOWN_PX_PER_MM
        lo = world.min(axis=0) - pad
        hi = world.max(axis=0) + pad
        canvas = np.full((h, w, 3), _BED_GREY, dtype=np.uint8)

        i0 = math.floor((lo[0] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        i1 = math.ceil((hi[0] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        j0 = math.floor((lo[1] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        j1 = math.ceil((hi[1] - _DOT_OFFSET_MM) / _DOT_GRID_MM)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                W = np.array([_DOT_OFFSET_MM + i * _DOT_GRID_MM, _DOT_OFFSET_MM + j * _DOT_GRID_MM])
                p = centre + M @ (W - H)
                if _inside(p, dot_r + 2):
                    cv2.circle(canvas, _pt(p), int(round(dot_r * _SCALE)), (_DOT_GREY,) * 3,
                               -1, cv2.LINE_AA, _SHIFT)

        pc = centre + M @ (cross - H)
        if _inside(pc, arm * SIM_DOWN_PX_PER_MM + 10):
            for d in ((arm, 0.0), (0.0, arm)):
                a = cross - np.array(d)
                b = cross + np.array(d)
                pa = centre + M @ (a - H)
                pb = centre + M @ (b - H)
                cv2.line(canvas, _pt(pa), _pt(pb), (_DOT_GREY,) * 3, _CROSS_THICK_PX, cv2.LINE_AA, _SHIFT)

        for board in board_list:
            _draw_board(canvas, board, H, M, centre, lo, hi)

        return _add_noise(canvas, bank, rng)

    return render


SIM_TIP_DIAMETERS = {1: 0.8, 2: 0.4}  # mm, N1 thick, N2 thin (match VisionConfig.tip_diameter)
DEFAULT_PART = ((2.0, 1.25), 0.0)  # size (long, short) mm and axis deg when no feeder is near the tip
_PART_GREY = 225


@dataclass
class SimHeldPart:
    size: tuple[float, float]  # mm (length, width)
    offset: tuple[float, float]  # world mm from the nozzle axis, at nozzle angle a_pick
    angle: float  # world deg of the long side, at nozzle angle a_pick
    a_pick: float  # nozzle angle at the pick


def _wrap180(a: float) -> float:
    """Wrap degrees into (-180, 180]."""
    return 180.0 - ((180.0 - a) % 360.0)


class SimPartPhysics:
    """Gives each picked part a random position on the nozzle and records where it truly lands.

    Registers itself as a listener on `sim_machine`, so it sees every pick and release.
    With `drop_next` set, the next pick holds nothing: the camera sees only the bare tip.
    """

    def __init__(self, sim_machine, part_for_pick: Callable[[int, float, float], tuple | None] | None = None,
                 *, seed: int = 3, max_offset: float = 0.3, max_angle: float = 4.0):
        self.part_for_pick = part_for_pick
        self.max_offset = float(max_offset)
        self.max_angle = float(max_angle)
        self.drop_next = False
        self.held: dict[int, SimHeldPart] = {}
        self.placed: list[dict] = []
        self._rng = np.random.default_rng(seed)
        sim_machine.listeners.append(self.on_event)

    def on_event(self, event: tuple, angles: dict[str, float]) -> None:
        kind, n = event[0], event[1]
        if kind == "pick":
            self._pick(n, float(event[2]), float(event[3]), angles)
        elif kind == "release":
            self._release(n, float(event[2]), float(event[3]), angles)

    def _pick(self, n: int, hx: float, hy: float, angles: dict[str, float]) -> None:
        if self.drop_next:
            self.drop_next = False
            self.held.pop(n, None)
            return
        ox, oy = SIM_NOZZLE_OFFSETS[n]
        tip_x, tip_y = hx + ox, hy + oy
        found = self.part_for_pick(n, tip_x, tip_y) if self.part_for_pick is not None else None
        size, axis = found if found is not None else DEFAULT_PART
        r = self.max_offset * math.sqrt(self._rng.random())
        theta = 2 * math.pi * self._rng.random()
        error = float(self._rng.uniform(-self.max_angle, self.max_angle))
        self.held[n] = SimHeldPart(
            size=(float(size[0]), float(size[1])),
            offset=(r * math.cos(theta), r * math.sin(theta)),
            angle=float(axis) + error,
            a_pick=float(angles[f"a{n}"]),
        )

    def _release(self, n: int, hx: float, hy: float, angles: dict[str, float]) -> None:
        part = self.held.pop(n, None)
        if part is None:
            return
        ox, oy = SIM_NOZZLE_OFFSETS[n]
        tip = np.array([hx + ox, hy + oy])
        phi = float(angles[f"a{n}"]) - part.a_pick
        centre = tip + _rot(phi) @ np.asarray(part.offset, dtype=float)
        self.placed.append({
            "nozzle": n,
            "centre": [float(centre[0]), float(centre[1])],
            "angle": _wrap180(part.angle + phi),
            "size": part.size,
        })

    def view(self, n: int, nozzle_angle: float) -> tuple[SimHeldPart, float] | None:
        """The part held by nozzle n and how far the nozzle has turned from its pick angle (deg)."""
        part = self.held.get(n)
        if part is None:
            return None
        return part, float(nozzle_angle) - part.a_pick


def _part_corners_px(centre_w: np.ndarray, angle: float, size: tuple[float, float],
                     M: np.ndarray, U: np.ndarray, centre: np.ndarray) -> list[np.ndarray]:
    """Corners of a part rectangle (world mm) in continuous image px, minus the 0.5 px drawing shift."""
    a = math.radians(angle)
    u = np.array([math.cos(a), math.sin(a)])
    v = np.array([-math.sin(a), math.cos(a)])
    half_l, half_w = size[0] / 2, size[1] / 2
    corners = []
    for sl, sw in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
        c = centre_w + sl * half_l * u + sw * half_w * v
        corners.append(centre + M @ (c - U) - 0.5)
    return corners


def make_up_renderer(position: Callable[[], tuple[float, float]],
                     physics: SimPartPhysics | None = None,
                     angles: Callable[[], dict[str, float]] | None = None) -> Callable[[], np.ndarray]:
    """Render the nozzle tips (and held parts, if `physics` is given) as seen by the up camera.

    `angles()` returns {"a1": deg, "a2": deg}; without it, held parts are drawn at their pick angle.
    """
    M = sim_up_A()
    w, h = SIM_SIZE
    centre = np.array([w / 2, h / 2])
    U = np.array(SIM_UP_CAMERA_XY, dtype=float)
    tips = [(n, np.array(t, dtype=float), SIM_TIP_DIAMETERS[n] / 2 * SIM_UP_PX_PER_MM)
            for n, t in SIM_NOZZLE_OFFSETS.items()]
    rng = np.random.default_rng(2)
    bank = _noise_bank(rng)

    def render() -> np.ndarray:
        H = np.asarray(position(), dtype=float)
        canvas = np.full((h, w, 3), _UP_BG_GREY, dtype=np.uint8)
        for n, t, r in tips:
            p = centre + M @ (H + t - U)
            if _inside(p, r + 2):
                # A bare tip reflects the LEDs (bright, for the up-camera calibration). Under a part the
                # nozzle face is in the part's shadow, so it reads dark, like a real black nozzle:
                # a tip edge sticking out past a small part must not merge into the part outline.
                covered = physics is not None and n in physics.held
                grey = _TIP_COVERED_GREY if covered else _TIP_GREY
                cv2.circle(canvas, _pt(p), int(round(r * _SCALE)), (grey,) * 3,
                           -1, cv2.LINE_AA, _SHIFT)
        if physics is not None:
            now = angles() if angles is not None else None
            for n, t, _ in tips:
                part = physics.held.get(n)
                if part is None:
                    continue
                a_now = now[f"a{n}"] if now is not None else part.a_pick
                _, phi = physics.view(n, a_now)
                tip = H + t
                c_w = tip + _rot(phi) @ np.asarray(part.offset, dtype=float)
                corners = _part_corners_px(c_w, part.angle + phi, part.size, M, U, centre)
                margin = max(part.size) / 2 * SIM_UP_PX_PER_MM + 2
                if _inside(centre + M @ (c_w - U), margin):
                    _fill_poly(canvas, corners, _PART_GREY)
        return _add_noise(canvas, bank, rng)

    return render
