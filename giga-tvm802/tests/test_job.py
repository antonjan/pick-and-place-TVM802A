"""Jobs, panels, coordinate mapping, feeder matching and planning (no hardware)."""

import math
from types import SimpleNamespace

import pytest
import yaml

from pnp.config import HeadConfig
from pnp.job.feeders import Feeder
from pnp.job.job import (
    BoardInstance,
    Fiducial,
    Job,
    JobPart,
    JobStore,
    PlanItem,
    blank_mark,
    clear_fit,
    export_job_yaml,
    fit_rotation,
    grid,
    is_fiducial,
    job_from_pos,
    mark_results,
    match_feeder,
    norm,
    parse_job_yaml,
    placement_key,
    plan,
    refit_board,
    sync_board_marks,
    to_world,
    world_rotation,
)


class FakeRules:
    def __init__(self, nozzles=None, depths=None):
        self.nozzles = nozzles or {}
        self.depths = depths or {}

    def nozzle_for(self, package):
        return self.nozzles.get(package, 1)

    def place_depth_for(self, package):
        return self.depths.get(package)


def strip(id, value, package="", capacity=10, count=0, **kw) -> Feeder:
    return Feeder(
        id=id, kind="strip", pick=(0.0, 0.0), direction=(1, 0), pitch=4.0,
        capacity=capacity, count=count, value=value, package=package, **kw,
    )


def part(ref, value="10k", package="R0603", x=0.0, y=0.0, rotation=0.0, **kw) -> JobPart:
    return JobPart(ref=ref, value=value, package=package, x=x, y=y, rotation=rotation, **kw)


def close(a, b, tol=1e-6):
    return abs(a - b) < tol


def close_xy(p, q, tol=1e-6):
    return close(p[0], q[0], tol) and close(p[1], q[1], tol)


# --- grid and keys -------------------------------------------------------------

def test_grid_is_row_major_from_origin():
    boards = grid(2, 3, 10.0, 20.0)
    assert len(boards) == 6
    assert (boards[0].x, boards[0].y) == (0.0, 0.0)
    assert (boards[2].x, boards[2].y) == (20.0, 0.0)
    assert (boards[3].x, boards[3].y) == (0.0, 20.0)
    assert (boards[5].x, boards[5].y) == (20.0, 20.0)
    assert all(b.rotation == 0.0 and not b.skip for b in boards)


@pytest.mark.parametrize("rows, cols", [(0, 1), (1, 0), (51, 1), (1, 51)])
def test_grid_rejects_bad_size(rows, cols):
    with pytest.raises(ValueError):
        grid(rows, cols, 10, 10)


def test_placement_key():
    assert placement_key(0, "R1") == "0:R1"
    assert placement_key(3, "C10") == "3:C10"


def test_norm():
    assert norm("  10k   OHM \t") == "10k ohm"


# --- coordinates ----------------------------------------------------------------

def test_to_world_origin_only():
    job = Job(name="j", origin=(100.0, 50.0))
    assert to_world(job, 0, 10.0, 5.0) == pytest.approx((110.0, 55.0))


def test_to_world_with_job_rotation_90():
    job = Job(name="j", origin=(100.0, 50.0), rotation=90.0)
    # rot(90) @ (10, 0) = (0, 10)
    assert close_xy(to_world(job, 0, 10.0, 0.0), (100.0, 60.0))


def test_to_world_with_board_rotation_and_offset():
    job = Job(name="j", origin=(0.0, 0.0), boards=[BoardInstance(x=5.0, y=0.0, rotation=90.0)])
    # board: rot(90) @ (10, 0) = (0, 10), + (5, 0) = (5, 10)
    assert close_xy(to_world(job, 0, 10.0, 0.0), (5.0, 10.0))
    # rot(90) @ (1, 0) = (0, 1), + (5, 0) = (5, 1)
    assert close_xy(to_world(job, 0, 1.0, 0.0), (5.0, 1.0))


def test_to_world_board_and_job_rotation_together():
    job = Job(name="j", origin=(10.0, 20.0), rotation=90.0,
              boards=[BoardInstance(x=0.0, y=10.0, rotation=0.0)])
    # panel: (1, 0) + (0, 10) = (1, 10); world: rot(90) @ (1, 10) = (-10, 1); + origin = (0, 21)
    assert close_xy(to_world(job, 0, 1.0, 0.0), (0.0, 21.0))


def test_to_world_needs_origin():
    with pytest.raises(ValueError):
        to_world(Job(name="j"), 0, 1.0, 1.0)


def test_world_rotation_adds_and_normalises():
    job = Job(name="j", rotation=0.0, boards=[BoardInstance(rotation=90.0)])
    assert world_rotation(job, 0, 30.0) == pytest.approx(120.0)
    job = Job(name="j", rotation=90.0, boards=[BoardInstance()])
    assert world_rotation(job, 0, 90.0) == pytest.approx(180.0)  # 180 stays 180
    assert world_rotation(job, 0, 180.0) == pytest.approx(-90.0)  # 270 wraps to -90
    assert world_rotation(job, 0, 270.0) == pytest.approx(0.0)    # 90 + 270 = 360
    assert world_rotation(job, 0, -90.0) == pytest.approx(0.0)
    assert world_rotation(job, 0, 0.0) == pytest.approx(90.0)
    flat = Job(name="j")
    assert world_rotation(flat, 0, -180.0) == pytest.approx(180.0)
    assert world_rotation(flat, 0, 200.0) == pytest.approx(-160.0)


def test_fit_rotation_recovers_known_rotation():
    origin = (100.0, 50.0)
    board_xy = (10.0, 0.0)
    a = math.radians(30.0)
    world_xy = (origin[0] + 10 * math.cos(a), origin[1] + 10 * math.sin(a))
    job = Job(name="j", origin=origin)
    assert fit_rotation(job, board_xy, world_xy) == pytest.approx(30.0)


def test_fit_rotation_negative_and_wraps():
    job = Job(name="j", origin=(0.0, 0.0))
    assert fit_rotation(job, (10.0, 0.0), (0.0, -10.0)) == pytest.approx(-90.0)
    # board point at 170 deg, world point at -170 deg: rotation is +20 deg
    b = (10 * math.cos(math.radians(170)), 10 * math.sin(math.radians(170)))
    w = (10 * math.cos(math.radians(-170)), 10 * math.sin(math.radians(-170)))
    assert fit_rotation(job, b, w) == pytest.approx(20.0)


def test_fit_rotation_errors():
    with pytest.raises(ValueError):
        fit_rotation(Job(name="j"), (10.0, 0.0), (10.0, 0.0))
    job = Job(name="j", origin=(0.0, 0.0))
    with pytest.raises(ValueError):
        fit_rotation(job, (0.5, 0.0), (10.0, 0.0))  # board point too close to the origin
    with pytest.raises(ValueError):
        fit_rotation(job, (10.0, 0.0), (0.0, 0.5))  # world point too close to the origin


# --- feeder matching --------------------------------------------------------------

def test_match_feeder_by_value_and_package():
    f = strip("F1", "10k", "R0603")
    assert match_feeder(part("R1", "10k", "R0603"), [f]) is f
    assert match_feeder(part("R1", "10k", "R0402"), [f]) is None
    assert match_feeder(part("R1", "22k", "R0603"), [f]) is None


