"""
General tolerances for open dimensions: detection, ISO 2768 / IS 2102 and TSDS0407 lookups,
eligibility, precedence, teach-once rules, exports — unit tests and drawing-level integration.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import openpyxl
import pytest

from app.services.general_tolerance import engine as E
from app.services.general_tolerance import standards as S
from app.services.general_tolerance import store
from app.services.general_tolerance.classify import OpenDim, classify_feature, classify_text
from app.services.general_tolerance.detect import (
    DrawingGT,
    StatedStandard,
    detect_from_text,
    detect_process,
    parse_printed_table,
)
from tests.eval_against_key import FIXTURES

ISO, TSDS = "ISO2768", "TSDS0407"


def tol(lk):
    return (lk.upper, lk.lower) if lk.ok else None


def approx(a, b):
    return a is not None and b is not None and abs(a - b) < 1e-9


# =============================================================================
# detection
# =============================================================================

@pytest.mark.parametrize("text,cls,geo", [
    ("ISO 2768-mK", "m", "K"), ("ISO 2768-m", "m", None), ("ISO 2768 m-K", "m", "K"), ("ISO2768mK", "m", "K"),
    ("GENERAL TOLERANCES ISO 2768-mK", "m", "K"), ("ISO 2768-fH", "f", "H"), ("ISO 2768-cL", "c", "L"),
    ("ISO 2768-MK", "m", "K"), ("ISO 2768-1 m", "m", None),
])
def test_iso_variants(text, cls, geo):
    d = detect_from_text([f"GENERAL TOL {text}"], [])
    assert d.chosen.id == ISO and d.chosen.cls == cls and d.chosen.geo == geo


def test_iso_in_notes_unless_otherwise_specified():
    d = detect_from_text([], ["DIMENSIONS IN mm. UNLESS OTHERWISE SPECIFIED, GENERAL", "TOLERANCES TO ISO 2768-m."])
    assert d.chosen.id == ISO and d.chosen.cls == "m" and d.chosen.location == "notes"


@pytest.mark.parametrize("text,cls", [("IS 2102-m", "m"), ("IS:2102-PART-I MEDIUM", "m"), ("IS 2102", None)])
def test_is2102_maps_to_iso(text, cls):
    d = detect_from_text([], [f"GENERAL TOLERANCES AS PER {text}"])
    assert d.chosen.id == ISO and d.chosen.via == "IS 2102" and d.chosen.cls == cls
    if cls is None:
        assert any("class not stated" in f for f in d.flags)


@pytest.mark.parametrize("text", ["TSDS0407", "TSDS 0407", "UNSPECIFIED TOLERANCES AS PER TSDS0407",
                                  "TVS SYSTEM DESIGN STANDARD - UNSPECIFIED TOLERANCE CHART"])
def test_tsds_variants(text):
    d = detect_from_text([], [text])
    assert d.chosen.id == TSDS


def test_printed_table_parsed_and_first():
    lines = ["GENERAL TOLERANCES", "0-6 ±0.1", "OVER 6 UPTO 30 ±0.2", "30-120 ±0.3", "ANGLES ±0°30'"]
    rows = parse_printed_table(lines)
    sizes = [r for r in rows if r["kind"] == "size"]
    assert [(r["min"], r["max"], r["pm"]) for r in sizes] == [(0, 6, 0.1), (6, 30, 0.2), (30, 120, 0.3)]
    assert any(r["kind"] == "angle" and approx(r["pm"], 0.5) for r in rows)
    # printed table outranks a stated standard
    d = detect_from_text(["ISO 2768-m"], lines)
    ctx = E.GTContext(d)
    res = E.resolve(OpenDim("linear", 20), ctx)
    assert res.primary.source == "General tol – drawing table" and approx(res.primary.upper, 0.2)


def test_no_standard_and_single_row_is_not_a_table():
    assert detect_from_text([], ["REMOVE ALL BURRS."]).chosen is None
    assert parse_printed_table(["TOLERANCE", "0-6 ±0.1"]) == []


def test_conflict_title_block_wins():
    d = detect_from_text(["GENERAL TOLERANCES TSDS0407"], ["GENERAL TOLERANCES TO ISO 2768-m"])
    assert d.chosen.id == TSDS and "title block" in d.conflict


def test_conflict_ambiguous_uses_org_preference():
    d = detect_from_text([], ["UNSPECIFIED TOLERANCES AS PER TSDS0407", "GENERAL TOLERANCES ISO 2768-m"])
    assert d.chosen is None and d.conflict
    ctx = E.GTContext(d, org_settings={"preferred_standard": ISO})
    std, flags = ctx.stated()
    assert std.id == ISO and any("standard conflict" in f for f in flags)
    res = E.resolve(OpenDim("radius", 6), ctx)
    assert approx(res.primary.upper, 0.5) and any("standard conflict" in f for f in res.flags)
    none = E.resolve(OpenDim("radius", 6), E.GTContext(d))
    assert not none.primary.ok and any("no preferred standard" in f for f in none.flags)


@pytest.mark.parametrize("lines,material,proc", [
    (["ALL OVER MACHINED"], "", "machining"), (["HPDC PART"], "", "casting_hpdc"), (["PRESSURE DIE CASTING"], "", "casting_hpdc"),
    (["DIE CAST"], "", "casting_hpdc"), (["SAND CASTING"], "", "casting_sand"), (["SHELL MOULD CASTING"], "", "casting_shell"),
    (["PRESSED PART"], "", "pressing"), (["SHEET METAL t=1.6"], "", "pressing"), (["FORGED"], "", "forging"),
    (["WELDED ASSEMBLY"], "", "welding"), (["INJECTION MOULDED"], "", "plastic"), (["RUBBER BUSH"], "", "rubber_moulding"),
    (["EXTRUDED RUBBER PROFILE"], "", "rubber_extrusion"), (["INVESTMENT CASTING"], "", "lost_wax"), (["LOST WAX"], "", "lost_wax"),
    ([], "ADC12", "casting_hpdc"), ([], "SPCC 1.2 THK", "pressing"), ([], "PA66 GF30", "plastic"), ([], "NBR 70", "rubber_moulding"),
])
def test_process_detection(lines, material, proc):
    assert detect_process(lines, material)[0] == proc


def test_base_process_wins_over_machined_and_flags():
    d = detect_from_text([], ["MATERIAL ADC12, HPDC.", "MACHINED SURFACES Ra 1.6"])
    assert d.process == "casting_hpdc" and any("Several processes" in f for f in d.flags)


def test_as_cast_without_subprocess_hint():
    p, _src, _c, hint = detect_process(["AS CAST"], "")
    assert p is None and "sub-process" in hint


def test_sheet_thickness_and_geometric_class():
    d = detect_from_text([], ["SHEET METAL t=2.5", "GEOMETRIC TOLERANCES TSDS0407 CLASS 2"])
    assert d.sheet_thickness == 2.5 and d.geometric_class == 2


# =============================================================================
# ISO 2768 / IS 2102 tables
# =============================================================================

@pytest.mark.parametrize("size,expect", [
    (0.5, 0.1), (3, 0.1), (3.0001, 0.1), (6, 0.1), (6.5, 0.2), (30, 0.2), (30.5, 0.3), (120, 0.3), (121, 0.5),
    (400, 0.5), (1000, 0.8), (2000, 1.2), (4000, 2),
])
def test_iso_linear_m_band_boundaries(size, expect):
    assert approx(S.lookup(ISO, "linear", "m", size).upper, expect)


def test_iso_linear_all_classes_and_dashes():
    assert [S.lookup(ISO, "linear", c, 10).upper for c in "fmcv"] == [0.1, 0.2, 0.5, 1]
    assert not S.lookup(ISO, "linear", "f", 3000).ok          # '-' cell
    assert not S.lookup(ISO, "linear", "v", 2).ok             # '-' cell
    lk = S.lookup(ISO, "linear", "m", 0.4)
    assert not lk.ok and "outside range" in lk.reason
    assert not S.lookup(ISO, "linear", "m", 4001).ok


@pytest.mark.parametrize("cls,size,expect", [("m", 3, 0.2), ("m", 6, 0.5), ("m", 6.1, 1), ("f", 2, 0.2), ("c", 3, 0.4), ("v", 10, 2)])
def test_iso_radius_chamfer(cls, size, expect):
    assert approx(S.lookup(ISO, "radius_chamfer", cls, size).upper, expect)


@pytest.mark.parametrize("cls,leg,deg", [("m", 10, 1), ("m", 10.1, 0.5), ("m", 50, 0.5), ("m", 51, 1 / 3), ("m", 400, 1 / 6),
                                         ("m", 401, 1 / 12), ("c", 5, 1.5), ("v", 500, 1 / 3), ("f", 100, 1 / 3)])
def test_iso_angular(cls, leg, deg):
    lk = S.lookup(ISO, "angular", cls, leg)
    assert lk.unit == "deg" and approx(round(lk.upper, 9), round(deg, 9))


def test_iso_radius_below_half_mm_flagged():
    res = E.resolve(OpenDim("radius", 0.4), E.GTContext(detect_from_text(["ISO 2768-m"], [])))
    assert not res.primary.ok and "outside range" in res.primary.source


def test_geometric_class_letter_never_applied_to_sizes():
    d = detect_from_text(["ISO 2768-mH"], [])
    assert approx(E.resolve(OpenDim("linear", 10), E.GTContext(d)).primary.upper, 0.2)


# =============================================================================
# TSDS0407 tables (data file transcribed from the chart)
# =============================================================================

def test_every_tsds_cell_parses():
    d = S.load(TSDS)
    n = 0
    for tname, t in d["tables"].items():
        for cname, c in t["columns"].items():
            for cell in c.get("values", []):
                S.parse_cell(cell)  # raises on anything unreadable
                n += 1
            if "values" in c:
                assert len(c["values"]) == len(t["bands"]), (tname, cname)
    assert n > 150
    assert len([t for t in d["tables"] if t != "lost_wax_angular_roughness"]) == 12


@pytest.mark.parametrize("table,col,size,expect", [
    ("machining", "outside_dia", 20, (0.0, -0.2)), ("machining", "inside_dia_boring", 20, (0.2, 0.0)),
    ("machining", "inside_dia_drilling", 8, (0.4, 0.0)), ("machining", "length", 50, (0.2, -0.2)),
    ("machining", "length", 4, (0.1, -0.1)), ("machining", "length", 1, (0.1, -0.1)), ("machining", "corner_chamfer", 1, (0.25, -0.25)),
    ("machining", "corner_chamfer", 2, (0.5, -0.5)), ("machining", "corner_round", 2, (0.25, -0.25)),
    ("casting", "hpdc_length", 30, (0.3, -0.3)), ("casting", "hpdc_id", 30, (0.4, 0.0)), ("casting", "hpdc_od", 30, (0.0, -0.4)),
    ("casting", "shell_casting", 30, (0.4, -0.4)), ("casting", "sand_casting", 30, (0.7, -0.7)), ("casting", "hpdc_length", 1, (0.1, -0.1)),
    ("pressing", "length", 10, (0.5, -0.5)), ("pressing", "inside_dia_t_le_2_3", 10, (0.4, 0.0)),
    ("pressing", "inside_dia_t_gt_2_3", 10, (0.8, 0.0)), ("forging", "thickness", 10, (0.6, -0.3)),
    ("forging", "thickness", 100, (1.5, -0.5)), ("forging", "length", 10, (0.4, -0.4)),
    ("positional_casting", "die", 10, (0.3, -0.3)), ("positional_casting", "shell", 10, (0.5, -0.5)),
    ("positional_casting", "sand", 10, (1.0, -1.0)), ("welding", "length", 300, (3.2, -3.2)), ("welding", "length", 4, (0.4, -0.4)),
    ("positional_pressing", "tol", 10, (0.1, -0.1)), ("positional_pressing", "tol", 300, (0.3, -0.3)),
    ("plastic", "length", 10, (0.2, -0.2)), ("plastic", "thickness", 10, (0.3, -0.3)), ("plastic", "id", 10, (0.3, 0.0)),
    ("plastic", "od", 10, (0.0, -0.3)), ("rubber", "moulding", 3, (0.3, -0.3)), ("rubber", "extrusion_and_others", 450, (7.0, -7.0)),
    ("lost_wax", "dia_lg", 0.3, (0.1, -0.1)), ("lost_wax", "rounding", 200, (2.0, -2.0)),
    ("angular_machining", "degrees", 4, (1.5, -1.5)), ("angular_machining", "degrees", 5, (1.0, -1.0)),
    ("angular_machining", "degrees", 20, (0.75, -0.75)), ("angular_machining", "degrees", 500, (0.25, -0.25)),
])
def test_tsds_cells(table, col, size, expect):
    got = tol(S.lookup(TSDS, table, col, size, nominal=size))
    assert got is not None and approx(got[0], expect[0]) and approx(got[1], expect[1]), got


def test_tsds_unilateral_values_stay_unilateral():
    lk = S.lookup(TSDS, "machining", "outside_dia", 20)
    assert lk.cell == "-0.2" and lk.upper == 0.0 and lk.lower == -0.2


@pytest.mark.parametrize("table,col,size,why", [
    ("machining", "inside_dia_drilling", 100, "not specified"), ("machining", "corner_round", 20, "not specified"),
    ("machining", "corner_chamfer", 17, "not specified"), ("plastic", "thickness", 20, "not specified"),
    ("casting", "hpdc_length", 0.5, "outside range"), ("forging", "length", 300, "outside range"),
    ("machining", "length", 1500, "outside range"), ("lost_wax", "dia_lg", 400, "outside range"),
    ("angular_machining", "degrees", 2000, "outside range"),
])
def test_tsds_null_and_out_of_range_never_extrapolated(table, col, size, why):
    lk = S.lookup(TSDS, table, col, size)
    assert not lk.ok and why in lk.reason


def test_rubber_percentage_row():
    assert approx(S.lookup(TSDS, "rubber", "moulding", 600, nominal=600).upper, 4.8)
    assert approx(S.lookup(TSDS, "rubber", "extrusion_and_others", 600, nominal=600).lower, -9.0)


def test_constant_columns():
    assert S.lookup(TSDS, "forging", "deviation_of_centering", 120).upper == 0.5
    assert S.lookup(TSDS, "geometric_machining", "runout_circular_class1", 900).upper == 0.1
    assert S.lookup(TSDS, "geometric_machining", "straightness_flatness_parallelism_class2", 10).upper == 0.08
    assert S.lookup(TSDS, "geometric_machining", "perpendicularity_class1", 2).upper == 0.10
    lk = S.lookup(TSDS, "lost_wax_angular_roughness", "angles", None)
    assert lk.unit == "deg" and lk.upper == 1.0


def test_range_convention_option():
    assert approx(S.lookup(TSDS, "machining", "length", 4).upper, 0.1)                                  # 4 in '1-4'
    assert approx(S.lookup(TSDS, "machining", "length", 4, convention="lower_inclusive").upper, 0.15)   # 4 in '4-16'
    assert approx(S.lookup(TSDS, "machining", "length", 16, convention="lower_inclusive").upper, 0.2)   # 16 in '16-63'
    assert approx(S.lookup(TSDS, "machining", "length", 16).upper, 0.15)                                # 16 in '4-16'
    assert approx(S.lookup(TSDS, "pressing", "length", 4, convention="lower_inclusive").upper, 0.3)     # '≤4' includes 4


# =============================================================================
# eligibility
# =============================================================================

@pytest.mark.parametrize("ftype,text,nom,up,lo,fit", [
    ("Basic dimension", "Ø95", 95, None, None, None), ("Basic angle", "45°", 45, None, None, None),
    ("Reference dimension", "(10)", 10, None, None, None), ("Fit (hole)", "Ø35 H7", 35, 0.025, 0, "H7"),
    ("Thread", "M10", 10, None, None, None), ("GD&T - Position", "0.1", 0.1, None, None, None),
    ("Surface finish", "Ra 1.6", 1.6, 1.6, None, None), ("Datum feature", "A", None, None, None, None),
    ("Linear dimension", "52 ±0.1", 52, 0.1, -0.1, None),
])
def test_never_eligible(ftype, text, nom, up, lo, fit):
    assert classify_feature(ftype, text, nom, up, lo, fit=fit) is None


def test_eligible_categories():
    assert classify_text("2× R6").category == "radius" and classify_text("2× R6").size == 6
    c = classify_text("2 × 45°")
    assert c.category == "chamfer" and c.size == 2 and c.angle == 45 and c.leg == 2
    assert classify_text("Ø8 DRILL").hole_type == "drilled"
    assert classify_text("Ø20 BORE").category == "inside_dia"
    assert classify_text("Ø20").category == "dia" and classify_text("Ø20").flags
    assert classify_feature("Linear dimension", "12", 12, context_text="PLATE THK").category == "thickness"
    assert classify_text("[25]") is None and classify_text("Ø20", "basic") is None
    a = classify_text("30°")
    assert a.category == "angular" and a.leg is None and a.leg_estimated


# =============================================================================
# precedence and rules
# =============================================================================

def _ctx(std_lines=(), notes=(), **kw) -> E.GTContext:
    return E.GTContext(detect_from_text(list(std_lines), list(notes)), **kw)


def _rule(scope, key, cat, up, lo, smin=None, smax=None, **kw):
    return {"id": f"r{scope}{cat}{up}", "scope": scope, "scope_key": key, "category": cat, "upper": up, "lower": lo,
            "size_min": smin, "size_max": smax, "unit": kw.pop("unit", "mm"), "active": kw.pop("active", True),
            "name": kw.pop("name", f"{scope} {cat}"), **kw}


def test_precedence_order():
    od = OpenDim("radius", 6)
    rules = [_rule("drawing", "D1", "radius", 0.1, -0.1, 3, 6), _rule("family", "F1", "radius", 0.2, -0.2, 3, 6),
             _rule("customer", "O1", "radius", 0.3, -0.3, 3, 6)]
    iso = _ctx(["ISO 2768-m"], rules=rules, drawing_key="D1", family_key="F1", org_id="O1")
    assert E.resolve(od, iso, manual={"upper": 0.05, "lower": -0.05}).primary.source == "Manual"   # 2
    r = E.resolve(od, iso).primary
    assert r.level == 3 and approx(r.upper, 0.1) and "(drawing)" in r.source                      # 3
    iso.rules = rules[1:]
    r = E.resolve(od, iso)
    assert r.primary.level == 5 and approx(r.primary.upper, 0.5)                                  # 5 beats 6/7
    assert sum("standard conflict" in f for f in r.flags) == 2                                    # warned, not applied
    none = _ctx(rules=rules[1:], drawing_key="D1", family_key="F1", org_id="O1")
    assert E.resolve(od, none).primary.level == 6                                                 # 6
    none.family_key = ""
    assert E.resolve(od, none).primary.level == 7 and "(customer)" in E.resolve(od, none).primary.source
    none.rules = []
    none.org_settings = {"default_standard": TSDS, "default_process": "machining"}
    r = E.resolve(od, none).primary
    assert r.source.startswith("Org default – TSDS0407 machining") and approx(r.upper, 0.5)       # 7 default
    none.org_settings = {}
    r = E.resolve(od, none).primary
    assert not r.ok and r.source == "None – No general tolerance stated"                          # 8


def test_rule_matching_details():
    ctx = _ctx(org_id="O", rules=[
        _rule("customer", "O", "radius", 0.3, -0.3, 3, 6),
        _rule("customer", "O", "inside_dia", 0.05, 0, None, None, hole_type="drilled"),
        _rule("customer", "O", "linear", 1, -1, unit="%"),
        _rule("customer", "O", "outside_dia", 9, -9, active=False),
    ])
    assert not E.resolve(OpenDim("radius", 3), ctx).primary.ok          # size_min exclusive
    assert approx(E.resolve(OpenDim("radius", 6), ctx).primary.upper, 0.3)   # size_max inclusive
    assert not E.resolve(OpenDim("inside_dia", 8, hole_type="bored"), ctx).primary.ok
    assert approx(E.resolve(OpenDim("inside_dia", 8, hole_type="drilled"), ctx).primary.upper, 0.05)
    assert approx(E.resolve(OpenDim("linear", 200), ctx).primary.upper, 2.0)   # 1 % of 200
    assert not E.resolve(OpenDim("outside_dia", 8), ctx).primary.ok      # inactive rule ignored


def test_asymmetric_rule_and_process_specific_rule():
    ctx = _ctx(["TSDS0407"], drawing_key="D", drawing_settings={"process": "forging"},
               rules=[_rule("drawing", "D", "linear", 0.4, -0.1, process="forging"),
                      _rule("drawing", "D", "radius", 1, -1, process="machining")])
    r = E.resolve(OpenDim("linear", 10), ctx).primary
    assert r.upper == 0.4 and r.lower == -0.1
    r = E.resolve(OpenDim("radius", 2), ctx).primary          # machining-only rule does not match a forging
    assert not r.ok and "not specified for radius / corner round under forging" in r.source


def test_family_pattern():
    assert store.family_key("K123-456-A", r"^(K\d{3})") == "K123"
    assert store.family_key("K123-456-A", r"^K\d{3}") == "K123"
    assert store.family_key("K123", "") == "" and store.family_key("X1", r"^(K\d)") == ""


def test_tsds_process_handling():
    od = OpenDim("inside_dia", 20)
    ctx = _ctx(["TSDS0407"])
    r = E.resolve(od, ctx)
    assert "process assumed: Machining" in r.flags and "hole type assumed: boring" in r.flags and approx(r.primary.upper, 0.2)
    ctx.drawing_settings = {"process": "casting_hpdc", "feature_process": {"7": "machining"}}
    assert approx(E.resolve(od, ctx).primary.upper, 0.4)              # HPDC I.D, 16-63
    assert approx(E.resolve(od, ctx, balloon=7).primary.upper, 0.2)   # per-feature machining override
    ctx.drawing_settings = {"process": "pressing"}
    assert "sheet thickness unknown" in E.resolve(od, ctx).primary.source
    ctx.drawing_settings = {"process": "pressing", "sheet_thickness": 2.0}
    assert approx(E.resolve(od, ctx).primary.upper, 0.6)
    ctx.drawing_settings = {"process": "pressing", "sheet_thickness": 3.0}
    assert approx(E.resolve(od, ctx).primary.upper, 1.2)
    ctx.drawing_settings = {"process": "forging"}
    r = E.resolve(OpenDim("thickness", 10), ctx).primary
    assert (r.upper, r.lower) == (0.6, -0.3)
    assert "not specified" in E.resolve(OpenDim("radius", 2), ctx).primary.source


def test_hpdc_inside_dia_value():
    ctx = _ctx(["TSDS0407"], drawing_settings={"process": "casting_hpdc"})
    assert approx(E.resolve(OpenDim("inside_dia", 20), ctx).primary.upper, 0.4)


def test_dia_side_unknown_and_angles():
    tsds = _ctx(["TSDS0407"])
    r = E.resolve(OpenDim("dia", 20, flags=["Ø inside/outside not determined"]), tsds)
    assert not r.primary.ok and "inside/outside" in r.primary.source
    iso = _ctx(["ISO 2768-m"])
    assert approx(E.resolve(OpenDim("dia", 20), iso).primary.upper, 0.2)           # ISO: same column either way
    a = E.resolve(OpenDim("angular", 30, angle=30, leg=None, leg_estimated=True), iso)
    assert approx(a.primary.upper, 1.0) and "leg length estimated" in a.flags
    a = E.resolve(OpenDim("angular", 30, angle=30, leg=60), iso)
    assert approx(a.primary.upper, 1 / 3) and not a.flags
    t = E.resolve(OpenDim("angular", 30, angle=30, leg=None), tsds)
    assert approx(t.primary.upper, 1.5) and "leg length estimated" in t.flags
    ch = E.resolve(OpenDim("chamfer", 1, angle=45, leg=1), tsds)
    assert approx(ch.primary.upper, 0.25) and approx(ch.angle.upper, 1.5)


# =============================================================================
# store
# =============================================================================

def test_rule_store_crud_and_audit():
    r = store.create_rule("O", {"scope": "customer", "scope_key": "O", "category": "radius", "size_min": 3, "size_max": 6,
                                "upper": 0.3, "lower": -0.3}, "qa")
    assert r["name"].startswith("radius / corner round") and r["active"]
    store.update_rule(r["id"], {"active": False}, "qa")
    assert store.list_rules("O", active_only=True) == []
    store.update_rule(r["id"], {"upper": 0.4, "lower": -0.2, "active": True}, "qa")
    store.delete_rule(r["id"], "qa")
    assert [h["action"] for h in store.rule_history(r["id"])] == ["create", "disable", "enable", "delete"]
    with pytest.raises(ValueError):
        store.create_rule("O", {"scope": "galaxy", "scope_key": "x", "category": "radius", "upper": 1, "lower": 2}, "qa")


def test_org_and_drawing_settings_roundtrip():
    s = store.put_org_settings("O", {"default_standard": TSDS, "default_process": "machining", "family_pattern": r"^(K\d+)"}, "qa")
    assert s["default_standard"] == TSDS and store.get_org_settings("O")["family_pattern"] == r"^(K\d+)"
    store.put_drawing_settings("O", "PN-X", {"process": "forging"}, "qa")
    assert store.get_drawing_settings("O", "PN-X")["process"] == "forging"
    assert store.get_drawing_settings("O", "PN-Y")["process"] is None


# =============================================================================
# integration: SMX-DEMO-HF-001 (title block ISO 2768-mK)
# =============================================================================

def _rows(xlsx: bytes, sheet="Extraction") -> list[dict]:
    ws = openpyxl.load_workbook(io.BytesIO(xlsx), data_only=True)[sheet]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    return [dict(zip(hdr, r)) for r in it]


@pytest.fixture(scope="module")
def hf001():
    from app.services.tvs_extraction_report import build_tvs_extraction_xlsx

    pdf = FIXTURES / "SMX-DEMO-HF-001.pdf"
    return (build_tvs_extraction_xlsx({}, pdf), build_tvs_extraction_xlsx({}, pdf, general_tolerances=False))


def test_hf001_open_dimensions(hf001):
    on, _off = hf001
    by = {r["Text as shown on drawing"]: r for r in _rows(on)}
    for text, t, nom in (("2× R6", 0.5, 6), ("R3", 0.2, 3), ("R1", 0.2, 1), ("R12", 1, 12), ("2 × 45°", 0.2, 2), ("1 × 45°", 0.2, 1)):
        r = by[text]
        assert approx(r["Upper tolerance"], t) and approx(r["Lower tolerance"], -t), (text, r)
        assert approx(r["Max limit"], nom + t) and approx(r["Min limit"], nom - t)
        assert "Tolerance source: General tol – ISO 2768-m" in r["Notes"]
    assert "chamfer angle 45° ±1°" in by["2 × 45°"]["Notes"] and "chamfer angle 45° ±1°" in by["1 × 45°"]["Notes"]


def test_hf001_everything_else_unchanged(hf001):
    on, off = hf001
    a, b = _rows(on), _rows(off)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        if "Tolerance source:" in str(x.get("Notes") or ""):
            continue
        assert x == y, (x, y)
    by = {r["Text as shown on drawing"]: r for r in a}
    for text in ("Ø95", "Ø75", "45°", "(10)", "(1.5)", "(134.4)", "(52)"):
        assert by[text]["Upper tolerance"] is None and "Tolerance source" not in str(by[text]["Notes"] or "")
    for text in ("Ø35 H7 +0.025/0", "Ø42 g6 -0.009/-0.025", "52 ±0.1", "16 0/-0.1"):
        assert by[text] == {r["Text as shown on drawing"]: r for r in b}[text]


def test_hf001_summary_section(hf001):
    on, _ = hf001
    ws = openpyxl.load_workbook(io.BytesIO(on), data_only=True)["Summary"]
    rows = {r[0]: r for r in ws.iter_rows(values_only=True) if r and r[0]}
    assert "ISO 2768-mK" in rows["General-tolerance standard used"][2]
    assert rows["Open dimensions found"][1] == 6 and rows["Auto-filled from the standard"][1] == 6
    assert rows["Unresolved (blank, see Notes)"][1] == 0


def test_hf001_evaluator_keys_still_valid_with_general_tolerances_off():
    from tests.eval_against_key import evaluate, run_pipeline

    pdf = FIXTURES / "SMX-DEMO-HF-001.pdf"
    off = evaluate(run_pipeline(pdf), FIXTURES / "SMX-DEMO-HF-001_expected_extraction.xlsx")
    for f in ("Upper tolerance", "Lower tolerance", "Max limit", "Min limit"):
        assert off["field_accuracy"][f][0] == off["field_accuracy"][f][1]


# =============================================================================
# integration: synthetic TSDS0407 drawings
# =============================================================================

def _by_text(rows):
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["Text as shown on drawing"], []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["X (mm)"])
    return out


def test_tsds_machining_drawing(tmp_path):
    from app.services.tvs_extraction_report import build_tvs_extraction_xlsx
    from tests.synth_drawings import make_gentol_drawing

    pdf = tmp_path / "tsds_machining.pdf"
    make_gentol_drawing("machining", pdf)
    by = _by_text(_rows(build_tvs_extraction_xlsx({}, pdf)))
    shaft, bore = by["Ø20"]
    assert (shaft["Upper tolerance"], shaft["Lower tolerance"]) == (0, -0.2) and "outside Ø" in shaft["Notes"]
    assert (bore["Upper tolerance"], bore["Lower tolerance"]) == (0.2, 0) and "inside Ø boring" in bore["Notes"]
    d = by["Ø8 DRILL"][0]
    assert (d["Upper tolerance"], d["Lower tolerance"]) == (0.4, 0) and "drilling" in d["Notes"]
    assert (by["50"][0]["Upper tolerance"], by["50"][0]["Lower tolerance"]) == (0.2, -0.2)
    assert (by["4"][0]["Upper tolerance"], by["4"][0]["Lower tolerance"]) == (0.1, -0.1)
    ch = by["1 × 45°"][0]
    assert (ch["Upper tolerance"], ch["Lower tolerance"]) == (0.25, -0.25) and "±1°30′" in ch["Notes"]
    assert (by["R2"][0]["Upper tolerance"], by["R2"][0]["Lower tolerance"]) == (0.25, -0.25)
    assert (by["R5"][0]["Upper tolerance"], by["R5"][0]["Lower tolerance"]) == (0.5, -0.5)
    r20 = by["R20"][0]
    assert r20["Upper tolerance"] is None and "not specified" in r20["Notes"]


def test_tsds_hpdc_drawing_with_machining_override(tmp_path):
    from app.services.drawing_versions import drawing_key_for
    from app.services.tvs_extraction_report import build_tvs_extraction_xlsx
    from app.services.tvs_extractor import extract_drawing
    from tests.synth_drawings import make_gentol_drawing

    pdf = tmp_path / "tsds_hpdc.pdf"
    make_gentol_drawing("hpdc", pdf)
    bore = next(f for f in extract_drawing(pdf).features if f.text == "Ø40")
    store.put_drawing_settings("default", drawing_key_for("", pdf.name)[0],
                               {"feature_process": {str(bore.balloon): "machining"}}, "qa")
    by = _by_text(_rows(build_tvs_extraction_xlsx({}, pdf)))
    assert (by["30"][0]["Upper tolerance"], by["30"][0]["Lower tolerance"]) == (0.3, -0.3)
    od, idia = by["Ø30"]
    assert (od["Upper tolerance"], od["Lower tolerance"]) == (0, -0.4)
    assert (idia["Upper tolerance"], idia["Lower tolerance"]) == (0.4, 0)
    b = by["Ø40"][0]
    assert (b["Upper tolerance"], b["Lower tolerance"]) == (0.2, 0) and "machining / inside Ø boring" in b["Notes"]


def test_stroked_text_without_ocr_is_flagged(tmp_path, monkeypatch):
    from app.services.general_tolerance import detect
    from tests.synth_drawings import make_gentol_drawing

    pdf = tmp_path / "stroked.pdf"
    make_gentol_drawing("machining", pdf, stroked=True)
    monkeypatch.setattr(detect, "_ocr_words", lambda page: None)
    d = detect.detect_drawing(pdf)
    assert d.chosen is None and d.text_source.startswith("none") and d.flags
    ctx = E.GTContext(d)
    assert "unreadable" in E.resolve(OpenDim("radius", 2), ctx).primary.source


def _tessdata():
    try:
        from app.services.autoballoon_engine import _resolve_tessdata
        return _resolve_tessdata()
    except Exception:
        return None


@pytest.mark.skipif(not _tessdata(), reason="Tesseract OCR not installed (existing OCR path)")
def test_stroked_text_detected_through_ocr(tmp_path):
    from app.services.general_tolerance.detect import detect_drawing
    from tests.synth_drawings import make_gentol_drawing

    pdf = tmp_path / "stroked.pdf"
    make_gentol_drawing("machining", pdf, stroked=True)
    d = detect_drawing(pdf)
    assert d.text_source == "OCR" and d.chosen is not None and d.chosen.id == TSDS


# =============================================================================
# teach once
# =============================================================================

def test_teach_once_customer_rule(tmp_path):
    from app.services.tvs_extraction_report import build_tvs_extraction_xlsx
    from tests.synth_drawings import make_gentol_drawing

    store.create_rule("default", {"scope": "customer", "scope_key": "default", "category": "radius",
                                  "size_min": 3, "size_max": 6, "upper": 0.3, "lower": -0.3, "name": "R >3-6 ±0.3"}, "qa")
    # HF-001 states ISO 2768-mK: the drawing's standard wins, with a conflict warning
    by = {r["Text as shown on drawing"]: r for r in _rows(build_tvs_extraction_xlsx({}, FIXTURES / "SMX-DEMO-HF-001.pdf"))}
    r6 = by["2× R6"]
    assert r6["Upper tolerance"] == 0.5 and "standard conflict" in r6["Notes"] and "R >3-6 ±0.3" in r6["Notes"]
    # A drawing with no stated standard gets the rule for open radii in its range
    pdf = tmp_path / "nostd.pdf"
    make_gentol_drawing("machining", pdf, state_standard=False)
    by = _by_text(_rows(build_tvs_extraction_xlsx({}, pdf)))
    r5 = by["R5"][0]
    assert (r5["Upper tolerance"], r5["Lower tolerance"]) == (0.3, -0.3) and "Rule: R >3-6 ±0.3 (customer)" in r5["Notes"]
    assert by["R2"][0]["Upper tolerance"] is None and "No general tolerance stated" in by["R2"][0]["Notes"]
    assert by["R20"][0]["Upper tolerance"] is None


# =============================================================================
# inspection report rows (job path)
# =============================================================================

def test_report_rows_auto_manual_and_reset():
    from app.services.general_tolerance.apply import apply_rows

    balloons = [{"balloon": 1, "page": 1, "text": "R6", "category": "radius"},
                {"balloon": 2, "page": 1, "text": "52 ±0.1", "category": "linear"},
                {"balloon": 3, "page": 1, "text": "Ø95", "category": "basic"}]
    rows = [{"balloon": 1, "page": 1, "nominal": "6", "tol_low": "", "tol_high": ""},
            {"balloon": 2, "page": 1, "nominal": "52", "tol_low": "-0.1", "tol_high": "0.1"},
            {"balloon": 3, "page": 1, "nominal": "95", "tol_low": "", "tol_high": ""}]
    ctx = _ctx(["ISO 2768-m"], drawing_key="D")
    c = apply_rows(rows, balloons, ctx, [])
    assert (rows[0]["tol_low"], rows[0]["tol_high"], rows[0]["tol_auto"]) == ("-0.5", "+0.5", True)
    assert rows[0]["tol_source"] == "General tol – ISO 2768-m" and rows[1]["tol_source"] == "Drawing"
    assert "tol_source" not in rows[2] and c["open"] == 1 and c["standard"] == 1
    rows[0].update(tol_low="-0.3", tol_high="+0.3", tol_source="Manual")
    c = apply_rows(rows, balloons, ctx, [])
    assert rows[0]["tol_source"] == "Manual" and rows[0]["tol_high"] == "+0.3" and c["manual_updates"]["1"]["tol_high"] == "+0.3"
    rows[0]["tol_source"] = "Reset"
    c = apply_rows(rows, balloons, ctx, [])
    assert rows[0]["tol_source"].startswith("General tol") and c["manual_updates"]["1"] is None
    # manual override persisted for the drawing comes back after a balloon refresh blanks the row
    ctx.drawing_settings = {"manual": {"1": {"text": "R6", "tol_low": "-0.3", "tol_high": "+0.3"}}}
    rows[0].update(tol_low="", tol_high="", tol_source="Manual")
    apply_rows(rows, balloons, ctx, [])
    assert rows[0]["tol_source"] == "Manual" and rows[0]["tol_high"] == "+0.3"
    # ...but not once the feature changed
    balloons[0]["text"] = "R8"
    rows[0].update(tol_low="", tol_high="", tol_source="")
    apply_rows(rows, balloons, ctx, [])
    assert rows[0]["tol_source"].startswith("General tol") and rows[0]["tol_high"] == "+1"


def test_exports_carry_tolerance_source():
    from app.services.cmq_report import build_cmq_xlsx
    from app.services.master_report import build_master_xlsx
    from app.services.report_pdf import build_report_pdf, build_report_xlsx, report_rows_to_csv

    rows = [{"sno": 1, "balloon": 1, "page": 1, "nominal": "6", "tol_low": "-0.5", "tol_high": "+0.5",
             "tol_source": "General tol – ISO 2768-m", "tol_auto": True, "measured": ["6.6", ""]},
            {"sno": 2, "balloon": 2, "page": 1, "nominal": "52", "tol_low": "-0.1", "tol_high": "0.1", "tol_source": "Drawing"}]
    payload = {"rows": rows, "measure_columns": [{"id": "m1", "label": "M1", "values": {"1": "6.6"}}]}
    csv_text = report_rows_to_csv(rows).decode("utf-8-sig")
    assert "Tolerance source" in csv_text and "GT ISO 2768-m" in csv_text
    wb = openpyxl.load_workbook(io.BytesIO(build_report_xlsx(payload)))
    ws = wb.active
    cells = {c.value: c for row in ws.iter_rows() for c in row if c.value}
    assert "Tolerance\nsource" in cells and cells["GT ISO 2768-m"].font.italic
    import fitz

    text = " ".join(p.get_text() for p in fitz.open(stream=build_report_pdf(payload), filetype="pdf"))
    assert "GT ISO 2768-m" in text or "GT ISO" in text
    for build in (build_master_xlsx, build_cmq_xlsx):
        wb = openpyxl.load_workbook(io.BytesIO(build(payload)))
        vals = [c.value for ws in wb for row in ws.iter_rows() for c in row if c.value]
        assert "GT ISO 2768-m" in vals


def test_out_of_tolerance_uses_applied_limits():
    from app.services.report_pdf import measure_status

    assert measure_status("6", "-0.5", "+0.5", "6.6") == "red"
    assert measure_status("6", "-0.5", "+0.5", "6.4") == "yellow"
