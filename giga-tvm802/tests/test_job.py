"""Jobs, panels, coordinate mapping, feeder matching and planning (no hardware)."""

import math
from types import SimpleNamespace

import pytest
import yaml

from pnp.job.feeders import Feeder
from pnp.job.job import (
    BoardInstance,
    Fiducial,
    Job,
    JobPart,
    JobStore,
    clear_fit,
    fit_rotation,
    grid,
    is_fiducial,
    job_from_pos,
    match_feeder,
    norm,
    placement_key,
    plan,
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
        "name", "source", "origin", "rotation", "boards", "parts", "status",
        "fiducials", "fiducial_mode", "fiducial_diameter", "fit", "board_fits",
        "references", "reference_board",  # two-point board alignment (2026-11-13)
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
