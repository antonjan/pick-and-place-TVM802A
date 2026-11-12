"""Start the PnP server.

    python -m pnp.api                     # real machine, config/machine.yaml
    python -m pnp.api --sim               # built-in simulator, no hardware needed
    python -m pnp.api --config my.yaml --port 8080
"""

import argparse
import logging
import tempfile
from pathlib import Path

import uvicorn

from pnp.api.app import DEFAULT_CALIBRATION, DEFAULT_FEEDERS, create_app
from pnp.config import load_config
from pnp.job.feeders import Feeder, FeederStore
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import SIM_NOZZLE_OFFSETS, SIM_PRICK_OFFSET, sim_down_A, sim_up_A
from pnp.vision.calibration_store import Calibration

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "machine.yaml"
DEFAULT_CAMERAS = Path(__file__).resolve().parents[2] / "config" / "cameras.yaml"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else None)
    ap.add_argument("--sim", action="store_true", help="run against the built-in simulator")
    ap.add_argument("--host", help="HTTP bind address (default from config)")
    ap.add_argument("--port", type=int, help="HTTP port (default from config)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-cameras", action="store_true", help="do not start any cameras")
    ap.add_argument("--cameras", default=str(DEFAULT_CAMERAS), help="camera config (real mode)")
    ap.add_argument("--calibration", help="calibration file (default: config/calibration.yaml; "
                    "in --sim mode a temporary file)")
    ap.add_argument("--feeders", help="feeder file (default: config/feeders.yaml; "
                    "in --sim mode a temporary file)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    machine = Machine(cfg)
    startup = shutdown = None
    if args.sim:
        from pnp.sim.tvm_sim import TvmSimulator

        sim = TvmSimulator("127.0.0.1", 0)

        async def startup():
            await sim.start()
            machine.t.host, machine.t.port = sim.host, sim.port

        shutdown = sim.stop
        cfg.controller.reconnect_delay = 0.5

    cameras = None
    if args.no_cameras:
        pass
    elif args.sim:
        from pnp.sim.sim_vision import make_down_renderer, make_up_renderer
        from pnp.vision.camera import CameraManager, SimCamera

        def head_xy() -> tuple[float, float]:
            pos = machine.position()
            return pos["x"], pos["y"]

        cameras = CameraManager({
            "down": SimCamera("down", make_down_renderer(head_xy)),
            "up": SimCamera("up", make_up_renderer(head_xy)),
        })
    elif Path(args.cameras).exists():
        from pnp.vision.camera import build_uvc_manager

        cameras = build_uvc_manager(args.cameras)
    else:
        logging.getLogger(__name__).warning("no camera config at %s: cameras disabled", args.cameras)

    if args.calibration:
        calibration_path = Path(args.calibration)
    elif args.sim:
        calibration_path = Path(tempfile.gettempdir()) / "tvm-sim-calibration.yaml"
    else:
        calibration_path = DEFAULT_CALIBRATION

    if args.feeders:
        feeders_path = Path(args.feeders)
    elif args.sim:
        feeders_path = Path(tempfile.gettempdir()) / "tvm-sim-feeders.yaml"
    else:
        feeders_path = DEFAULT_FEEDERS

    if args.sim:
        # Seed the simulator with the true calibration and two example feeders, so
        # a pick and place demo works at once. Existing files are never overwritten.
        # Fill in whatever the sim calibration is missing (an older file may lack newer
        # fields such as the prick offset); values calibrated in the UI are kept.
        seed = Calibration.load(calibration_path)
        if "down" not in seed.cameras:
            seed.set_camera("down", sim_down_A(), 0.0)
        if "up" not in seed.cameras:
            seed.set_camera("up", sim_up_A(), 0.0)
        for n, t in SIM_NOZZLE_OFFSETS.items():
            seed.nozzle_offset.setdefault(n, t)
        if seed.prick_offset is None:
            seed.prick_offset = SIM_PRICK_OFFSET
        seed.save(calibration_path)
        if not feeders_path.exists():
            store = FeederStore(feeders_path)
            store.upsert(Feeder(id="F1", name="0805 10k", kind="strip", pick=(60.0, 40.0),
                                direction=(1.0, 0.0), pitch=4.0, capacity=50, pick_depth=3.0))
            store.upsert(Feeder(id="F2", name="SOT-23", kind="drag", pick=(60.0, 80.0),
                                direction=(1.0, 0.0), pitch=4.0, prick=(56.0, 84.0), pick_depth=3.0))
        if not cfg.head.discard:
            cfg.head.discard = (20.0, 20.0)

    app = create_app(machine, cameras=cameras, calibration_path=calibration_path,
                     config_path=args.config,
                     cameras_path=None if (args.sim or args.no_cameras) else args.cameras,
                     extra_startup=startup, extra_shutdown=shutdown, feeders_path=feeders_path)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    logging.getLogger(__name__).info("UI on http://%s:%s/ (%s), cameras: %s", host, port,
                                     "SIMULATOR" if args.sim else f"machine {cfg.controller.host}",
                                     cameras.names() if cameras else "none")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
