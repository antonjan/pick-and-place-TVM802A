"""Feeder, pick-and-place and head endpoints (see docs/phase3-design.md, section 6)."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pnp.api.errors import status_for
from pnp.job.feeders import Feeder, FeederError, FeederStore
from pnp.job.head_ops import HeadOps, NotCalibrated, PickError
from pnp.machine.machine import MachineError


class FeederIn(BaseModel):
    """Body of PUT /api/feeders/{id}: the feeder fields (the id comes from the path)."""

    name: str = ""
    kind: str = "strip"
    pick: list[float] = [0.0, 0.0]
    direction: list[float] = [1.0, 0.0]
    pitch: float = 4.0
    count: int = 0
    capacity: int | None = None
    rotation: float = 0.0
    pick_depth: float = 3.0
    prick: list[float] | None = None
    needs_feed: bool = True
    enabled: bool = True
    value: str = ""
    package: str = ""


class PickReq(BaseModel):
    nozzle: int


class PickAndPlaceReq(BaseModel):
    nozzle: int
    rotation: float = 0.0
    depth: float | None = None


class PlaceReq(BaseModel):
    nozzle: int
    x: float | None = None  # default: current head (camera centre) X
    y: float | None = None  # default: current head (camera centre) Y
    rotation: float = 0.0
    depth: float | None = None


class DiscardReq(BaseModel):
    nozzle: int


@contextlib.contextmanager
def _mapped() -> Iterator[None]:
    """Translate feeder/head/machine errors into HTTP status codes."""
    try:
        yield
    except FeederError as exc:
        raise HTTPException(404 if "unknown" in str(exc) else 409, str(exc)) from exc
    except PickError as exc:
        raise HTTPException(422, str(exc)) from exc
    except NotCalibrated as exc:
        raise HTTPException(409, str(exc)) from exc
    except MachineError as exc:
        raise HTTPException(status_for(exc), str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def feeder_json(f: Feeder) -> dict:
    out = {fld.name: getattr(f, fld.name) for fld in dataclasses.fields(Feeder)}
    out["pick"] = list(f.pick)
    out["direction"] = list(f.direction)
    out["prick"] = list(f.prick) if f.prick is not None else None
    out["pick_location"] = list(f.pick_location())
    out["remaining"] = f.remaining()
    return out


def add_feeder_routes(app: FastAPI, machine, feeders_path: Path) -> None:
    """Must run after add_vision_routes (uses app.state.calibration)."""
    calibration = app.state.calibration
    store = FeederStore(feeders_path)
    head = HeadOps(machine, calibration, machine.cfg.head, store)
    app.state.feeders = store
    app.state.head = head

    def need_homed() -> None:
        if not machine.homed:
            raise HTTPException(409, "home the machine first")

    def idle() -> None:
        if head.busy:
            raise HTTPException(409, f"head busy ({head.busy})")

    def head_xy() -> tuple[float, float]:
        pos = machine.position()
        return float(pos["x"]), float(pos["y"])

    def nozzle_no(n: int) -> int:
        if n not in (1, 2):
            raise HTTPException(400, "nozzle must be 1 or 2")
        return n

    def get_feeder(id: str) -> Feeder:
        with _mapped():
            return store.get(id)

    def reach(tool: int | str, W) -> None:
        """Refuse a target the head cannot reach, before anything moves (no part lost mid-way)."""
        if not machine.homed:
            return
        x, y = head.head_for(tool, W)
        lim = machine.cfg.limits
        if not (lim.x[0] <= x <= lim.x[1] and lim.y[0] <= y <= lim.y[1]):
            raise HTTPException(400, f"point ({W[0]:.2f}, {W[1]:.2f}) is out of reach: the head would go to "
                                     f"X {x:.2f} Y {y:.2f}, soft limits X {lim.x} Y {lim.y}")

    def holding() -> dict[str, str | None]:
        return {str(n): head.holding.get(n) for n in (1, 2)}

    # ------------------------------------------------------------ feeders

    @app.get("/api/feeders")
    async def list_feeders():
        return [feeder_json(f) for f in store.all()]

    @app.put("/api/feeders/{id}")
    async def put_feeder(id: str, body: FeederIn):
        idle()
        with _mapped():
            feeder = Feeder(id=id, **body.model_dump())
            store.upsert(feeder)
        return feeder_json(feeder)

    @app.delete("/api/feeders/{id}")
    async def delete_feeder(id: str):
        idle()
        with _mapped():
            store.delete(id)
        return {"ok": True}

    @app.post("/api/feeders/{id}/teach/pick")
    async def teach_pick(id: str):
        feeder = get_feeder(id)
        need_homed()
        idle()
        x, y = head_xy()
        changes = {"pick": (x, y)}
        if feeder.kind == "strip":
            changes["count"] = 0
        updated = dataclasses.replace(feeder, **changes)
        with _mapped():
            store.upsert(updated)
        return feeder_json(updated)

    @app.post("/api/feeders/{id}/teach/prick")
    async def teach_prick(id: str):
        feeder = get_feeder(id)
        need_homed()
        idle()
        x, y = head_xy()
        updated = dataclasses.replace(feeder, prick=(x, y))
        with _mapped():
            store.upsert(updated)
        return feeder_json(updated)

    @app.post("/api/feeders/{id}/goto")
    async def goto(id: str):
        feeder = get_feeder(id)
        idle()
        x, y = feeder.pick_location()
        with _mapped():
            pos = await machine.move_to(x=x, y=y)
        return {"position": pos}

    @app.post("/api/feeders/{id}/feed")
    async def feed(id: str):
        feeder = get_feeder(id)
        with _mapped():
            await head.feed(feeder)
        return feeder_json(feeder)

    @app.post("/api/feeders/{id}/pick")
    async def pick(id: str, req: PickReq):
        nozzle_no(req.nozzle)
        feeder = get_feeder(id)
        with _mapped():
            reach(req.nozzle, feeder.pick_location())
            await head.pick(req.nozzle, feeder)
        return feeder_json(feeder)

    @app.post("/api/feeders/{id}/pick-and-place")
    async def pick_and_place(id: str, req: PickAndPlaceReq):
        nozzle_no(req.nozzle)
        feeder = get_feeder(id)
        need_homed()
        idle()
        target = head_xy()  # the camera crosshair, captured now
        with _mapped():
            reach(req.nozzle, feeder.pick_location())
            reach(req.nozzle, target)
            await head.pick_and_place(req.nozzle, feeder, target, req.rotation, req.depth)
        return feeder_json(feeder)

    # ------------------------------------------------------------ head

    @app.get("/api/head")
    async def get_head():
        return {"busy": head.busy, "holding": holding()}

    @app.post("/api/head/place")
    async def place(req: PlaceReq):
        nozzle_no(req.nozzle)
        if req.x is None or req.y is None:
            need_homed()
        idle()
        cx, cy = head_xy()
        target = (cx if req.x is None else req.x, cy if req.y is None else req.y)
        with _mapped():
            reach(req.nozzle, target)
            await head.place(req.nozzle, target, req.rotation, req.depth)
        return {"holding": holding()}

    @app.post("/api/head/discard")
    async def discard(req: DiscardReq):
        nozzle_no(req.nozzle)
        with _mapped():
            await head.discard(req.nozzle)
        return {"holding": holding()}
