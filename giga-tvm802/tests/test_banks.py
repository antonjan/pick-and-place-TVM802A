"""Feeder banks: lane geometry, layouts and config/banks.yaml round trips (no hardware)."""

import pytest
import yaml

from pnp.job.banks import BankError, BankStore, GenerateSpec, LaneSpec, generate_lanes, lane_offsets
from pnp.job.feeders import Feeder, FeederStore


def back_spec(**kw) -> GenerateSpec:
    base = dict(
        origin=(40.0, 300.0), lane_axis=(1, 0), feed_direction=(0, -1), sprocket_side=-1,
        lanes=[LaneSpec(width=8, pitch=4) for _ in range(4)],
    )
    base.update(kw)
    return GenerateSpec(**base)


def stores(tmp_path):
    feeders = FeederStore(tmp_path / "feeders.yaml")
    banks = BankStore(tmp_path / "banks.yaml", feeders)
    return banks, feeders


def loaded_back(banks: BankStore, feeders: FeederStore) -> None:
    """Back bank with lanes 1-4 loaded, saved as layout Passives."""
    banks.upsert("back", "Back stack")
    banks.generate("back", back_spec())
    values = [("10k", "R_0805_2012Metric"), ("100n", "C_0402_1005Metric"),
              ("1u", "C_0805_2012Metric"), ("BC847", "SOT-23")]
    for lane, (value, package) in enumerate(values, 1):
        f = feeders.get(f"back-{lane}")
        f.value, f.package = value, package
        feeders.upsert(f)
    banks.save_layout("back", "Passives")


# ------------------------------------------------------------------ geometry

def test_offsets_without_explicit_values_stack_on_tape_width():
    spec = GenerateSpec(origin=(0, 0), lanes=[LaneSpec(8), LaneSpec(12), LaneSpec(8)])
    assert lane_offsets(spec) == pytest.approx([0.0, 10.0, 20.0])


def test_offsets_include_gap():
    spec = GenerateSpec(origin=(0, 0), gap=1.5, lanes=[LaneSpec(8), LaneSpec(8)])
    assert lane_offsets(spec) == pytest.approx([0.0, 9.5])


def test_explicit_offset_wins_and_restarts_the_chain():
    spec = GenerateSpec(origin=(0, 0), gap=2.0, lanes=[
        LaneSpec(8, offset=5.0),        # lane 1 explicit
        LaneSpec(8),                    # 5 + (8 + 8) / 2 + 2 = 15
        LaneSpec(16, offset=40.0),      # explicit, chain restarts from here
        LaneSpec(8),                    # 40 + (16 + 8) / 2 + 2 = 54
    ])
    assert lane_offsets(spec) == pytest.approx([5.0, 15.0, 40.0, 54.0])


def test_lane_one_without_offset_is_zero():
    assert lane_offsets(GenerateSpec(origin=(0, 0), lanes=[LaneSpec(8)])) == [0.0]


def test_back_bank_geometry():
    feeders = generate_lanes("back", back_spec(), [])
    assert [f.id for f in feeders] == ["back-1", "back-2", "back-3", "back-4"]
    assert [f.lane for f in feeders] == [1, 2, 3, 4]
    assert [c for f in feeders for c in f.pick] == pytest.approx([40, 300, 48, 300, 56, 300, 64, 300])
    for f in feeders:
        assert f.kind == "drag"
        assert f.direction == pytest.approx((0.0, -1.0))
        assert f.pitch == 4.0
        assert f.bank == "back"
        assert f.needs_feed and f.enabled
        assert f.name == "8 mm lane " + str(f.lane)
        # sprocket side is -X for 8 mm tape, and the default hole is 2 mm upstream (feed is -Y):
        # prick = pick + (-3.5, +2)
        assert f.prick == pytest.approx((f.pick[0] - 3.5, f.pick[1] + 2.0))


def test_prick_along_moves_needle_along_feed():
    feeders = generate_lanes("back", back_spec(prick_along=2.0, lanes=[LaneSpec(8)]), [])
    assert feeders[0].prick == pytest.approx((36.5, 300.0 - 2.0))


def test_normalises_vectors():
    spec = back_spec(lane_axis=(0, 5), feed_direction=(3, 4))
    assert spec.lane_axis == pytest.approx((0.0, 1.0))
    assert spec.feed_direction == pytest.approx((0.6, 0.8))


