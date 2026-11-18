"""Unilateral / one-sided tolerances and boxed (basic) dimensions."""

from __future__ import annotations

import fitz
import pytest

from app.services import autoballoon_engine as eng
from app.services.report_pdf import measure_status, parse_tolerance


@pytest.mark.parametrize(
    "text, pattern, category",
    [
        ("25 +0.05/0", "tol_unilateral", "tolerance"),
        ("25 0/-0.02", "tol_unilateral", "tolerance"),
        ("25 +0.2/+0.1", "tol_unilateral", "tolerance"),
        ("25 -0.1/-0.3", "tol_unilateral", "tolerance"),
        ("25 (+0.05/0)", "tol_unilateral", "tolerance"),
        ("Ø20 0/-0.021", "diameter_tol_dev", "diameter"),
        ("R5 +0.1/0", "radius_tol_dev", "radius"),
        ("25 +0.05/-0.02", "tol_unequal", "tolerance"),  # existing bilateral still wins
    ],
)
def test_one_sided_patterns(text, pattern, category):
    assert eng._is_dimension(text)
    assert eng._is_dimension_verbose(text)[1:] == (pattern, category)


@pytest.mark.parametrize("text", ["25 0/0", "25.05/25.00", "M6 - 8.0 DEEP"])
def test_one_sided_patterns_do_not_steal(text):
    _, name, _ = eng._is_dimension_verbose(text)
    assert name not in {"tol_unilateral", "diameter_tol_dev", "radius_tol_dev"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("25±0.1", ("25", "-0.1", "0.1")),
        ("25 +0.05/-0.02", ("25", "-0.02", "+0.05")),
        ("25 +0.05/0", ("25", "0", "+0.05")),
        ("Ø20 0/-0.021", ("20", "-0.021", "0")),
        ("Ø20 +0.2/+0.1", ("20", "+0.1", "+0.2")),
        ("60 H7 (+0.030/0)", ("60", "0", "+0.030")),
        ("60 H7 (+0.030)", ("60", "0", "+0.030")),
        ("[25]", ("25", "", "")),
        ("M8x1.25", ("8", "", "")),
    ],
)
def test_parse_tolerance(text, expected):
    assert parse_tolerance(text) == expected


def test_unilateral_band_colours():
    nom, lo, hi = parse_tolerance("25 +0.05/0")
    assert measure_status(nom, lo, hi, "25.03") == "yellow"
    assert measure_status(nom, lo, hi, "24.99") == "red"  # below a 0 lower limit
    assert measure_status(nom, lo, hi, "25.06") == "red"


def _detect(build) -> list[eng.Characteristic]:
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)  # A4 landscape
    build(page)
    eng._clear_page_geom_cache()
    try:
        return eng.detect_dimensions(doc, use_zones=False)
    finally:
        eng._clear_page_geom_cache()


def _by_text(chars):
    return {c.text: c for c in chars}


def test_stacked_unilateral_tolerance_is_merged():
    def build(page):
        page.insert_text((300, 250), "25", fontsize=10)
        page.insert_text((314, 245), "+0.05", fontsize=6)
        page.insert_text((314, 253), "0", fontsize=6)

    found = _by_text(_detect(build))
    assert "25 +0.05/0" in found
    assert found["25 +0.05/0"].pattern == "tol_unilateral"


def test_boxed_value_is_basic_dimension():
    def build(page):
        page.insert_text((300, 250), "42", fontsize=10)
        page.draw_rect(fitz.Rect(296, 240, 316, 254), width=0.5)
        page.insert_text((450, 250), "37", fontsize=10)  # plain, unboxed

    found = _by_text(_detect(build))
    assert found["[42]"].category == "basic"
    assert found["[42]"].pattern == "basic_boxed"
    assert found["37"].category != "basic"


def test_feature_control_frame_cell_is_not_basic():
    def build(page):
        # Three-cell frame: [symbol | 0.05 | A] drawn as one outer rect + dividers
        page.draw_rect(fitz.Rect(296, 240, 366, 254), width=0.5)
        page.draw_line((312, 240), (312, 254), width=0.5)
        page.draw_line((346, 240), (346, 254), width=0.5)
        page.insert_text((318, 251), "0.05", fontsize=10)
        page.insert_text((352, 251), "A", fontsize=10)

    for c in _detect(build):
        assert c.category != "basic", c.text


def test_nominal_digits_are_not_split_into_a_deviation():
    # "10 +1" must not be read as nominal 1 with "0 +1"
    assert eng._is_dimension_verbose("10 +1")[1] != "tol_unilateral"
    assert parse_tolerance("10 +1")[0] == "10"
    assert parse_tolerance("10 +1/0") == ("10", "0", "+1")


def test_vertical_stacked_deviation_is_merged():
    def build(page):
        # Vertical callout read bottom-to-top: "10 +1" on one line, lower "0" separate
        page.insert_text((300, 300), "10 +1", fontsize=10, rotate=90)
        page.insert_text((306, 285), "0", fontsize=7, rotate=90)

    found = _by_text(_detect(build))
    assert "10 +1/0" in found, list(found)


def test_bare_zero_upper_deviation_is_merged():
    def build(page):
        # PDF text layer: "16 0" (nominal + upper 0 on one line) and lower "-0.1" separately
        page.insert_text((300, 250), "16 0", fontsize=10)
        page.insert_text((316, 256), "-0.1", fontsize=7)

    found = _by_text(_detect(build))
    assert "16 0/-0.1" in found, list(found)
    assert parse_tolerance("16 0/-0.1") == ("16", "-0.1", "0")


@pytest.mark.parametrize(
    "text, pattern",
    [
        ("Ø35 H7 +0.025/0", "diameter_tol_dev"),
        ("Ø60 h6 0/-0.019", "diameter_tol_dev"),
        ("Ø42 g6 -0.009/-0.025", "diameter_tol_dev"),
        ("Ø48 p7 +0.051/+0.026", "diameter_tol_dev"),
    ],
)
def test_fit_with_printed_limits(text, pattern):
    assert eng._is_dimension_verbose(text)[1] == pattern


def test_fit_limits_and_angle_tolerance_parse():
    assert parse_tolerance("Ø60 h6 0/-0.019") == ("60", "-0.019", "0")
    assert parse_tolerance("Ø48 p7 +0.051/+0.026") == ("48", "+0.026", "+0.051")
    assert parse_tolerance("30° ±0.25°") == ("30", "-0.25", "0.25")


def test_vertical_fit_with_stacked_limit_is_merged():
    def build(page):
        page.insert_text((300, 300), "Ø60 h6 0", fontsize=10, rotate=90)
        page.insert_text((306, 260), "-0.019", fontsize=7, rotate=90)

    found = _by_text(_detect(build))
    assert "Ø60 h6 0/-0.019" in found, list(found)


def test_rotated_box_is_basic_dimension():
    def build(page):
        pivot = fitz.Point(300, 250)
        page.insert_text(pivot, "45°", fontsize=10, morph=(pivot, fitz.Matrix(-22.5)))
        # Box around the unrotated text, rotated with it (PyMuPDF morph turns the other way)
        rot = fitz.Matrix(22.5)
        corners = [fitz.Point(298, 240), fitz.Point(319, 240), fitz.Point(319, 253), fitz.Point(298, 253)]
        pts = [(c - pivot) * rot + pivot for c in corners]
        shape = page.new_shape()
        for k in range(4):
            shape.draw_line(pts[k], pts[(k + 1) % 4])
        shape.finish(width=0.5)
        shape.commit()

    found = _by_text(_detect(build))
    assert "[45°]" in found, list(found)
