"""Machine configuration (config/machine.yaml) as typed dataclasses.

Unknown keys in the YAML are rejected so typos don't silently fall back to
defaults.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ControllerConfig:
    host: str = "192.168.0.8"
    port: int = 701
    poll_interval_idle: float = 0.05  # s between state requests when idle (fast enough to catch a panel key tap)
    poll_interval_moving: float = 0.03  # s between state requests while moving
    reconnect_delay: float = 2.0


@dataclass
class StepsConfig:
    x: float = 32808  # steps per mm
    y: float = 32808
    z: float = 32808
    a: float = 4444.44  # steps per degree (both nozzles)


@dataclass
class LimitsConfig:
    # Soft limits in mm (machine coordinates after homing). Home is at the
    # max corner: X register 9 843 000 = 300.0 mm, Y 11 532 000 = 351.5 mm.
    # The min ends are NOT measured yet. Measure and tighten before trusting them.
    x: tuple[float, float] = (0.0, 299.0)
    y: tuple[float, float] = (0.0, 350.5)
    # Maximum nozzle depth (mm) below the up position for each nozzle.
    z1_max: float = 8.0
    z2_max: float = 8.0


@dataclass
class HomingConfig:
    x_register: int = 9_843_000  # register value written at the X switch
    y_register: int = 11_532_000
    approach_mm: float = 1000.0  # "far" target, past the end of travel
    back_off_mm: float = 5.0
    approach_timeout: float = 60.0
    release_timeout: float = 15.0
    second_approach_timeout: float = 15.0


@dataclass
class MotionConfig:
    # The controller only moves in whole multiples of this many steps (it
    # rounds the requested *distance* up). Measured on the real machine:
    # 1000 steps on every axis = 0.0305 mm (X/Y/Z), 0.225 deg (A).
    step_quantum: int = 1000
    tolerance_steps: int = 2  # an axis counts as arrived within this many steps
    settle_polls: int = 3  # identical consecutive positions required
    # Conservative speed estimates, only used to compute move timeouts.
    xy_speed: float = 25.0  # mm/s (real speed not measured yet; keep low)
    z_speed: float = 20.0  # mm/s
    a_speed: float = 180.0  # deg/s
    min_timeout: float = 3.0  # s added to every move timeout
    safe_z_tolerance: float = 0.05  # mm; |z| above this counts as "nozzle down"
    allow_unhomed_jog: bool = True  # X/Y jogs allowed before homing (no soft limits)
    # Speed levels of the vendor software (1..11, shown as (level+1)*10 %; 4 = "50 %",
    # its default and what this app always used). Jog = manual moves, job = placing.
    jog_speed_level: int = 4
    job_speed_level: int = 4
    max_unhomed_jog: float = 10.0  # mm per jog while unhomed


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class HeadConfig:
    pick_dwell: float = 0.15  # s with the nozzle down on the part, vacuum on
    # s to wait for the feeder needle sensor to confirm a down/up switch; longer
    # means the needle is jammed (didn't go down) or stuck (didn't come up).
    needle_timeout: float = 1.0
    place_dwell: float = 0.10  # s with the nozzle down before releasing
    blow_time: float = 0.10  # s of blow-off after releasing vacuum at place/discard
    vacuum_settle: float = 0.10  # s after raising before reading the vacuum sensor
    vacuum_timeout: float = 0.60  # s to keep reading the sensor before calling the pick failed
    prick_settle: float = 0.20  # s after switching the needle down/up
    # Verify the part with the vacuum sensor after a pick (failed check = vacuum off).
    # Off by default since 2026-11-13: on the real machine the sensor did not report
    # held parts, so good picks were dropped. Vacuum now stays on until place/discard.
    check_vacuum: bool = False
    place_depth: float = 3.0  # default nozzle depth for placing (mm)
    # Opt-in place depth model (the vendor software's): with bed_depth > 0 a part is placed at
    # bed_depth - PCB thickness - part height, the nozzle holding the part by its top.
    bed_depth: float = 0.0  # nozzle depth (mm) where the tip touches the surface the PCB lies on; 0 = not set
    part_height: float = 0.5  # part height (mm) when neither the part nor its package rule gives one
    pick_depth: float = 3.0  # default nozzle depth for picking, used for new feeders/lanes (mm)
    discard: tuple[float, ...] = ()  # world [x, y] of the discard bin; empty = not set
    # The automatic run picks one part with each nozzle, then places both (saves a board<->feeder
    # trip per pair). Step mode always does one part at a time.
    dual_pick: bool = False


@dataclass
class VisionConfig:
    bottom_vision: bool = True  # inspect every part on the up camera before placing
    leds: bool = True  # switch the leds output on while inspecting (off afterwards)
    settle: float = 0.15  # s after arriving over the up camera before taking the frame
    min_brightness: int = 0  # 0 = Otsu threshold; otherwise a fixed grey threshold
    search_mm: float = 6.0  # only bright regions within this radius of the nozzle axis count
    tip_diameter: tuple[float, float] = (0.8, 0.4)  # N1 (thick), N2 (thin): a lone round blob this size = no part
    size_tolerance: float = 0.3  # measured body vs expected body, relative
    max_offset_mm: float = 1.0  # reject a part further than this from the nozzle axis
    max_angle_deg: float = 15.0  # reject a part turned more than this


@dataclass
class MachineConfig:
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    steps: StepsConfig = field(default_factory=StepsConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    homing: HomingConfig = field(default_factory=HomingConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    head: HeadConfig = field(default_factory=HeadConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)


def _build(cls, data: dict[str, Any] | None, path: str):
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(fields)
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        if dataclasses.is_dataclass(default):
            kwargs[name] = _build(type(default), value, f"{path}.{name}")
        elif isinstance(default, tuple):
            kwargs[name] = tuple(float(v) for v in value)
        else:
            kwargs[name] = type(default)(value)
    return cls(**kwargs)


def load_config(path: str | Path | None) -> MachineConfig:
    if path is None:
        return MachineConfig()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return _build(MachineConfig, data, Path(path).name)
