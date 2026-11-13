"""Package rules: which nozzle places each package, and an optional place depth (config/packages.yaml).

N1 is the thick tip, N2 the thin tip. The first rule whose pattern matches the package wins.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

NOZZLES = (1, 2)


@dataclass
class PackageRule:
    pattern: str                      # fnmatch pattern, matched case-insensitively against the package
    nozzle: int = 2                   # 1 (thick tip) or 2 (thin tip)
    place_depth: float | None = None  # None = machine.yaml head.place_depth
    note: str = ""
    body: tuple[float, float] | None = None   # (length, width) mm of the part as the up camera sees it
                                              # (body + leads), None = don't check the size
    vision: bool = True                       # inspect on the up camera before placing


DEFAULT_RULES: list[PackageRule] = [
    PackageRule("*0201*", 2, body=(0.6, 0.3)), PackageRule("*0402*", 2, body=(1.0, 0.5)),
    PackageRule("*0603*", 2, body=(1.6, 0.8)), PackageRule("*0805*", 2, body=(2.0, 1.25)),
    PackageRule("SOD-*", 2), PackageRule("SOT-23*", 2, body=(2.9, 2.4)), PackageRule("SOT-323*", 2),
    PackageRule("SOT-363*", 2),
    PackageRule("*1206*", 1, body=(3.2, 1.6)), PackageRule("*1210*", 1, body=(3.2, 2.5)),
    PackageRule("*2512*", 1, body=(6.3, 3.2)),
    PackageRule("SOIC-8*", 1, body=(6.0, 4.9)),
    PackageRule("SOIC*", 1), PackageRule("SOP*", 1), PackageRule("TSSOP*", 1), PackageRule("MSOP*", 1),
    PackageRule("QFN*", 1), PackageRule("QFP*", 1), PackageRule("LQFP*", 1), PackageRule("TQFP*", 1),
    PackageRule("SOT-223*", 1), PackageRule("TO-252*", 1), PackageRule("D_SMA*", 1), PackageRule("D_SMB*", 1),
    PackageRule("CP_Elec*", 1),
]

_RULE_KEYS = {f.name for f in dataclasses.fields(PackageRule)}


def _check_nozzle(value: Any, what: str) -> None:
    if isinstance(value, bool) or value not in NOZZLES:
        raise ValueError(f"{what} must be 1 or 2, got {value!r}")


def _check_rule(rule: PackageRule) -> None:
    if not isinstance(rule.pattern, str) or not rule.pattern.strip():
        raise ValueError("rule pattern must not be empty")
    _check_nozzle(rule.nozzle, f"rule {rule.pattern!r}: nozzle")
    depth = rule.place_depth
    if depth is not None:
        if isinstance(depth, bool) or not isinstance(depth, (int, float)) or not math.isfinite(depth) or depth <= 0:
            raise ValueError(f"rule {rule.pattern!r}: place_depth must be > 0 or null, got {depth!r}")
    if not isinstance(rule.note, str):
        raise ValueError(f"rule {rule.pattern!r}: note must be a string")
    rule.body = _check_body(rule.body, f"rule {rule.pattern!r}: body")
    if not isinstance(rule.vision, bool):
        raise ValueError(f"rule {rule.pattern!r}: vision must be true or false, got {rule.vision!r}")


def _check_body(body: Any, what: str) -> tuple[float, float] | None:
    """None, or two positive finite numbers returned as (length, width) with length >= width."""
    if body is None:
        return None
    if not isinstance(body, (list, tuple)) or len(body) != 2:
        raise ValueError(f"{what} must be [length, width] in mm or null, got {body!r}")
    sides = []
    for side in body:
        if isinstance(side, bool) or not isinstance(side, (int, float)) or not math.isfinite(side) or side <= 0:
            raise ValueError(f"{what} must be two positive numbers, got {body!r}")
        sides.append(float(side))
    return (max(sides), min(sides))


def _rule_from_dict(entry: Any) -> PackageRule:
    if not isinstance(entry, dict):
        raise ValueError(f"rule entry must be a mapping, got {entry!r}")
    unknown = set(entry) - _RULE_KEYS
    if unknown:
        raise ValueError(f"rule {entry.get('pattern', '?')!r}: unknown key(s) {sorted(unknown)}")
    if "pattern" not in entry:
        raise ValueError("rule entry without a pattern")
    rule = PackageRule(**entry)
    if rule.place_depth is not None:
        rule.place_depth = float(rule.place_depth)
    _check_rule(rule)
    return rule


class PackageRules:
    """Package to nozzle rules, kept in a YAML file. set() and save() write atomically."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.default_nozzle: int = 1
        self.rules: list[PackageRule] = [dataclasses.replace(r) for r in DEFAULT_RULES]
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{self.path}: top level must be a mapping")
        default_nozzle = data.get("default_nozzle", 1)
        _check_nozzle(default_nozzle, "default_nozzle")
        raw_rules = data.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ValueError("rules must be a list")
        self.default_nozzle = default_nozzle
        self.rules = [_rule_from_dict(entry) for entry in raw_rules]

    def match(self, package: str) -> PackageRule | None:
        """First rule whose pattern matches the package (case-insensitive), else None."""
        pkg = package.lower()
        for rule in self.rules:
            if fnmatch.fnmatchcase(pkg, rule.pattern.lower()):
                return rule
        return None

    def nozzle_for(self, package: str) -> int:
        rule = self.match(package)
        return rule.nozzle if rule is not None else self.default_nozzle

    def place_depth_for(self, package: str) -> float | None:
        """Place depth from the matching rule, or None to use machine.yaml head.place_depth."""
        rule = self.match(package)
        return rule.place_depth if rule is not None else None

    def body_for(self, package: str) -> tuple[float, float] | None:
        """(length, width) mm from the matching rule, or None (no size check)."""
        rule = self.match(package)
        return rule.body if rule is not None else None

    def vision_for(self, package: str) -> bool:
        """Inspect on the up camera before placing; True when no rule matches."""
        rule = self.match(package)
        return rule.vision if rule is not None else True

    def set(self, default_nozzle: int, rules: list[PackageRule]) -> None:
        """Validate, save, then replace the in-memory values. On ValueError nothing changes."""
        _check_nozzle(default_nozzle, "default_nozzle")
        new_rules = [dataclasses.replace(r) for r in rules]
        for rule in new_rules:
            _check_rule(rule)
        self._write(default_nozzle, new_rules)
        self.default_nozzle = default_nozzle
        self.rules = new_rules

    def save(self) -> None:
        self._write(self.default_nozzle, self.rules)

    def _write(self, default_nozzle: int, rules: list[PackageRule]) -> None:
        entries = []
        for rule in rules:
            entry = dataclasses.asdict(rule)
            entry["body"] = list(rule.body) if rule.body is not None else None
            entries.append(entry)
        data = {"default_nozzle": default_nozzle, "rules": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
