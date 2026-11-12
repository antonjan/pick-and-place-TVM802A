"""Persistent calibration data (config/calibration.yaml)."""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml


@dataclass
class CameraCal:
    A: list[list[float]]     # px per mm
    rms_px: float
    date: str                # ISO timestamp

    def matrix(self) -> np.ndarray:
        return np.asarray(self.A, dtype=float)


@dataclass
class Calibration:
    cameras: dict[str, CameraCal] = field(default_factory=dict)            # "down", "up"
    nozzle_offset: dict[int, tuple[float, float]] = field(default_factory=dict)  # t_n (mm)
    up_position: dict[int, tuple[float, float]] = field(default_factory=dict)    # U_n (mm)
    prick_offset: tuple[float, float] | None = None                              # t_prick (mm)

    def set_camera(self, name: str, A, rms_px: float) -> None:
        """Store camera scale A (2x2, px per mm), stamped with the current UTC time."""
        self.cameras[name] = CameraCal(
            A=np.asarray(A, dtype=float).tolist(),
            rms_px=float(rms_px),
            date=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        )

    def A(self, name: str) -> np.ndarray | None:
        cam = self.cameras.get(name)
        return None if cam is None else cam.matrix()

    def save(self, path) -> None:
        """Write YAML atomically: temp file in the same directory, then os.replace."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cameras": {
                name: {"A": [[float(v) for v in row] for row in cam.A],
                       "rms_px": float(cam.rms_px),
                       "date": str(cam.date)}
                for name, cam in self.cameras.items()
            },
            "nozzle_offset": {int(k): [float(v[0]), float(v[1])] for k, v in self.nozzle_offset.items()},
            "up_position": {int(k): [float(v[0]), float(v[1])] for k, v in self.up_position.items()},
        }
        if self.prick_offset is not None:
            data["prick_offset"] = [float(self.prick_offset[0]), float(self.prick_offset[1])]
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, path) -> "Calibration":
        """Load from YAML. A missing file gives an empty Calibration."""
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        cameras = {
            str(name): CameraCal(
                A=[[float(v) for v in row] for row in c["A"]],
                rms_px=float(c.get("rms_px", 0.0)),
                date=str(c.get("date", "")),
            )
            for name, c in (data.get("cameras") or {}).items()
        }
        nozzle = {int(k): (float(v[0]), float(v[1])) for k, v in (data.get("nozzle_offset") or {}).items()}
        up = {int(k): (float(v[0]), float(v[1])) for k, v in (data.get("up_position") or {}).items()}
        po = data.get("prick_offset")
        prick = (float(po[0]), float(po[1])) if po else None
        return cls(cameras=cameras, nozzle_offset=nozzle, up_position=up, prick_offset=prick)
