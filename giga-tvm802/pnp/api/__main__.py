"""Start the PnP server.

    python -m pnp.api                     # real machine, config/machine.yaml
    python -m pnp.api --sim               # built-in simulator, no hardware needed
    python -m pnp.api --config my.yaml --port 8080
"""

import argparse
import dataclasses
import logging
import math
import tempfile
from pathlib import Path

import uvicorn

from pnp.api.app import DEFAULT_BANKS, DEFAULT_CALIBRATION, DEFAULT_FEEDERS, DEFAULT_JOB, DEFAULT_PACKAGES, create_app
from pnp.config import load_config
from pnp.job.banks import BankStore, GenerateSpec, LaneSpec
from pnp.job.feeders import Feeder, FeederStore
from pnp.job.job import JobStore, grid, job_from_pos
from pnp.job.kicad import parse_pos
from pnp.machine.machine import Machine
from pnp.sim.sim_vision import (
    SIM_NOZZLE_OFFSETS,
    SIM_PRICK_OFFSET,
    SIM_UP_CAMERA_XY,
    SimPartPhysics,
    sim_board_truth,
    sim_down_A,
    sim_up_A,
)
from pnp.vision.calibration_store import Calibration

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "machine.yaml"
DEFAULT_CAMERAS = ROOT / "config" / "cameras.yaml"
DEMO_POS = ROOT / "examples" / "demo.pos"

# Sim seed: (bank id, lane spec, origin, lane_axis, feed_direction, sprocket_side, lane loads, layout name).
# All seeded pick points, prick points and placements are inside the soft limits with the
# nozzle offsets (N1 (-42, -3), N2 (-58, -3), prick (-50, 12)): the head goes to W - offset.
#   back:  pick X 40..96, Y 300 (heads up to X 154, Y 303); prick (36.5, 302) -> head (86.5, 290).
#   front: pick X 40..76, Y 20 (heads up to X 134, Y 23); prick (45.5, 18) -> head (95.5, 6),
#          and after the 8 mm drag (+Y) the head is at Y 14, all inside the soft limits.
SIM_BANKS = (
    ("back", "Back", GenerateSpec(origin=(40.0, 300.0), lane_axis=(1.0, 0.0), feed_direction=(0.0, -1.0),
                                  sprocket_side=-1, lanes=[LaneSpec(8.0, 4.0) for _ in range(8)]),
     [("10k", "R_0805_2012Metric"), ("100n", "C_0402_1005Metric"), ("1u", "C_0805_2012Metric"),
      ("BC847", "SOT-23")], "Passives"),
    ("front", "Front", GenerateSpec(origin=(40.0, 20.0), lane_axis=(1.0, 0.0), feed_direction=(0.0, 1.0),
                                    sprocket_side=1, lanes=[LaneSpec(12.0, 8.0) for _ in range(4)]),
     [("NE555", "SOIC-8_3.9x4.9mm_P1.27mm")], "ICs"),
)


def _load_lanes(feeders: FeederStore, bank: str, parts: list[tuple[str, str]]) -> None:
    """Put value and package into lanes 1..n of a bank (only the lanes given)."""
    for lane, (value, package) in enumerate(parts, 1):
        feeder = feeders.get(f"{bank}-{lane}")
        feeders.upsert(dataclasses.replace(feeder, value=value, package=package))


def seed_sim_banks(banks_path: Path, feeders: FeederStore) -> None:
    """Two lane stacks with parts loaded, each with one saved layout (simulator only)."""
    banks = BankStore(banks_path, feeders)
    for bank_id, name, spec, loads, layout in SIM_BANKS:
        banks.upsert(bank_id, name)
        banks.generate(bank_id, spec)
        _load_lanes(feeders, bank_id, loads)
        banks.save_layout(bank_id, layout)


def sim_demo_boards():
    """The demo board at its true position on the simulated bed: the seeded panel (2 boards, 50 mm apart)
    and origin (150, 150) with the simulator's board error. Drawn on every start, job file or not."""
    job = job_from_pos("Demo board", DEMO_POS.name, parse_pos(DEMO_POS.read_text(encoding="utf-8")))
    offsets = [(b.x, b.y) for b in grid(1, 2, 50.0, 0.0)]
    fiducials = [(f.x, f.y) for f in job.fiducials]
    parts = [(p.x, p.y, p.rotation) for p in job.parts if p.side == "top"]
    return sim_board_truth((150.0, 150.0), 0.0, offsets, (40.0, 30.0), fiducials, parts)


def sim_part_at(feeders: FeederStore, rules, x: float, y: float):
    """(size, axis deg) of the part a simulated nozzle picks at (x, y): the enabled feeder whose pick
    point is within 1 mm, sized by its package rule. None when no feeder is there (a default part)."""
    for feeder in feeders.all():
        if not feeder.enabled:
            continue
        px, py = feeder.pick_location()
        if math.hypot(px - x, py - y) <= 1.0:
            size = rules.body_for(feeder.package) or (2.0, 1.25)
            axis = math.degrees(math.atan2(feeder.direction[1], feeder.direction[0])) + feeder.rotation
            return tuple(size), axis
    return None


