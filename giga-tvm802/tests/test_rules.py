"""Package rules: nozzle choice, place depth, validation and the YAML file (no hardware)."""

import pytest
import yaml

from pnp.job.rules import DEFAULT_RULES, PackageRule, PackageRules


def test_missing_file_gives_defaults_and_is_not_saved(tmp_path):
    path = tmp_path / "packages.yaml"
    rules = PackageRules(path)
    assert rules.default_nozzle == 1
    assert rules.rules == DEFAULT_RULES
    assert not path.exists()


def test_defaults_are_copied_not_shared(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.rules[0].nozzle = 1
    assert DEFAULT_RULES[0].nozzle == 2


def test_default_nozzles_for_known_packages(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    assert rules.nozzle_for("R_0805_2012Metric") == 2
    assert rules.nozzle_for("C_0402_1005Metric") == 2
    assert rules.nozzle_for("SOT-23") == 2
    assert rules.nozzle_for("SOT-223") == 1
    assert rules.nozzle_for("SOIC-8_3.9x4.9mm_P1.27mm") == 1
    assert rules.nozzle_for("QFN-32") == 1


def test_unmatched_package_uses_default_nozzle(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    assert rules.match("WEIRD-PKG") is None
    assert rules.nozzle_for("WEIRD-PKG") == 1
    assert rules.place_depth_for("WEIRD-PKG") is None


def test_first_match_wins(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.set(2, [PackageRule("*0805*", 1, 0.4), PackageRule("*", 2, 0.9)])
    assert rules.nozzle_for("R_0805_2012Metric") == 1
    assert rules.place_depth_for("R_0805_2012Metric") == 0.4
    assert rules.nozzle_for("SOIC-8") == 2
    assert rules.match("SOIC-8").pattern == "*"


def test_matching_is_case_insensitive(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.set(1, [PackageRule("SOIC*", 1), PackageRule("*0805*", 2)])
    assert rules.nozzle_for("soic-8") == 1
    assert rules.nozzle_for("SoIc-8") == 1
    assert rules.nozzle_for("r_0805_2012metric") == 2


def test_pattern_case_is_ignored_on_match(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.set(1, [PackageRule("qfn*", 1, 0.25)])
    assert rules.match("QFN-32") is not None


def test_place_depth_for(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.set(1, [PackageRule("*0402*", 2, 0.5), PackageRule("*0603*", 2, None)])
    assert rules.place_depth_for("C_0402_1005Metric") == 0.5
    assert rules.place_depth_for("R_0603_1608Metric") is None  # rule says "use machine default"
    assert rules.place_depth_for("SOIC-8") is None  # no rule at all


def test_set_rejects_bad_values_and_keeps_old_ones(tmp_path):
    path = tmp_path / "packages.yaml"
    rules = PackageRules(path)
    rules.set(2, [PackageRule("*0805*", 1, 0.3, "keep")])
    before = (rules.default_nozzle, list(rules.rules))

    bad_calls = [
        lambda: rules.set(3, []),                                   # default nozzle out of range
        lambda: rules.set(1, [PackageRule("*0603*", 4)]),           # rule nozzle out of range
        lambda: rules.set(1, [PackageRule("", 1)]),                 # empty pattern
        lambda: rules.set(1, [PackageRule("*", 1, 0)]),             # place depth must be > 0
        lambda: rules.set(1, [PackageRule("*", 1, -0.1)]),
        lambda: rules.set(1, [PackageRule("*", 1), PackageRule("  ", 2)]),
    ]
    for call in bad_calls:
        with pytest.raises(ValueError):
            call()
        assert (rules.default_nozzle, rules.rules) == before

    reloaded = PackageRules(path)
    assert (reloaded.default_nozzle, reloaded.rules) == before


def test_save_and_reload_round_trip(tmp_path):
    path = tmp_path / "packages.yaml"
    rules = PackageRules(path)
    new_rules = [PackageRule("*0805*", 1, 0.35, "thick tip"), PackageRule("SOT-*", 2, None, "")]
    rules.set(2, new_rules)
    assert path.exists()

    reloaded = PackageRules(path)
    assert reloaded.default_nozzle == 2
    assert reloaded.rules == new_rules
    assert reloaded.nozzle_for("R_0805_2012Metric") == 1
    assert reloaded.place_depth_for("R_0805_2012Metric") == 0.35


def test_save_writes_the_documented_yaml_shape(tmp_path):
    path = tmp_path / "packages.yaml"
    PackageRules(path).set(1, [PackageRule("*0402*", 2, 0.2, "small")])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["default_nozzle"] == 1
    assert data["rules"][0]["pattern"] == "*0402*"
    assert data["rules"][0]["nozzle"] == 2
    assert data["rules"][0]["place_depth"] == 0.2


def test_file_with_unknown_rule_key_is_rejected(tmp_path):
    path = tmp_path / "packages.yaml"
    path.write_text(
        "default_nozzle: 1\nrules:\n  - pattern: '*0805*'\n    nozzle: 2\n    speed: fast\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown"):
        PackageRules(path)


def test_file_with_bad_nozzle_is_rejected(tmp_path):
    path = tmp_path / "packages.yaml"
    path.write_text("default_nozzle: 3\nrules: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        PackageRules(path)


def test_file_without_rules_has_no_rules(tmp_path):
    path = tmp_path / "packages.yaml"
    path.write_text("default_nozzle: 2\n", encoding="utf-8")
    rules = PackageRules(path)
    assert rules.default_nozzle == 2
    assert rules.rules == []
    assert rules.nozzle_for("SOIC-8") == 2


# --- body and vision ------------------------------------------------------------------

def test_default_bodies_and_vision(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    assert rules.body_for("R_0805_2012Metric") == (2.0, 1.25)
    assert rules.body_for("SOT-23") == (2.9, 2.4)
    assert rules.body_for("QFN-32") is None
    assert rules.vision_for("R_0805_2012Metric") is True
    assert rules.vision_for("QFN-32") is True
    assert rules.vision_for("WEIRD-PKG") is True
    assert rules.body_for("WEIRD-PKG") is None


def test_soic8_rule_matches_before_generic_soic(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    assert rules.match("SOIC-8_3.9x4.9mm_P1.27mm").pattern == "SOIC-8*"
    assert rules.nozzle_for("SOIC-8_3.9x4.9mm_P1.27mm") == 1
    assert rules.body_for("SOIC-8_3.9x4.9mm_P1.27mm") == (6.0, 4.9)
    other = rules.match("SOIC-16_3.9x9.9mm")
    assert other.pattern == "SOIC*"
    assert rules.body_for("SOIC-16_3.9x9.9mm") is None
    assert rules.nozzle_for("SOIC-16_3.9x9.9mm") == 1


def test_body_is_normalised_length_first(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.set(1, [PackageRule("*0805*", 2, body=[1.25, 2.0]), PackageRule("*1206*", 1, body=(1, 2))])
    assert rules.body_for("R_0805") == (2.0, 1.25)
    assert rules.body_for("R_1206") == (2.0, 1.0)
    assert all(isinstance(v, float) for v in rules.body_for("R_1206"))
    assert rules.rules[0].body == (2.0, 1.25)


def test_invalid_body_and_vision_are_rejected_and_keep_old_values(tmp_path):
    path = tmp_path / "packages.yaml"
    rules = PackageRules(path)
    rules.set(2, [PackageRule("*0805*", 1, 0.3, "keep", body=(2.0, 1.25), vision=False)])
    before = (rules.default_nozzle, list(rules.rules))

    bad_calls = [
        lambda: rules.set(1, [PackageRule("*", 1, body=(2.0,))]),          # one number
        lambda: rules.set(1, [PackageRule("*", 1, body=(2.0, 1.0, 3.0))]), # three numbers
        lambda: rules.set(1, [PackageRule("*", 1, body=(0.0, 1.0))]),     # not positive
        lambda: rules.set(1, [PackageRule("*", 1, body=(-2.0, 1.0))]),
        lambda: rules.set(1, [PackageRule("*", 1, body=(float("nan"), 1.0))]),
        lambda: rules.set(1, [PackageRule("*", 1, body=(float("inf"), 1.0))]),
        lambda: rules.set(1, [PackageRule("*", 1, body=("2", 1.0))]),
        lambda: rules.set(1, [PackageRule("*", 1, body=(True, 1.0))]),
        lambda: rules.set(1, [PackageRule("*", 1, body="2x1")]),
        lambda: rules.set(1, [PackageRule("*", 1, vision=1)]),            # not a bool
        lambda: rules.set(1, [PackageRule("*", 1, vision="yes")]),
    ]
    for call in bad_calls:
        with pytest.raises(ValueError):
            call()
        assert (rules.default_nozzle, rules.rules) == before

    reloaded = PackageRules(path)
    assert (reloaded.default_nozzle, reloaded.rules) == before


def test_vision_false_is_looked_up(tmp_path):
    rules = PackageRules(tmp_path / "packages.yaml")
    rules.set(1, [PackageRule("*0603*", 2, vision=False), PackageRule("*", 1)])
    assert rules.vision_for("R_0603") is False
    assert rules.vision_for("QFN-32") is True


def test_body_and_vision_yaml_round_trip(tmp_path):
    path = tmp_path / "packages.yaml"
    rules = PackageRules(path)
    new_rules = [
        PackageRule("SOIC-8*", 1, body=(6.0, 4.9)),
        PackageRule("*0603*", 2, None, "", body=None, vision=False),
    ]
    rules.set(1, new_rules)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["rules"][0]["body"] == [6.0, 4.9]
    assert data["rules"][0]["vision"] is True
    assert data["rules"][1]["body"] is None
    assert data["rules"][1]["vision"] is False

    reloaded = PackageRules(path)
    assert reloaded.rules == new_rules
    assert reloaded.body_for("SOIC-8_3.9x4.9mm") == (6.0, 4.9)
    assert reloaded.vision_for("R_0603") is False


def test_old_format_file_without_body_or_vision_loads(tmp_path):
    path = tmp_path / "packages.yaml"
    path.write_text(
        "default_nozzle: 1\nrules:\n  - pattern: '*0805*'\n    nozzle: 2\n    place_depth: 0.3\n    note: ''\n",
        encoding="utf-8",
    )
    rules = PackageRules(path)
    assert rules.nozzle_for("R_0805") == 2
    assert rules.body_for("R_0805") is None
    assert rules.vision_for("R_0805") is True


def test_file_with_body_list_loads_normalised(tmp_path):
    path = tmp_path / "packages.yaml"
    path.write_text(
        "default_nozzle: 1\nrules:\n  - pattern: '*0805*'\n    nozzle: 2\n    body: [1.25, 2.0]\n    vision: false\n",
        encoding="utf-8",
    )
    rules = PackageRules(path)
    assert rules.body_for("R_0805") == (2.0, 1.25)
    assert rules.vision_for("R_0805") is False
