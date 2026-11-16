"""Job runner: places the pending parts of the current job, one at a time (docs/phase4-design.md, section 6).

The loop runs as an asyncio task. Pause and stop take effect between parts: the part in
progress always finishes, so the head is never left mid-pick or mid-place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from pnp.job.align import AlignError
from pnp.job.feeders import FeederError, FeederStore
from pnp.job.head_ops import HeadOps, PickError
from pnp.job.job import JobStore, PlanItem, plan
from pnp.job.rules import PackageRules
from pnp.machine.machine import Machine, MachineError
from pnp.vision.fiducials import FiducialError

log = logging.getLogger(__name__)

# "stepping" = the job runs one stage per press of Step (front panel or Job tab).
ACTIVE = ("running", "pausing", "stopping", "stepping")

# The stages of one placement in step mode, in order. The feed stages only run for a
# drag lane whose pick pocket is empty.
FEED_STAGES = ("feed_move", "feed_down", "feed_up")
PICK_STAGES = ("pick_move", "pick_down", "pick_up", "place_move", "place")
STAGE_LABELS = {
    "feed_move": "move the needle over the lane",
    "feed_down": "needle down and feed the lane",
    "feed_up": "needle up",
    "pick_move": "move the nozzle over the part",
    "pick_down": "nozzle down to pick the part",
    "pick_up": "nozzle up with the part",
    "place_move": "move the part to its place",
    "place": "place the part",
}


class JobError(Exception):
    """The runner can't start or change state now. The message is for the user."""


