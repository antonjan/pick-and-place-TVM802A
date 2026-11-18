"""Machine defaults editor: describe, validate, save (keeping comments) and apply live.

Backs the UI's Defaults tab. Every editable value of config/machine.yaml is listed
in SECTIONS with a label, unit and a one-line explanation. Saving writes only the
changed sections into machine.yaml with ruamel.yaml, so the file's comments stay.
Changes are applied to the running MachineConfig *in place* (the same objects
HeadOps, the runner and the machine already hold), so no restart is needed -
except for the controller address.
"""

from __future__ import annotations

import dataclasses
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from pnp.config import MachineConfig, _build

# (field, label, unit, help, advanced)
Field = tuple[str, str, str, str, bool]

SECTIONS: list[dict[str, Any]] = [
    {"key": "head", "title": "Pick & place", "advanced": False, "fields": [
        ("pick_depth", "Default pick depth", "mm", "Nozzle depth for picking. Used for new feeders and bank lanes; each feeder can override it.", False),
        ("place_depth", "Default place depth", "mm", "Nozzle depth for placing when no package rule sets one.", False),
        ("bed_depth", "Bed depth", "mm", "Nozzle depth where the tip touches the surface the PCB lies on. When set, the place depth is bed depth − PCB thickness − part height. 0 = not set: use the place depths as before.", False),
        ("part_height", "Default part height", "mm", "Part height used when neither the part nor its package rule gives one (only with the bed depth set).", False),
        ("pick_dwell", "Pick dwell", "s", "Time the nozzle stays down on the part with vacuum on.", False),
        ("place_dwell", "Place dwell", "s", "Time the nozzle stays down before releasing the part.", False),
        ("blow_time", "Blow-off time", "s", "Short air blow after releasing a part at place or discard.", False),
        ("vacuum_settle", "Vacuum settle", "s", "Wait after lifting before the vacuum sensor is read.", False),
        ("check_vacuum", "Check vacuum sensor", "", "Use the vacuum sensor to confirm a pick (a failed check drops the part). Off by default: the sensor didn't report parts.", False),
        ("vacuum_timeout", "Vacuum check time", "s", "How long the sensor is read before a pick counts as failed (only with the check on).", False),
        ("prick_settle", "Needle settle", "s", "Extra wait after switching the feeder needle down or up.", False),
        ("needle_timeout", "Needle sensor timeout", "s", "How long the needle sensor may take to confirm down/up before an error or the stuck-needle alarm.", False),
        ("discard", "Discard bin X, Y", "mm", "Where rejected parts are dropped. Leave empty if there is no bin.", False),
        ("dual_pick", "Pick with both nozzles", "", "The automatic run picks the next part for nozzle 1 and the next part for nozzle 2, then places both. Saves a trip per pair. Step mode always does one part at a time.", False),
    ]},
    {"key": "vision", "title": "Bottom vision", "advanced": False, "fields": [
        ("bottom_vision", "Bottom vision on", "", "Check and correct each part on the up camera before placing.", False),
        ("leds", "Up LEDs while inspecting", "", "Switch the up-camera LEDs on during an inspection.", False),
        ("settle", "Camera settle", "s", "Wait over the up camera before taking the picture.", False),
        ("min_brightness", "Brightness threshold", "0–255", "0 = automatic (Otsu); otherwise parts must be brighter than this grey level.", False),
        ("search_mm", "Search radius", "mm", "Only bright areas within this distance of the nozzle axis count as the part.", False),
        ("tip_diameter", "Nozzle tip diameters N1, N2", "mm", "A lone round blob this size is the bare tip, i.e. no part.", False),
        ("size_tolerance", "Size tolerance", "fraction", "Allowed difference between measured and expected body size (0.3 = 30 %).", False),
        ("max_offset_mm", "Max part offset", "mm", "Reject a part sitting further than this from the nozzle centre.", False),
        ("max_angle_deg", "Max part angle", "°", "Reject a part turned more than this from the expected angle.", False),
    ]},
    {"key": "limits", "title": "Soft limits", "advanced": False, "fields": [
        ("x", "X range (min, max)", "mm", "Moves outside this range are refused (machine coordinates after homing).", False),
        ("y", "Y range (min, max)", "mm", "Moves outside this range are refused.", False),
        ("z1_max", "N1 lowest point", "mm", "How far nozzle 1 may go down from Z 0. Z has no limit switch.", False),
        ("z2_max", "N2 lowest point", "mm", "How far nozzle 2 may go down from Z 0.", False),
    ]},
    {"key": "motion", "title": "Motion", "advanced": False, "fields": [
        ("xy_speed", "X/Y speed estimate", "mm/s", "Only used to work out move timeouts: keep it at or below the real speed.", False),
        ("z_speed", "Z speed estimate", "mm/s", "Used for move timeouts.", False),
        ("a_speed", "Rotation speed estimate", "°/s", "Used for move timeouts.", False),
        ("min_timeout", "Extra move time", "s", "Added to every move timeout.", False),
        ("job_speed_level", "Job speed level", "1–11", "Speed of the moves while placing. Shown as (level+1)×10 %: 4 = 50 %, the vendor software's default. Higher = faster moves and harder acceleration.", False),
        ("jog_speed_level", "Jog speed level", "1–11", "Speed of manual moves (jog, Go to, camera clicks, feeder actions). 4 = 50 %.", False),
        ("allow_unhomed_jog", "Allow jogging before homing", "", "X/Y jogs before homing (no soft limits then).", False),
        ("max_unhomed_jog", "Max jog before homing", "mm", "Largest single X/Y jog allowed before homing.", False),
        ("safe_z_tolerance", "Nozzle-up tolerance", "mm", "|Z| below this counts as both nozzles up.", True),
        ("step_quantum", "Controller step size", "steps", "The controller moves in multiples of this many steps (measured: 1000).", True),
        ("tolerance_steps", "Arrival tolerance", "steps", "An axis counts as arrived within this many steps.", True),
        ("settle_polls", "Settle reads", "", "Identical position reads required before a move counts as finished.", True),
    ]},
    {"key": "homing", "title": "Homing", "advanced": True, "fields": [
        ("x_register", "X position at the switch", "steps", "Value written to the X position at the Right switch (9 843 000 = 300.018 mm).", True),
        ("y_register", "Y position at the switch", "steps", "Value written to the Y position at the Top switch (11 532 000 = 351.5 mm).", True),
        ("approach_mm", "Approach distance", "mm", "How far homing drives towards the switch at most.", True),
        ("back_off_mm", "Back-off", "mm", "Distance moved off the switch before the slow second approach.", True),
        ("approach_timeout", "Approach timeout", "s", "", True),
        ("release_timeout", "Release timeout", "s", "", True),
        ("second_approach_timeout", "Second approach timeout", "s", "", True),
    ]},
    {"key": "steps", "title": "Steps per unit", "advanced": True, "fields": [
        ("x", "X", "steps/mm", "Changing this changes every X distance. Re-home afterwards.", True),
        ("y", "Y", "steps/mm", "Re-home afterwards.", True),
        ("z", "Z", "steps/mm", "", True),
        ("a", "Rotation", "steps/°", "Both nozzles.", True),
    ]},
    {"key": "controller", "title": "Controller connection", "advanced": True, "fields": [
        ("host", "IP address", "", "Needs an app restart to take effect.", True),
        ("port", "Port", "", "Needs an app restart to take effect.", True),
        ("poll_interval_idle", "Status poll (idle)", "s", "", True),
        ("poll_interval_moving", "Status poll (moving)", "s", "", True),
        ("reconnect_delay", "Reconnect delay", "s", "Wait between connection attempts.", True),
    ]},
]

