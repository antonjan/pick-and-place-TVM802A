"""Feeder banks: stacks of drag-feeder lanes with saved, named layouts (config/banks.yaml).

The lanes themselves are ordinary drag feeders in the FeederStore, tagged with their bank.
A layout is a named snapshot of those lanes, so a stack can be swapped by loading its layout.
See docs/phase4-design.md, section 2.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pnp.job.feeders import Feeder, FeederStore, feeder_from_dict, feeder_to_dict

BANK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Distance across the tape from the pocket centre line to the sprocket-hole line (EIA-481 "F"), by tape width.
SPROCKET_ACROSS = {8: 3.5, 12: 5.5, 16: 7.5, 24: 11.5, 32: 14.2, 44: 20.2}
# Distance along the tape from a pocket centre to the nearest sprocket hole (EIA-481 "P2").
# Holes are 4 mm apart (P0), so with a 4 mm or 8 mm part pitch a hole is back under the needle after each drag.
SPROCKET_ALONG = 2.0

MAX_LANES = 99


def _unit(v, name: str) -> tuple[float, float]:
    x, y = float(v[0]), float(v[1])
    n = math.hypot(x, y)
    if n == 0:
        raise ValueError(f"{name} must not be zero")
    return (x / n, y / n)


@dataclass
class LaneSpec:
    width: float = 8.0              # tape width (mm): 8, 12, 16, 24, 32, 44
    pitch: float = 4.0              # part pitch (mm): the drag distance per feed
    offset: float | None = None     # mm along lane_axis from the bank origin to this lane's pocket line;
                                    # None = previous lane's offset + (previous width + this width) / 2 + gap
                                    # (lane 1: None = 0)


@dataclass
class GenerateSpec:
    origin: tuple[float, float]               # world mm: pick point (first pocket) of lane 1
    lane_axis: tuple[float, float] = (1.0, 0.0)       # unit vector from lane 1 toward lane 2 (normalised)
    feed_direction: tuple[float, float] = (0.0, -1.0) # unit vector the tape is dragged (normalised)
    gap: float = 0.0                          # extra space between neighbouring tapes (mm)
    sprocket_side: int = -1                   # +1: sprocket holes on the +lane_axis side of the pocket, -1: the other side
    prick_along: float = -SPROCKET_ALONG      # needle point offset along feed_direction from the pick point (mm).
                                              # Pocket centres sit P2 = 2 mm along the tape from the nearest
                                              # sprocket hole (EIA-481, 8-24 mm tape), so 0 would put the needle
                                              # on solid tape between two holes. Default: the hole just upstream.
    pick_depth: float = 3.0
    lanes: list[LaneSpec] = field(default_factory=list)   # at least 1, at most 99
    keep_parts: bool = True                   # carry value/package/rotation/pick_depth/count/enabled over from
                                              # existing lanes with the same lane number
    # Rotation offset for every lane of the stack (deg, + = anticlockwise, -90 = parts placed 90 deg
    # more clockwise). None = keep each lane's own value. Stored as Feeder.rotation = -offset.
    rotation_offset: float | None = None

    def __post_init__(self) -> None:
        self.origin = (float(self.origin[0]), float(self.origin[1]))
        self.lane_axis = _unit(self.lane_axis, "lane_axis")
        self.feed_direction = _unit(self.feed_direction, "feed_direction")


def lane_offsets(spec: GenerateSpec) -> list[float]:
    """Resolved offset (mm along lane_axis) of every lane, in lane order."""
    offsets: list[float] = []
    for i, lane in enumerate(spec.lanes):
        if lane.offset is not None:
            offsets.append(float(lane.offset))
        elif i == 0:
            offsets.append(0.0)
        else:
            prev = spec.lanes[i - 1]
            offsets.append(offsets[-1] + (prev.width + lane.width) / 2 + spec.gap)
    return offsets


def generate_lanes(bank_id: str, spec: GenerateSpec, existing: list[Feeder]) -> list[Feeder]:
    """Build the drag feeders for a bank from a spec, keeping loaded parts of lanes with the same number."""
    if not BANK_ID.fullmatch(bank_id):
        raise ValueError(f"bad bank id {bank_id!r}: use 1-32 of a-z, 0-9, '-' or '_', starting with a letter or digit")
    if not spec.lanes:
        raise ValueError("a bank needs at least one lane")
    if len(spec.lanes) > MAX_LANES:
        raise ValueError(f"at most {MAX_LANES} lanes, got {len(spec.lanes)}")
    if spec.sprocket_side not in (-1, 1):
        raise ValueError("sprocket_side must be +1 or -1")
    for i, lane in enumerate(spec.lanes, 1):
        if not lane.width > 0:
            raise ValueError(f"lane {i}: width must be > 0")
        if not lane.pitch > 0:
            raise ValueError(f"lane {i}: pitch must be > 0")

    by_lane = {f.lane: f for f in existing if f.lane is not None}
    ox, oy = spec.origin
    ax, ay = spec.lane_axis
    fx, fy = spec.feed_direction
    feeders: list[Feeder] = []
    for i, (lane, offset) in enumerate(zip(spec.lanes, lane_offsets(spec)), 1):
        pick = (ox + offset * ax, oy + offset * ay)
        across = SPROCKET_ACROSS.get(round(lane.width), lane.width / 2 - 0.5)
        side = spec.sprocket_side * across
        prick = (pick[0] + side * ax + spec.prick_along * fx, pick[1] + side * ay + spec.prick_along * fy)
        feeder = Feeder(
            id=f"{bank_id}-{i}", kind="drag", pick=pick, direction=spec.feed_direction, pitch=lane.pitch,
            prick=prick, pick_depth=spec.pick_depth, needs_feed=True, enabled=True,
            name=f"{lane.width:g} mm lane {i}", bank=bank_id, lane=i,
        )
        old = by_lane.get(i)
        if spec.keep_parts and old is not None:
            feeder.value = old.value
            feeder.package = old.package
            feeder.rotation = old.rotation
            feeder.pick_depth = old.pick_depth
            feeder.count = old.count
            feeder.enabled = old.enabled
            feeder.name = old.name
        if spec.rotation_offset is not None:
            feeder.rotation = -float(spec.rotation_offset) + 0.0  # + 0.0: no -0.0
        feeders.append(feeder)
    return feeders


@dataclass
class Bank:
    id: str
    name: str = ""
    active_layout: str | None = None
    layouts: dict[str, list[dict]] = field(default_factory=dict)  # layout name -> feeder dicts (all Feeder fields)


class BankError(Exception):
    """Unknown bank or layout (message contains "unknown"), or a conflict."""


class BankStore:
    """Banks and their layouts in config/banks.yaml; the lanes themselves live in the FeederStore."""

    def __init__(self, path, feeders: FeederStore) -> None:
        self.path = Path(path)
        self.feeders = feeders
        self._banks: dict[str, Bank] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open() as f:
            data = yaml.safe_load(f) or {}
        for entry in data.get("banks") or []:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ValueError(f"bank entry must be a mapping with an id, got {entry!r}")
            bank_id = str(entry["id"])
            if not BANK_ID.fullmatch(bank_id):
                raise ValueError(f"bad bank id {bank_id!r} in {self.path.name}")
            layouts: dict[str, list[dict]] = {}
            for lname, rows in (entry.get("layouts") or {}).items():
                layouts[str(lname)] = [feeder_to_dict(feeder_from_dict(row)) for row in rows or []]
            active = entry.get("active_layout")
            self._banks[bank_id] = Bank(
                id=bank_id,
                name=str(entry.get("name") or ""),
                active_layout=str(active) if active is not None else None,
                layouts=layouts,
            )

    def save(self) -> None:
        data = {"banks": [
            {"id": b.id, "name": b.name, "active_layout": b.active_layout,
             "layouts": {n: list(rows) for n, rows in b.layouts.items()}}
            for b in self._banks.values()
        ]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def all(self) -> list[Bank]:
        return list(self._banks.values())

    def get(self, id: str) -> Bank:
        try:
            return self._banks[id]
        except KeyError:
            raise BankError(f"unknown bank {id!r}") from None

    def upsert(self, id: str, name: str = "") -> Bank:
        if not BANK_ID.fullmatch(id):
            raise ValueError(f"bad bank id {id!r}: use 1-32 of a-z, 0-9, '-' or '_', starting with a letter or digit")
        bank = self._banks.get(id)
        if bank is None:
            bank = Bank(id=id, name=name)
            self._banks[id] = bank
        else:
            bank.name = name
        self.save()
        return bank

    def delete(self, id: str) -> None:
        self.get(id)
        del self._banks[id]
        self.feeders.delete_bank(id)
        self.save()

    def generate(self, id: str, spec: GenerateSpec) -> list[Feeder]:
        bank = self.get(id)
        lanes = generate_lanes(id, spec, self.feeders.by_bank(id))
        try:
            self.feeders.replace_bank(id, lanes)
        except ValueError as exc:
            raise BankError(str(exc)) from exc
        bank.active_layout = None  # the lanes no longer match a saved layout
        self.save()
        return lanes

    def set_rotation_offset(self, id: str, offset: float) -> list[Feeder]:
        """Give every lane of the bank the same rotation offset (deg, -90 = 90 deg clockwise)."""
        self.get(id)
        lanes = self.feeders.by_bank(id)
        for f in lanes:
            f.rotation = -float(offset) + 0.0
        self.feeders.save()
        return lanes

    def save_layout(self, id: str, name: str) -> None:
        bank = self.get(id)
        name = name.strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("layout name must be 1 to 40 characters")
        bank.layouts[name] = [feeder_to_dict(f) for f in self.feeders.by_bank(id)]
        bank.active_layout = name
        self.save()

    def load_layout(self, id: str, name: str) -> list[Feeder]:
        bank = self.get(id)
        if name not in bank.layouts:
            raise BankError(f"unknown layout {name!r} in bank {id!r}")
        lanes = []
        for row in bank.layouts[name]:
            row = dict(row, bank=id)
            lanes.append(feeder_from_dict(row))
        try:
            self.feeders.replace_bank(id, lanes)
        except ValueError as exc:
            raise BankError(str(exc)) from exc
        bank.active_layout = name
        self.save()
        return lanes

    def delete_layout(self, id: str, name: str) -> None:
        bank = self.get(id)
        if name not in bank.layouts:
            raise BankError(f"unknown layout {name!r} in bank {id!r}")
        del bank.layouts[name]
        if bank.active_layout == name:
            bank.active_layout = None
        self.save()
