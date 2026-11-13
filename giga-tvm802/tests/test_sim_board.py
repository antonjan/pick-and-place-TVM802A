"""Simulated board tests: fiducial placement, mask look, culling and speed (no hardware, no pnp.vision)."""

from __future__ import annotations

import time

import cv2
import numpy as np

from pnp.sim.sim_vision import (
    SIM_BOARD_ERROR_ROT,
    SIM_BOARD_ERROR_XY,
    SIM_SIZE,
    SimBoard,
    make_down_renderer,
    sim_board_truth,
    sim_down_A,
)

W_PX, H_PX = SIM_SIZE
CENTRE = np.array([W_PX / 2, H_PX / 2])
M = -sim_down_A()  # pixel = centre + M @ (W - H)
ANGLE = 0.4

FIDUCIALS = [(3.0, 3.0), (37.0, 3.0), (3.0, 27.0)]
PARTS = [(8.0 + 2.6 * i, 10.0 + 3.0 * (i % 4), 90.0 * (i % 4)) for i in range(11)]


def _rot(deg: float) -> np.ndarray:
    a = np.radians(deg)
    return np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])


def _board() -> SimBoard:
    return SimBoard(origin=(100.35, 99.7), rotation=ANGLE, size=(40.0, 30.0),
                    fiducials=FIDUCIALS, fiducial_diameter=1.0, parts=PARTS)


def _world(board: SimBoard, p) -> np.ndarray:
    return _rot(board.rotation) @ np.asarray(p, dtype=float) + np.asarray(board.origin, dtype=float)


def _render(boards, head) -> np.ndarray:
    return make_down_renderer(lambda: tuple(head), boards)()


def _grey(img: np.ndarray) -> np.ndarray:
    return img.astype(float).mean(axis=2)


def _copper_centroid(img: np.ndarray, expected: np.ndarray, radius: int = 30) -> np.ndarray:
    """Centroid (continuous convention, as the vision code measures) of the copper > 150 near `expected` px."""
    gray = img[..., 0]
    x0 = int(round(expected[0])) - radius
    y0 = int(round(expected[1])) - radius
    win = gray[y0:y0 + 2 * radius, x0:x0 + 2 * radius]
    m = cv2.moments((win > 150).astype(np.uint8), binaryImage=True)
    assert m["m00"] > 50, "no copper found near the expected fiducial position"
    return np.array([m["m10"] / m["m00"] + x0 + 0.5, m["m01"] / m["m00"] + y0 + 0.5])


# ---------------------------------------------------------------- no boards

def test_no_boards_same_as_old_renderer():
    # Same code path and the same RNG use: a renderer without boards and one with boards=None
    # must produce identical frames.
    for head in [(100.0, 100.0), (101.3, 97.2)]:
        old = make_down_renderer(lambda: head)
        new = make_down_renderer(lambda: head, boards=None)
        for _ in range(2):
            assert np.array_equal(old(), new())


def test_empty_boards_same_as_no_boards():
    head = (100.0, 100.0)
    a = make_down_renderer(lambda: head)
    b = make_down_renderer(lambda: head, [])
    assert np.array_equal(a(), b())


# ---------------------------------------------------------------- fiducials

def test_fiducial_centroid_at_expected_pixel():
    board = _board()
    fid_world = _world(board, FIDUCIALS[0])
    for off in [(0.0, 0.0), (0.3, 0.0), (0.0, -0.3)]:
        head = fid_world + np.asarray(off)
        img = _render([board], head)
        expected = CENTRE + M @ (fid_world - head)
        c = _copper_centroid(img, expected)
        assert np.allclose(c, expected, atol=0.1), (off, c, expected)
    # Head exactly over the fiducial: the copper sits at the image centre.
    img = _render([board], fid_world)
    c = _copper_centroid(img, CENTRE)
    assert np.allclose(c, CENTRE, atol=0.1), c


def test_sim_board_truth_offsets_and_rotation():
    nominal = (100.0, 100.0)
    boards = sim_board_truth(nominal, 0.0, [(0.0, 0.0), (45.0, 0.0)], (40.0, 30.0), FIDUCIALS, PARTS)
    assert len(boards) == 2
    true_origin = np.array(nominal) + np.array(SIM_BOARD_ERROR_XY)
    R = _rot(SIM_BOARD_ERROR_ROT)
    assert np.allclose(boards[0].origin, true_origin)
    assert np.allclose(boards[1].origin, R @ np.array([45.0, 0.0]) + true_origin)
    for b in boards:
        assert b.rotation == SIM_BOARD_ERROR_ROT
        assert b.size == (40.0, 30.0)
        assert b.fiducials == FIDUCIALS
        assert b.fiducial_diameter == 1.0
        assert len(b.parts) == 11


# ---------------------------------------------------------------- mask and bed

def test_board_mask_darker_than_bed():
    board = _board()
    # Head over board point (20, 25): a patch at the image centre is inside the board and
    # away from the copper (the nearest pad is about 5.7 mm away).
    head = _world(board, (20.0, 25.0))
    img = _grey(_render([board], head))
    inside = img[205:215, 315:325].mean()
    assert inside < 70, inside

    # Head 16 mm right of the board's right edge (x = 40): the image left strip is outside the board.
    head_out = _world(board, (56.0, 25.0))
    img_out = _grey(_render([board], head_out))
    outside = img_out[200:240, 0:30].mean()
    assert 185 < outside < 210, outside


def test_far_head_renders_no_board_pixels():
    board = _board()
    far = (500.0, 500.0)
    with_board = _render([board], far)
    without = _render(None, far)
    assert np.array_equal(with_board, without)
    # Sanity: the board is drawn when the head is over it.
    assert not np.array_equal(_render([board], _world(board, (20.0, 15.0))),
                              _render(None, _world(board, (20.0, 15.0))))


# ---------------------------------------------------------------- speed

def test_two_board_panel_frame_time():
    boards = sim_board_truth((100.0, 100.0), 0.0, [(0.0, 0.0), (45.0, 0.0)],
                             (40.0, 30.0), FIDUCIALS, PARTS)
    head = (142.5, 115.0)  # both boards partly in view
    render = make_down_renderer(lambda: head, boards)
    render()  # warm up
    t0 = time.perf_counter()
    for _ in range(20):
        render()
    per_frame = (time.perf_counter() - t0) / 20
    assert per_frame < 0.050, f"frame took {per_frame * 1000:.1f} ms"
