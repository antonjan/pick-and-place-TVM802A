"""Front-panel keys of the TVM802: jog, jog step, jog mode and job control.

The controller only reports the keys (state bytes 9 and 10, see protocol.PANEL_KEYS);
it does nothing with them itself. FrontPanel watches every state reply, turns key
changes into presses and releases, and acts on them through the same Machine and
JobRunner calls the web UI uses, so all the usual checks apply (homing, soft limits,
needle interlock, nozzles raised first, no manual moves while a job runs).

    DotMove/ConMove  switch the jog mode: dot = one step per press, con = move while held
    X-, X+, Y+, Y-   jog the head (the X/Y step is the one selected on the Machine tab)
    Step             run the job one stage per press (JobRunner.step): the first press
                     starts the job in step mode; then feed (move, needle down + drag,
                     needle up), pick (move, down, up), move to the place, place; repeat
    Start/Stop       start the job when idle; stop it while it runs, steps or is paused
    Pause/Cont       pause a running job; resume a paused one

The jog mode and step live in Machine.panel, so the UI shows and sets the same values.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from pnp.job.runner import JobError, JobRunner
from pnp.machine.machine import Machine, MachineError
from pnp.tvm import protocol as p

log = logging.getLogger(__name__)

ARROWS = {"x-": ("x", -1), "x+": ("x", 1), "y+": ("y", 1), "y-": ("y", -1)}
# Handle presses in a fixed order when several keys change in the same state reply.
KEY_ORDER = ("start", "pause", "mode", "step", "x-", "x+", "y+", "y-")


class FrontPanel:
    def __init__(self, machine: Machine, runner: JobRunner | None = None) -> None:
        self.m = machine
        self.runner = runner
        self._keys: frozenset[str] | None = None  # None = no baseline yet (just connected)
        self._held: str | None = None  # arrow key driving a ConMove jog
        self._released: asyncio.Event | None = None
        self._jog: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        machine.t.on_state(self._on_state)
        machine.t.on_connection(self._on_connection)

    # ------------------------------------------------------------------ key changes

    def _on_connection(self, up: bool) -> None:
        # After a (re)connect the first reply is only a baseline: a key that is already
        # held then (or stuck) doesn't count as a press.
        self._keys = None
        self._end_hold()

    def _on_state(self, s: p.MachineState) -> None:
        prev, self._keys = self._keys, s.keys
        if prev is None:
            return
        for key in KEY_ORDER:
            if key in s.keys and key not in prev:
                self._pressed(key)
            elif key in prev and key not in s.keys:
                self._released_key(key)

    def _pressed(self, key: str) -> None:
        log.info("front panel: %s pressed", key)
        if key == "mode":
            self.m.panel["mode"] = "con" if self.m.panel["mode"] == "dot" else "dot"
            log.info("front panel: jog mode %s", self.m.panel["mode"])
            self.m._notify()
            if hasattr(self.m, "set_panel_leds"):  # the panel's speed lights show the mode
                self._spawn("speed lights", self.m.set_panel_leds(self.m.panel["mode"]))
        elif key == "step":
            if self.runner is not None:
                self._spawn("step", self.runner.step())
        elif key in ARROWS:
            self._jog_key(key)
        elif key == "start":
            self._start_stop()
        elif key == "pause":
            self._pause_cont()

    def _released_key(self, key: str) -> None:
        if key == self._held:
            self._end_hold()

    def _end_hold(self) -> None:
        if self._released is not None:
            self._released.set()
        self._held = None
        self._released = None

    # ------------------------------------------------------------------ actions

    def _jog_key(self, key: str) -> None:
        if self.runner is not None and self.runner.busy():
            self._report("jog", "a job is running")
            return
        if self._jog is not None and not self._jog.done():
            return  # one jog at a time; a press during a jog is ignored
        axis, direction = ARROWS[key]
        if self.m.panel["mode"] == "con":
            self._held, self._released = key, asyncio.Event()
            self._jog = self._spawn(f"jog {key}", self.m.jog_hold(axis, direction, self._released))
        else:
            self._jog = self._spawn(f"jog {key}", self.m.jog(axis, direction * self.m.panel["step"]))

    def _start_stop(self) -> None:
        r = self.runner
        if r is None:
            return
        if r.state in ("running", "pausing", "stepping", "paused", "error"):
            self._call("stop", r.stop)
        elif r.state == "idle":
            self._spawn("start", r.start())

    def _pause_cont(self) -> None:
        r = self.runner
        if r is None:
            return
        if r.state == "running":
            self._call("pause", r.pause)
        elif r.state == "paused":
            self._spawn("resume", r.resume())
        else:
            self._report("pause", "the job isn't running")

    # ------------------------------------------------------------------ helpers

    def _call(self, what: str, fn) -> None:
        try:
            fn()
        except JobError as exc:
            self._report(what, str(exc))
        self.m._notify()

    def _spawn(self, what: str, coro: Awaitable) -> asyncio.Task:
        async def run() -> None:
            try:
                await coro
            except (MachineError, JobError, ValueError) as exc:
                self._report(what, str(exc))
            except Exception as exc:  # noqa: BLE001 - keep listening; show it
                log.exception("front panel %s failed", what)
                self._report(what, f"failed: {exc}")
            finally:
                self.m._notify()

        task = asyncio.get_running_loop().create_task(run(), name=f"panel-{what}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _report(self, what: str, msg: str) -> None:
        log.warning("front panel %s: %s", what, msg)
        self.m.last_error = f"Front panel {what}: {msg}"
        self.m._notify()