def test_keep_parts_carries_loaded_state_over():
    old = [Feeder(id="back-2", kind="drag", pick=(48, 300), prick=(44.5, 300), bank="back", lane=2,
                  value="100n", package="C_0402", rotation=90.0, pick_depth=2.5, count=7,
                  enabled=False, name="custom")]
    feeders = generate_lanes("back", back_spec(), old)
    lane2 = feeders[1]
    assert (lane2.value, lane2.package, lane2.rotation, lane2.pick_depth, lane2.count) == \
        ("100n", "C_0402", 90.0, 2.5, 7)
    assert lane2.enabled is False and lane2.name == "custom"
    assert feeders[0].value == ""  # no old lane 1


def test_keep_parts_false_starts_fresh():
    old = [Feeder(id="back-1", kind="drag", pick=(40, 300), prick=(36.5, 300), bank="back", lane=1,
                  value="10k", count=3)]
    lane1 = generate_lanes("back", back_spec(keep_parts=False), old)[0]
    assert lane1.value == "" and lane1.count == 0


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("bad_id", ["", "Back", "-back", "has space", "x" * 33])
def test_bad_bank_id(tmp_path, bad_id):
    banks, _ = stores(tmp_path)
    with pytest.raises(ValueError, match="bank id"):
        banks.upsert(bad_id)


@pytest.mark.parametrize("spec_kw, msg", [
    (dict(lanes=[]), "at least one lane"),
    (dict(lanes=[LaneSpec(8)] * 100), "at most 99"),
    (dict(lanes=[LaneSpec(0)]), "width"),
    (dict(lanes=[LaneSpec(8, pitch=0)]), "pitch"),
    (dict(sprocket_side=0), "sprocket_side"),
])
def test_generate_validation(tmp_path, spec_kw, msg):
    banks, _ = stores(tmp_path)
    banks.upsert("back")
    with pytest.raises(ValueError, match=msg):
        banks.generate("back", back_spec(**spec_kw))


def test_zero_vectors_rejected():
    with pytest.raises(ValueError, match="lane_axis"):
        back_spec(lane_axis=(0, 0))
    with pytest.raises(ValueError, match="feed_direction"):
        back_spec(feed_direction=(0, 0))


def test_unknown_bank_and_layout(tmp_path):
    banks, _ = stores(tmp_path)
    with pytest.raises(BankError, match="unknown"):
        banks.get("nope")
    with pytest.raises(BankError, match="unknown"):
        banks.generate("nope", back_spec())
    banks.upsert("back")
    with pytest.raises(BankError, match="unknown"):
        banks.load_layout("back", "missing")
    with pytest.raises(BankError, match="unknown"):
        banks.delete_layout("back", "missing")


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_layout_name_length(tmp_path, name):
    banks, _ = stores(tmp_path)
    banks.upsert("back")
    with pytest.raises(ValueError, match="layout name"):
        banks.save_layout("back", name)


def test_generate_id_clash_with_other_bank_changes_nothing(tmp_path):
    banks, feeders = stores(tmp_path)
    banks.upsert("back")
    banks.upsert("front")
    # "back-1" is the id generate would use, but another bank owns it
    feeders.upsert(Feeder(id="back-1", kind="drag", pick=(0, 0), prick=(0, 0), bank="front", lane=1))
    before = [f.id for f in feeders.all()]
    with pytest.raises(BankError):
        banks.generate("back", back_spec(lanes=[LaneSpec(8)]))
    assert [f.id for f in feeders.all()] == before
    assert feeders.get("back-1").bank == "front"
    assert banks.get("back").active_layout is None


# ------------------------------------------------------------------ layouts and persistence

