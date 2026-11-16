"""Find the front-panel key signals: log every bit that changes in the controller's replies.

    python -m pnp.tvm.keyprobe                      # real machine 192.168.0.8:701
    python -m pnp.tvm.keyprobe --log keys.log       # also write the log to a file
    python -m pnp.tvm.keyprobe --host 127.0.0.1 --port 7001   # simulator

Read-only: after the connect-time init (pump off, slow mode, up LEDs on) it only sends
state requests (0x00) and polls (0x01), so it never moves an axis. Stop the web app
first: the controller talks to one client at a time.

Every --interval seconds it reads the 44-byte state reply and the 12-byte poll reply
and prints each bit that flips, with a timestamp. Position bytes (16-35) are shown as
"moved" instead of bit by bit. Frames the controller sends without being asked are
printed too. Press one front-panel key at a time, a few seconds apart, and note the order.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from . import protocol as p
from .transport import TvmTransport

POS_BYTES = range(16, 36)
KNOWN = {  # what is already known about a byte, to label the output
    ("state", 4): "outputs", ("state", 5): "outputs", ("state", 6): "outputs", ("state", 7): "outputs",
    ("state", 8): "limits/vacuum1", ("state", 10): "vacuum2/needle",
    ("poll", 4): "outputs", ("poll", 5): "outputs", ("poll", 6): "outputs", ("poll", 7): "outputs",
}


def bit_changes(kind: str, old: bytes, new: bytes) -> list[str]:
    out = []
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b or (kind == "state" and i in POS_BYTES):
            continue
        label = KNOWN.get((kind, i), "unknown")
        for bit in range(8):
            m = 1 << bit
            if (a ^ b) & m:
                out.append(f"{kind} byte {i:2d} bit 0x{m:02x} {'ON ' if b & m else 'off'}  ({label})")
    if kind == "state" and old[16:36] != new[16:36]:
        out.append("state positions changed (axis moved)")
    if len(old) != len(new):
        out.append(f"{kind} length {len(old)} -> {len(new)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.0.8")
    ap.add_argument("--port", type=int, default=p.DEFAULT_PORT)
    ap.add_argument("--interval", type=float, default=0.025, help="seconds between reads")
    ap.add_argument("--log", help="also append the output to this file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logf = open(args.log, "a", buffering=1) if args.log else None  # noqa: SIM115
    t0 = time.monotonic()

    def emit(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} +{time.monotonic() - t0:7.3f}s  {msg}"
        print(line, flush=True)
        if logf:
            logf.write(line + "\n")

    async def run() -> None:
        # Long poll interval: this script does its own reads below.
        t = TvmTransport(args.host, args.port, poll_interval=3600)
        t.on_frame(lambda f: None if f.known else emit(f"unsolicited/unknown frame 0x{f.type:02x}: {f.data.hex(' ')}"))
        await t.start()
        await t.wait_connected(timeout=15)
        last: dict[str, bytes] = {}
        emit("connected; press the front-panel keys one at a time (Ctrl+C to stop)")
        while True:
            try:
                state = (await t.request_state(timeout=1.0)).raw
                poll = await t.command(p.encode_poll(), timeout=1.0) or b""
            except Exception as exc:  # noqa: BLE001 - keep listening through hiccups
                emit(f"read failed: {exc}")
                await asyncio.sleep(0.5)
                continue
            for kind, data in (("state", state), ("poll", poll)):
                if kind not in last:
                    emit(f"{kind:5s} start: {data.hex(' ')}")
                else:
                    changes = bit_changes(kind, last[kind], data)
                    if changes:
                        emit(f"{kind:5s} now:   {data.hex(' ')}")
                        for c in changes:
                            emit("    " + c)
                last[kind] = data
            await asyncio.sleep(args.interval)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if logf:
            logf.close()


if __name__ == "__main__":
    main()