def test_match_feeder_empty_package_matches_any():
    f = strip("F1", "10k", "")
    assert match_feeder(part("R1", "10k", "R0402"), [f]) is f


def test_match_feeder_ignores_case_and_whitespace():
    f = strip("F1", " 10K  ohm ", "r0603")
    assert match_feeder(part("R1", "10k ohm", "R0603"), [f]) is f


def test_match_feeder_excludes_disabled():
    f = strip("F1", "10k", "R0603", enabled=False)
    assert match_feeder(part("R1", "10k", "R0603"), [f]) is None


def test_match_feeder_override():
    a = strip("F1", "10k", "R0603")
    b = strip("F2", "22k", "R0603")
    p = part("R1", "10k", "R0603", feeder="F2")
    assert match_feeder(p, [a, b]) is b
    p.feeder = "F9"
    assert match_feeder(p, [a, b]) is None
    p.feeder = "F1"
    b.enabled = False
    assert match_feeder(part("R1", feeder="F2"), [a, b]) is None


def test_match_feeder_prefers_parts_left_then_lowest_id():
    empty = strip("F1", "10k", "R0603", capacity=3, count=3)
    left = strip("F3", "10k", "R0603", capacity=3, count=1)
    assert match_feeder(part("R1"), [empty, left]) is left
    other = strip("F2", "10k", "R0603", capacity=3, count=0)
    assert match_feeder(part("R1"), [left, other]) is other  # both have parts: lowest id
    # no parts anywhere: still a match, so plan can report "is empty"
    assert match_feeder(part("R1"), [empty]) is empty


def test_match_feeder_prefers_lower_bank_then_lane():
    a = strip("A", "10k", "R0603")
    b = strip("B", "10k", "R0603")
    setattr(a, "bank", "front")
    setattr(a, "lane", 2)
    setattr(b, "bank", "back")
    setattr(b, "lane", 1)
    assert match_feeder(part("R1"), [a, b]) is b  # "back" sorts before "front"
    setattr(a, "bank", "back")
    setattr(a, "lane", 3)
    assert match_feeder(part("R1"), [a, b]) is b  # same bank: lower lane wins


# --- planning -----------------------------------------------------------------------

def simple_rules():
    return FakeRules(nozzles={"SOIC": 1, "0603": 2}, depths={"SOIC": 2.5})


def two_feeder_job(parts, boards=None, origin=(0.0, 0.0), status=None):
    job = Job(name="j", parts=parts, origin=origin,
              boards=boards if boards is not None else [BoardInstance()])
    job.status = dict(status or {})
    return job


def feeders_for_order():
    return [
        strip("F1", "1u", "SOIC"),
        strip("F2", "100n", "0603"),
        strip("F3", "100n", "0603"),
    ]


def test_plan_order_board_nozzle_feeder_natural_ref():
    parts = [
        part("C10", "100n", "0603", x=1, y=0),
        part("R1", "1u", "SOIC", x=2, y=0),
        part("C2", "100n", "0603", x=3, y=0),
    ]
    boards = [BoardInstance(), BoardInstance(x=50.0)]
    job = two_feeder_job(parts, boards=boards)
    items = plan(job, feeders_for_order(), simple_rules())
    assert [i.key for i in items] == ["0:R1", "0:C2", "0:C10", "1:R1", "1:C2", "1:C10"]


def test_plan_orders_by_nozzle_before_feeder():
    # both nozzle 1, but one nozzle-2 part with a feeder id sorts after all nozzle-1 parts
    parts = [
        part("R2", "100n", "0603"),                 # nozzle 2 (rules: 0603)
        part("U1", "1u", "SOIC"),                   # nozzle 1
    ]
    job = two_feeder_job(parts)
    items = plan(job, feeders_for_order(), simple_rules())
    assert [i.ref for i in items] == ["U1", "R2"]


def test_plan_orders_by_feeder_id_within_nozzle():
    parts = [
        part("A1", "100n", "0603", feeder="F3"),
        part("A2", "100n", "0603", feeder="F2"),
        part("A3", "100n", "0603"),                 # auto: F2 (lowest id), so it sorts with F2
    ]
    job = two_feeder_job(parts)
    items = plan(job, feeders_for_order(), simple_rules())
    assert [i.ref for i in items] == ["A2", "A3", "A1"]


def test_plan_fills_world_fields_and_nozzle_depth():
    job = two_feeder_job([part("U1", "1u", "SOIC", x=10.0, y=5.0, rotation=45.0)], origin=(100.0, 50.0))
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert (item.x, item.y) == pytest.approx((110.0, 55.0))
    assert item.rotation == pytest.approx(45.0)
    assert item.nozzle == 1
    assert item.place_depth == 2.5
    assert item.feeder == "F1"
    assert item.status == "pending"
    assert item.problem is None


def test_plan_nozzle_override_wins():
    job = two_feeder_job([part("U1", "1u", "SOIC", nozzle=2)])
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.nozzle == 2


def test_plan_status_from_job_is_kept_for_placed_and_failed():
    parts = [part("R1", "1u", "SOIC"), part("R2", "1u", "SOIC")]
    job = two_feeder_job(parts, status={"0:R1": "placed", "0:R2": "failed"})
    items = {i.ref: i for i in plan(job, feeders_for_order(), simple_rules())}
    assert items["R1"].status == "placed" and items["R1"].problem is None
    assert items["R2"].status == "failed" and items["R2"].problem is None


def test_plan_status_skipped_from_job_status():
    job = two_feeder_job([part("R1", "1u", "SOIC")], status={"0:R1": "skipped"})
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.status == "skipped"


def test_plan_part_skip():
    job = two_feeder_job([part("R1", "1u", "SOIC", skip=True)])
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.status == "skipped"
    assert item.problem == "Part skipped"


def test_plan_bottom_side():
    job = two_feeder_job([part("R1", "1u", "SOIC", side="bottom", skip=True)])
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.status == "skipped"
    assert item.problem == "Bottom side"  # the side is the more useful reason
    job = two_feeder_job([part("R1", "1u", "SOIC", side="bottom")])
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.status == "skipped"
    assert item.problem == "Bottom side"


def test_plan_board_skip():
    boards = [BoardInstance(), BoardInstance(x=50.0, skip=True)]
    job = two_feeder_job([part("R1", "1u", "SOIC")], boards=boards)
    items = plan(job, feeders_for_order(), simple_rules())
    assert items[0].status == "pending" and items[0].problem is None
    assert items[1].status == "skipped"
    assert items[1].problem == "Board skipped"


def test_plan_no_feeder():
    job = two_feeder_job([part("R1", "47k", "R0603")])
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.feeder is None
    assert item.status == "pending"
    assert item.problem == "No feeder for 47k R0603"


def test_plan_empty_feeder():
    feeders = [strip("F5", "1u", "SOIC", capacity=2, count=2)]
    job = two_feeder_job([part("U1", "1u", "SOIC")])
    (item,) = plan(job, feeders, simple_rules())
    assert item.feeder == "F5"
    assert item.problem == "Feeder F5 is empty"


