"""Feeder model, feeder store, head config loading and calibration prick offset (no hardware)."""

from pathlib import Path

import pytest
import yaml

from pnp.config import HeadConfig, load_config
from pnp.job.feeders import Feeder, FeederError, FeederStore
from pnp.vision.calibration_store import Calibration

REPO = Path(__file__).resolve().parent.parent


def strip(**kw) -> Feeder:
    base = dict(id="F1", kind="strip", pick=(100.0, 50.0), direction=(0, 2), pitch=4.0, capacity=3)
    base.update(kw)
    return Feeder(**base)


def test_direction_normalised_on_load():
    f = strip()
    assert f.direction == pytest.approx((0.0, 1.0))
    f.direction = (0.0, 5.0)
    f.validate()
    assert f.direction == pytest.approx((0.0, 1.0))


def test_strip_pick_location_advances_by_pitch():
    f = strip()
    assert f.pick_location() == pytest.approx((100.0, 50.0))
    f.after_pick()
    assert f.pick_location() == pytest.approx((100.0, 54.0))
    f.after_pick()
    assert f.pick_location() == pytest.approx((100.0, 58.0))
    assert f.count == 2


def test_strip_remaining():
    f = strip(capacity=3)
    assert f.remaining() == 3
    f.after_pick()
    f.after_pick()
    assert f.remaining() == 1
    f.after_pick()
    f.after_pick()
    assert f.remaining() == 0
    assert strip(capacity=None).remaining() is None
    assert Feeder(id="D", kind="drag", pick=(0, 0), prick=(1, 1)).remaining() is None


def test_drag_pick_location_fixed_and_feed_flags():
    f = Feeder(id="D1", kind="drag", pick=(100.0, 50.0), direction=(1, 0), pitch=2.0, prick=(80.0, 40.0))
    assert f.needs_feed is True
    assert f.pick_location() == (100.0, 50.0)
    assert f.drag_vector() == pytest.approx((2.0, 0.0))
    f.after_feed()
    assert f.needs_feed is False
    f.after_pick()
    assert f.pick_location() == (100.0, 50.0)
    assert f.needs_feed is True
    assert f.count == 1


@pytest.mark.parametrize("bad, msg", [
    (dict(kind="bogus"), "kind"),
    (dict(pitch=0), "pitch"),
    (dict(direction=(0, 0)), "direction"),
    (dict(kind="drag", prick=None), "prick"),
])
def test_validate_errors(bad, msg):
    f = strip(**bad)
    with pytest.raises(ValueError, match=msg):
        f.validate()


def test_store_round_trip(tmp_path):
    path = tmp_path / "feeders.yaml"
    store = FeederStore(path)
    assert store.all() == []
    store.upsert(strip(id="F1", name="strip", count=2, capacity=None, rotation=90.0, value="10k"))
    store.upsert(Feeder(id="D1", kind="drag", pick=(100, 50), direction=(1, 1), pitch=2,
                        prick=(80.5, 40), needs_feed=False, enabled=False, package="0603"))

    again = FeederStore(path)
    f1, d1 = again.get("F1"), again.get("D1")
    assert f1.pick == (100.0, 50.0) and isinstance(f1.pick, tuple)
    assert f1.direction == pytest.approx((0.0, 1.0))
    assert f1.count == 2 and isinstance(f1.count, int)
    assert f1.capacity is None
    assert f1.rotation == 90.0 and f1.value == "10k"
    assert d1.prick == (80.5, 40.0) and isinstance(d1.prick, tuple)
    assert d1.direction == pytest.approx((0.7071, 0.7071), abs=1e-4)
    assert d1.needs_feed is False and d1.enabled is False and d1.package == "0603"
    raw = yaml.safe_load(path.read_text())
    assert raw["feeders"][0]["pick"] == [100.0, 50.0]
    assert raw["feeders"][0]["prick"] is None


def test_upsert_replaces_by_id(tmp_path):
    store = FeederStore(tmp_path / "f.yaml")
    store.upsert(strip(name="first"))
    store.upsert(strip(name="second"))
    assert [f.name for f in store.all()] == ["second"]
    assert [f.name for f in FeederStore(tmp_path / "f.yaml").all()] == ["second"]


def test_delete_and_unknown_id(tmp_path):
    store = FeederStore(tmp_path / "f.yaml")
    store.upsert(strip())
    store.delete("F1")
    assert store.all() == []
    assert FeederStore(tmp_path / "f.yaml").all() == []
    with pytest.raises(FeederError):
        store.get("F1")
    with pytest.raises(FeederError):
        store.delete("F1")