RESTART_FIELDS = {("controller", "host"), ("controller", "port")}


def _kind(default: Any, section: str, name: str) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, str):
        return "str"
    if isinstance(default, tuple):
        return "optional_pair" if (section, name) == ("head", "discard") else "pair"
    raise TypeError(f"{section}.{name}: unsupported type {type(default).__name__}")


def _plain(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


def describe(cfg: MachineConfig) -> list[dict[str, Any]]:
    """All editable values with labels, units, help, current value and default."""
    defaults = MachineConfig()
    out = []
    for sec in SECTIONS:
        cur, dflt = getattr(cfg, sec["key"]), getattr(defaults, sec["key"])
        fields = []
        for name, label, unit, help_, advanced in sec["fields"]:
            d = getattr(dflt, name)
            fields.append({
                "key": name, "label": label, "unit": unit, "help": help_,
                "advanced": advanced or sec["advanced"], "type": _kind(d, sec["key"], name),
                "value": _plain(getattr(cur, name)), "default": _plain(d),
            })
        out.append({"key": sec["key"], "title": sec["title"], "advanced": sec["advanced"], "fields": fields})
    return out


def _known(section: str) -> dict[str, Field]:
    for sec in SECTIONS:
        if sec["key"] == section:
            return {f[0]: f for f in sec["fields"]}
    raise ValueError(f"unknown section {section!r}")


def validate(cfg: MachineConfig, changes: dict[str, dict[str, Any]]) -> MachineConfig:
    """New config = current config + changes, fully checked. Raises ValueError."""
    data: dict[str, dict[str, Any]] = {}
    for f in dataclasses.fields(MachineConfig):
        sec = getattr(cfg, f.name)
        data[f.name] = {g.name: _plain(getattr(sec, g.name)) for g in dataclasses.fields(sec)}
    for section, values in changes.items():
        known = _known(section)
        if not isinstance(values, dict):
            raise ValueError(f"{section}: expected a mapping")
        for name, value in values.items():
            if name not in known:
                raise ValueError(f"{section}.{name} can't be edited here")
            data[section][name] = value
    try:
        new = _build(MachineConfig, data, "defaults")
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    _check(new)
    return new


def _check(c: MachineConfig) -> None:
    def label(section: str, name: str) -> str:
        return _known(section)[name][1]

    for sec in SECTIONS:
        obj = getattr(c, sec["key"])
        for name, *_ in sec["fields"]:
            v = getattr(obj, name)
            nums = v if isinstance(v, tuple) else (v,)
            for n in nums:
                if isinstance(n, (int, float)) and not isinstance(n, bool) and not math.isfinite(n):
                    raise ValueError(f"{label(sec['key'], name)}: not a number")
    for axis in ("x", "y"):
        lo, hi = getattr(c.limits, axis)
        if not lo < hi:
            raise ValueError(f"{label('limits', axis)}: min must be below max")
    positive = [("limits", "z1_max"), ("limits", "z2_max"), ("head", "needle_timeout"), ("vision", "search_mm"),
                ("motion", "xy_speed"), ("motion", "z_speed"), ("motion", "a_speed"), ("motion", "step_quantum"),
                ("motion", "settle_polls"), ("steps", "x"), ("steps", "y"), ("steps", "z"), ("steps", "a"),
                ("homing", "back_off_mm"), ("homing", "approach_mm"), ("controller", "poll_interval_idle"),
                ("controller", "poll_interval_moving"), ("controller", "port")]
    for section, name in positive:
        if not getattr(getattr(c, section), name) > 0:
            raise ValueError(f"{label(section, name)} must be greater than 0")
    not_negative = [("head", n) for n in ("pick_depth", "place_depth", "pick_dwell", "place_dwell", "blow_time",
                                          "vacuum_settle", "vacuum_timeout", "prick_settle")]
    not_negative += [("vision", n) for n in ("settle", "size_tolerance", "max_offset_mm", "max_angle_deg")]
    not_negative += [("head", "bed_depth"), ("head", "part_height")]
    not_negative += [("motion", "min_timeout"), ("motion", "max_unhomed_jog")]
    for section, name in not_negative:
        if getattr(getattr(c, section), name) < 0:
            raise ValueError(f"{label(section, name)} can't be negative")
    for depth in ("pick_depth", "place_depth"):
        if getattr(c.head, depth) > max(c.limits.z1_max, c.limits.z2_max):
            raise ValueError(f"{label('head', depth)} is deeper than the nozzles' lowest point")
    # With the bed depth set, place depths are computed from it: the deepest the nozzle may
    # reach is the same limit the fixed depths are held to.
    if c.head.bed_depth > max(c.limits.z1_max, c.limits.z2_max):
        raise ValueError(f"{label('head', 'bed_depth')} is deeper than the nozzles' lowest point")
    if len(c.head.discard) not in (0, 2):
        raise ValueError("Discard bin: give X and Y, or leave both empty")
    for name in ("job_speed_level", "jog_speed_level"):
        if not 1 <= getattr(c.motion, name) <= 11:
            raise ValueError(f"{label('motion', name)} must be 1–11")
    if not 0 <= c.vision.min_brightness <= 255:
        raise ValueError("Brightness threshold must be 0–255")
    if len(c.vision.tip_diameter) != 2 or min(c.vision.tip_diameter) <= 0:
        raise ValueError("Nozzle tip diameters: give two values above 0")
    if not c.controller.host.strip():
        raise ValueError("IP address can't be empty")


def _yaml_value(v: Any) -> Any:
    if isinstance(v, tuple):
        seq = CommentedSeq(list(v))
        seq.fa.set_flow_style()  # [a, b] like the rest of the file
        return seq
    return v


def save(path: str | Path, old: MachineConfig, new: MachineConfig) -> list[str]:
    """Write the sections that changed into machine.yaml, keeping its comments.
    Returns 'section.field' for every changed value."""
    path = Path(path)
    yaml = YAML()  # round-trip: comments and order are kept
    yaml.preserve_quotes = True
    yaml.width = 4096
    doc = yaml.load(path.read_text()) if path.exists() else None
    if doc is None:
        doc = CommentedMap()
    changed: list[str] = []
    for sec in SECTIONS:
        key = sec["key"]
        o, n = getattr(old, key), getattr(new, key)
        for name, *_ in sec["fields"]:
            if getattr(o, name) != getattr(n, name):
                if key not in doc or doc[key] is None:
                    doc[key] = CommentedMap()
                doc[key][name] = _yaml_value(getattr(n, name))
                changed.append(f"{key}.{name}")
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(doc, f)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return changed


def apply_live(cfg: MachineConfig, new: MachineConfig) -> None:
    """Copy new values into the running config objects in place, so everything that
    holds a reference (HeadOps, runner, machine) sees them at once."""
    for sec in SECTIONS:
        if sec["key"] == "controller":
            continue  # host/port need a restart; poll values are copied below
        target, source = getattr(cfg, sec["key"]), getattr(new, sec["key"])
        for name, *_ in sec["fields"]:
            setattr(target, name, getattr(source, name))
    for name in ("poll_interval_idle", "poll_interval_moving", "reconnect_delay"):
        setattr(cfg.controller, name, getattr(new.controller, name))


def warnings_for(changed: list[str]) -> list[str]:
    out = []
    if any(tuple(c.split(".")) in RESTART_FIELDS for c in changed):
        out.append("Controller IP/port saved: restart the app to connect with them.")
    if any(c.startswith("steps.") or c in ("homing.x_register", "homing.y_register") for c in changed):
        out.append("Steps or homing values changed: home the machine again before relying on positions.")
    return out
