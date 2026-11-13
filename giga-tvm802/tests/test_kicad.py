"""KiCad position file parsing: ASCII .pos and CSV, units, rotation, errors (no hardware)."""

from pathlib import Path

import pytest

from pnp.job.kicad import PosError, parse_pos

REPO = Path(__file__).resolve().parent.parent

ASCII_MM = """### Footprint positions - created on 2026-11-12 10:00:00 ###
### Printed by KiCad version 8.0.5
## Unit = mm, Angle = deg.
## Side : All
# Ref     Val          Package                     PosX       PosY       Rot  Side
C1        100n         C_0402_1005Metric        12.5000    -8.2500    90.0000  top
R1        10k 1%       R_0805_2012Metric        20.0000   -10.0000   180.0000  top
## End
"""


def test_ascii_mm_parses_fields():
    parts = parse_pos(ASCII_MM)
    assert [p.ref for p in parts] == ["C1", "R1"]
    c1 = parts[0]
    assert (c1.value, c1.package, c1.side) == ("100n", "C_0402_1005Metric", "top")
    assert c1.x == pytest.approx(12.5)
    assert c1.y == pytest.approx(-8.25)
    assert c1.rotation == pytest.approx(90.0)


def test_value_with_spaces_is_kept_whole():
    r1 = parse_pos(ASCII_MM)[1]
    assert r1.value == "10k 1%"
    assert r1.package == "R_0805_2012Metric"
    assert (r1.x, r1.y, r1.rotation) == pytest.approx((20.0, -10.0, 180.0))


def test_ascii_inches_are_converted_to_mm():
    text = ASCII_MM.replace("## Unit = mm", "## Unit = inches")
    parts = parse_pos(text)
    assert parts[0].x == pytest.approx(12.5 * 25.4)
    assert parts[0].y == pytest.approx(-8.25 * 25.4)
    assert parts[1].rotation == pytest.approx(180.0)  # rotation is not scaled


def test_unknown_unit_is_an_error():
    with pytest.raises(PosError, match="line 3"):
        parse_pos(ASCII_MM.replace("## Unit = mm", "## Unit = furlongs"))


def test_rotation_is_normalised():
    text = (
        "## Unit = mm\n"
        "A1 x PKG 1 1 -90 top\n"
        "A2 x PKG 1 1 360 top\n"
        "A3 x PKG 1 1 -360 top\n"
        "A4 x PKG 1 1 450 top\n"
        "A5 x PKG 1 1 -0.0000001 top\n"
    )
    rots = [p.rotation for p in parse_pos(text)]
    assert rots[0] == pytest.approx(270.0)
    assert rots[1] == pytest.approx(0.0)
    assert rots[2] == pytest.approx(0.0)
    assert rots[3] == pytest.approx(90.0)
    assert 0.0 <= rots[4] < 360.0


def test_bad_number_names_the_line():
    text = ASCII_MM.replace("12.5000    -8.2500", "12.5O00    -8.2500")  # letter O, on line 6
    with pytest.raises(PosError, match="line 6") as info:
        parse_pos(text)
    assert "PosX" in str(info.value)


def test_bad_side_names_the_line():
    text = ASCII_MM.replace("180.0000  top", "180.0000  middle")
    with pytest.raises(PosError, match="line 7"):
        parse_pos(text)


def test_too_few_fields_names_the_line():
    with pytest.raises(PosError, match="line 2"):
        parse_pos("## Unit = mm\nR1 10k 0603 1.0 top\n")


def test_duplicate_ref_is_an_error():
    text = ASCII_MM.replace("R1        10k", "C1        10k")
    with pytest.raises(PosError, match="duplicate ref 'C1'"):
        parse_pos(text)


def test_empty_file_has_no_parts():
    with pytest.raises(PosError, match="no parts found"):
        parse_pos("")


def test_comments_only_has_no_parts():
    with pytest.raises(PosError, match="no parts found"):
        parse_pos("### created ###\n## Unit = mm\n## End\n")


def test_bom_and_crlf_are_accepted():
    text = "﻿" + ASCII_MM.replace("\n", "\r\n")
    parts = parse_pos(text)
    assert [p.ref for p in parts] == ["C1", "R1"]
    assert parts[0].y == pytest.approx(-8.25)


CSV_SHUFFLED = (
    '"Side","Rot","PosY","PosX","Package","Val","Ref"\n'
    '"top","90","-8.25","12.5","C_0402_1005Metric","100n","C1"\n'
    '"BOTTOM","-90","4","6.5","SOT-23, low profile","BC847 ""B""","Q1"\n'
)


def test_csv_quoted_fields_and_shuffled_columns():
    c1, q1 = parse_pos(CSV_SHUFFLED)
    assert (c1.ref, c1.value, c1.package, c1.side) == ("C1", "100n", "C_0402_1005Metric", "top")
    assert (c1.x, c1.y, c1.rotation) == pytest.approx((12.5, -8.25, 90.0))
    assert q1.package == "SOT-23, low profile"
    assert q1.value == 'BC847 "B"'
    assert q1.side == "bottom"
    assert q1.rotation == pytest.approx(270.0)


def test_csv_missing_column_is_an_error():
    with pytest.raises(PosError, match="line 1"):
        parse_pos("Ref,Val,Package,PosX,PosY,Rot\nC1,100n,C0402,1,2,0\n")


def test_csv_bad_number_names_the_line():
    text = "Ref,Val,Package,PosX,PosY,Rot,Side\nC1,100n,C0402,1,2,0,top\nC2,100n,C0402,x,2,0,top\n"
    with pytest.raises(PosError, match="line 3"):
        parse_pos(text)


def test_csv_duplicate_ref_is_an_error():
    text = "Ref,Val,Package,PosX,PosY,Rot,Side\nC1,100n,C0402,1,2,0,top\nC1,100n,C0402,3,4,0,top\n"
    with pytest.raises(PosError, match="duplicate ref 'C1'"):
        parse_pos(text)


def test_csv_header_only_has_no_parts():
    with pytest.raises(PosError, match="no parts found"):
        parse_pos("Ref,Val,Package,PosX,PosY,Rot,Side\n")


def test_demo_file_parses_to_fourteen_parts_with_one_bottom():
    text = (REPO / "examples" / "demo.pos").read_text(encoding="utf-8")
    parts = parse_pos(text)
    assert len(parts) == 14  # 11 placeable parts and 3 fiducials
    assert sum(1 for p in parts if p.side == "bottom") == 1
    assert sum(1 for p in parts if p.side == "top") == 13
    for p in parts:
        assert 2.0 <= p.x <= 38.0 and 2.0 <= p.y <= 28.0
        assert 0.0 <= p.rotation < 360.0
    assert {p.rotation for p in parts} == {0.0, 90.0, 180.0, 270.0}