def test_unknown_yaml_key_rejected(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("feeders:\n  - id: F1\n    pitchh: 4\n")
    with pytest.raises(ValueError, match="pitchh"):
        FeederStore(path)


def test_missing_file_gives_empty_store(tmp_path):
    assert FeederStore(tmp_path / "nope.yaml").all() == []


def test_head_config_loads_from_yaml(tmp_path):
    path = tmp_path / "machine.yaml"
    path.write_text("head:\n  discard: [10, 20.5]\n  pick_dwell: 0.3\n  check_vacuum: false\n")
    cfg = load_config(path)
    assert isinstance(cfg.head, HeadConfig)
    assert cfg.head.discard == (10.0, 20.5)
    assert cfg.head.pick_dwell == 0.3
    assert cfg.head.check_vacuum is False
    assert cfg.head.place_depth == 3.0


def test_head_config_unknown_key_rejected(tmp_path):
    path = tmp_path / "machine.yaml"
    path.write_text("head:\n  pick_dwel: 0.3\n")
    with pytest.raises(ValueError, match="pick_dwel"):
        load_config(path)


def test_repo_machine_config_still_loads():
    # The head values may be changed on the Defaults tab; the file must still load.
    cfg = load_config(REPO / "config" / "machine.yaml")
    assert isinstance(cfg.head, HeadConfig)


def test_prick_offset_round_trip(tmp_path):
    path = tmp_path / "calibration.yaml"
    cal = Calibration(prick_offset=(-50.0, 12.0))
    cal.save(path)
    assert Calibration.load(path).prick_offset == pytest.approx((-50.0, 12.0))


def test_old_calibration_file_has_no_prick_offset(tmp_path):
    path = tmp_path / "calibration.yaml"
    path.write_text("cameras: {}\nnozzle_offset:\n  1: [-42.0, -3.0]\nup_position: {}\n")
    cal = Calibration.load(path)
    assert cal.prick_offset is None
    assert cal.nozzle_offset[1] == (-42.0, -3.0)
    assert Calibration().prick_offset is None


def drag(id: str, bank: str = "", lane: int | None = None, **kw) -> Feeder:
    base = dict(id=id, kind="drag", pick=(10.0, 20.0), prick=(5.0, 20.0), bank=bank, lane=lane)
    base.update(kw)
    return Feeder(**base)


def test_bank_fields_round_trip(tmp_path):
    path = tmp_path / "f.yaml"
    FeederStore(path).upsert(drag("back-1", bank="back", lane=1, value="10k"))
    f = FeederStore(path).get("back-1")
    assert f.bank == "back" and f.lane == 1 and f.value == "10k"
    assert FeederStore(path).get("back-1").bank == "back"


def test_by_bank_orders_by_lane_then_id(tmp_path):
    store = FeederStore(tmp_path / "f.yaml")
    store.upsert(drag("loose"))
    store.upsert(drag("b-3", bank="b", lane=3))
    store.upsert(drag("b-x", bank="b"))  # no lane number: last
    store.upsert(drag("b-1", bank="b", lane=1))
    store.upsert(drag("other-1", bank="other", lane=1))
    assert [f.id for f in store.by_bank("b")] == ["b-1", "b-3", "b-x"]
    assert store.by_bank("missing") == []


def test_replace_bank_swaps_only_that_bank(tmp_path):
    path = tmp_path / "f.yaml"
    store = FeederStore(path)
    store.upsert(drag("loose"))
    store.upsert(drag("b-1", bank="b", lane=1))
    store.upsert(drag("b-2", bank="b", lane=2))
    store.upsert(drag("o-1", bank="o", lane=1))
    store.replace_bank("b", [drag("b-1", bank="b", lane=1, value="new"), drag("b-9", bank="b", lane=9)])
    again = FeederStore(path)
    assert sorted(f.id for f in again.all()) == ["b-1", "b-9", "loose", "o-1"]
    assert again.get("b-1").value == "new"


def test_replace_bank_clash_raises_and_changes_nothing(tmp_path):
    path = tmp_path / "f.yaml"
    store = FeederStore(path)
    store.upsert(drag("a-1", bank="a", lane=1, value="keep"))
    store.upsert(drag("b-1", bank="b", lane=1))
    before = yaml.safe_load(path.read_text())
    with pytest.raises(ValueError):
        store.replace_bank("b", [drag("b-2", bank="b", lane=2), drag("a-1", bank="b", lane=1)])
    assert yaml.safe_load(path.read_text()) == before
    assert [f.id for f in store.by_bank("b")] == ["b-1"]
    assert store.get("a-1").value == "keep"


def test_replace_bank_rejects_wrong_bank_and_duplicates(tmp_path):
    store = FeederStore(tmp_path / "f.yaml")
    with pytest.raises(ValueError, match="bank"):
        store.replace_bank("b", [drag("x", bank="c", lane=1)])
    with pytest.raises(ValueError, match="duplicate"):
        store.replace_bank("b", [drag("x", bank="b", lane=1), drag("x", bank="b", lane=2)])
    assert store.all() == []


def test_delete_bank_keeps_other_feeders(tmp_path):
    path = tmp_path / "f.yaml"
    store = FeederStore(path)
    store.upsert(drag("loose"))
    store.upsert(drag("b-1", bank="b", lane=1))
    store.delete_bank("b")
    assert [f.id for f in FeederStore(path).all()] == ["loose"]


def tray(cols=3, rows=2, end=(120.0, 70.0), **kw) -> Feeder:
    base = dict(id="T1", kind="tray", pick=(100.0, 50.0), tray_end=end, tray_cols=cols, tray_rows=rows)
    base.update(kw)
    return Feeder(**base)


def test_tray_pick_location_goes_row_by_row():
    # 3 columns x 2 rows: pick is the first part, tray_end the last (opposite corner).
    f = tray()
    spots = []
    for _ in range(6):
        spots.append(f.pick_location())
        f.after_pick()
    assert spots == [(100.0, 50.0), (110.0, 50.0), (120.0, 50.0),
                     (100.0, 70.0), (110.0, 70.0), (120.0, 70.0)]
    assert f.count == 6


def test_tray_single_row_and_single_column():
    row = tray(cols=4, rows=1, end=(130.0, 50.0))
    assert row.pick_location() == pytest.approx((100.0, 50.0))
    row.count = 3
    assert row.pick_location() == pytest.approx((130.0, 50.0))  # rows == 1: y stays on the first line
    col = tray(cols=1, rows=3, end=(100.0, 90.0))
    col.count = 2
    assert col.pick_location() == pytest.approx((100.0, 90.0))  # cols == 1: x stays on the first line
    single = tray(cols=1, rows=1, end=None)
    assert single.pick_location() == (100.0, 50.0)


def test_tray_does_not_need_feed_and_counts_remaining():
    f = tray()
    assert f.remaining() == 6
    f.after_pick()
    assert f.remaining() == 5 and f.needs_feed is True and f.kind == "tray"
    for _ in range(5):
        f.after_pick()
    assert f.remaining() == 0
    f.after_pick()
    assert f.remaining() == 0  # never negative
    assert f.tray_capacity() == 6


def test_tray_validation():
    with pytest.raises(ValueError, match="tray_end"):
        tray(cols=2, rows=2, end=None).validate()
    with pytest.raises(ValueError, match="tray_cols"):
        tray(cols=0).validate()
    with pytest.raises(ValueError, match="tray_rows"):
        tray(rows=101).validate()
    tray(cols=1, rows=1, end=None).validate()  # one part needs no end position
    tray(cols=100, rows=100).validate()
    with pytest.raises(ValueError, match="kind"):
        Feeder(id="X", kind="tary").validate()


def test_tray_end_normalised_on_load():
    f = tray(end=[120, 70])
    assert f.tray_end == (120.0, 70.0) and isinstance(f.tray_end, tuple)


def test_tray_round_trip(tmp_path):
    path = tmp_path / "f.yaml"
    FeederStore(path).upsert(tray(count=4, value="QFN"))
    again = FeederStore(path).get("T1")
    assert again.tray_end == (120.0, 70.0) and isinstance(again.tray_end, tuple)
    assert (again.tray_cols, again.tray_rows, again.count) == (3, 2, 4)
    assert again.remaining() == 2 and again.value == "QFN"
    raw = yaml.safe_load(path.read_text())["feeders"][0]
    assert raw["tray_end"] == [120.0, 70.0] and raw["tray_cols"] == 3 and raw["tray_rows"] == 2


def test_old_feeder_dicts_load_without_tray_fields(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("feeders:\n"
                    "  - id: S1\n    kind: strip\n    pick: [1, 2]\n    capacity: 5\n"
                    "  - id: D1\n    kind: drag\n    pick: [3, 4]\n    prick: [1, 1]\n")
    store = FeederStore(path)
    s1, d1 = store.get("S1"), store.get("D1")
    assert s1.tray_end is None and s1.tray_cols == 1 and s1.tray_rows == 1
    assert d1.tray_end is None and d1.remaining() is None
    assert s1.remaining() == 5