def seed_sim_job(job_path: Path) -> None:
    """The demo board from examples/demo.pos, as a 2 x 1 panel with its origin set (simulator only)."""
    job = job_from_pos("Demo board", DEMO_POS.name, parse_pos(DEMO_POS.read_text(encoding="utf-8")))
    job.boards = grid(1, 2, 50.0, 0.0)
    job.origin = (150.0, 150.0)
    JobStore(job_path).save(job)


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
    ap.add_argument("--banks", help="feeder bank file (default: config/banks.yaml; "
                    "in --sim mode a temporary file)")
    ap.add_argument("--rules", help="package nozzle rules (default: config/packages.yaml; "
                    "in --sim mode a temporary file)")
    ap.add_argument("--job", help="current job file (default: config/job.yaml; "
                    "in --sim mode a temporary file)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    config_path = args.config
    if args.sim and config_path:
        # The Defaults tab saves into the config file: in the simulator, work on a
        # fresh copy so the real machine's machine.yaml is never changed.
        import shutil
        config_path = str(Path(tempfile.gettempdir()) / "tvm-sim-machine.yaml")
        shutil.copyfile(args.config, config_path)
    machine = Machine(cfg)
    startup = shutdown = None
    sim = None
    sim_app: dict = {}  # the app, once create_app has built it (the sim part lookup reads its feeders and rules)
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

        def sim_angles() -> dict[str, float]:
            return {"a1": sim.machine.nozzle_angle(1), "a2": sim.machine.nozzle_angle(2)}

        def part_for_pick(nozzle: int, x: float, y: float):
            app = sim_app.get("app")
            if app is None:
                return None
            return sim_part_at(app.state.feeders, app.state.rules, x, y)

        # Held parts are simulated on the nozzles and drawn by the up camera.
        sim_physics = SimPartPhysics(sim.machine, part_for_pick)
        cameras = CameraManager({
            "down": SimCamera("down", make_down_renderer(head_xy, sim_demo_boards())),
            "up": SimCamera("up", make_up_renderer(head_xy, sim_physics, sim_angles)),
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

    if args.banks:
        banks_path = Path(args.banks)
    elif args.sim:
        banks_path = Path(tempfile.gettempdir()) / "tvm-sim-banks.yaml"
    else:
        banks_path = DEFAULT_BANKS

    if args.rules:
        rules_path = Path(args.rules)
    elif args.sim:
        rules_path = Path(tempfile.gettempdir()) / "tvm-sim-packages.yaml"
    else:
        rules_path = DEFAULT_PACKAGES

    if args.job:
        job_path = Path(args.job)
    elif args.sim:
        job_path = Path(tempfile.gettempdir()) / "tvm-sim-job.yaml"
    else:
        job_path = DEFAULT_JOB

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
            # The up position puts nozzle n's tip over the up camera: head = camera - tip offset.
            seed.up_position.setdefault(n, (SIM_UP_CAMERA_XY[0] - t[0], SIM_UP_CAMERA_XY[1] - t[1]))
        if seed.prick_offset is None:
            seed.prick_offset = SIM_PRICK_OFFSET
        seed.save(calibration_path)
        if not feeders_path.exists():
            store = FeederStore(feeders_path)
            store.upsert(Feeder(id="F1", name="0805 10k", kind="strip", pick=(60.0, 40.0),
                                direction=(1.0, 0.0), pitch=4.0, capacity=50, pick_depth=3.0))
            store.upsert(Feeder(id="F2", name="SOT-23", kind="drag", pick=(60.0, 80.0),
                                direction=(1.0, 0.0), pitch=4.0, prick=(56.0, 84.0), pick_depth=3.0))
        if not banks_path.exists():
            seed_sim_banks(banks_path, FeederStore(feeders_path))
        if not job_path.exists():
            seed_sim_job(job_path)
        if not cfg.head.discard:
            cfg.head.discard = (20.0, 20.0)

    app = create_app(machine, cameras=cameras, calibration_path=calibration_path,
                     config_path=config_path,
                     cameras_path=None if (args.sim or args.no_cameras) else args.cameras,
                     extra_startup=startup, extra_shutdown=shutdown, feeders_path=feeders_path,
                     banks_path=banks_path, rules_path=rules_path, job_path=job_path)
    sim_app["app"] = app
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    logging.getLogger(__name__).info("UI on http://%s:%s/ (%s), cameras: %s", host, port,
                                     "SIMULATOR" if args.sim else f"machine {cfg.controller.host}",
                                     cameras.names() if cameras else "none")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