def tray_feeder(id, value, package="", cols=2, rows=2, count=0) -> Feeder:
    # 2 x 2 tray: first part at (0, 0), last part at (4, 4)
    return Feeder(id=id, kind="tray", pick=(0.0, 0.0), tray_end=(4.0, 4.0), tray_cols=cols,
                  tray_rows=rows, count=count, value=value, package=package)


def test_plan_tray_feeder_matches_without_problem():
    job = two_feeder_job([part("U1", "1u", "SOIC")])
    (item,) = plan(job, [tray_feeder("T1", "1u", "SOIC")], simple_rules())
    assert item.feeder == "T1"
    assert item.problem is None


def test_plan_empty_tray_is_empty_problem():
    job = two_feeder_job([part("U1", "1u", "SOIC")])
    (item,) = plan(job, [tray_feeder("T1", "1u", "SOIC", count=4)], simple_rules())
    assert item.feeder == "T1"
    assert item.problem == "Feeder T1 is empty"


def test_plan_no_origin():
    job = two_feeder_job([part("U1", "1u", "SOIC", x=1.0, y=2.0)], origin=None)
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.x is None and item.y is None
    assert item.problem == "Board origin not set"


def test_plan_problem_precedence():
    # a skipped part with no origin and no feeder reports the part skip first
    job = two_feeder_job([part("R1", "47k", "R0603", skip=True)], origin=None)
    (item,) = plan(job, [], simple_rules())
    assert item.problem == "Part skipped"
    # no feeder beats no origin
    job = two_feeder_job([part("R1", "47k", "R0603")], origin=None)
    (item,) = plan(job, [], simple_rules())
    assert item.problem == "No feeder for 47k R0603"


def test_plan_uses_rules_defaults():
    job = two_feeder_job([part("R1", "100n", "0603")])
    rules = FakeRules(nozzles={"0603": 2}, depths={})
    (item,) = plan(job, feeders_for_order(), rules)
    assert item.nozzle == 2
    assert item.place_depth is None


class VisionRules(FakeRules):
    """Rules with the phase 6 lookups."""

    def __init__(self, nozzles=None, depths=None, bodies=None, visions=None):
        super().__init__(nozzles, depths)
        self.bodies = bodies or {}
        self.visions = visions or {}

    def body_for(self, package):
        return self.bodies.get(package)

    def vision_for(self, package):
        return self.visions.get(package, True)


def test_plan_item_vision_and_body_defaults():
    job = two_feeder_job([part("U1", "1u", "SOIC", x=10.0, y=5.0)], origin=(0.0, 0.0))
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.vision is True
    assert item.body is None


def test_plan_fills_vision_and_body_from_rules():
    job = two_feeder_job([
        part("U1", "1u", "SOIC", x=10.0, y=5.0),
        part("R1", "100n", "0603"),
    ], origin=(0.0, 0.0))
    rules = VisionRules(
        nozzles={"SOIC": 1, "0603": 2},
        bodies={"SOIC": (6.0, 4.9), "0603": (1.6, 0.8)},
        visions={"0603": False},
    )
    items = {i.ref: i for i in plan(job, feeders_for_order(), rules)}
    assert items["U1"].body == [6.0, 4.9]
    assert items["U1"].vision is True
    assert items["R1"].body == [1.6, 0.8]
    assert items["R1"].vision is False


def test_plan_body_is_a_plain_list_and_defaults_without_rules_methods():
    job = two_feeder_job([part("R1", "100n", "0603")])
    rules = VisionRules(nozzles={"0603": 2}, bodies={"0603": (1.6, 0.8)})
    (item,) = plan(job, feeders_for_order(), rules)
    assert isinstance(item.body, list)
    assert item.body == [1.6, 0.8]

    # The fake rules without vision_for/body_for still plan: vision on, no size check.
    (item,) = plan(job, feeders_for_order(), FakeRules(nozzles={"0603": 2}))
    assert item.vision is True
    assert item.body is None


def test_plan_part_vision_override_wins_over_rule():
    rules = VisionRules(visions={"0603": True, "SOIC": False})
    job = two_feeder_job([
        part("R1", "100n", "0603", vision=False),   # rule on, part off
        part("R2", "100n", "0603", vision=True),    # rule on, part on
        part("R3", "100n", "0603"),                  # no override: follows the rule
        part("U1", "1u", "SOIC", vision=True),      # rule off, part on
        part("U2", "1u", "SOIC"),                    # no override: rule off
    ], origin=(0.0, 0.0))
    items = {i.ref: i for i in plan(job, feeders_for_order(), rules)}
    assert items["R1"].vision is False
    assert items["R2"].vision is True
    assert items["R3"].vision is True
    assert items["U1"].vision is True
    assert items["U2"].vision is False


# --- job from pos ---------------------------------------------------------------------

def test_job_from_pos_marks_bottom_parts_skipped():
    pos = [
        SimpleNamespace(ref="R1", value="10k", package="R0603", x=1.0, y=2.0, rotation=90.0, side="top"),
        SimpleNamespace(ref="C1", value="1u", package="C0805", x=3.0, y=4.0, rotation=0.0, side="bottom"),
    ]
    job = job_from_pos("demo", "demo.pos", pos)
    assert job.name == "demo" and job.source == "demo.pos"
    assert job.origin is None
    assert [b.skip for b in job.boards] == [False]
    r1, c1 = job.parts
    assert (r1.ref, r1.value, r1.package, r1.x, r1.y, r1.rotation, r1.side, r1.skip) == (
        "R1", "10k", "R0603", 1.0, 2.0, 90.0, "top", False)
    assert c1.side == "bottom" and c1.skip is True


# --- store ------------------------------------------------------------------------------

def sample_job() -> Job:
    job = Job(
        name="demo",
        source="demo.pos",
        parts=[
            part("R1", "10k", "R0603", x=1.0, y=2.0, rotation=90.0),
            part("C1", "1u", "C0805", x=3.0, y=4.0, side="bottom", skip=True),
            part("U1", "NE555", "SOIC-8", x=5.0, y=6.0, nozzle=1, feeder="F4"),
        ],
        boards=grid(1, 2, 50.0, 0.0),
        origin=(150.0, 150.0),
        rotation=90.0,
        status={"0:R1": "placed", "1:U1": "failed"},
    )
    return job


