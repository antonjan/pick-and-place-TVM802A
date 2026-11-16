"""Pick and place with the head: feeders, nozzles, the feeder prick needle, place and discard.

Coordinates: a tool with offset t (nozzle n or the prick needle) reaches world
point W when the head (down camera centre) is at W - t.

Bottom vision (docs/phase6-design.md, section 4): after a pick the head moves over the up
camera, the part is measured, and the place move is corrected for its offset and angle error.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pnp.config import HeadConfig, VisionConfig
from pnp.job.feeders import Feeder, FeederError, FeederStore
from pnp.machine.machine import EStopped, Machine, MachineError
from pnp.vision.bottom import InspectError, PartView, angle_error
from pnp.vision.calibration_store import Calibration

log = logging.getLogger(__name__)

Inspector = Callable[[int], Awaitable[tuple[PartView | None, float]]]


class NotCalibrated(MachineError):
    pass


class PickError(MachineError):
    pass


class HeadConflict(MachineError):
    """The request conflicts with the head's current state (busy, nozzle full, no discard bin)."""


@dataclass
class Inspection:
    """The last inspection of a nozzle, for the API and UI."""

    nozzle: int
    result: str  # "ok" | "skipped" | "no part" | "rejected"
    message: str
    offset: list[float] | None  # mm
    angle_error: float | None  # deg
    size: list[float] | None  # mm (length, width)
    at: str  # ISO UTC


def _expected_axis(feeder: Feeder) -> float:
    """World angle (deg) of the part's long side on the tape, at the pick (nozzle at 0 deg)."""
    dx, dy = feeder.direction
    return math.degrees(math.atan2(dy, dx)) + feeder.rotation


def _size_matches(measured, expected, tolerance: float) -> bool:
    """Compare two (length, width) pairs after sorting: each side within `tolerance` of the expected."""
    m = sorted((float(v) for v in measured), reverse=True)
    e = sorted((float(v) for v in expected), reverse=True)
    return all(abs(a - b) <= tolerance * b for a, b in zip(m, e))