class JobRunner:
    def __init__(self, machine: Machine, head: HeadOps, feeders: FeederStore, jobs: JobStore,
                 rules: PackageRules, pick_retries: int = 2,
                 *, align: Callable[[], Awaitable[None]] | None = None) -> None:
        self.machine = machine
        self.align = align  # measures the fiducials and saves the job; run before the first part
        self.head = head
        self.feeders = feeders
        self.jobs = jobs
        self.rules = rules
        self.pick_retries = pick_retries
        self.state: str = "idle"
        self.current: str | None = None
        self.message: str | None = None
        self._task: asyncio.Task | None = None
        # step mode
        self._stages: list[str] = []  # stages of the part in progress; empty = next press starts a part
        self._stage_i = 0  # index of the next stage to run
        self._item: PlanItem | None = None
        self._stage_running: str | None = None

    # ------------------------------------------------------------------ queries

    def busy(self) -> bool:
        return self.state in ACTIVE

    def _plan(self) -> list[PlanItem]:
        job = self.jobs.load()
        if job is None:
            return []
        return plan(job, self.feeders.all(), self.rules)

    def status(self) -> dict:
        """State plus counts over the plan. total = every item that is not skipped."""
        counts = {"placed": 0, "failed": 0, "skipped": 0, "pending": 0}
        total = 0
        for item in self._plan():
            if item.status == "skipped":
                counts["skipped"] += 1
                continue
            total += 1
            if item.status == "placed":
                counts["placed"] += 1
            elif item.status == "failed":
                counts["failed"] += 1
            elif item.status == "pending" and item.problem is None:
                counts["pending"] += 1
        return {"state": self.state, "current": self.current, "message": self.message, **counts, "total": total,
                "last_inspection": self.head.inspection_json(), "step": self._step_status(),
                "holding": {str(n): self.head.holding.get(n) for n in (1, 2)}}

    def _running_label(self) -> str | None:
        if not self._stage_running:
            return None
        if self._stage_running in STAGE_LABELS:
            return STAGE_LABELS[self._stage_running]
        return "measuring the fiducials" if self.current == "fiducials" else "starting the next part"

    def _step_status(self) -> dict | None:
        """Step mode: the stage now running and the one the next press runs."""
        if self.state not in ("stepping", "stopping") or (self.state == "stopping" and not self._stage_running):
            return None
        # While a stage runs, the next press runs the stage after it.
        i = self._stage_i + (1 if self._stage_running in STAGE_LABELS else 0)
        if self._stages and i < len(self._stages):
            nxt, ref = self._stages[i], (self._item.ref if self._item else None)
        else:
            nxt, ref = "next_part", None
        return {
            "running": self._running_label(),
            "next": "start the next part" if nxt == "next_part" else STAGE_LABELS.get(nxt),
            "ref": ref,
            "index": i + 1 if ref else None,
            "count": len(self._stages) if ref else None,
        }

    # ------------------------------------------------------------------ control

    async def start(self) -> None:
        """Start from the first pending part. JobError if the job can't run from here."""
        if self.state in ACTIVE:
            raise JobError(f"the job is already {self.state}")
        self._check_startable()
        self.state = "running"
        self.current = None
        self.message = None
        self._task = asyncio.create_task(self._run(), name="job-runner")

    def _check_startable(self) -> None:
        job = self.jobs.load()
        if job is None:
            raise JobError("no job loaded: import a KiCad position file first")
        if not self.machine.homed:
            raise JobError("The machine isn't homed. Home it, then start the job.")
        if job.origin is None:
            raise JobError("Board origin not set: set it from the camera first.")
        pending = [item for item in plan(job, self.feeders.all(), self.rules) if item.status == "pending"]
        if not pending:
            raise JobError("Nothing left to place.")
        if pending[0].problem is not None:
            raise JobError(f"{pending[0].ref} can't be placed: {pending[0].problem}. "
                           "Fix it or skip it first.")

    async def resume(self) -> None:
        if self.state in ACTIVE:
            raise JobError(f"the job is already {self.state}")
        await self.start()

    def pause(self) -> None:
        """Running -> pausing. The part in progress finishes, then the job pauses."""
        if self.state != "running":
            raise JobError("the job isn't running")
        self.state = "pausing"

    def stop(self) -> None:
        """Running or paused -> stopping (the part in progress finishes) -> idle.
        Step mode: ends after the stage in progress, lifting the needle and nozzles."""
        if self.state == "stepping":
            self.state = "stopping"
            if not self._stage_running:
                self._task = asyncio.create_task(self._end_stepping(), name="job-step-stop")
        elif self.state in ("running", "pausing"):
            self.state = "stopping"
        elif self.state in ("paused", "error"):
            self.state = "idle"
            self.current = None
            self.message = None
            self._task = asyncio.create_task(self._pump_off(), name="job-pump-off")
        else:
            raise JobError("the job isn't running")

    async def _pump_off(self) -> None:
        """The job is over (stopped or finished): switch the vacuum pump off (user request
        2026-11-16). A pick turns it on again."""
        with contextlib.suppress(MachineError):
            await self.machine.set_output("pump", False)
            log.info("job over: pump off")

    async def step(self) -> None:
        """Run the next stage of the job (step mode). The first press starts the job in step
        mode; each press then runs one stage: feed (move, needle down + drag, needle up) for
        a drag lane that needs it, then pick (move, down, up), move to the place, place.
        Returns once the stage has started; JobError if a step can't run now."""
        if self.state == "stepping":
            if self._stage_running:
                raise JobError(f"still busy: {self._running_label()}")
        elif self.state in ACTIVE:
            raise JobError(f"the job is {self.state}: stop it before stepping")
        else:
            self._check_startable()
            self.state = "stepping"
            self.current = None
            self.message = None
            self._stages, self._stage_i, self._item = [], 0, None
        # Mark the stage as running before returning, so a second press can't start another.
        self._stage_running = "starting"
        self._task = asyncio.create_task(self._step_once(), name="job-step")

    async def _step_once(self) -> None:
        try:
            if not self._stages and not await self._next_part():
                return
            stage = self._stages[self._stage_i]
            self._stage_running = stage
            self.message = None
            await self._run_stage(stage, self._item)
            self._stage_i += 1
            if self._stage_i == len(self._stages):
                self._set_status(self._item.key, "placed")
                log.info("step mode: %s placed", self._item.ref)
                self._stages, self._stage_i, self._item = [], 0, None
                self.current = None
                job = self.jobs.load()
                if job is not None and not any(i.status == "pending" for i in plan(job, self.feeders.all(), self.rules)):
                    self.state = "done" if self.state == "stepping" else self.state
        except PickError as exc:
            # Bottom vision rejected the part (it was discarded): this part starts again.
            ref = self._item.ref if self._item else "the part"
            self._stages, self._stage_i, self._item = [], 0, None
            self.current = None
            self.message = f"{exc}. Press Step to try {ref} again."
        except (FeederError, JobError) as exc:
            self._stages, self._stage_i, self._item = [], 0, None
            self._halt(str(exc))
        except (AlignError, FiducialError, MachineError) as exc:
            self._error(str(exc))
        except Exception as exc:  # the task must never die silently
            log.exception("job step failed")
            self._error(f"Job step failed: {exc}")
        finally:
            self._stage_running = None
            if self.state == "stopping":
                await self._end_stepping()
            elif self.state in ("done", "idle"):
                await self._pump_off()

    async def _next_part(self) -> bool:
        """Set up the stages of the next pending part. False when there is nothing to run."""
        job = self.jobs.load()
        if job is None:
            self._error("The job file is gone.")
            return False
        if self._needs_alignment(job):
            self.current = "fiducials"
            self._stage_running = "starting"
            await self.align()
            self.current = None
        if not self.machine.homed:
            raise JobError("The machine isn't homed. Home it, then press Step.")
        pending = [i for i in plan(self.jobs.load() or job, self.feeders.all(), self.rules) if i.status == "pending"]
        if not pending:
            self.state = "done"
            self.current = None
            return False
        item = pending[0]
        if item.problem is not None:
            raise JobError(f"{item.ref} can't be placed: {item.problem}. Fix it or skip it, then press Step.")
        feeder = self.feeders.get(item.feeder or "")
        self.head.check_pick(item.nozzle, feeder)
        feed = feeder.kind == "drag" and feeder.needs_feed
        self._stages = list((FEED_STAGES if feed else ()) + PICK_STAGES)
        self._stage_i = 0
        self._item = item
        self.current = item.key
        return True

    async def _run_stage(self, stage: str, item: PlanItem) -> None:
        feeder = self.feeders.get(item.feeder or "")
        h, n = self.head, item.nozzle
        async with h.exclusive(f"step: {STAGE_LABELS[stage]}"):
            if stage == "feed_move":
                await h.feed_move(feeder)
            elif stage == "feed_down":
                await h.feed_drag(feeder)
            elif stage == "feed_up":
                await h.feed_up(feeder)
            elif stage == "pick_move":
                h.check_pick(n, feeder)
                await h.pick_move(n, feeder)
            elif stage == "pick_down":
                await h.pick_down(n, feeder)
            elif stage == "pick_up":
                await h.pick_up(n, feeder)
            elif stage == "place_move":
                await h.inspect_pick(n, feeder, vision=item.vision, body=item.body)
                await h.place_move(n, (item.x, item.y), item.rotation)
            elif stage == "place":
                await h.place_release(n, item.place_depth)

    async def _end_stepping(self) -> None:
        """Leave step mode safely: needle up, nozzles up, pump off. The pump going off
        releases a part already picked, so its nozzle is cleared and the user is told."""
        done = set(self._stages[: self._stage_i])
        item = self._item
        with contextlib.suppress(MachineError):
            if self.machine.needle_is_down():
                await self.machine.set_output("prick", False)
        with contextlib.suppress(MachineError):
            await self.machine.nozzles_up()
        await self._pump_off()
        note = None
        if item is not None and "pick_down" in done and "place" not in done:
            with contextlib.suppress(MachineError):
                await self.machine.set_output(f"vacuum{item.nozzle}", False)
            self.head.holding[item.nozzle] = None
            note = (f"Stopped in step mode: the pump is off, so {item.ref} was released from nozzle "
                    f"{item.nozzle} where the head is. Remove it before the next run.")
        self._stages, self._stage_i, self._item = [], 0, None
        self.state = "idle"
        self.current = None
        self.message = note

    async def shutdown(self) -> None:
        """Cancel the loop task at server shutdown."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        try:
            await self._loop()
            if self.state in ("idle", "done"):
                await self._pump_off()
        except asyncio.CancelledError:
            self._error("The job runner was cancelled.")
            raise
        except Exception as exc:  # the task must never die silently
            log.exception("job runner failed")
            self._error(f"Job runner failed: {exc}")

    def _needs_alignment(self, job) -> bool:
        return (self.align is not None and job.fiducial_mode != "off" and bool(job.fiducials)
                and job.fit is None)

    async def _loop(self) -> None:
        job = self.jobs.load()
        if job is not None and self._needs_alignment(job):
            self.current = "fiducials"
            try:
                await self.align()
            except (AlignError, FiducialError, MachineError) as exc:
                self.current = None
                self._error(str(exc))
                return
            self.current = None
        while True:
            if self.state == "stopping":
                self.state = "idle"
                self.current = None
                self.message = None
                return
            if self.state == "pausing":
                self.state = "paused"
                self.current = None
                return

            job = self.jobs.load()
            if job is None:
                self._error("The job file is gone.")
                return
            items = plan(job, self.feeders.all(), self.rules)
            pending = [item for item in items if item.status == "pending"]
            if not pending:
                self.state = "done"
                self.current = None
                self.message = None
                return

            item = pending[0]
            if item.problem is not None:
                # The next part can't run (no feeder, empty lane). Stop here so the order is kept.
                self._halt(f"{item.ref} can't be placed: {item.problem}. Fix it or skip it, then resume.")
                return
            if not self.machine.homed:
                self._error("The machine isn't homed. Home it, then resume.")
                return
            if not await self._place_item(item):
                return

    async def _place_item(self, item: PlanItem) -> bool:
        """Pick and place one part. False when the loop must stop (state already set)."""
        self.current = item.key
        try:
            feeder = self.feeders.get(item.feeder or "")
        except FeederError as exc:
            self._halt(str(exc))
            return False

        attempts = self.pick_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await self.head.pick_and_place(item.nozzle, feeder, (item.x, item.y), item.rotation,
                                               item.place_depth, vision=item.vision, body=item.body)
                break
            except PickError:
                if attempt < attempts:
                    log.info("pick of %s from %s failed (attempt %d of %d): retrying",
                             item.ref, feeder.id, attempt, attempts)
                    if feeder.kind == "drag":
                        feeder.needs_feed = True  # the pocket was probably empty: feed before the retry
                        self.feeders.save()
                    continue
                self._set_status(item.key, "failed")
                self._halt(f"Couldn't pick {item.ref} from {feeder.id} after {attempts} tries. "
                           "Check the lane, then resume or skip it.")
                return False
            except FeederError as exc:
                self._halt(str(exc))
                return False
            except MachineError as exc:
                self._error(str(exc))
                return False

        self._set_status(item.key, "placed")
        self.current = None
        return True

    # ------------------------------------------------------------------ helpers

    def _set_status(self, key: str, value: str) -> None:
        job = self.jobs.load()
        if job is None:
            return
        job.status[key] = value
        self.jobs.save(job)

    def _halt(self, message: str) -> None:
        """Pause with a message, or go idle if a stop was requested meanwhile."""
        if self.state == "stopping":
            self.state = "idle"
            self.current = None
            self.message = None
        else:
            self.state = "paused"
            self.message = message
        log.info("job paused: %s", message)

    def _error(self, message: str) -> None:
        self.state = "error"
        self.message = message
        log.error("job error: %s", message)
