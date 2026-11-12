"""Asyncio TCP link to the TVM802 controller.

The controller is strictly request/response and processes only one command
per TCP read: commands sent back-to-back are silently dropped. So every frame
goes through `command()`, which holds a lock, sends ONE frame and waits for
its reply (or a short gap for the few commands that never reply) before the
next frame may go out.

Responsibilities:
  * keep one connection open, reconnecting (and replaying the init sequence)
    whenever it drops;
  * serialise commands and match replies;
  * poll the state continuously and publish every decoded MachineState.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from . import protocol as p

log = logging.getLogger(__name__)


class NotConnected(ConnectionError):
    pass


class NoReply(NotConnected):
    """The controller did not answer a command in time."""


class TvmTransport:
    def __init__(
        self,
        host: str = "192.168.0.8",
        port: int = p.DEFAULT_PORT,
        *,
        init_sequence: Iterable[bytes] = p.INIT_SEQUENCE,
        poll_interval: float = 0.2,
        delay_before_polling: float = 0.2,
        reconnect_delay: float = 2.0,
        connect_timeout: float = 5.0,
        reply_timeout: float = 0.5,
        no_reply_gap: float = 0.05,
    ):
        self.host = host
        self.port = port
        self.init_sequence = tuple(init_sequence)
        self.poll_interval = poll_interval
        self.delay_before_polling = delay_before_polling
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout
        self.reply_timeout = reply_timeout
        self.no_reply_gap = no_reply_gap

        self.state: p.MachineState | None = None
        self.state_seq = 0  # increments on every decoded state reply
        self.last_poll_reply: bytes | None = None
        self.connected = asyncio.Event()

        self._reassembler = p.FrameReassembler()
        self._writer: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()
        self._pending: tuple[int, asyncio.Future] | None = None  # (reply type, future)
        self._state_cond = asyncio.Condition()
        self._runner: asyncio.Task | None = None
        self._stopping = False
        self._state_listeners: list[Callable[[p.MachineState], None]] = []
        self._frame_listeners: list[Callable[[p.Frame], None]] = []
        self._connection_listeners: list[Callable[[bool], None]] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._runner is None:
            self._stopping = False
            self._runner = asyncio.create_task(self._run(), name="tvm-transport")

    async def stop(self) -> None:
        self._stopping = True
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._close_writer()

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self.connected.wait(), timeout)

    # ------------------------------------------------------------------ listeners

    def on_state(self, cb: Callable[[p.MachineState], None]) -> None:
        self._state_listeners.append(cb)

    def on_frame(self, cb: Callable[[p.Frame], None]) -> None:
        """Every received frame, including unknown ones (for discovery)."""
        self._frame_listeners.append(cb)

    def on_connection(self, cb: Callable[[bool], None]) -> None:
        self._connection_listeners.append(cb)

    # ------------------------------------------------------------------ I/O

    async def command(self, frame: bytes, timeout: float | None = None) -> bytes | None:
        """Send one command and return its reply frame (None for no-reply commands)."""
        async with self._cmd_lock:
            return await self._command_locked(frame, timeout)

    async def send(self, *frames: bytes) -> None:
        """Send several commands strictly one after another, each acknowledged.

        The whole group holds the command lock, so no state poll can slip in
        between (e.g. speed setup + move stay together).
        """
        async with self._cmd_lock:
            for frame in frames:
                await self._command_locked(frame, None)

    async def wait_state(self, after_seq: int | None = None, timeout: float = 2.0) -> p.MachineState:
        """Wait for a state reply newer than `after_seq` (default: the current one)."""
        target = self.state_seq if after_seq is None else after_seq
        async with self._state_cond:
            await asyncio.wait_for(self._state_cond.wait_for(lambda: self.state_seq > target), timeout)
            assert self.state is not None
            return self.state

    async def request_state(self, timeout: float | None = None) -> p.MachineState:
        """Ask for a fresh state reply and return it."""
        reply = await self.command(p.encode_request_state(), timeout)
        assert reply is not None
        return p.decode_state(reply)

    # ------------------------------------------------------------------ internals

    async def _command_locked(self, frame: bytes, timeout: float | None) -> bytes | None:
        writer = self._writer
        if writer is None or not self.connected.is_set():
            raise NotConnected(f"not connected to {self.host}:{self.port}")
        cmd = frame[0]
        if cmd in p.NO_REPLY:
            writer.write(frame)
            await writer.drain()
            await asyncio.sleep(self.no_reply_gap)  # keep it in its own TCP read
            return None
        fut = asyncio.get_running_loop().create_future()
        self._pending = (cmd, fut)
        try:
            writer.write(frame)
            await writer.drain()
            return await asyncio.wait_for(fut, timeout or self.reply_timeout)
        except asyncio.TimeoutError as exc:
            log.warning("no reply to command 0x%02x (%s)", cmd, frame.hex(" "))
            raise NoReply(f"controller did not answer command 0x{cmd:02x}") from exc
        except (OSError, RuntimeError) as exc:
            raise NotConnected(str(exc)) from exc
        finally:
            self._pending = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), self.connect_timeout
                )
            except (OSError, asyncio.TimeoutError) as exc:
                log.warning("TVM connect to %s:%s failed: %s", self.host, self.port, exc)
                await asyncio.sleep(self.reconnect_delay)
                continue

            log.info("TVM connected (%s:%s)", self.host, self.port)
            self._writer = writer
            self._reassembler.reset()
            self.connected.set()
            reader_task = asyncio.create_task(self._read_loop(reader), name="tvm-read")
            poller = None
            try:
                await self.send(*self.init_sequence)
                self._notify_connection(True)
                poller = asyncio.create_task(self._poll_loop(), name="tvm-poll")
                await reader_task
            except (OSError, NotConnected) as exc:
                log.warning("TVM connection error: %s", exc)
            finally:
                for task in (poller, reader_task):
                    if task:
                        task.cancel()
                was_up = self.connected.is_set()
                self.connected.clear()
                if self._pending:
                    _, fut = self._pending
                    if not fut.done():
                        fut.set_exception(NotConnected("connection lost"))
                await self._close_writer()
                if was_up:
                    self._notify_connection(False)
                log.info("TVM connection closed")
            if not self._stopping:
                await asyncio.sleep(self.reconnect_delay)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                for frame in self._reassembler.feed(data):
                    await self._handle_frame(frame)
        except OSError as exc:
            log.warning("TVM read error: %s", exc)

    async def _handle_frame(self, frame: p.Frame) -> None:
        for cb in self._frame_listeners:
            cb(frame)
        if not frame.known:
            log.debug("unknown frame type 0x%02x: %s", frame.type, frame.data.hex(" "))
            return
        if frame.data == p.GREETING:
            log.debug("controller greeting received")
            return
        if frame.type == 0x00:
            state = p.decode_state(frame.data)
            async with self._state_cond:
                self.state = state
                self.state_seq += 1
                self._state_cond.notify_all()
            for cb in self._state_listeners:
                cb(state)
        elif frame.type == 0x01:
            self.last_poll_reply = frame.data
        if self._pending and self._pending[0] == frame.type and not self._pending[1].done():
            self._pending[1].set_result(frame.data)
        elif frame.type != 0x00:
            log.debug("unsolicited frame: %s", frame.data.hex(" "))

    async def _poll_loop(self) -> None:
        await asyncio.sleep(self.delay_before_polling)
        while True:
            try:
                await self.request_state()
            except NoReply:
                pass  # logged; keep polling
            except NotConnected:
                return
            await asyncio.sleep(self.poll_interval)

    async def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def _notify_connection(self, up: bool) -> None:
        for cb in self._connection_listeners:
            cb(up)
