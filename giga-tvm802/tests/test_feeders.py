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
    cfg = load_config(REPO / "config" / "machine.yaml")
    assert cfg.head == HeadConfig()


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