def test_job_store_round_trip(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    job = sample_job()
    store.save(job)
    assert store.load() == job


def test_job_store_writes_plain_yaml(tmp_path):
    path = tmp_path / "job.yaml"
    JobStore(path).save(sample_job())
    data = yaml.safe_load(path.read_text())
    assert data["origin"] == [150.0, 150.0]
    assert data["boards"][1] == {"x": 50.0, "y": 0.0, "rotation": 0.0, "skip": False}
    assert data["parts"][0]["ref"] == "R1"
    assert data["status"] == {"0:R1": "placed", "1:U1": "failed"}
    assert set(data) == {
        "name", "source", "origin", "rotation", "scale", "boards", "parts", "status",
        "fiducials", "fiducial_mode", "fiducial_diameter", "fit", "board_fits",
        "references", "reference_board",  # two-point board alignment (2026-11-13)
        "pcb_thickness",  # bed depth place model
        "board_marks", "mark_fits",  # two marks per board (board marks)
    }


def test_job_store_missing_file(tmp_path):
    store = JobStore(tmp_path / "nope.yaml")
    assert store.load() is None


def test_job_store_clear(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    store.save(sample_job())
    store.clear()
    assert store.load() is None
    store.clear()  # clearing again is fine


def test_job_store_rejects_unknown_keys(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: demo\nbogus: 1\n")
    with pytest.raises(ValueError, match="bogus"):
        JobStore(path).load()
    path.write_text("name: demo\nparts:\n  - ref: R1\n    value: 10k\n    package: R0603\n"
                    "    x: 1\n    y: 2\n    rotation: 0\n    colour: red\n")
    with pytest.raises(ValueError, match="colour"):
        JobStore(path).load()


def test_job_store_round_trips_fiducials_and_fit(tmp_path):
    job = sample_job()
    job.fiducials = [Fiducial("FID1", 2.0, 2.0), Fiducial("FID2", 38.0, 2.5)]
    job.fiducial_mode = "board"
    job.fiducial_diameter = 1.2
    job.fit = {
        "mode": "board", "rms": 0.02, "scale": 1.0001, "rotation": 0.4,
        "origin": [150.35, 149.75], "points": [{"board": 0, "name": "FID1", "error": 0.01}],
    }
    job.board_fits = {0: [150.35, 149.75, 0.4], 1: [200.0, 150.0, -0.2]}
    store = JobStore(tmp_path / "job.yaml")
    store.save(job)
    loaded = store.load()
    assert loaded == job
    assert all(isinstance(k, int) for k in loaded.board_fits)
    data = yaml.safe_load((tmp_path / "job.yaml").read_text())
    assert sorted(data["board_fits"]) == [0, 1]  # int keys in the file
    assert data["fiducials"][1] == {"name": "FID2", "x": 38.0, "y": 2.5}


def test_job_store_fit_tuples_saved_as_lists(tmp_path):
    job = sample_job()
    job.fit = {"origin": (1.0, 2.0)}
    store = JobStore(tmp_path / "job.yaml")
    store.save(job)
    assert yaml.safe_load((tmp_path / "job.yaml").read_text())["fit"] == {"origin": [1.0, 2.0]}


def test_job_store_loads_old_file_with_defaults(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: old\norigin: [10, 20]\nrotation: 0\nboards:\n  - x: 0\n    y: 0\n")
    job = JobStore(path).load()
    assert job.name == "old" and job.origin == (10.0, 20.0)
    assert job.fiducials == []
    assert job.fiducial_mode == "off"
    assert job.fiducial_diameter == 1.0
    assert job.fit is None
    assert job.board_fits == {}


def test_job_store_round_trips_part_vision_override(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    job = Job(name="vis", parts=[
        part("R1", "10k", "R0603", vision=True),
        part("R2", "10k", "R0603", vision=False),
        part("R3", "10k", "R0603"),
    ], boards=grid(1, 1, 50.0, 0.0))
    store.save(job)
    loaded = store.load()
    assert [p.vision for p in loaded.parts] == [True, False, None]
    assert loaded == job


def test_job_store_part_without_vision_key_loads_as_none(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: old\nparts:\n  - ref: R1\n    value: 10k\n    package: R0603\n"
                    "    x: 1\n    y: 2\n    rotation: 0\n")
    job = JobStore(path).load()
    assert job.parts[0].vision is None


def test_job_store_rejects_bad_part_vision(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: bad\nparts:\n  - ref: R1\n    value: 10k\n    package: R0603\n"
                    "    x: 1\n    y: 2\n    rotation: 0\n    vision: maybe\n")
    with pytest.raises(ValueError):
        JobStore(path).load()


def test_job_store_board_fits_accept_string_keys(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: demo\nboard_fits:\n  '1': [1, 2, 3]\n")
    assert JobStore(path).load().board_fits == {1: [1.0, 2.0, 3.0]}


def test_job_store_rejects_bad_fiducial_mode_and_unknown_fiducial_key(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: demo\nfiducial_mode: sometimes\n")
    with pytest.raises(ValueError, match="fiducial_mode"):
        JobStore(path).load()
    path.write_text("name: demo\nfiducials:\n  - name: FID1\n    x: 1\n    y: 2\n    colour: red\n")
    with pytest.raises(ValueError, match="colour"):
        JobStore(path).load()


# --- fiducials and board fits ------------------------------------------------------------

def test_is_fiducial_by_ref_or_package():
    assert is_fiducial(SimpleNamespace(ref="FID1", package="Fiducial_1mm_Mask2mm"))
    assert is_fiducial(SimpleNamespace(ref="fid12", package="X"))
    assert is_fiducial(SimpleNamespace(ref="M1", package="Fiducial_0.5mm"))
    assert not is_fiducial(SimpleNamespace(ref="FIDX", package="SOT-23"))
    assert not is_fiducial(SimpleNamespace(ref="R1", package="R_0805_2012Metric"))


def test_job_from_pos_extracts_top_fiducials_and_drops_bottom_ones():
    pos = [
        SimpleNamespace(ref="R1", value="10k", package="R0603", x=5.0, y=4.0, rotation=0.0, side="top"),
        SimpleNamespace(ref="FID1", value="Fiducial", package="Fiducial_1mm_Mask2mm", x=2.0, y=2.0, rotation=0.0, side="top"),
        SimpleNamespace(ref="FID9", value="Fiducial", package="Fiducial_1mm_Mask2mm", x=30.0, y=3.0, rotation=0.0, side="bottom"),
        SimpleNamespace(ref="C1", value="1u", package="C0805", x=3.0, y=4.0, rotation=0.0, side="bottom"),
    ]
    job = job_from_pos("demo", "demo.pos", pos)
    assert job.fiducials == [Fiducial(name="FID1", x=2.0, y=2.0)]
    assert job.fiducial_mode == "panel"
    assert [p.ref for p in job.parts] == ["R1", "C1"]
    assert job.parts[1].skip is True


def test_job_from_pos_without_fiducials_is_mode_off():
    pos = [SimpleNamespace(ref="R1", value="10k", package="R0603", x=5.0, y=4.0, rotation=0.0, side="top")]
    job = job_from_pos("demo", "demo.pos", pos)
    assert job.fiducials == [] and job.fiducial_mode == "off"


def test_to_world_with_board_fit_rotation_04_deg():
    # rot(0.4 deg) @ (10, 0) = (10 cos 0.4, 10 sin 0.4) = (9.99976, 0.06981); + origin (150.35, 149.75)
    job = Job(name="j", origin=(150.0, 150.0), rotation=0.0, boards=[BoardInstance()],
              board_fits={0: [150.35, 149.75, 0.4]})
    wx, wy = to_world(job, 0, 10.0, 0.0)
    assert wx == pytest.approx(160.34976, abs=1e-5)
    assert wy == pytest.approx(149.81981, abs=1e-5)
    # the board fit replaces the panel origin and rotation, so a board offset is not added again
    job.boards = [BoardInstance(x=50.0, rotation=90.0)]
    assert close_xy(to_world(job, 0, 10.0, 0.0), (wx, wy))


def test_world_rotation_with_board_fit():
    job = Job(name="j", origin=(0.0, 0.0), rotation=90.0, boards=[BoardInstance(rotation=30.0)],
              board_fits={0: [150.35, 149.75, 0.4]})
    assert world_rotation(job, 0, 30.0) == pytest.approx(30.4)
    assert world_rotation(job, 0, 179.9) == pytest.approx(-179.7)  # 180.3 wraps


def test_to_world_and_rotation_unchanged_without_board_fit():
    job = Job(name="j", origin=(150.0, 150.0), rotation=0.0, boards=[BoardInstance()])
    assert close_xy(to_world(job, 0, 10.0, 0.0), (160.0, 150.0))
    assert world_rotation(job, 0, 30.0) == pytest.approx(30.0)


def test_to_world_with_board_fit_works_without_origin():
    job = Job(name="j", board_fits={0: [150.35, 149.75, 0.4]})
    assert job.origin is None
    wx, wy = to_world(job, 0, 10.0, 0.0)
    assert wx == pytest.approx(160.34976, abs=1e-5)
    # a board without a fit still needs the origin
    job.boards = [BoardInstance(), BoardInstance(x=50.0)]
    with pytest.raises(ValueError):
        to_world(job, 1, 1.0, 1.0)


def test_plan_without_origin_still_reports_origin_not_set_with_board_fit():
    job = Job(name="j", parts=[part("U1", "1u", "SOIC", x=1.0, y=2.0)], board_fits={0: [1.0, 2.0, 0.0]})
    job.origin = None
    rules = FakeRules(nozzles={"SOIC": 1})
    (item,) = plan(job, feeders_for_order(), rules)
    assert item.x is None and item.y is None
    assert item.problem == "Board origin not set"


def test_clear_fit():
    job = Job(name="j", fit={"mode": "panel"}, board_fits={0: [1.0, 2.0, 0.1]})
    clear_fit(job)
    assert job.fit is None and job.board_fits == {}



# ---------------------------------------------------------------- two-point alignment

def _world(origin, rot_deg, p):
    import math
    a = math.radians(rot_deg)
    return (origin[0] + math.cos(a) * p[0] - math.sin(a) * p[1],
            origin[1] + math.sin(a) * p[0] + math.cos(a) * p[1])


@pytest.mark.parametrize("rot", [0.0, 0.7, -12.5, 90.0, 179.0])
def test_fit_two_points_recovers_origin_and_rotation(rot):
    from pnp.job.job import fit_two_points
    origin = (152.3, 101.7)
    a, b = (4.0, 3.0), (38.0, 22.5)
    r = fit_two_points(a, _world(origin, rot, a), b, _world(origin, rot, b))
    assert r["rotation"] == pytest.approx(rot, abs=1e-9)
    assert r["origin"] == pytest.approx(list(origin), abs=1e-9)
    assert r["distance_error"] == pytest.approx(0.0, abs=1e-9)


def test_fit_two_points_reports_distance_error_and_rejects_close_points():
    from pnp.job.job import fit_two_points
    r = fit_two_points((0.0, 0.0), (100.0, 100.0), (30.0, 0.0), (130.2, 100.0))
    assert r["distance_error"] == pytest.approx(0.2)
    with pytest.raises(ValueError, match="apart on the board"):
        fit_two_points((0.0, 0.0), (100.0, 100.0), (1.0, 0.0), (130.0, 100.0))
    with pytest.raises(ValueError, match="camera positions"):
        fit_two_points((0.0, 0.0), (100.0, 100.0), (30.0, 0.0), (100.5, 100.0))


def test_job_references_round_trip_and_old_files_load(tmp_path):
    from pnp.job.job import JobStore
    store = JobStore(tmp_path / "job.yaml")
    job = Job(name="t", parts=[JobPart("R1", "10k", "R_0805", 5.0, 4.0, 0.0)])
    job.references = [{"ref": "R1", "x": 5.0, "y": 4.0}, {"ref": None, "x": 40.0, "y": 30.0}]
    job.reference_board = 0
    store.save(job)
    again = store.load()
    assert again.references == job.references and again.reference_board == 0
    # a job file written before reference points existed
    import yaml
    data = yaml.safe_load((tmp_path / "job.yaml").read_text())
    data.pop("references"); data.pop("reference_board")
    (tmp_path / "job.yaml").write_text(yaml.safe_dump(data))
    old = store.load()
    assert old.references == [] and old.reference_board == 0


def _page_coords_job():
    """A board as KiCad exports it without the drill/place origin: page coordinates, Y negated
    (like grelay4-low_power: outline X 92..150, Y -41.75..-120.75)."""
    from pnp.job.job import Job, JobPart
    parts = [JobPart("C3", "10u", "C_0805", 132.48, -47.77, 270.0), JobPart("C21", "100n", "C_0805", 117.0, -99.6, 270.0),
             JobPart("C16", "8p", "C_0805", 108.94, -109.0, 180.0), JobPart("R7", "10k", "R_0805", 137.0, -86.41, 90.0)]
    return Job(name="page", parts=parts, origin=(10.0, 10.0))


def test_reference_point_far_from_the_parts_is_refused_with_the_reason():
    from pnp.job.job import check_reference_point
    job = _page_coords_job()
    with pytest.raises(ValueError, match="page origin"):
        check_reference_point(job, "Point 1", 0.0, 0.0)
    with pytest.raises(ValueError, match=r"try Y -90\.0938"):
        check_reference_point(job, "Point 2", 67.056, 90.0938)
    # board corners and parts in the file's coordinates are fine
    for x, y in [(92.0, -41.747), (150.0, -120.75), (117.0, -99.6)]:
        check_reference_point(job, "P", x, y)


def test_distance_mismatch_is_refused():
    from pnp.job.job import check_distances, fit_two_points
    ok = fit_two_points((0.0, 0.0), (100.0, 100.0), (60.0, 0.0), (160.3, 100.0))
    check_distances(ok)  # 0.3 mm on 60 mm: fine
    bad = fit_two_points((0.0, 0.0), (100.0, 100.0), (60.0, 0.0), (165.0, 100.0))
    with pytest.raises(ValueError, match="wrong feature"):
        check_distances(bad)


def test_placements_outside_the_machine_are_listed():
    from pnp.job.job import placements_outside
    job = _page_coords_job()
    job.origin, job.rotation = (-40.326, -39.655), -0.138  # the bad fit seen on the machine
    out = placements_outside(job, (-120.0, 299.0), (-75.0, 350.5))
    assert {r for r, _, _ in out} == {"C3", "C21", "C16", "R7"}
    job.origin = (-80.0, 160.0)  # a fit that puts the board on the bed
    assert placements_outside(job, (-120.0, 299.0), (-75.0, 350.5)) == []


# ---------------------------------------------------------------- scale correction

def test_to_world_panel_scale_multiplies_the_whole_panel_point():
    # panel point = board point rotated by 0 and offset by (20, 5) = (30, 5); scaled 1.01 = (30.3, 5.05);
    # job rotation 90 deg: (x, y) -> (-y, x) = (-5.05, 30.3)
    job = Job(name="j", origin=(0.0, 0.0), rotation=90.0, scale=1.01, boards=[BoardInstance(x=20.0, y=5.0)])
    assert close_xy(to_world(job, 0, 10.0, 0.0), (-5.05, 30.3))


def test_to_world_scale_default_is_identity():
    job = Job(name="j", origin=(150.0, 150.0), boards=[BoardInstance(x=50.0)])
    assert job.scale == 1.0
    assert close_xy(to_world(job, 0, 10.0, 0.0), (210.0, 150.0))


def test_to_world_board_fit_with_scale():
    s, r = 0.9966, 0.4
    job = Job(name="j", board_fits={0: [150.35, 149.75, r, s]})
    a = math.radians(r)
    expected = (150.35 + math.cos(a) * 10.0 * s - math.sin(a) * 4.0 * s,
                149.75 + math.sin(a) * 10.0 * s + math.cos(a) * 4.0 * s)
    assert close_xy(to_world(job, 0, 10.0, 4.0), expected, tol=1e-9)
    # world_rotation does not depend on the scale
    assert world_rotation(job, 0, 30.0) == pytest.approx(30.4)


def test_fit_two_points_recovers_origin_rotation_and_scale():
    from pnp.job.job import fit_two_points
    origin, rot, s = (152.3, 101.7), 0.7, 0.9966
    a, b = (4.0, 3.0), (38.0, 22.5)

    def world(p):  # the camera sees the board scaled by s, then rotated and moved
        return _world(origin, rot, (s * p[0], s * p[1]))

    r = fit_two_points(a, world(a), b, world(b))
    assert r["scale"] == pytest.approx(s, abs=1e-9)
    assert r["rotation"] == pytest.approx(rot, abs=1e-9)
    assert r["origin"] == pytest.approx(list(origin), abs=1e-9)
    # both points map exactly through to_world with the fitted job values
    job = Job(name="j", origin=tuple(r["origin"]), rotation=r["rotation"], scale=r["scale"], boards=[BoardInstance()])
    assert close_xy(to_world(job, 0, *a), world(a), tol=1e-9)
    assert close_xy(to_world(job, 0, *b), world(b), tol=1e-9)


def test_old_three_value_board_fit_loads_and_maps_as_before(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    (tmp_path / "job.yaml").write_text(
        "name: old\norigin: [0, 0]\nboards:\n  - x: 0\n    y: 0\nboard_fits:\n  0: [150.35, 149.75, 0.4]\n")
    job = store.load()
    assert job.board_fits == {0: [150.35, 149.75, 0.4]}
    assert job.scale == 1.0
    wx, wy = to_world(job, 0, 10.0, 0.0)
    assert wx == pytest.approx(160.34976, abs=1e-5)
    assert wy == pytest.approx(149.81981, abs=1e-5)
    # saving keeps the three values
    store.save(job)
    assert yaml.safe_load((tmp_path / "job.yaml").read_text())["board_fits"] == {0: [150.35, 149.75, 0.4]}


def test_four_value_board_fit_round_trips(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    job = Job(name="j", board_fits={1: [1.0, 2.0, 0.5, 0.9977]})
    store.save(job)
    assert store.load().board_fits == {1: [1.0, 2.0, 0.5, 0.9977]}


def test_job_scale_round_trips_and_missing_key_loads_as_one(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    job = sample_job()
    job.scale = 0.9966
    store.save(job)
    assert store.load().scale == pytest.approx(0.9966)
    data = yaml.safe_load((tmp_path / "job.yaml").read_text())
    data.pop("scale")
    (tmp_path / "job.yaml").write_text(yaml.safe_dump(data))
    assert store.load().scale == 1.0


@pytest.mark.parametrize("text", [
    "name: j\nscale: 1.2\n",
    "name: j\nscale: 0.5\n",
    "name: j\nscale: abc\n",
    "name: j\nboard_fits:\n  0: [1, 2, 0, 0.5]\n",
    "name: j\nboard_fits:\n  0: [1, 2]\n",
    "name: j\nboard_fits:\n  0: [1, 2, 0, 1, 1]\n",
])
def test_scale_outside_limits_or_bad_board_fit_is_refused(tmp_path, text):
    (tmp_path / "job.yaml").write_text(text)
    with pytest.raises(ValueError):
        JobStore(tmp_path / "job.yaml").load()


# --- place depth from bed depth, PCB thickness and part height -----------------

class HeightRules(FakeRules):
    """FakeRules plus package heights, as PackageRules.height_for gives them."""

    def __init__(self, heights=None, **kw):
        super().__init__(**kw)
        self.heights = heights or {}

    def height_for(self, package):
        return self.heights.get(package)


def bed_head(**kw) -> HeadConfig:
    return HeadConfig(bed_depth=kw.pop("bed_depth", 8.0), **kw)


def test_plan_explicit_rule_place_depth_wins_over_bed_depth():
    job = two_feeder_job([part("U1", "1u", "SOIC")])
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head())
    assert item.place_depth == 2.5  # the SOIC rule's own depth, not 8 - 1.6 - 0.5


def test_plan_place_depth_from_bed_depth_thickness_and_part_height():
    job = two_feeder_job([part("R1", "100n", "0603")])
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head())
    assert item.place_depth == pytest.approx(8.0 - 1.6 - 0.5)  # head.part_height default
    assert item.height == pytest.approx(0.5)
    assert item.problem is None


def test_plan_part_height_and_pcb_thickness_used():
    job = two_feeder_job([part("R1", "100n", "0603", height=1.2)])
    job.pcb_thickness = 1.0
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head())
    assert item.place_depth == pytest.approx(8.0 - 1.0 - 1.2)
    assert item.height == pytest.approx(1.2)


def test_plan_rule_height_used_when_part_has_none():
    rules = HeightRules(heights={"0603": 0.9}, nozzles={"0603": 2})
    job = two_feeder_job([part("R1", "100n", "0603")])
    (item,) = plan(job, feeders_for_order(), rules, head=bed_head())
    assert item.height == pytest.approx(0.9)
    assert item.place_depth == pytest.approx(8.0 - 1.6 - 0.9)


def test_plan_part_height_beats_rule_height():
    rules = HeightRules(heights={"0603": 0.9}, nozzles={"0603": 2})
    job = two_feeder_job([part("R1", "100n", "0603", height=0.3)])
    (item,) = plan(job, feeders_for_order(), rules, head=bed_head())
    assert item.height == pytest.approx(0.3)


def test_plan_head_part_height_is_the_fallback():
    job = two_feeder_job([part("R1", "100n", "0603")])
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head(part_height=0.7))
    assert item.height == pytest.approx(0.7)
    assert item.place_depth == pytest.approx(8.0 - 1.6 - 0.7)


def test_plan_bed_depth_zero_keeps_the_default_place_depth():
    job = two_feeder_job([part("R1", "100n", "0603")])
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=HeadConfig(bed_depth=0.0))
    assert item.place_depth is None  # runner then uses head.place_depth, as before
    (item,) = plan(job, feeders_for_order(), simple_rules())
    assert item.place_depth is None
    assert item.height == pytest.approx(0.5)  # the height is still reported


def test_plan_negative_computed_depth_is_a_problem():
    job = two_feeder_job([part("R1", "100n", "0603", height=2.0)])
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head(bed_depth=2.0))
    assert item.place_depth == pytest.approx(2.0 - 1.6 - 2.0)
    assert item.problem == "Place depth below 0: check bed depth, PCB thickness and part height"


def test_plan_negative_depth_problem_only_for_pending_items():
    job = two_feeder_job([part("R1", "100n", "0603", height=2.0)], status={"0:R1": "placed"})
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head(bed_depth=2.0))
    assert item.problem is None


def test_plan_explicit_rule_depth_is_not_checked_for_sign_here():
    # An explicit rule depth is validated by PackageRules (> 0); the computed-depth check is only for computed depths.
    job = two_feeder_job([part("U1", "1u", "SOIC")])
    (item,) = plan(job, feeders_for_order(), simple_rules(), head=bed_head(bed_depth=0.5))
    assert item.place_depth == 2.5 and item.problem is None


def test_plan_item_height_has_a_default():
    item = PlanItem(key="0:R1", board=0, ref="R1", value="", package="", x=None, y=None, rotation=0.0,
                    nozzle=1, feeder=None, place_depth=None, status="pending", problem=None)
    assert item.height == 0.0


def test_job_pcb_thickness_defaults_and_round_trips(tmp_path):
    store = JobStore(tmp_path / "job.yaml")
    store.save(Job(name="j"))
    assert store.load().pcb_thickness == 1.6
    job = Job(name="j", pcb_thickness=0.8)
    store.save(job)
    assert store.load().pcb_thickness == 0.8


def test_job_pcb_thickness_absent_loads_as_default(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: old\n")
    assert JobStore(path).load().pcb_thickness == 1.6


@pytest.mark.parametrize("value", [0, -1, 10.5, "abc", float("nan")])
def test_job_rejects_bad_pcb_thickness(tmp_path, value):
    path = tmp_path / "job.yaml"
    path.write_text(f"name: j\npcb_thickness: {value!r}\n" if not isinstance(value, float) or value == value
                    else "name: j\npcb_thickness: .nan\n")
    with pytest.raises(ValueError, match="pcb_thickness"):
        JobStore(path).load()


def test_job_part_height_loads_and_validates(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: j\nparts:\n"
                    "  - {ref: R1, value: 10k, package: R, x: 0, y: 0, rotation: 0}\n"
                    "  - {ref: R2, value: 10k, package: R, x: 0, y: 0, rotation: 0, height: 1.5}\n")
    job = JobStore(path).load()
    assert job.parts[0].height is None
    assert job.parts[1].height == 1.5
    for bad in ("-0.1", "30.5"):
        path.write_text(f"name: j\nparts:\n  - {{ref: R1, value: 10k, package: R, x: 0, y: 0, rotation: 0, height: {bad}}}\n")
        with pytest.raises(ValueError, match="height"):
            JobStore(path).load()


def _edited_job() -> Job:
    return Job(
        name="Board A",
        source="a.pos",
        parts=[
            JobPart(ref="R1", value="10k", package="R_0805", x=1.5, y=2.0, rotation=90.0, side="top",
                    skip=True, nozzle=2, feeder="F1", vision=False, speed=50, height=1.2),
            JobPart(ref="C1", value="100n", package="C_0603", x=10.0, y=-3.25, rotation=0.0, speed=100),
            JobPart(ref="R4", value="1k", package="R_0603", x=4.0, y=5.0, rotation=180.0, side="bottom", skip=True),
        ],
        boards=[BoardInstance(x=0.0, y=0.0), BoardInstance(x=50.0, y=0.0, rotation=90.0, skip=True)],
        origin=(120.5, -40.25),
        rotation=12.5,
        scale=1.002,
        status={"0:R1": "placed", "1:C1": "failed"},
        fiducials=[Fiducial(name="FID1", x=2.0, y=2.0), Fiducial(name="FID2", x=40.0, y=2.0)],
        fiducial_mode="board",
        fiducial_diameter=1.5,
        fit={"rms": 0.02, "rotation": 0.1, "origin": [120.5, -40.25]},
        board_fits={0: [120.5, -40.25, 12.5], 1: [170.0, -40.0, 12.5, 1.001]},
        references=[{"ref": "R1", "x": 1.5, "y": 2.0}, {"ref": None, "x": 10.0, "y": 10.0}],
        reference_board=1,
        pcb_thickness=0.8,
    )


def test_job_file_round_trip_keeps_every_edit():
    job = _edited_job()
    text = export_job_yaml(job)
    header = yaml.safe_load(text)
    assert header["format"] == "tvm802-job" and header["version"] == 1
    back = parse_job_yaml(text)
    assert back == job
    assert export_job_yaml(back) == text  # exporting again gives the same file


@pytest.mark.parametrize("text, match", [
    ("name: j\n", "not a job file exported from this app"),
    ("- just\n- a list\n", "not a job file exported from this app"),
    (yaml.safe_dump({"format": "tvm802-feeders", "version": 1, "feeders": []}), "not a job file"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 2, "job": {"name": "j"}}), "version"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1}), "no job"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1, "job": 5}), "mapping"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1, "job": {"source": "x"}}), "without a name"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1, "job": {"name": "j", "bogus": 1}}), "unknown"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1, "job": {"name": "j", "scale": 1.5}}), "scale"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1,
                     "job": {"name": "j", "parts": [{"ref": "R1"}]}}), "part 0"),
    (yaml.safe_dump({"format": "tvm802-job", "version": 1, "job": {"name": "j", "parts": 7}}), "not valid"),
    ("format: [unclosed", "not a valid YAML"),
])
def test_job_file_bad_content_is_refused_with_a_reason(text, match):
    with pytest.raises(ValueError, match=match):
        parse_job_yaml(text)


