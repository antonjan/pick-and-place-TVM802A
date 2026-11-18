"""Defaults tab: describe / validate / save (comments kept) / apply live, and the API."""

import pytest
from fastapi.testclient import TestClient

from pnp.api.app import create_app
from pnp.config import MachineConfig, load_config
from pnp.config_editor import apply_live, describe, save, validate, warnings_for
from pnp.machine.machine import Machine
from pnp.sim.tvm_sim import TvmSimulator

YAML_TEXT = """\
# TVM802 machine config
controller:
  host: 192.168.0.8   # the controller
  port: 701

limits:
  # measured 2026-11-13
  x: [-120.0, 299.0]
  y: [-75.0, 350.5]
  z1_max: 9.5  # N1
  z2_max: 9.5
"""


def field(sections, section, key):
    s = next(s for s in sections if s["key"] == section)
    return next(f for f in s["fields"] if f["key"] == key)


def test_describe_lists_values_and_defaults(tmp_path):
    p = tmp_path / "machine.yaml"
    p.write_text(YAML_TEXT)
    d = describe(load_config(p))
    x = field(d, "limits", "x")
    assert x["value"] == [-120.0, 299.0] and x["default"] == [0.0, 299.0] and x["type"] == "pair"
    assert field(d, "head", "pick_depth")["value"] == 3.0
    assert field(d, "head", "discard")["type"] == "optional_pair"
    assert field(d, "head", "bed_depth")["default"] == 0.0 and field(d, "head", "bed_depth")["unit"] == "mm"
    assert field(d, "head", "part_height")["default"] == 0.5 and not field(d, "head", "bed_depth")["advanced"]
    assert field(d, "head", "check_vacuum")["type"] == "bool"
    assert field(d, "steps", "x")["advanced"] and not field(d, "head", "place_depth")["advanced"]
    assert field(d, "motion", "step_quantum")["advanced"] and not field(d, "motion", "xy_speed")["advanced"]
    # every config field except the server section is editable
    keys = {(s["key"], f["key"]) for s in d for f in s["fields"]}
    import dataclasses
    cfg = MachineConfig()
    for sec in dataclasses.fields(cfg):
        if sec.name == "server":
            continue
        for f in dataclasses.fields(getattr(cfg, sec.name)):
            assert (sec.name, f.name) in keys, f"{sec.name}.{f.name} missing from the Defaults tab"


@pytest.mark.parametrize("changes, msg", [
    ({"limits": {"x": [10, 5]}}, "min must be below max"),
    ({"head": {"discard": [1]}}, "Discard"),
    ({"head": {"pick_dwell": -1}}, "negative"),
    ({"motion": {"xy_speed": 0}}, "greater than 0"),
    ({"head": {"pick_depth": 20}}, "deeper"),
    ({"head": {"nope": 1}}, "can't be edited"),
    ({"server": {"port": 1}}, "unknown section"),
    ({"vision": {"min_brightness": 300}}, "0–255"),
    ({"controller": {"host": " "}}, "empty"),
    ({"head": {"pick_depth": "abc"}}, "abc"),
    ({"head": {"bed_depth": -1}}, "can't be negative"),
    ({"head": {"part_height": -0.5}}, "can't be negative"),
    ({"head": {"bed_depth": 12}}, "deeper"),
])
def test_validate_rejects(changes, msg):
    with pytest.raises(ValueError, match=msg):
        validate(MachineConfig(), changes)


def test_save_keeps_comments_and_only_touches_changed_values(tmp_path):
    p = tmp_path / "machine.yaml"
    p.write_text(YAML_TEXT)
    old = load_config(p)
    new = validate(old, {"head": {"pick_depth": 2.5, "discard": [10, 20]}, "limits": {"z1_max": 9.0}})
    changed = save(p, old, new)
    assert sorted(changed) == ["head.discard", "head.pick_depth", "limits.z1_max"]
    text = p.read_text()
    assert "# measured 2026-11-13" in text and "# the controller" in text and "# N1" in text
    assert "x: [-120.0, 299.0]" in text and "discard: [10.0, 20.0]" in text
    again = load_config(p)
    assert again.head.pick_depth == 2.5 and again.head.discard == (10.0, 20.0) and again.limits.z1_max == 9.0
    assert again.limits.x == (-120.0, 299.0)
    # emptying the discard bin
    new2 = validate(again, {"head": {"discard": []}})
    save(p, again, new2)
    assert load_config(p).head.discard == ()


def test_save_without_changes_does_not_rewrite(tmp_path):
    p = tmp_path / "machine.yaml"
    p.write_text(YAML_TEXT)
    old = load_config(p)
    assert save(p, old, validate(old, {"head": {"pick_depth": 3.0}})) == []
    assert p.read_text() == YAML_TEXT


def test_apply_live_updates_objects_in_place():
    cfg = MachineConfig()
    head, limits = cfg.head, cfg.limits
    new = validate(cfg, {"head": {"place_depth": 1.5}, "limits": {"y": [-70, 300]},
                         "controller": {"host": "10.0.0.1", "poll_interval_idle": 0.5}})
    apply_live(cfg, new)
    assert cfg.head is head and head.place_depth == 1.5
    assert cfg.limits is limits and limits.y == (-70.0, 300.0)
    assert cfg.controller.host == "192.168.0.8"  # needs a restart
    assert cfg.controller.poll_interval_idle == 0.5


def test_warnings():
    assert warnings_for(["head.pick_depth"]) == []
    w = warnings_for(["controller.host", "steps.x"])
    assert any("restart" in x for x in w) and any("home" in x for x in w)


def make_client(config_path):
    sim = TvmSimulator(port=0)
    cfg = load_config(config_path)
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    return machine, TestClient(create_app(machine, config_path=config_path,
                                          extra_startup=startup, extra_shutdown=sim.stop))


def test_api_get_and_put_defaults(tmp_path):
    p = tmp_path / "machine.yaml"
    p.write_text(YAML_TEXT)
    machine, client = make_client(p)
    with client as c:
        d = c.get("/api/config/defaults").json()
        assert field(d["sections"], "limits", "x")["value"] == [-120.0, 299.0]
        r = c.put("/api/config/defaults", json={"changes": {"head": {"pick_depth": 2.0, "check_vacuum": True}}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["changed"]) == ["head.check_vacuum", "head.pick_depth"]
        assert field(body["sections"], "head", "pick_depth")["value"] == 2.0
        assert machine.cfg.head.pick_depth == 2.0 and machine.cfg.head.check_vacuum is True
        assert load_config(p).head.check_vacuum is True
        r = c.put("/api/config/defaults", json={"changes": {"limits": {"x": [5, 1]}}})
        assert r.status_code == 400 and "min must be below max" in r.json()["detail"]
        assert machine.cfg.limits.x == (-120.0, 299.0)
        assert c.put("/api/config/defaults", json={"oops": 1}).status_code == 400


def test_bed_depth_and_part_height_save_and_apply(tmp_path):
    cfg = MachineConfig()
    new = validate(cfg, {"head": {"bed_depth": 7.5, "part_height": 0.7}})
    assert new.head.bed_depth == 7.5 and new.head.part_height == 0.7
    p = tmp_path / "machine.yaml"
    p.write_text("head:\n  place_depth: 3.0\n")
    assert save(p, cfg, new) == ["head.bed_depth", "head.part_height"]
    loaded = load_config(p)
    assert loaded.head.bed_depth == 7.5 and loaded.head.part_height == 0.7
    apply_live(cfg, new)
    assert cfg.head.bed_depth == 7.5
