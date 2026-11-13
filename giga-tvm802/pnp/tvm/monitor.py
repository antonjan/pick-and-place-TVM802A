"""Connect to a TVM802 (or the simulator) and print its live state.

    python -m pnp.tvm.monitor                       # real machine 192.168.0.8:701
    python -m pnp.tvm.monitor --host 127.0.0.1 --port 7001   # simulator
    python -m pnp.tvm.monitor --unknown             # also dump unknown reply frames

Only sends the connect-time init sequence (same as the Node bridge: pump
off, slow mode, up-camera LEDs on) and state requests. It never moves an axis.
"""

import argparse
import asyncio
import logging

from . import protocol as p
from .transport import TvmTransport

STEPS_PER_MM = 32808
STEPS_PER_DEG = 4444.44


def format_state(s: p.MachineState) -> str:
    pos = s.positions
    lim = "".join(k[0].upper() for k, v in s.limits.items() if v) or "-"
    outs = ",".join(o.name for o in (p.Output.PUMP, p.Output.VACUUM1, p.Output.VACUUM2, p.Output.BLOW1,
                                     p.Output.BLOW2, p.Output.PRICK, p.Output.LEDS, p.Output.BUZZER)
                    if s.output(o)) or "-"
    return (
        f"X {pos[p.Axis.X] / STEPS_PER_MM:9.3f}  Y {pos[p.Axis.Y] / STEPS_PER_MM:9.3f}  "
        f"Z {pos[p.Axis.Z] / STEPS_PER_MM:7.3f}  A1 {pos[p.Axis.A1] / STEPS_PER_DEG:8.2f}  "
        f"A2 {pos[p.Axis.A2] / STEPS_PER_DEG:8.2f}  lim {lim:4s} "
        f"vac {int(s.pressure1)}{int(s.pressure2)}  {s.speed_mode or '?':4s}  out {outs}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.0.8")
    ap.add_argument("--port", type=int, default=p.DEFAULT_PORT)
    ap.add_argument("--interval", type=float, default=0.2, help="state poll interval (s)")
    ap.add_argument("--unknown", action="store_true", help="print unknown reply frames as hex")
    ap.add_argument("--raw", action="store_true", help="also print raw state bytes")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def run() -> None:
        t = TvmTransport(args.host, args.port, poll_interval=args.interval)
        last = [""]

        def show(s: p.MachineState) -> None:
            line = format_state(s)
            if line != last[0]:  # only print changes
                print(line + (f"\n    {s.raw.hex(' ')}" if args.raw else ""), flush=True)
                last[0] = line

        t.on_state(show)
        if args.unknown:
            t.on_frame(lambda f: None if f.known else print(f"unknown 0x{f.type:02x}: {f.data.hex(' ')}"))
        await t.start()
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
