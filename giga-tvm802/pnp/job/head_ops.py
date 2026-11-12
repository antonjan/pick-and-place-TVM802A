"""Pick and place with the head: feeders, nozzles, the feeder prick needle, place and discard.

Coordinates: a tool with offset t (nozzle n or the prick needle) reaches world
point W when the head (down camera centre) is at W - t.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from pnp.config import HeadConfig
from pnp.job.feeders import Feeder, FeederError, FeederStore
from pnp.machine.machine import Machine, MachineError
from pnp.vision.calibration_store import Calibration


class NotCalibrated(MachineError):
    pass


class PickError(MachineError):
    pass


class HeadConflict(MachineError):
    """The request conflicts with the head's current state (busy, nozzle full, no discard bin)."""


class HeadOps:
    def __init__(self, machine: Machine, calibration: Calibration, cfg: HeadConfig,
                 feeders: FeederStore) -> None:
        self.machine = machine
        self.calibration = calibration
        self.cfg = cfg
        self.feeders = feeders
        self.busy: str | None = None  # "picking" / "placing" / "feeding" / "moving" / "discarding" / None
        self.holding: dict[int, str | None] = {1: None, 2: None}  # feeder id of the part on each nozzle
        self._tape_rotation: dict[int, float] = {}  # rotation of the held part in its tape
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers

    def head_for(self, tool: int | str, W) -> tuple[float, float]:
        """Head position that puts `tool` (1, 2 or "prick") over world point W."""
        if tool in (1, 2):
            t = self.calibration.nozzle_offset.get(tool)
            if t is None:
                raise NotCalibrated(f"nozzle {tool} has no offset: run the nozzle offset calibration")
        elif tool == "prick":
            t = self.calibration.prick_offset
            if t is None:
                raise NotCalibrated("feeder needle has no offset: run the prick offset calibration")
        else:
            raise ValueError(f"unknown tool {tool!r}")
        return (float(W[0]) - t[0], float(W[1]) - t[1])

    @contextlib.asynccontextmanager
    async def _exclusive(self, what: str) -> AsyncIterator[None]:
        if self._lock.locked():
            raise HeadConflict(f"head busy ({self.busy})")
        async with self._lock:
            self.busy = what
            try:
                yield
            finally:
                self.busy = None

    async def _move_tool(self, tool: int | str, W) -> None:
        x, y = self.head_for(tool, W)
        await self.machine.move_to(x=x, y=y)

    async def _safe_off(self, nozzle: int) -> None:
        """Best effort after a failure: vacuum off, nozzles up."""
        with contextlib.suppress(MachineError):
            await self.machine.set_output(f"vacuum{nozzle}", False)
        with contextlib.suppress(MachineError):
            await self.machine.nozzles_up()

    # ------------------------------------------------------------------ public operations

    async def move_tool_over(self, tool: int | str, W) -> None:
        """Move the head so that `tool` sits over world point W (nozzles go up first)."""
        async with self._exclusive("moving"):
            await self._move_tool(tool, W)

    async def feed(self, feeder: Feeder) -> None:
        async with self._exclusive("feeding"):
            await self._feed(feeder)

    async def pick(self, nozzle: int, feeder: Feeder) -> None:
        async with self._exclusive("picking"):
            await self._pick(nozzle, feeder)

    async def place(self, nozzle: int, W, rotation: float = 0.0, depth: float | None = None) -> None:
        async with self._exclusive("placing"):
            await self._place(nozzle, W, rotation, depth)

    async def discard(self, nozzle: int) -> None:
        async with self._exclusive("discarding"):
            if len(self.cfg.discard) != 2:
                raise HeadConflict("discard position not set (head.discard in machine.yaml)")
            await self._move_tool(nozzle, self.cfg.discard)
            await self.machine.set_output(f"vacuum{nozzle}", False)
            await self.machine.set_output(f"blow{nozzle}", True)
            await asyncio.sleep(self.cfg.blow_time)
            await self.machine.set_output(f"blow{nozzle}", False)
            await self.machine.nozzles_up()
            self.holding[nozzle] = None
            self._tape_rotation.pop(nozzle, None)

    async def pick_and_place(self, nozzle: int, feeder: Feeder, W, rotation: float = 0.0,
                             depth: float | None = None) -> None:
        async with self._exclusive("picking"):
            await self._pick(nozzle, feeder)
            self.busy = "placing"
            await self._place(nozzle, W, rotation, depth)

    # ------------------------------------------------------------------ operations (lock held)

    async def _feed(self, feeder: Feeder) -> None:
        if feeder.kind != "drag":
            raise FeederError(f"feeder {feeder.id} is not a drag feeder")
        if feeder.prick is None:
            raise FeederError(f"feeder {feeder.id} has no prick position")
        dx, dy = feeder.drag_vector()
        await self._move_tool("prick", feeder.prick)
        on = False
        try:
            on = True
            await self.machine.set_output("prick", True)
            await asyncio.sleep(self.cfg.prick_settle)
            pos = self.machine.position()
            await self.machine.move_to(x=pos["x"] + dx, y=pos["y"] + dy)
            await self.machine.set_output("prick", False)
            on = False
            await asyncio.sleep(self.cfg.prick_settle)
        finally:
            if on:
                with contextlib.suppress(MachineError):
                    await self.machine.set_output("prick", False)
        feeder.after_feed()
        self.feeders.save()

    async def _pick(self, nozzle: int, feeder: Feeder) -> None:
        if self.holding.get(nozzle):
            raise HeadConflict(f"nozzle {nozzle} already holds a part from {self.holding[nozzle]}: "
                               "place or discard it first")
        if not feeder.enabled:
            raise FeederError(f"feeder {feeder.id} is disabled")
        if feeder.remaining() == 0:
            raise FeederError(f"feeder {feeder.id} is empty")
        if feeder.kind == "drag" and feeder.needs_feed:
            await self._feed(feeder)

        await self.machine.set_output("pump", True)
        await self.machine.rotate(nozzle, 0.0)
        await self._move_tool(nozzle, feeder.pick_location())
        await self.machine.set_output(f"vacuum{nozzle}", True)
        try:
            await self.machine.nozzle_down(nozzle, feeder.pick_depth)
            await asyncio.sleep(self.cfg.pick_dwell)
            await self.machine.nozzles_up()
            await asyncio.sleep(self.cfg.vacuum_settle)
            if self.cfg.check_vacuum and not await self.machine.has_part(nozzle):
                raise PickError(f"no part on nozzle {nozzle} after picking from {feeder.id}")
        except BaseException:
            await self._safe_off(nozzle)
            raise

        feeder.after_pick()
        self.feeders.save()
        self.holding[nozzle] = feeder.id
        self._tape_rotation[nozzle] = feeder.rotation

    async def _place(self, nozzle: int, W, rotation: float, depth: float | None) -> None:
        tape = self._tape_rotation.get(nozzle, 0.0)
        await self.machine.rotate(nozzle, rotation - tape)
        await self._move_tool(nozzle, W)
        released = False
        try:
            await self.machine.nozzle_down(nozzle, self.cfg.place_depth if depth is None else depth)
            await asyncio.sleep(self.cfg.place_dwell)
            released = True
            await self.machine.set_output(f"vacuum{nozzle}", False)
            await self.machine.set_output(f"blow{nozzle}", True)
            await asyncio.sleep(self.cfg.blow_time)
            await self.machine.set_output(f"blow{nozzle}", False)
        except BaseException:
            if released:
                await self._safe_off(nozzle)
            else:
                # Not at the release yet: keep holding the part, just lift it clear.
                with contextlib.suppress(MachineError):
                    await self.machine.nozzles_up()
            raise
        await self.machine.nozzles_up()
        self.holding[nozzle] = None
        self._tape_rotation.pop(nozzle, None)
