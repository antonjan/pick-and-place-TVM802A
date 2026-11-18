"""Feeder definitions (config/feeders.yaml) and their bookkeeping.

A strip feeder holds parts in pockets along a tape; part n sits `n * pitch`
further along `direction` from `pick`. A drag feeder has one fixed pick
position; a needle on the head pulls the tape by one pitch between picks.
A tray feeder holds a matrix of parts (an IC tray): `pick` is the first part,
`tray_end` the last part, and parts are taken row by row. Like a strip it never
needs a feed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

KINDS = ("strip", "drag", "tray")

# Largest tray grid per side. Keeps a typo (e.g. 1000 columns) from making an empty tray look real.
MAX_TRAY_SIDE = 100


class FeederError(Exception):
    """Feeder unknown, disabled, or out of parts."""


def _pair(value, name: str) -> tuple[float, float]:
    try:
        x, y = value
        return (float(x), float(y))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be [x, y], got {value!r}") from exc


def _unit(direction: tuple[float, float]) -> tuple[float, float]:
    dx, dy = direction
    n = math.hypot(dx, dy)
    if n == 0:
        return direction  # validate() reports the zero vector
    return (dx / n, dy / n)


@dataclass
class Feeder:
    id: str                          # unique, e.g. "F1"
    name: str = ""
    kind: str = "strip"              # "strip" | "drag" | "tray"
    pick: tuple[float, float] = (0.0, 0.0)       # world mm, see pick_location(); tray: first part
    direction: tuple[float, float] = (1.0, 0.0)  # unit vector along the tape (normalised on load)
    pitch: float = 4.0               # mm between pockets
    count: int = 0                   # parts taken so far
    capacity: int | None = None      # strip: pockets in the strip (None = unknown)
    rotation: float = 0.0            # part rotation in the tape (deg)
    pick_depth: float = 3.0          # nozzle depth to touch the part (mm)
    prick: tuple[float, float] | None = None     # drag: world mm where the needle enters the tape
    needs_feed: bool = True          # drag: True when the pick pocket is empty
    tray_end: tuple[float, float] | None = None  # tray: world mm of the last part (opposite corner)
    tray_cols: int = 1               # tray: parts per row (along X)
    tray_rows: int = 1               # tray: rows (along Y)
    enabled: bool = True
    value: str = ""                  # part value / package, matched against jobs later
    package: str = ""
    bank: str = ""                   # id of the bank this lane belongs to; "" = loose feeder
    lane: int | None = None          # 1-based lane number within the bank

    def __post_init__(self) -> None:
        self.pick = _pair(self.pick, "pick")
        self.direction = _unit(_pair(self.direction, "direction"))
        if self.prick is not None:
            self.prick = _pair(self.prick, "prick")
        if self.tray_end is not None:
            self.tray_end = _pair(self.tray_end, "tray_end")

    def pick_location(self) -> tuple[float, float]:
        """World point where the next part is picked."""
        if self.kind == "drag":
            return self.pick
        if self.kind == "tray":
            return self._tray_location(self.count)
        px, py = self.pick
        dx, dy = self.direction
        k = self.count * self.pitch
        return (px + k * dx, py + k * dy)

    def _tray_location(self, index: int) -> tuple[float, float]:
        """World point of part `index` in a tray, numbered row by row from `pick`."""
        px, py = self.pick
        if self.tray_end is None:
            return (px, py)  # validate() refuses a bigger grid without tray_end
        ex, ey = self.tray_end
        col, row = index % self.tray_cols, index // self.tray_cols
        # A single column or row has no spacing to divide: every part sits on the first line.
        x = px + col * (ex - px) / (self.tray_cols - 1) if self.tray_cols > 1 else px
        y = py + row * (ey - py) / (self.tray_rows - 1) if self.tray_rows > 1 else py
        return (x, y)

    def tray_capacity(self) -> int:
        """Parts in a tray (columns times rows)."""
        return self.tray_cols * self.tray_rows

    def remaining(self) -> int | None:
        """Parts left in a strip with known capacity or in a tray; None otherwise."""
        if self.kind == "tray":
            return max(self.tray_capacity() - self.count, 0)
        if self.kind != "strip" or self.capacity is None:
            return None
        return max(self.capacity - self.count, 0)

    def drag_vector(self) -> tuple[float, float]:
        """Head move (mm) that pulls the tape by one pitch."""
        return (self.pitch * self.direction[0], self.pitch * self.direction[1])

    def after_pick(self) -> None:
        self.count += 1
        if self.kind == "drag":
            self.needs_feed = True

    def after_feed(self) -> None:
        self.needs_feed = False

    def validate(self) -> None:
        if not self.id:
            raise ValueError("feeder id must not be empty")
        if self.kind not in KINDS:
            raise ValueError(f"feeder {self.id}: kind must be one of {KINDS}, got {self.kind!r}")
        if not self.pitch > 0:
            raise ValueError(f"feeder {self.id}: pitch must be > 0")
        self.direction = _unit(self.direction)
        if self.direction == (0.0, 0.0):
            raise ValueError(f"feeder {self.id}: direction must not be zero")
        if self.kind == "drag" and self.prick is None:
            raise ValueError(f"feeder {self.id}: drag feeder needs a prick position")
        for name, side in (("tray_cols", self.tray_cols), ("tray_rows", self.tray_rows)):
            if not 1 <= side <= MAX_TRAY_SIDE:
                raise ValueError(f"feeder {self.id}: {name} must be 1 to {MAX_TRAY_SIDE}, got {side}")
        if self.kind == "tray" and self.tray_end is None and (self.tray_cols > 1 or self.tray_rows > 1):
            raise ValueError(f"feeder {self.id}: tray feeder needs a tray_end (last part) position")


_FEEDER_KEYS = {f.name for f in dataclasses.fields(Feeder)}


def feeder_to_dict(f: Feeder) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fld in dataclasses.fields(Feeder):
        value = getattr(f, fld.name)
        out[fld.name] = list(value) if isinstance(value, tuple) else value
    return out


def feeder_from_dict(entry: Any) -> Feeder:
    if not isinstance(entry, dict):
        raise ValueError(f"feeder entry must be a mapping, got {entry!r}")
    unknown = set(entry) - _FEEDER_KEYS
    if unknown:
        raise ValueError(f"feeder {entry.get('id', '?')}: unknown key(s) {sorted(unknown)}")
    if "id" not in entry:
        raise ValueError("feeder entry without an id")
    feeder = Feeder(**entry)
    feeder.validate()
    return feeder


# Old private names, kept for existing callers.
_feeder_to_dict = feeder_to_dict
_feeder_from_dict = feeder_from_dict


class FeederStore:
    """Feeders kept in a YAML file. Every change is saved atomically."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._feeders: dict[str, Feeder] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open() as f:
            data = yaml.safe_load(f) or {}
        for entry in data.get("feeders") or []:
            feeder = _feeder_from_dict(entry)
            self._feeders[feeder.id] = feeder

    def all(self) -> list[Feeder]:
        return list(self._feeders.values())

    def get(self, id: str) -> Feeder:
        try:
            return self._feeders[id]
        except KeyError:
            raise FeederError(f"unknown feeder {id!r}") from None

    def upsert(self, feeder: Feeder) -> None:
        feeder.validate()
        self._feeders[feeder.id] = feeder  # replaces in place, or appends
        self.save()

    def delete(self, id: str) -> None:
        if id not in self._feeders:
            raise FeederError(f"unknown feeder {id!r}")
        del self._feeders[id]
        self.save()

    def by_bank(self, bank: str) -> list[Feeder]:
        """Feeders of one bank, ordered by lane number then id."""
        found = [f for f in self._feeders.values() if f.bank == bank]
        return sorted(found, key=lambda f: (f.lane is None, f.lane or 0, f.id))

    def replace_bank(self, bank: str, feeders: list[Feeder]) -> None:
        """Swap all lanes of `bank` for `feeders`. Nothing changes if any check fails."""
        new_ids: set[str] = set()
        for f in feeders:
            f.validate()
            if f.bank != bank:
                raise ValueError(f"feeder {f.id}: bank must be {bank!r}, got {f.bank!r}")
            if f.id in new_ids:
                raise ValueError(f"duplicate feeder id {f.id!r} in bank {bank!r}")
            new_ids.add(f.id)
        for existing in self._feeders.values():
            if existing.id in new_ids and existing.bank != bank:
                raise ValueError(f"feeder id {existing.id!r} is already used by another feeder")
        for old in self.by_bank(bank):
            del self._feeders[old.id]
        for f in feeders:
            self._feeders[f.id] = f
        self.save()

    def delete_bank(self, bank: str) -> None:
        for old in self.by_bank(bank):
            del self._feeders[old.id]
        self.save()

    def save(self) -> None:
        data = {"feeders": [feeder_to_dict(f) for f in self._feeders.values()]}
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