def test_layout_round_trip(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    assert banks.get("back").active_layout == "Passives"

    banks.generate("back", back_spec(keep_parts=False, lanes=[LaneSpec(12, pitch=8)] * 2))
    assert banks.get("back").active_layout is None
    assert [f.id for f in feeders.by_bank("back")] == ["back-1", "back-2"]
    assert feeders.get("back-1").value == ""

    lanes = banks.load_layout("back", "Passives")
    assert [f.id for f in lanes] == ["back-1", "back-2", "back-3", "back-4"]
    assert banks.get("back").active_layout == "Passives"
    restored = feeders.by_bank("back")
    assert [f.value for f in restored] == ["10k", "100n", "1u", "BC847"]
    assert [f.package for f in restored] == ["R_0805_2012Metric", "C_0402_1005Metric", "C_0805_2012Metric", "SOT-23"]
    assert all(f.bank == "back" for f in restored)
    assert restored[1].pick == pytest.approx((48.0, 300.0))


def test_save_layout_overwrites_and_sets_active(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    f = feeders.get("back-1")
    f.value = "22k"
    feeders.upsert(f)
    banks.save_layout("back", "Passives")
    banks.generate("back", back_spec(keep_parts=False))
    banks.load_layout("back", "Passives")
    assert feeders.get("back-1").value == "22k"


def test_delete_layout_clears_active(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    banks.delete_layout("back", "Passives")
    assert banks.get("back").layouts == {}
    assert banks.get("back").active_layout is None


def test_delete_bank_removes_its_lanes(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    feeders.upsert(Feeder(id="loose", kind="strip", pick=(0, 0)))
    banks.delete("back")
    assert feeders.by_bank("back") == []
    assert [f.id for f in feeders.all()] == ["loose"]
    with pytest.raises(BankError):
        banks.get("back")


def test_reload_from_disk_keeps_everything(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    banks.upsert("front", "Front stack")
    banks.generate("front", back_spec(origin=(40, 20), feed_direction=(0, 1), sprocket_side=1,
                                      lanes=[LaneSpec(12, pitch=8)] * 4))

    banks2, feeders2 = stores(tmp_path)
    assert sorted(b.id for b in banks2.all()) == ["back", "front"]
    back = banks2.get("back")
    assert back.name == "Back stack"
    assert back.active_layout == "Passives"
    assert list(back.layouts) == ["Passives"]
    assert [f.value for f in feeders2.by_bank("back")] == ["10k", "100n", "1u", "BC847"]
    front = feeders2.by_bank("front")
    assert len(front) == 4 and front[0].direction == pytest.approx((0.0, 1.0))
    assert front[0].prick == pytest.approx((40 + 5.5, 20.0 - 2.0))  # feed +Y: hole 2 mm upstream
    assert banks2.get("front").active_layout is None

    # the restored bank still loads its layout
    banks2.load_layout("back", "Passives")
    assert feeders2.get("back-4").value == "BC847"


def test_old_feeders_file_without_bank_or_lane_still_loads(tmp_path):
    path = tmp_path / "feeders.yaml"
    path.write_text("feeders:\n  - id: F1\n    kind: strip\n    pick: [1, 2]\n    capacity: 5\n")
    feeders = FeederStore(path)
    f = feeders.get("F1")
    assert f.bank == "" and f.lane is None
    banks = BankStore(tmp_path / "banks.yaml", feeders)  # no banks file either
    assert banks.all() == []


def test_missing_banks_file_gives_no_banks(tmp_path):
    banks, _ = stores(tmp_path)
    assert banks.all() == []
    assert not (tmp_path / "banks.yaml").exists()


def test_banks_file_format(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    raw = yaml.safe_load((tmp_path / "banks.yaml").read_text())
    entry = raw["banks"][0]
    assert entry["id"] == "back" and entry["active_layout"] == "Passives"
    assert len(entry["layouts"]["Passives"]) == 4
    assert entry["layouts"]["Passives"][0]["bank"] == "back"


# ------------------------------------------------------------------ rotation offset

def test_generate_with_rotation_offset_sets_every_lane():
    feeders = generate_lanes("left", back_spec(rotation_offset=-90.0), [])
    assert [f.rotation for f in feeders] == [90.0] * 4  # stored as the part's rotation in the tape
    kept = generate_lanes("left", back_spec(), [Feeder(id="left-1", kind="drag", pick=(0, 0), lane=1, rotation=90.0)])
    assert kept[0].rotation == 90.0  # no offset given: each lane keeps its own


def test_set_rotation_offset_on_existing_lanes(tmp_path):
    banks, feeders = stores(tmp_path)
    loaded_back(banks, feeders)
    banks.set_rotation_offset("back", -90.0)
    assert {f.rotation for f in FeederStore(tmp_path / "feeders.yaml").by_bank("back")} == {90.0}
    with pytest.raises(BankError):
        banks.set_rotation_offset("nope", 0.0)