# ---------------------------------------------------------------- board marks

LIMIT_X, LIMIT_Y = (-100.0, 300.0), (-100.0, 300.0)


def marked(boards=1) -> Job:
    """Parts on a 40 x 30 mm board, `boards` copies 50 mm apart in a row, every board with the
    design marks (5, 4) R1 and (40, 30) on the file's coordinates, nothing measured."""
    parts = [part("R1", x=5.0, y=4.0), part("U1", x=20.0, y=10.0), part("C1", x=40.0, y=30.0)]
    job = Job(name="m", parts=parts, boards=grid(1, boards, 50.0, 0.0), origin=(0.0, 0.0), rotation=0.0)
    sync_board_marks(job)
    for entry in job.board_marks:
        entry["m1"] = blank_mark(5.0, 4.0, "R1")
        entry["m2"] = blank_mark(40.0, 30.0)
    return job


def measure(job, board, origin, rot_deg):
    """Record both marks of `board` as the camera sees them on a board at `origin` rotated `rot_deg`."""
    for name, p in (("m1", (5.0, 4.0)), ("m2", (40.0, 30.0))):
        job.board_marks[board][name]["mx"], job.board_marks[board][name]["my"] = _world(origin, rot_deg, p)


def test_to_world_uses_the_fiducial_fit_then_the_mark_fit_then_the_panel():
    job = Job(name="j", origin=(150.0, 150.0), rotation=0.0, boards=[BoardInstance(), BoardInstance(x=50.0)])
    assert close_xy(to_world(job, 1, 10.0, 0.0), (210.0, 150.0))  # panel formula
    job.mark_fits = {1: [100.0, 100.0, 90.0, 1.0]}
    assert close_xy(to_world(job, 1, 10.0, 0.0), (100.0, 110.0))
    assert world_rotation(job, 1, 30.0) == pytest.approx(120.0)
    job.board_fits = {1: [0.0, 0.0, 0.0, 1.0]}  # the fiducial fit wins over the mark fit
    assert close_xy(to_world(job, 1, 10.0, 0.0), (10.0, 0.0))
    assert world_rotation(job, 1, 30.0) == pytest.approx(30.0)


