"""Camera capture: one capture thread per camera, only the newest frame kept.

Frames are delivered already rotated and flipped. `rotate` is clockwise in
degrees. `flip_x` mirrors the image left/right (x in image coordinates) and
`flip_y` mirrors it top/bottom.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
import yaml

log = logging.getLogger(__name__)

ROTATIONS: dict[int, int | None] = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}
RECONNECT_DELAY = 2.0  # s between UVC open/read retries
SETTLE_DELAY = 3.0  # s to wait after a device node (re)appears before opening it
# Only one UVC camera is opened at a time (across all cameras in this process):
# every hang seen on the TVM802's cameras happened while both were starting at once.
_OPEN_LOCK = threading.Lock()
OPEN_TURN_TIMEOUT = 10.0  # s a camera waits for its turn to open before going ahead


def device_holders(device: str) -> list[int]:
    """PIDs (other than ours) that have `device` open, found via /proc/*/fd.

    Two processes opening a UVC camera at the same moment (e.g. a Chromium
    based app probing a freshly plugged camera) can hang these Microsoft
    cameras' firmware until they are replugged, so we never open a busy one.
    Processes of other users are invisible without root; that is acceptable.
    """
    try:
        target = os.path.realpath(device)
    except OSError:
        return []
    me = os.getpid()
    pids = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            for fd in os.scandir(f"/proc/{entry.name}/fd"):
                if os.readlink(fd.path) == target:
                    pids.append(int(entry.name))
                    break
        except OSError:
            continue
    return pids
STATUS_STALE = 2.0  # s without a frame before status() reports fps 0


@dataclass
class CameraConfig:
    name: str
    device: str
    width: int = 960
    height: int = 720
    fourcc: str = "MJPG"
    fps: int = 30
    rotate: int = 0  # 0/90/180/270, applied after capture
    flip_x: bool = False
    flip_y: bool = False
    controls: dict[str, int] = field(default_factory=dict)  # v4l2 controls set at start


class Frame(NamedTuple):
    image: np.ndarray  # BGR uint8, already rotated/flipped
    t: float  # time.monotonic() when captured
    seq: int  # increments per frame


class CameraError(Exception):
    pass


class Camera:
    """Base camera. Subclasses implement `_grab()`; the capture thread calls it in a loop."""

    def __init__(
        self,
        name: str,
        *,
        device: str = "",
        rotate: int = 0,
        flip_x: bool = False,
        flip_y: bool = False,
    ) -> None:
        if rotate not in ROTATIONS:
            raise ValueError(f"{name}: rotate must be 0, 90, 180 or 270, not {rotate}")
        self.name = name
        self.device = device
        self.rotate = rotate
        self.flip_x = flip_x
        self.flip_y = flip_y
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._seq = 0
        self._fps = 0.0
        self._last_t: float | None = None
        self._connected = False
        self._error: str | None = None
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ subclass hooks

    def _grab(self) -> np.ndarray | None:
        """Return the next raw BGR image, or None if nothing is available yet."""
        raise NotImplementedError

    def _close(self) -> None:
        """Release hardware. Called from the capture thread when it exits."""

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
            if thread.is_alive():
                # Usually a camera hung inside open(). Keep the handle so callers can
                # see it (`stopped` is False) and start() won't open the device twice.
                log.warning("camera %s: capture thread did not stop within 3 s", self.name)
                return
        self._thread = None

    @property
    def stopped(self) -> bool:
        return self._thread is None or not self._thread.is_alive()

    def _run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                try:
                    img = self._grab()
                except Exception as exc:  # never let the capture thread die
                    log.warning("camera %s: capture error: %s", self.name, exc)
                    with self._lock:
                        self._connected = False
                        self._error = f"{type(exc).__name__}: {exc}"
                    self._stop_evt.wait(RECONNECT_DELAY)
                    continue
                if img is None:
                    self._stop_evt.wait(0.005)
                    continue
                self._publish(self._transform(img))
        finally:
            try:
                self._close()
            except Exception as exc:
                log.warning("camera %s: close failed: %s", self.name, exc)

    def _transform(self, img: np.ndarray) -> np.ndarray:
        code = ROTATIONS[self.rotate]
        if code is not None:
            img = cv2.rotate(img, code)
        if self.flip_x and self.flip_y:
            img = cv2.flip(img, -1)
        elif self.flip_x:
            img = cv2.flip(img, 1)
        elif self.flip_y:
            img = cv2.flip(img, 0)
        return img

    def _publish(self, img: np.ndarray) -> None:
        now = time.monotonic()
        with self._lock:
            self._seq += 1
            if self._last_t is not None and now > self._last_t:
                inst = 1.0 / (now - self._last_t)
                self._fps = inst if self._fps == 0.0 else 0.9 * self._fps + 0.1 * inst
            self._last_t = now
            self._frame = Frame(img, now, self._seq)
            self._connected = True
            self._error = None

    # ------------------------------------------------------------ consumers

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    async def fresh_frame(self, after: float | None = None, timeout: float = 3.0) -> Frame:
        """Wait for the first frame captured after `after` (default: now)."""
        if after is None:
            after = time.monotonic()
        deadline = time.monotonic() + timeout
        while True:
            frame = self.latest()
            if frame is not None and frame.t > after:
                return frame
            if time.monotonic() >= deadline:
                with self._lock:
                    err = self._error
                detail = f" ({err})" if err else ""
                raise CameraError(f"{self.name}: no frame after t={after:.3f} within {timeout} s{detail}")
            await asyncio.sleep(0.01)

    def jpeg(self, quality: int = 80) -> bytes | None:
        frame = self.latest()
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame.image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        return buf.tobytes() if ok else None

    @property
    def size(self) -> tuple[int, int]:
        """(width, height) of the delivered frames; (0, 0) before the first frame."""
        frame = self.latest()
        if frame is None:
            return (0, 0)
        h, w = frame.image.shape[:2]
        return (w, h)

    def status(self) -> dict:
        frame = self.latest()
        now = time.monotonic()
        fps = self._fps if frame is not None and now - frame.t < STATUS_STALE else 0.0
        width, height = self.size
        with self._lock:
            connected = self._connected
            error = self._error
        return {
            "name": self.name,
            "device": self.device,
            "connected": connected,
            "fps": round(fps, 1),
            "width": width,
            "height": height,
            "error": error,
        }


class UvcCamera(Camera):
    """A V4L2 webcam read through OpenCV. Reconnects after failures; never raises from the thread."""

    def __init__(self, cfg: CameraConfig) -> None:
        super().__init__(cfg.name, device=cfg.device, rotate=cfg.rotate, flip_x=cfg.flip_x, flip_y=cfg.flip_y)
        self.cfg = cfg
        self._cap: cv2.VideoCapture | None = None
        self._v4l2_missing_logged = False
        self._missing = False  # device node was absent at the last attempt

    def _apply_controls(self) -> None:
        for key, value in self.cfg.controls.items():
            cmd = ["v4l2-ctl", "-d", self.cfg.device, "--set-ctrl", f"{key}={value}"]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except FileNotFoundError:
                if not self._v4l2_missing_logged:
                    log.warning("camera %s: v4l2-ctl not found, controls not applied", self.name)
                    self._v4l2_missing_logged = True
                return
            except subprocess.TimeoutExpired:
                log.warning("camera %s: v4l2-ctl timed out setting %s", self.name, key)
                continue
            if res.returncode != 0:
                log.warning("camera %s: setting %s=%s failed: %s", self.name, key, value, res.stderr.strip())

    def _open(self) -> tuple[cv2.VideoCapture, np.ndarray]:
        if not os.path.exists(self.cfg.device):
            self._missing = True
            raise CameraError(f"{self.cfg.device} not present (unplugged?)")
        if self._missing:
            # Just (re)plugged: let udev and other programs finish probing it.
            log.info("camera %s: device appeared, waiting %.0f s before opening", self.name, SETTLE_DELAY)
            self._missing = False
            if self._stop_evt.wait(SETTLE_DELAY):
                raise CameraError("stopping")
        # One camera at a time - but a camera hung inside open() (a kernel wait that
        # only a replug ends) must not keep the other camera from ever opening, so
        # wait at most OPEN_TURN_TIMEOUT for the turn and then go ahead anyway.
        got_turn = _OPEN_LOCK.acquire(timeout=OPEN_TURN_TIMEOUT)
        if not got_turn:
            log.warning("camera %s: another camera has been opening for %.0f s (hung?); opening anyway",
                        self.name, OPEN_TURN_TIMEOUT)
        try:
            holders = device_holders(self.cfg.device)
            if holders:
                raise CameraError(f"{self.cfg.device} is in use by process {', '.join(map(str, holders))}")
            return self._open_locked()
        finally:
            if got_turn:
                _OPEN_LOCK.release()

    def _open_locked(self) -> tuple[cv2.VideoCapture, np.ndarray]:
        self._apply_controls()
        cap = cv2.VideoCapture(self.cfg.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"cannot open {self.cfg.device}")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, img = cap.read()
        if not ok or img is None:
            cap.release()
            raise CameraError(f"{self.cfg.device} opened but gave no frame")
        return cap, img

    def _fail(self, msg: str) -> None:
        log.warning("camera %s: %s; retrying in %.0f s", self.name, msg, RECONNECT_DELAY)
        with self._lock:
            self._connected = False
            self._error = msg
        self._release()
        self._stop_evt.wait(RECONNECT_DELAY)

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _grab(self) -> np.ndarray | None:
        if self._cap is None:
            try:
                self._cap, first = self._open()
            except CameraError as exc:
                self._fail(str(exc))
                return None
            return first
        ok, img = self._cap.read()
        if not ok or img is None:
            self._fail("read failed (device disconnected?)")
            return None
        return img

    def _close(self) -> None:
        self._release()


class SimCamera(Camera):
    """A camera whose frames come from `render()`, paced to `fps`."""

    def __init__(self, name: str, render: Callable[[], np.ndarray | None], fps: float = 15) -> None:
        super().__init__(name, device="sim")
        self._render = render
        self._period = 1.0 / fps

    def _grab(self) -> np.ndarray | None:
        t0 = time.monotonic()
        img = self._render()
        remaining = self._period - (time.monotonic() - t0)
        if remaining > 0:
            self._stop_evt.wait(remaining)
        return img


class CameraManager:
    def __init__(self, cameras: dict[str, Camera]) -> None:
        self._cameras = dict(cameras)

    def get(self, name: str) -> Camera:
        return self._cameras[name]  # KeyError if unknown

    def names(self) -> list[str]:
        return list(self._cameras)

    def start_all(self) -> None:
        for cam in self._cameras.values():
            cam.start()

    def stop_all(self) -> None:
        for cam in self._cameras.values():
            cam.stop()


_CONFIG_KEYS = {f.name for f in dataclasses.fields(CameraConfig)} - {"name"}


def load_cameras_config(path: str | Path) -> dict[str, CameraConfig]:
    """Read config/cameras.yaml: a mapping of camera name -> fields. Unknown keys raise ValueError."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{Path(path).name}: expected a mapping of camera names")
    configs: dict[str, CameraConfig] = {}
    for name, fields in data.items():
        where = f"{Path(path).name}: camera {name!r}"
        if not isinstance(fields, dict):
            raise ValueError(f"{where}: expected a mapping of fields")
        unknown = set(fields) - _CONFIG_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}")
        if "device" not in fields:
            raise ValueError(f"{where}: missing required key 'device'")
        cfg = CameraConfig(name=str(name), **fields)
        if cfg.rotate not in ROTATIONS:
            raise ValueError(f"{where}: rotate must be 0, 90, 180 or 270")
        if len(cfg.fourcc) != 4:
            raise ValueError(f"{where}: fourcc must be 4 characters, e.g. MJPG")
        if not isinstance(cfg.controls, dict):
            raise ValueError(f"{where}: controls must be a mapping")
        configs[str(name)] = cfg
    return configs


def build_uvc_manager(path: str | Path) -> CameraManager:
    return CameraManager({name: UvcCamera(cfg) for name, cfg in load_cameras_config(path).items()})