class HeadOps:
    def __init__(self, machine: Machine, calibration: Calibration, cfg: HeadConfig,
                 feeders: FeederStore, *, inspector: Inspector | None = None) -> None:
        self.machine = machine
        self.calibration = calibration
        self.cfg = cfg
        self.feeders = feeders
        self.inspector = inspector  # (nozzle) -> (PartView | None, nozzle angle); None = no up camera
        self.vision_cfg: VisionConfig = getattr(getattr(machine, "cfg", None), "vision", None) or VisionConfig()
        self.busy: str | None = None  # "picking" / "placing" / "feeding" / "moving" / "discarding" / "inspecting" / None
        self.holding: dict[int, str | None] = {1: None, 2: None}  # feeder id of the part on each nozzle
        self.last_inspection: dict[int, Inspection] = {}
        self._tape_rotation: dict[int, float] = {}  # rotation of the held part in its tape
        self._correction: dict[int, tuple[tuple[float, float], float, float]] = {}  # (offset, angle error, a0)
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

    def exclusive(self, what: str):
        """Hold the head for `what` (an async context manager; HeadConflict if it is busy)."""
        return self._exclusive(what)

    async def _move_tool(self, tool: int | str, W) -> None:
        x, y = self.head_for(tool, W)
        await self.machine.move_to(x=x, y=y)

    async def _safe_off(self, nozzle: int) -> None:
        """Best effort after a failure: vacuum off, nozzles up."""
        with contextlib.suppress(MachineError):
            await self.machine.set_output(f"vacuum{nozzle}", False)
        with contextlib.suppress(MachineError):
            await self.machine.nozzles_up()

    def inspection_json(self) -> dict[str, dict | None]:
        """The last inspection of each nozzle as JSON-safe dicts, keys "1" and "2"."""
        out: dict[str, dict | None] = {}
        for n in (1, 2):
            insp = self.last_inspection.get(n)
            out[str(n)] = dataclasses.asdict(insp) if insp is not None else None
        return out

    # ------------------------------------------------------------------ public operations

    async def move_tool_over(self, tool: int | str, W) -> None:
        """Move the head so that `tool` sits over world point W (nozzles go up first)."""
        async with self._exclusive("moving"):
            await self._move_tool(tool, W)

    async def feed(self, feeder: Feeder) -> None:
        async with self._exclusive("feeding"):
            await self._feed(feeder)

    async def pick(self, nozzle: int, feeder: Feeder, *, vision: bool = True, body=None) -> None:
        async with self._exclusive("picking"):
            await self._pick(nozzle, feeder, vision=vision, body=body)

    async def place(self, nozzle: int, W, rotation: float = 0.0, depth: float | None = None) -> None:
        async with self._exclusive("placing"):
            await self._place(nozzle, W, rotation, depth)

    async def discard(self, nozzle: int) -> None:
        async with self._exclusive("discarding"):
            await self._discard(nozzle)

    async def pick_and_place(self, nozzle: int, feeder: Feeder, W, rotation: float = 0.0,
                             depth: float | None = None, *, vision: bool = True, body=None) -> None:
        async with self._exclusive("picking"):
            await self._pick(nozzle, feeder, vision=vision, body=body)
            self.busy = "placing"
            await self._place(nozzle, W, rotation, depth)

    async def reset_nozzles(self, nozzles: tuple[int, ...] = (1, 2)) -> None:
        """Forget what the nozzles hold: vacuum off, nozzles up, holding cleared.

        For a pick that went wrong (nothing or a crooked part on the nozzle): the head
        otherwise refuses every new pick until a place or discard. A part still on the
        nozzle drops where the head is when the vacuum goes off. Doesn't move X/Y.
        """
        async with self._exclusive("resetting nozzles"):
            for n in nozzles:
                with contextlib.suppress(MachineError):
                    await self.machine.set_output(f"vacuum{n}", False)
            with contextlib.suppress(MachineError):
                await self.machine.nozzles_up()
            for n in nozzles:
                self.holding[n] = None
                self._tape_rotation.pop(n, None)
                self._correction.pop(n, None)
            log.info("nozzle state reset: %s", ", ".join(f"N{n}" for n in nozzles))

    async def inspect(self, nozzle: int) -> Inspection:
        """Measure whatever is on the nozzle from the up camera. Never rejects or discards."""
        async with self._exclusive("inspecting"):
            view, _ = await self._measure(nozzle)
            if view is None:
                return self._record(nozzle, "no part", f"No part on nozzle {nozzle}")
            err = None
            held = self.holding.get(nozzle)
            if held:
                with contextlib.suppress(FeederError):
                    err = angle_error(view.angle, _expected_axis(self.feeders.get(held)))
            return self._record(nozzle, "ok", f"Part on nozzle {nozzle} measured", view, err)

    # ------------------------------------------------------------------ operations (lock held)

    async def _feed(self, feeder: Feeder) -> None:
        await self.feed_move(feeder)
        await self.feed_drag(feeder)
        await self.feed_up(feeder)

    # Single stages of feed / pick / place. The step-by-step job (JobRunner.step) runs
    # them one per press; the full operations above call them in the same order. The
    # head lock must be held by the caller.

    @staticmethod
    def _check_feed(feeder: Feeder) -> None:
        if feeder.kind != "drag":
            raise FeederError(f"feeder {feeder.id} is not a drag feeder")
        if feeder.prick is None:
            raise FeederError(f"feeder {feeder.id} has no prick position")

    async def feed_move(self, feeder: Feeder) -> None:
        """Feed stage 1: put the needle over the lane's sprocket hole (needle up)."""
        self._check_feed(feeder)
        await self._move_tool("prick", feeder.prick)

    async def feed_drag(self, feeder: Feeder) -> None:
        """Feed stage 2: needle down, then drag the tape one pitch. The needle stays down."""
        self._check_feed(feeder)
        dx, dy = feeder.drag_vector()
        try:
            await self.machine.set_output("prick", True)
            await asyncio.sleep(self.cfg.prick_settle)
            pos = self.machine.position()
            with self.machine.needle_moves_allowed():  # the drag is the one move allowed with the needle down
                await self.machine.move_to(x=pos["x"] + dx, y=pos["y"] + dy)
        except BaseException:
            with contextlib.suppress(MachineError):
                await self.machine.set_output("prick", False)
            raise

    async def feed_up(self, feeder: Feeder) -> None:
        """Feed stage 3: needle up. The lane now has a part in the pick pocket."""
        try:
            await self.machine.set_output("prick", False)
            await asyncio.sleep(self.cfg.prick_settle)
        except BaseException:
            with contextlib.suppress(MachineError):
                await self.machine.set_output("prick", False)
            raise
        feeder.after_feed()
        self.feeders.save()

    async def _pick(self, nozzle: int, feeder: Feeder, *, vision: bool = True, body=None) -> None:
        self.check_pick(nozzle, feeder)
        if feeder.kind == "drag" and feeder.needs_feed:
            await self._feed(feeder)
        await self.pick_move(nozzle, feeder)
        await self.pick_down(nozzle, feeder)
        await self.pick_up(nozzle, feeder)
        await self._inspect_pick(nozzle, feeder, vision=vision, body=body)

    def check_pick(self, nozzle: int, feeder: Feeder) -> None:
        if self.holding.get(nozzle):
            raise HeadConflict(f"nozzle {nozzle} already holds a part from {self.holding[nozzle]}: "
                               "place or discard it first")
        if not feeder.enabled:
            raise FeederError(f"feeder {feeder.id} is disabled")
        if feeder.remaining() == 0:
            raise FeederError(f"feeder {feeder.id} is empty")

    async def pick_move(self, nozzle: int, feeder: Feeder) -> None:
        """Pick stage 1: pump on, nozzle at 0°, nozzle over the pick pocket (still up)."""
        await self.machine.set_output("pump", True)
        await self.machine.rotate(nozzle, 0.0)
        await self._move_tool(nozzle, feeder.pick_location())

    def _pick_interrupted(self, nozzle: int, feeder: Feeder) -> None:
        # Vacuum stays on until a place or discard (user request 2026-11-13): a part
        # may already be on the nozzle, so don't drop it here. The caller lifts the
        # nozzle; mark it as holding, so the next step must be Place or Discard.
        self.holding[nozzle] = feeder.id
        self._tape_rotation[nozzle] = feeder.rotation

    async def pick_down(self, nozzle: int, feeder: Feeder) -> None:
        """Pick stage 2: vacuum on, nozzle down onto the part (it stays down)."""
        await self.machine.set_output(f"vacuum{nozzle}", True)
        try:
            await self.machine.nozzle_down(nozzle, feeder.pick_depth)
            await asyncio.sleep(self.cfg.pick_dwell)
        except EStopped:
            raise  # E-stop already switched the vacuum off: nothing is held
        except BaseException:
            with contextlib.suppress(MachineError):
                await self.machine.nozzles_up()
            self._pick_interrupted(nozzle, feeder)
            raise

    async def pick_up(self, nozzle: int, feeder: Feeder) -> None:
        """Pick stage 3: nozzle up with the part; the part now counts as held."""
        try:
            await self.machine.nozzles_up()
            await asyncio.sleep(self.cfg.vacuum_settle)
        except EStopped:
            raise
        except BaseException:
            with contextlib.suppress(MachineError):
                await self.machine.nozzles_up()
            self._pick_interrupted(nozzle, feeder)
            raise
        if self.cfg.check_vacuum and not await self._wait_for_part(nozzle):
            # Only when the sensor check is switched on (head.check_vacuum): nothing
            # is held, so release the vacuum for the retry.
            await self._safe_off(nozzle)
            raise PickError(f"no part on nozzle {nozzle} after picking from {feeder.id} "
                            f"(vacuum sensor saw nothing within {self.cfg.vacuum_timeout:g} s)")

        feeder.after_pick()
        self.feeders.save()
        self.holding[nozzle] = feeder.id
        self._tape_rotation[nozzle] = feeder.rotation
        self._correction.pop(nozzle, None)

    async def inspect_pick(self, nozzle: int, feeder: Feeder, *, vision: bool, body) -> None:
        """Bottom vision for the held part (PickError after discarding a rejected part)."""
        await self._inspect_pick(nozzle, feeder, vision=vision, body=body)

    async def _place(self, nozzle: int, W, rotation: float, depth: float | None) -> None:
        await self.place_move(nozzle, W, rotation)
        await self.place_release(nozzle, depth)

    async def place_move(self, nozzle: int, W, rotation: float) -> None:
        """Place stage 1: turn the part and move it over its place (nozzle up),
        with the bottom-vision correction when the part was measured."""
        tape = self._tape_rotation.get(nozzle, 0.0)
        correction = self._correction.get(nozzle)
        if correction is None:
            await self.machine.rotate(nozzle, rotation - tape)
            await self._move_tool(nozzle, W)
        else:
            (px, py), err, a0 = correction
            await self.machine.rotate(nozzle, rotation - tape - err)
            # The angle actually reached (the controller may take the equivalent angle nearest the
            # current one), so the turn of the part is measured, not assumed.
            phi = math.radians(float(self.machine.position()[f"a{nozzle}"]) - a0)
            c, s = math.cos(phi), math.sin(phi)
            # The part centre is at tip + rot(phi) @ p, so put the tip where that lands on W.
            await self._move_tool(nozzle, (W[0] - (c * px - s * py), W[1] - (s * px + c * py)))

    async def place_release(self, nozzle: int, depth: float | None) -> None:
        """Place stage 2: nozzle down, release (vacuum off, blow), nozzle up."""
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
        self._correction.pop(nozzle, None)

    async def _discard(self, nozzle: int) -> None:
        """Blow the nozzle's part into the discard bin and raise the nozzle. The lock must be held."""
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
        self._correction.pop(nozzle, None)

    async def _wait_for_part(self, nozzle: int) -> bool:
        """Read the vacuum sensor until it reports a part or vacuum_timeout runs out.
        Real pneumatics need a moment to build up vacuum; a single read right after
        lifting called good picks failed on the machine (2026-11-13)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, self.cfg.vacuum_timeout)
        while True:
            if await self.machine.has_part(nozzle):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ bottom vision (lock held)

    def _record(self, nozzle: int, result: str, message: str, view: PartView | None = None,
                err: float | None = None) -> Inspection:
        insp = Inspection(
            nozzle=nozzle,
            result=result,
            message=message,
            offset=[float(v) for v in view.offset] if view is not None else None,
            angle_error=float(err) if err is not None else None,
            size=[float(v) for v in view.size] if view is not None else None,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.last_inspection[nozzle] = insp
        return insp

    def _skip_reason(self, nozzle: int, vision: bool) -> str | None:
        """Why the pick is not inspected, or None when it should be."""
        if not vision:
            return "vision off for this part"
        if not self.vision_cfg.bottom_vision:
            return "bottom vision off"
        if self.inspector is None:
            return "no up camera"
        if nozzle not in self.calibration.up_position:
            return f"nozzle {nozzle} has no up position: calibrate the up camera first"
        return None

    def _up_xy(self, nozzle: int) -> tuple[float, float]:
        t = self.calibration.up_position.get(nozzle)
        if t is None:
            raise NotCalibrated(f"nozzle {nozzle} has no up position: calibrate the up camera first")
        return float(t[0]), float(t[1])

    async def _measure(self, nozzle: int) -> tuple[PartView | None, float]:
        """Move over the up camera and measure. (view or None, nozzle angle at the frame)."""
        if self.inspector is None:
            raise NotCalibrated("no up camera: the bottom vision needs an 'up' camera")
        x, y = self._up_xy(nozzle)
        await self.machine.move_to(x=x, y=y)
        return await self.inspector(nozzle)

    async def _inspect_pick(self, nozzle: int, feeder: Feeder, *, vision: bool, body) -> None:
        """Measure the part just picked; reject (discard and PickError) a missing, wrong or badly held part."""
        reason = self._skip_reason(nozzle, vision)
        if reason is None:
            try:
                view, a0 = await self._measure(nozzle)
            except InspectError as exc:
                reason = str(exc)
                log.warning("bottom vision skipped for nozzle %d (%s): placing without correction", nozzle, reason)
        if reason is not None:
            self._record(nozzle, "skipped", reason)
            self._correction.pop(nozzle, None)
            return

        cfg = self.vision_cfg
        err = angle_error(view.angle, _expected_axis(feeder)) if view is not None else None
        if view is None:
            result, message = "no part", f"No part on nozzle {nozzle} after picking from {feeder.id}"
        elif body is not None and not _size_matches(view.size, body, cfg.size_tolerance):
            result = "rejected"
            message = (f"Part on nozzle {nozzle} is {view.size[0]:.2f} × {view.size[1]:.2f} mm, "
                       f"expected {body[0]:.2f} × {body[1]:.2f} mm")
        elif math.hypot(*view.offset) > cfg.max_offset_mm:
            result = "rejected"
            message = (f"Part on nozzle {nozzle} sits {math.hypot(*view.offset):.2f} mm off the nozzle "
                       f"centre (limit {cfg.max_offset_mm:.2f} mm)")
        elif abs(err) > cfg.max_angle_deg:
            result = "rejected"
            message = (f"Part on nozzle {nozzle} is turned {err:.1f}° from the expected angle "
                       f"(limit {cfg.max_angle_deg:.1f}°)")
        else:
            self._correction[nozzle] = (tuple(view.offset), float(err), float(a0))
            self._record(nozzle, "ok", f"Part on nozzle {nozzle} measured: offset {math.hypot(*view.offset):.2f} mm, "
                         f"angle {err:.1f}°, size {view.size[0]:.2f} × {view.size[1]:.2f} mm", view, err)
            return

        self._record(nozzle, result, message, view, err)
        await self._discard(nozzle)
        raise PickError(message)
