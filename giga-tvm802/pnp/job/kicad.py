"""KiCad component position files: ASCII .pos and CSV exports, read into PosPart lists.

Coordinates are board mm (inches are converted). Rotation is normalised to [0, 360).
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass

SIDES = ("top", "bottom")
INCH_MM = 25.4
_CSV_COLUMNS = ("ref", "val", "package", "posx", "posy", "rot", "side")
_UNIT_RE = re.compile(r"^#+\s*Unit\s*=\s*([A-Za-z]+)", re.IGNORECASE)


@dataclass
class PosPart:
    ref: str
    value: str
    package: str
    x: float          # mm, board coordinates
    y: float          # mm
    rotation: float   # deg, normalised to [0, 360)
    side: str         # "top" | "bottom"


class PosError(ValueError):
    """Malformed position file. The message names the line number."""


def _number(token: str, what: str, lineno: int) -> float:
    try:
        value = float(token)
    except ValueError:
        raise PosError(f"line {lineno}: {what} is not a number: {token!r}") from None
    if not math.isfinite(value):
        raise PosError(f"line {lineno}: {what} is not a finite number: {token!r}")
    return value


def _side(token: str, lineno: int) -> str:
    side = token.strip().lower()
    if side not in SIDES:
        raise PosError(f"line {lineno}: side must be top or bottom, got {token!r}")
    return side


def _rotation(deg: float) -> float:
    r = deg % 360.0
    return 0.0 if r >= 360.0 else r  # a tiny negative angle can round up to 360.0


def _unit_scale(token: str, lineno: int) -> float:
    unit = token.lower()
    if unit == "mm":
        return 1.0
    if unit in ("inch", "inches", "in"):
        return INCH_MM
    raise PosError(f"line {lineno}: unknown unit {token!r}")


def _add(parts: list[PosPart], seen: dict[str, int], part: PosPart, lineno: int) -> None:
    if part.ref in seen:
        raise PosError(f"line {lineno}: duplicate ref {part.ref!r} (first on line {seen[part.ref]})")
    seen[part.ref] = lineno
    parts.append(part)


def parse_pos(text: str) -> list[PosPart]:
    """Parse KiCad ASCII .pos or CSV text. Raises PosError on bad data or when no parts are found."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    first = next(
        ((n, ln) for n, ln in enumerate(lines, 1) if ln.strip() and not ln.lstrip().startswith("#")),
        None,
    )
    if first is None:
        raise PosError("no parts found")
    if "," in first[1]:
        return _parse_csv(text)
    return _parse_ascii(lines)


def _parse_ascii(lines: list[str]) -> list[PosPart]:
    scale = 1.0  # KiCad writes mm unless the unit line says otherwise
    for n, line in enumerate(lines, 1):
        match = _UNIT_RE.match(line.strip())
        if match:
            scale = _unit_scale(match.group(1), n)
            break

    parts: list[PosPart] = []
    seen: dict[str, int] = {}
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) < 7:
            raise PosError(f"line {n}: expected 'Ref Val Package PosX PosY Rot Side', got {len(tokens)} field(s)")
        package, x, y, rot, side = tokens[-5:]
        part = PosPart(
            ref=tokens[0],
            value=" ".join(tokens[1:-5]),
            package=package,
            x=_number(x, "PosX", n) * scale,
            y=_number(y, "PosY", n) * scale,
            rotation=_rotation(_number(rot, "Rot", n)),
            side=_side(side, n),
        )
        _add(parts, seen, part, n)

    if not parts:
        raise PosError("no parts found")
    return parts


def _parse_csv(text: str) -> list[PosPart]:
    reader = csv.reader(io.StringIO(text))
    index: dict[str, int] = {}
    parts: list[PosPart] = []
    seen: dict[str, int] = {}
    for row in reader:
        lineno = reader.line_num
        if not row or not any(cell.strip() for cell in row):
            continue
        if not index:
            names = [cell.strip().lower() for cell in row]
            missing = [c for c in _CSV_COLUMNS if c not in names]
            if missing:
                raise PosError(f"line {lineno}: CSV header missing column(s) {', '.join(missing)}")
            index = {c: names.index(c) for c in _CSV_COLUMNS}
            continue
        if len(row) <= max(index.values()):
            raise PosError(f"line {lineno}: too few fields ({len(row)})")

        def field(col: str) -> str:
            return row[index[col]].strip()

        ref = field("ref")
        if not ref:
            raise PosError(f"line {lineno}: empty ref")
        part = PosPart(
            ref=ref,
            value=field("val"),
            package=field("package"),
            x=_number(field("posx"), "PosX", lineno),
            y=_number(field("posy"), "PosY", lineno),
            rotation=_rotation(_number(field("rot"), "Rot", lineno)),
            side=_side(field("side"), lineno),
        )
        _add(parts, seen, part, lineno)

    if not parts:
        raise PosError("no parts found")
    return parts
