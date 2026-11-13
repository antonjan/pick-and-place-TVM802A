"""Camera, config and simulated-vision tests. No hardware needed."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from pnp.sim.sim_vision import (
    SIM_NOZZLE_OFFSETS,
    SIM_UP_CAMERA_XY,
    make_down_renderer,
    make_up_renderer,
    sim_down_A,
)
from pnp.vision.camera import (
    Camera,
    CameraError,
    CameraManager,
    SimCamera,
    load_cameras_config,
)

ROOT = Path(__file__).resolve().parent.parent
CAMERAS_YAML = ROOT / "config" / "cameras.yaml"


class StaticCamera(Camera):
    """Returns a fixed image, paced like a real camera."""

    def __init__(self, name: str, img: np.ndarray, **kw) -> None:
        super().__init__(name, device="test", **kw)
        self._img = img

    def _grab(self):
        time.sleep(0.01)
        return self._img.copy()


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _subpixel_peak(res: np.ndarray, x: int, y: int) -> tuple[float, float]:
    """Parabolic refinement of a correlation peak at integer (x, y)."""
    def off(dx, dy):
        return float(res[y + dy, x + dx]) if 0 <= y + dy < res.shape[0] and 0 <= x + dx < res.shape[1] else 0.0

    c = off(0, 0)
    l, r = off(-1, 0), off(1, 0)
    u, d = off(0, -1), off(0, 1)
    dx = 0.5 * (l - r) / (l - 2 * c + r) if (l - 2 * c + r) != 0 else 0.0
    dy = 0.5 * (u - d) / (u - 2 * c + d) if (u - 2 * c + d) != 0 else 0.0
    return x + dx, y + dy


def _locate(template: np.ndarray, img: np.ndarray) -> tuple[float, float]:
    """Top-left corner of `template` in `img`, sub-pixel."""
    res = cv2.matchTemplate(_gray(img), _gray(template), cv2.TM_CCOEFF_NORMED)
    _, _, _, loc = cv2.minMaxLoc(res)
    return _subpixel_peak(res, loc[0], loc[1])


# ---------------------------------------------------------------- SimCamera / Camera base


def test_simcamera_frames_have_increasing_seq():
    cam = SimCamera("sim", lambda: np.zeros((48, 64, 3), np.uint8), fps=50)
    cam.start()
    try:
        first = asyncio.run(cam.fresh_frame(timeout=2.0))
        second = asyncio.run(cam.fresh_frame(after=first.t, timeout=2.0))
    finally:
        cam.stop()
    assert second.seq > first.seq
    assert second.t > first.t
    assert cam.status()["connected"] is True
    assert cam.status()["device"] == "sim"


def test_fresh_frame_after_future_time():
    cam = SimCamera("sim", lambda: np.zeros((48, 64, 3), np.uint8), fps=30)
    cam.start()
    try:
        after = time.monotonic() + 0.1
        frame = asyncio.run(cam.fresh_frame(after=after, timeout=2.0))
    finally:
        cam.stop()
    assert frame.t > after


def test_fresh_frame_times_out_when_no_frames():
    cam = SimCamera("blocked", lambda: None, fps=30)
    cam.start()
    try:
        with pytest.raises(CameraError):
            asyncio.run(cam.fresh_frame(timeout=0.2))
    finally:
        cam.stop()


def test_rotate_90_swaps_width_and_height():
    img = np.zeros((720, 960, 3), np.uint8)
    cam = StaticCamera("rot", img, rotate=90)
    cam.start()
    try:
        frame = asyncio.run(cam.fresh_frame(timeout=2.0))
    finally:
        cam.stop()
    assert frame.image.shape[:2] == (960, 720)
    assert cam.size == (720, 960)


def test_flip_x_mirrors_left_right():
    img = np.zeros((20, 30, 3), np.uint8)
    img[:, 0] = 255  # left edge marked
    cam = StaticCamera("flip", img, flip_x=True)
    cam.start()
    try:
        frame = asyncio.run(cam.fresh_frame(timeout=2.0))
    finally:
        cam.stop()
    assert frame.image[0, -1, 0] == 255
    assert frame.image[0, 0, 0] == 0


def test_flip_y_mirrors_top_bottom():
    img = np.zeros((20, 30, 3), np.uint8)
    img[0, :] = 255  # top edge marked
    cam = StaticCamera("flipy", img, flip_y=True)
    cam.start()
    try:
        frame = asyncio.run(cam.fresh_frame(timeout=2.0))
    finally:
        cam.stop()
    assert frame.image[-1, 0, 0] == 255
    assert frame.image[0, 0, 0] == 0


def test_jpeg_is_decodable():
    cam = StaticCamera("jpg", np.full((48, 64, 3), 128, np.uint8))
    assert cam.jpeg() is None  # no frame yet
    cam.start()
    try:
        asyncio.run(cam.fresh_frame(timeout=2.0))
        data = cam.jpeg(quality=90)
    finally:
        cam.stop()
    assert data is not None
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (48, 64, 3)


def test_bad_rotate_rejected():
    with pytest.raises(ValueError):
        StaticCamera("bad", np.zeros((4, 4, 3), np.uint8), rotate=45)


# ---------------------------------------------------------------- config


def test_load_repo_cameras_config():
    cfgs = load_cameras_config(CAMERAS_YAML)
    assert set(cfgs) == {"down", "up"}
    # Roles confirmed by the user 2026-11-12: head camera ...56215848, up camera ...11585848.
    assert cfgs["down"].device.endswith("000F330056215848-video-index0")
    assert cfgs["up"].device.endswith("000F330011585848-video-index0")
    for cfg in cfgs.values():
        assert (cfg.width, cfg.height, cfg.fourcc, cfg.fps) == (640, 480, "MJPG", 30)
        assert not cfg.flip_x and not cfg.flip_y
        assert cfg.controls == {}
    # Down camera turned 90 deg anticlockwise so X+ pans right and Y+ pans up.
    assert cfgs["down"].rotate == 270 and cfgs["up"].rotate == 0


def test_unknown_key_rejected(tmp_path):
    path = tmp_path / "cams.yaml"
    path.write_text("down:\n  device: /dev/video0\n  wdith: 640\n")
    with pytest.raises(ValueError, match="wdith"):
        load_cameras_config(path)


def test_missing_device_rejected(tmp_path):
    path = tmp_path / "cams.yaml"
    path.write_text("down:\n  width: 640\n")
    with pytest.raises(ValueError, match="device"):
        load_cameras_config(path)


# ---------------------------------------------------------------- manager


def test_camera_manager_get_names_start_stop():
    a = SimCamera("a", lambda: np.zeros((8, 8, 3), np.uint8), fps=30)
    b = SimCamera("b", lambda: np.zeros((8, 8, 3), np.uint8), fps=30)
    mgr = CameraManager({"a": a, "b": b})
    assert mgr.names() == ["a", "b"]
    assert mgr.get("a") is a
    with pytest.raises(KeyError):
        mgr.get("nope")
    mgr.start_all()
    try:
        asyncio.run(a.fresh_frame(timeout=2.0))
        asyncio.run(b.fresh_frame(timeout=2.0))
    finally:
        mgr.stop_all()
    assert a._thread is None and b._thread is None


# ---------------------------------------------------------------- simulated vision geometry


def _render_at(factory, pos):
    return factory(lambda: pos)()


def test_down_cross_at_image_centre():
    img = _render_at(make_down_renderer, (100.0, 100.0))
    gray = _gray(img).astype(float)
    cu, cv = 320.0, 240.0
    ys, xs = np.nonzero(gray < 100)
    keep = (xs - cu) ** 2 + (ys - cv) ** 2 < 40 ** 2
    assert keep.sum() > 50
    assert abs(xs[keep].mean() - cu) < 0.5
    assert abs(ys[keep].mean() - cv) < 0.5


def test_down_shift_matches_A():
    # The cross (2 mm arms, unique in the image) is the reference feature.
    img0 = _render_at(make_down_renderer, (100.0, 100.0))
    img1 = _render_at(make_down_renderer, (101.0, 100.0))
    x0, y0 = 320 - 30, 240 - 30
    template = img0[y0:y0 + 60, x0:x0 + 60]
    u, v = _locate(template, img1)
    shift = np.array([u - x0, v - y0])
    expected = sim_down_A() @ np.array([1.0, 0.0])
    assert np.allclose(shift, expected, atol=0.5), (shift, expected)


def test_up_nozzle1_at_centre():
    pos = (SIM_UP_CAMERA_XY[0] - SIM_NOZZLE_OFFSETS[1][0], SIM_UP_CAMERA_XY[1] - SIM_NOZZLE_OFFSETS[1][1])
    img = _render_at(make_up_renderer, pos)
    gray = _gray(img).astype(float)
    ys, xs = np.nonzero(gray > 128)
    keep = (xs - 320.0) ** 2 + (ys - 240.0) ** 2 < 40 ** 2
    assert keep.sum() > 50
    assert abs(xs[keep].mean() - 320.0) < 0.5
    assert abs(ys[keep].mean() - 240.0) < 0.5


def test_renderer_speed():
    pos = (100.0, 100.0)
    down = make_down_renderer(lambda: pos)
    up = make_up_renderer(lambda: pos)
    down()
    up()  # warm up
    t0 = time.perf_counter()
    for _ in range(20):
        down()
    for _ in range(20):
        up()
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.6, f"20+20 frames took {elapsed:.3f} s"


def test_device_holders_finds_other_process(tmp_path):
    import subprocess
    import sys
    import time as _time

    from pnp.vision.camera import device_holders

    dev = tmp_path / "fake-video"
    dev.write_text("x")
    assert device_holders(str(dev)) == []
    # A child process holding the file open must be reported.
    p = subprocess.Popen([sys.executable, "-c", f"f=open({str(dev)!r}); import time; time.sleep(5)"])
    try:
        for _ in range(50):
            if device_holders(str(dev)):
                break
            _time.sleep(0.05)
        assert device_holders(str(dev)) == [p.pid]
    finally:
        p.kill()
        p.wait()
    # Our own open file descriptors are ignored.
    with open(dev):
        assert device_holders(str(dev)) == []


def test_uvc_camera_waits_while_device_missing(tmp_path):
    from pnp.vision.camera import CameraConfig, CameraError, UvcCamera

    cam = UvcCamera(CameraConfig(name="t", device=str(tmp_path / "nope")))
    with pytest.raises(CameraError, match="not present"):
        cam._open()
    assert cam._missing



def test_hung_camera_does_not_block_the_other_forever(tmp_path, monkeypatch):
    """A camera stuck inside open() holds the one-at-a-time turn; the other camera
    must still get to open after OPEN_TURN_TIMEOUT."""
    import pnp.vision.camera as cam_mod
    from pnp.vision.camera import CameraConfig, UvcCamera

    monkeypatch.setattr(cam_mod, "OPEN_TURN_TIMEOUT", 0.2)
    dev = tmp_path / "video-fake"
    dev.write_text("x")
    cam = UvcCamera(CameraConfig(name="t", device=str(dev)))
    opened = []
    monkeypatch.setattr(cam, "_open_locked", lambda: opened.append(True) or ("cap", "img"))
    assert cam_mod._OPEN_LOCK.acquire()  # "the other camera is hung while opening"
    try:
        assert cam._open() == ("cap", "img")
    finally:
        cam_mod._OPEN_LOCK.release()
    assert opened == [True]
