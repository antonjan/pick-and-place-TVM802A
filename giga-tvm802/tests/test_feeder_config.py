"""Feeder configuration export/import (feeders + banks + layouts)."""

import pytest
import yaml

from pnp.job.banks import BankStore
from pnp.job.feeder_config import apply_config, export_yaml, parse_config
from pnp.job.feeders import Feeder, FeederStore


def stores(tmp_path, name):
    feeders = FeederStore(tmp_path / f"{name}-feeders.yaml")
    return feeders, BankStore(tmp_path / f"{name}-banks.yaml", feeders)


def populate(feeders: FeederStore, banks: BankStore) -> None:
    banks.upsert("back", "Back stack")
    feeders.upsert(Feeder(id="B1", kind="drag", pick=(10.0, 300.0), direction=(1.0, 0.0), pitch=4.0,
                          prick=(8.0, 302.0), value="10k", package="R_0805_2012Metric",
                          bank="back", lane=1, count=7, needs_feed=False))
    feeders.upsert(Feeder(id="S1", kind="strip", pick=(60.0, 40.0), pitch=4.0, capacity=50, count=12,
                          value="100n"))
    banks.save_layout("back", "Passives")


def test_export_then_import_round_trip_into_empty_stores(tmp_path):
    src_f, src_b = stores(tmp_path, "src")
    populate(src_f, src_b)
    text = export_yaml(src_f, src_b)
    assert yaml.safe_load(text)["format"] == "tvm802-feeders"

    dst_f, dst_b = stores(tmp_path, "dst")
    feeders, banks = parse_config(text)
    result = apply_config(dst_f, dst_b, feeders, banks, reset_counts=False)
    assert result["feeders"] == 2 and result["banks"] == 1
    assert dst_f.get("B1").prick == (8.0, 302.0) and dst_f.get("B1").count == 7
    assert dst_f.get("S1").count == 12
    assert "Passives" in dst_b.get("back").layouts
    # persisted: reopening the files gives the same configuration
    again_f = FeederStore(tmp_path / "dst-feeders.yaml")
    again_b = BankStore(tmp_path / "dst-banks.yaml", again_f)
    assert {f.id for f in again_f.all()} == {"B1", "S1"}
    assert again_b.get("back").name == "Back stack"


def test_reset_counts_starts_lanes_fresh(tmp_path):
    src_f, src_b = stores(tmp_path, "src")
    populate(src_f, src_b)
    dst_f, dst_b = stores(tmp_path, "dst")
    apply_config(dst_f, dst_b, *parse_config(export_yaml(src_f, src_b)))  # reset_counts defaults on
    assert dst_f.get("S1").count == 0
    assert dst_f.get("B1").count == 0 and dst_f.get("B1").needs_feed is True


def test_replace_removes_feeders_not_in_the_file_and_merge_keeps_them(tmp_path):
    src_f, src_b = stores(tmp_path, "src")
    populate(src_f, src_b)
    text = export_yaml(src_f, src_b)

    dst_f, dst_b = stores(tmp_path, "dst")
    dst_f.upsert(Feeder(id="OLD", kind="strip", pick=(1.0, 1.0), pitch=4.0))
    apply_config(dst_f, dst_b, *parse_config(text), mode="merge")
    assert {f.id for f in dst_f.all()} == {"OLD", "B1", "S1"}

    apply_config(dst_f, dst_b, *parse_config(text), mode="replace")
    assert {f.id for f in dst_f.all()} == {"B1", "S1"}


@pytest.mark.parametrize("bad, message", [
    ("just: text\n", "not a feeder configuration"),
    ("format: tvm802-feeders\nversion: 99\n", "unsupported"),
    ("format: tvm802-feeders\nversion: 1\nfeeders:\n  - {id: X, kind: weird}\n", "feeder 1"),
    ("format: tvm802-feeders\nversion: 1\nfeeders:\n"
     "  - {id: L1, kind: strip, pick: [1, 1], bank: front, lane: 1}\nbanks: []\n", "not in the file"),
    (": : :\n", "YAML"),
])
def test_bad_files_are_rejected_and_change_nothing(tmp_path, bad, message):
    dst_f, dst_b = stores(tmp_path, "dst")
    populate(dst_f, dst_b)
    before = {f.id for f in dst_f.all()}
    with pytest.raises(ValueError, match=message):
        apply_config(dst_f, dst_b, *parse_config(bad))
    assert {f.id for f in dst_f.all()} == before