def test_sync_board_marks_grows_copying_the_design_and_shrinks_with_its_fits():
    job = marked(boards=1)
    job.board_marks[0]["m1"]["mx"] = 1.0  # a measurement is not copied to the new boards
    job.boards = grid(1, 3, 50.0, 0.0)
    sync_board_marks(job)
    assert len(job.board_marks) == 3
    assert job.board_marks[0]["m1"]["mx"] == 1.0
    for entry in job.board_marks[1:]:
        assert entry["m1"] == blank_mark(5.0, 4.0, "R1")
        assert entry["m2"] == blank_mark(40.0, 30.0)
        assert entry["error"] is None
    job.mark_fits = {0: [1.0, 2.0, 0.0, 1.0], 2: [3.0, 4.0, 0.0, 1.0]}
    job.boards = grid(1, 2, 50.0, 0.0)
    sync_board_marks(job)
    assert len(job.board_marks) == 2
    assert job.mark_fits == {0: [1.0, 2.0, 0.0, 1.0]}


def test_old_job_file_takes_its_two_point_references_as_the_design_marks(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: old\nboards:\n- x: 0\n  y: 0\n- x: 50\n  y: 0\n"
                    "references:\n- {ref: R1, x: 5, y: 4}\n- {ref: null, x: 40, y: 30}\n")
    job = JobStore(path).load()
    assert len(job.board_marks) == 2
    for entry in job.board_marks:
        assert entry["m1"] == blank_mark(5.0, 4.0, "R1")
        assert entry["m2"] == blank_mark(40.0, 30.0)
    assert job.mark_fits == {}


