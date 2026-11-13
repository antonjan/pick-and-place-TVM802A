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

ACTIVE = ("running", "pausing", "stopping")


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
                "last_inspection": self.head.inspection_json()}

    # ------------------------------------------------------------------ control

    async def start(self) -> None:
        """Start from the first pending part. JobError if the job can't run from here."""
        if self.state in ACTIVE:
            raise JobError(f"the job is already {self.state}")
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
        self.state = "running"
        self.current = None
        self.message = None
        self._task = asyncio.create_task(self._run(), name="job-runner")

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
        """Running or paused -> stopping (the part in progress finishes) -> idle."""
        if self.state in ("running", "pausing"):
            self.state = "stopping"
        elif self.state in ("paused", "error"):
            self.state = "idle"
            self.current = None
            self.message = None
        else:
            raise JobError("the job isn't running")

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
