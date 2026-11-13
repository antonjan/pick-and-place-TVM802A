"""Export and import the whole feeder configuration (feeders + banks + bank layouts).

One YAML file holds everything a project needs, so a set of feeders can be saved
per project and loaded again later instead of re-teaching every lane:

    format: tvm802-feeders
    version: 1
    exported: 2026-11-13T14:05:00+00:00
    feeders: [ {feeder fields}, ... ]
    banks:   [ {id, name, active_layout, layouts: {name: [feeder fields, ...]}}, ... ]

Import checks the whole file before changing anything.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import yaml

from pnp.job.banks import BANK_ID, Bank, BankStore
from pnp.job.feeders import Feeder, FeederStore, feeder_from_dict, feeder_to_dict

FORMAT = "tvm802-feeders"
VERSION = 1


def export_config(feeders: FeederStore, banks: BankStore) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "feeders": [feeder_to_dict(f) for f in feeders.all()],
        "banks": [
            {"id": b.id, "name": b.name, "active_layout": b.active_layout,
             "layouts": {name: list(rows) for name, rows in b.layouts.items()}}
            for b in banks.all()
        ],
    }


def export_yaml(feeders: FeederStore, banks: BankStore) -> str:
    return yaml.safe_dump(export_config(feeders, banks), sort_keys=False)


def parse_config(text: str) -> tuple[list[Feeder], list[Bank]]:
    """Validate an exported file. Raises ValueError naming the first problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not a valid YAML file: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("not a feeder configuration exported from this app (format: tvm802-feeders missing)")
    if data.get("version") != VERSION:
        raise ValueError(f"unsupported feeder file version {data.get('version')!r} (expected {VERSION})")

    feeders: list[Feeder] = []
    seen: set[str] = set()
    for i, entry in enumerate(data.get("feeders") or []):
        try:
            f = feeder_from_dict(entry)
            f.validate()
        except (ValueError, TypeError) as exc:
            raise ValueError(f"feeder {i + 1}: {exc}") from exc
        if f.id in seen:
            raise ValueError(f"feeder id {f.id!r} appears twice")
        seen.add(f.id)
        feeders.append(f)

    banks: list[Bank] = []
    bank_ids: set[str] = set()
    for i, entry in enumerate(data.get("banks") or []):
        if not isinstance(entry, dict) or "id" not in entry:
            raise ValueError(f"bank {i + 1}: must be a mapping with an id")
        bank_id = str(entry["id"])
        if not BANK_ID.fullmatch(bank_id):
            raise ValueError(f"bank {i + 1}: bad id {bank_id!r}")
        if bank_id in bank_ids:
            raise ValueError(f"bank id {bank_id!r} appears twice")
        bank_ids.add(bank_id)
        layouts: dict[str, list[dict]] = {}
        for lname, rows in (entry.get("layouts") or {}).items():
            try:
                layouts[str(lname)] = [feeder_to_dict(feeder_from_dict(row)) for row in rows or []]
            except (ValueError, TypeError) as exc:
                raise ValueError(f"bank {bank_id!r} layout {lname!r}: {exc}") from exc
        active = entry.get("active_layout")
        banks.append(Bank(id=bank_id, name=str(entry.get("name") or ""),
                          active_layout=str(active) if active is not None else None, layouts=layouts))

    for f in feeders:  # a lane must belong to a bank that exists after the import
        if f.bank and f.bank not in bank_ids:
            raise ValueError(f"feeder {f.id!r} belongs to bank {f.bank!r}, which is not in the file")
    return feeders, banks


def apply_config(feeder_store: FeederStore, bank_store: BankStore, feeders: list[Feeder], banks: list[Bank],
                 *, mode: str = "replace", reset_counts: bool = True) -> dict[str, int]:
    """Install parsed feeders/banks. mode "replace": the file becomes the whole
    configuration; "merge": add or update by id, keep everything else.
    reset_counts: start every lane fresh (count 0, drag lanes need a feed)."""
    if mode not in ("replace", "merge"):
        raise ValueError(f"unknown import mode {mode!r}")
    if reset_counts:
        for f in feeders:
            f.count = 0
            f.needs_feed = True

    new_feeders: dict[str, Feeder] = {} if mode == "replace" else {f.id: f for f in feeder_store.all()}
    new_feeders.update({f.id: f for f in feeders})
    new_banks: dict[str, Bank] = {} if mode == "replace" else {b.id: b for b in bank_store.all()}
    for b in banks:
        if mode == "merge" and b.id in new_banks:
            merged = new_banks[b.id]
            merged.name = b.name or merged.name
            merged.layouts.update(b.layouts)
            merged.active_layout = b.active_layout or merged.active_layout
        else:
            new_banks[b.id] = b
    for f in new_feeders.values():
        if f.bank and f.bank not in new_banks:
            raise ValueError(f"feeder {f.id!r} belongs to bank {f.bank!r}, which would not exist after the import")

    # Everything checked: write both stores.
    feeder_store._feeders = new_feeders  # noqa: SLF001 - store-internal swap, then one save
    feeder_store.save()
    bank_store._banks = new_banks  # noqa: SLF001
    bank_store.save()
    return {"feeders": len(new_feeders), "banks": len(new_banks), "imported_feeders": len(feeders),
            "imported_banks": len(banks)}