def test_refit_board_fits_its_two_marks_and_the_panel_follows_it():
    job = marked(boards=1)
    measure(job, 0, (147.5, 152.25), 1.2)
    assert refit_board(job, 0, LIMIT_X, LIMIT_Y) is None
    assert job.board_marks[0]["error"] is None
    fit = job.mark_fits[0]
    assert fit[0] == pytest.approx(147.5, abs=1e-6) and fit[1] == pytest.approx(152.25, abs=1e-6)
    assert fit[2] == pytest.approx(1.2, abs=1e-6) and fit[3] == pytest.approx(1.0, abs=1e-9)
    assert close_xy(to_world(job, 0, 20.0, 10.0), _world((147.5, 152.25), 1.2, (20.0, 10.0)), tol=1e-6)
    assert job.rotation == pytest.approx(1.2, abs=1e-6)
    assert close_xy(job.origin, (147.5, 152.25), tol=1e-6)
    assert mark_results(job)[0] == {"board": 0, "ok": True, "error": None, "rotation": pytest.approx(1.2, abs=1e-6),
                                    "scale": pytest.approx(1.0, abs=1e-9),
                                    "distance_error": pytest.approx(0.0, abs=1e-9)}


def test_panel_follows_the_lowest_measured_board():
    job = marked(boards=2)
    measure(job, 1, (198.8, 153.0), 2.0)
    assert refit_board(job, 1, LIMIT_X, LIMIT_Y) is None
    assert job.rotation == pytest.approx(2.0, abs=1e-6)  # the only measured board
    # board 1 has no marks yet: it sits where board 2's fit puts its design position
    assert close_xy(to_world(job, 0, 5.0, 4.0), _world((198.8, 153.0), 2.0, (5.0 - 50.0, 4.0)), tol=1e-6)
    measure(job, 0, (147.5, 152.25), 1.2)
    assert refit_board(job, 0, LIMIT_X, LIMIT_Y) is None
    assert job.rotation == pytest.approx(1.2, abs=1e-6)  # the lowest measured board now
    job.board_marks[0]["m2"]["my"] = None  # board 1 is again the lowest with a fit
    assert refit_board(job, 0, LIMIT_X, LIMIT_Y) is None
    assert 0 not in job.mark_fits
    assert job.rotation == pytest.approx(2.0, abs=1e-6)


