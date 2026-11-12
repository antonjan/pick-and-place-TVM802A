"""Run the TVM802 simulator: python -m pnp.sim [--port 7001]"""

import argparse
import asyncio
import logging

from .tvm_sim import TvmSimulator


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated TVM802 controller")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7001, help="real machine uses 701 (needs root)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def run() -> None:
        sim = TvmSimulator(args.host, args.port)
        await sim.start()
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
