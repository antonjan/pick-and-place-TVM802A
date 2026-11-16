"""Feeder bank endpoints: banks, generated lanes and saved layouts (docs/phase4-design.md, section 7)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from pnp.api.feeder_routes import feeder_json
from pnp.job.banks import SPROCKET_ALONG, Bank, BankError, BankStore, GenerateSpec, LaneSpec
from pnp.job.feeders import FeederStore


class BankIn(BaseModel):
    name: str = ""


class LaneIn(BaseModel):
    width: float = 8.0
    pitch: float = 4.0
    offset: float | None = None


class GenerateIn(BaseModel):
    origin: list[float] = Field(min_length=2, max_length=2)
    lane_axis: list[float] = Field(default=[1.0, 0.0], min_length=2, max_length=2)
    feed_direction: list[float] = Field(default=[0.0, -1.0], min_length=2, max_length=2)
    gap: float = 0.0
    sprocket_side: int = -1
    prick_along: float = -SPROCKET_ALONG
    pick_depth: float = 3.0
    lanes: list[LaneIn] = Field(default_factory=list)
    keep_parts: bool = True
    rotation_offset: float | None = None  # deg for every lane; None = keep each lane's


class RotationIn(BaseModel):
    offset: float  # deg, + = anticlockwise, -90 = parts placed 90 deg more clockwise


class LayoutIn(BaseModel):
    name: str


@contextlib.contextmanager
def _mapped() -> Iterator[None]:
    try:
        yield
    except BankError as exc:
        raise HTTPException(404 if "unknown" in str(exc) else 409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def bank_json(bank: Bank, feeders: FeederStore) -> dict:
    return {
        "id": bank.id,
        "name": bank.name,
        "active_layout": bank.active_layout,
        "layouts": list(bank.layouts),
        "lanes": [feeder_json(f) for f in feeders.by_bank(bank.id)],
    }


class FeederImportIn(BaseModel):
    content: str                 # the exported YAML file's text
    mode: str = "replace"        # "replace" everything, or "merge" by id
    reset_counts: bool = True    # start every lane fresh (new tape)


def add_bank_routes(app: FastAPI, machine, banks_path: Path) -> None:
    """Must run after add_feeder_routes (uses app.state.feeders and app.state.head)."""
    feeders: FeederStore = app.state.feeders
    head = app.state.head
    banks = BankStore(banks_path, feeders)
    app.state.banks = banks

    def idle() -> None:
        """Bank edits move lanes, so they wait for a running job and for the head."""
        runner = getattr(app.state, "runner", None)
        if runner is not None and runner.busy():
            raise HTTPException(409, "job running")
        if head.busy:
            raise HTTPException(409, f"head busy ({head.busy})")

    def view(id: str) -> dict:
        with _mapped():
            return bank_json(banks.get(id), feeders)

    @app.get("/api/feeders/export")
    async def export_feeders():
        """Download all feeders, banks and bank layouts as one YAML file."""
        import datetime as _dt

        from fastapi.responses import Response

        from pnp.job.feeder_config import export_yaml

        name = f"feeders-{_dt.datetime.now().strftime('%Y-%m-%d-%H%M')}.yaml"
        return Response(export_yaml(feeders, banks), media_type="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/feeders/import")
    async def import_feeders(req: FeederImportIn):
        """Load an exported feeder file. The whole file is checked before anything changes."""
        from pnp.job.feeder_config import apply_config, parse_config

        idle()
        try:
            new_feeders, new_banks = parse_config(req.content)
            result = apply_config(feeders, banks, new_feeders, new_banks,
                                  mode=req.mode, reset_counts=req.reset_counts)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return result

    @app.get("/api/banks")
    async def list_banks():
        return [bank_json(b, feeders) for b in banks.all()]

    @app.put("/api/banks/{id}")
    async def put_bank(id: str, body: BankIn):
        idle()
        with _mapped():
            bank = banks.upsert(id, body.name.strip())
            return bank_json(bank, feeders)

    @app.delete("/api/banks/{id}")
    async def delete_bank(id: str):
        idle()
        with _mapped():
            banks.delete(id)
        return {"ok": True}

    @app.post("/api/banks/{id}/generate")
    async def generate(id: str, body: GenerateIn):
        idle()
        with _mapped():
            spec = GenerateSpec(
                origin=(body.origin[0], body.origin[1]),
                lane_axis=(body.lane_axis[0], body.lane_axis[1]),
                feed_direction=(body.feed_direction[0], body.feed_direction[1]),
                gap=body.gap,
                sprocket_side=body.sprocket_side,
                prick_along=body.prick_along,
                pick_depth=body.pick_depth,
                lanes=[LaneSpec(width=lane.width, pitch=lane.pitch, offset=lane.offset) for lane in body.lanes],
                keep_parts=body.keep_parts,
                rotation_offset=body.rotation_offset,
            )
            banks.generate(id, spec)
        return view(id)

    @app.post("/api/banks/{id}/rotation")
    async def set_rotation(id: str, body: RotationIn):
        """Same rotation offset for every lane of the stack (the tape orientation of the stack)."""
        idle()
        with _mapped():
            banks.set_rotation_offset(id, body.offset)
        return view(id)

    @app.post("/api/banks/{id}/layouts")
    async def save_layout(id: str, body: LayoutIn):
        idle()
        with _mapped():
            banks.save_layout(id, body.name)
        return view(id)

    @app.post("/api/banks/{id}/layouts/{name}/load")
    async def load_layout(id: str, name: str):
        idle()
        with _mapped():
            banks.load_layout(id, name)
        return view(id)

    @app.delete("/api/banks/{id}/layouts/{name}")
    async def delete_layout(id: str, name: str):
        idle()
        with _mapped():
            banks.delete_layout(id, name)
        return view(id)