def test_refit_board_refuses_a_distance_mismatch_but_keeps_the_measurements():
    job = marked(boards=1)
    measure(job, 0, (147.5, 152.25), 1.2)
    job.board_marks[0]["m2"]["mx"] += 3.0  # the second mark was captured 3 mm off
    error = refit_board(job, 0, LIMIT_X, LIMIT_Y)
    assert error and "wrong feature" in error
    assert 0 not in job.mark_fits
    assert job.board_marks[0]["m2"]["mx"] is not None and job.board_marks[0]["error"] == error
    result = mark_results(job)[0]
    assert result["ok"] is False and result["error"] == error


def test_refit_board_refuses_marks_that_put_parts_off_the_machine():
    job = marked(boards=1)
    measure(job, 0, (-500.0, 0.0), 0.0)
    error = refit_board(job, 0, LIMIT_X, LIMIT_Y)
    assert error and "outside the machine" in error
    assert 0 not in job.mark_fits and job.board_marks[0]["m1"]["mx"] is not None


def test_incomplete_marks_remove_the_fit_and_report_nothing():
    job = marked(boards=1)
    measure(job, 0, (147.5, 152.25), 1.2)
    assert refit_board(job, 0, LIMIT_X, LIMIT_Y) is None and 0 in job.mark_fits
    job.board_marks[0]["m2"]["my"] = None
    assert refit_board(job, 0, LIMIT_X, LIMIT_Y) is None
    assert 0 not in job.mark_fits and job.board_marks[0]["error"] is None
    assert mark_results(job)[0] == {"board": 0, "ok": False, "error": None,
                                    "rotation": None, "scale": None, "distance_error": None}


def test_clear_fit_leaves_the_mark_fits_alone():
    job = Job(name="j", fit={"mode": "panel"}, board_fits={0: [1.0, 2.0, 0.1]},
              mark_fits={0: [3.0, 4.0, 0.2, 1.0]})
    clear_fit(job)
    assert job.fit is None and job.board_fits == {}
    assert job.mark_fits == {0: [3.0, 4.0, 0.2, 1.0]}


def test_board_marks_and_mark_fits_round_trip(tmp_path):
    job = marked(boards=2)
    measure(job, 0, (147.5, 152.25), 1.2)
    refit_board(job, 0, LIMIT_X, LIMIT_Y)
    job.board_marks[1]["m1"] = blank_mark(5.0, 4.0, "R1")
    store = JobStore(tmp_path / "job.yaml")
    store.save(job)
    again = store.load()
    assert again == job
    data = yaml.safe_load((tmp_path / "job.yaml").read_text())
    assert sorted(data["mark_fits"]) == [0]
    assert data["board_marks"][0]["m1"]["ref"] == "R1" and data["board_marks"][0]["error"] is None
