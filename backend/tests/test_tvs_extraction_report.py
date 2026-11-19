"""TVS extraction report: feature classification and workbook layout."""

from __future__ import annotations

import io
from pathlib import Path

import fitz
import openpyxl
import pytest

from app.services.tvs_extraction_report import (
    EXTRACTION_COLUMNS,
    NOTES_COLUMNS,
    build_tvs_extraction_xlsx,
    classify_feature,
)


@pytest.mark.parametrize(
    "text, category, ftype, symbol, nominal, upper, lower, flag",
    [
        ("Ø120 ±0.2", "diameter", "Diameter", "Ø", 120, 0.2, -0.2, "-"),
        ("[Ø95]", "basic", "Basic dimension", "Ø", 95, None, None, "Boxed (basic)"),
        ("[45°]", "basic", "Basic angle", "°", 45, None, None, "Boxed (basic)"),
        ("(134.4)", "reference", "Reference dimension", "", 134.4, None, None, "Bracketed (reference)"),
        ("16 0/-0.1", "tolerance", "Linear dimension", "", 16, 0, -0.1, "-"),
        ("Ø35 H7 +0.025/0", "diameter", "Fit (hole)", "Ø", 35, 0.025, 0, "-"),
        ("Ø42 g6 -0.009/-0.025", "diameter", "Fit (shaft)", "Ø", 42, -0.009, -0.025, "-"),
        ("2 × 45°", "chamfer", "Chamfer", "°", 2, None, None, "-"),
        ("R12", "radius", "Radius", "R", 12, None, None, "-"),
        ("30° ±0.25°", "angle", "Angular dimension", "°", 30, 0.25, -0.25, "-"),
    ],
)
def test_classify_dimensions(text, category, ftype, symbol, nominal, upper, lower, flag):
    f = classify_feature(text, category)
    assert f["feature_type"] == ftype
    assert (f["symbol"] or "") == symbol
    assert f["nominal"] == pytest.approx(nominal)
    assert f["upper"] == (pytest.approx(upper) if upper is not None else None)
    assert f["lower"] == (pytest.approx(lower) if lower is not None else None)
    assert f["flag"] == flag


def test_classify_gdt_thread_surface():
    g = classify_feature("position | Ø0.2 M | A | B | C", "gdt")
    assert g["feature_type"] == "GD&T - Position"
    assert g["symbol"] == "⌖"
    assert g["nominal"] == pytest.approx(0.2)
    assert g["datums"] == ["A", "B", "C"]
    assert "at MMC" in g["detail"]

    t = classify_feature("4× M8×1.25-6H ↧10", "manual")
    assert t["feature_type"] == "Thread" and t["nominal"] == "M8"
    assert "M8×1.25" in t["detail"] and "6H" in t["detail"] and "depth 10" in t["detail"]
    assert t["note"] == "Qty 4 (4×)"

    s = classify_feature("U Ra 1.6 L Ra 0.4", "manual")
    assert s["feature_type"] == "Surface finish"
    assert (s["upper"], s["lower"]) == (1.6, 0.4)
    n = classify_feature("N7", "surface")
    assert n["nominal"] == "N7" and n["upper"] == 1.6


def _drawing(tmp_path: Path) -> Path:
    doc = fitz.open()
    p1 = doc.new_page(width=1190.55, height=841.89)  # A3 landscape
    p1.insert_text((200, 300), "Ø120 ±0.2", fontsize=10)
    p1.insert_text((400, 300), "16 0/-0.1", fontsize=10)
    p1.insert_text((450, 300), "CC5", fontsize=7)
    p1.insert_text((200, 600), "FRONT VIEW", fontsize=10)
    p2 = doc.new_page(width=1190.55, height=841.89)
    p2.insert_text((150, 500), "GENERAL NOTES", fontsize=9)
    p2.insert_text((150, 515), "1. MATERIAL: EN AW-6082-T6 ALUMINIUM ALLOY.", fontsize=8)
    p2.insert_text((150, 527), "2. REMOVE ALL BURRS AND SHARP EDGES.", fontsize=8)
    p2.insert_text((300, 300), "(52)", fontsize=10)
    out = tmp_path / "drawing.pdf"
    doc.save(out)
    return out


def test_workbook_layout(tmp_path):
    pdf = _drawing(tmp_path)
    balloons = [
        {"balloon": 1, "page": 1, "text": "Ø120 ±0.2", "x": 200, "y": 291, "width": 50, "height": 11, "category": "diameter"},
        {"balloon": 2, "page": 1, "text": "16 0/-0.1", "x": 400, "y": 291, "width": 38, "height": 11, "category": "tolerance"},
        {"balloon": 3, "page": 2, "text": "(52)", "x": 300, "y": 291, "width": 20, "height": 11, "category": "reference"},
    ]
    meta = {"part_number": "TEST-001", "revision": "A", "part_name": "Test part", "balloons": balloons}
    wb = openpyxl.load_workbook(io.BytesIO(build_tvs_extraction_xlsx(meta, pdf)))
    assert wb.sheetnames == ["Summary", "Extraction", "Notes"]
    assert wb["Summary"]["A1"].value == "TVS extraction report"

    ex = wb["Extraction"]
    assert [c.value for c in ex[1]] == [name for name, _ in EXTRACTION_COLUMNS]
    assert ex.freeze_panes == "A2" and ex.auto_filter.ref
    # column A = Balloon No.; the remaining columns keep their order (index + 1)
    all_rows = list(ex.iter_rows(min_row=2, values_only=True))
    assert [r[0] for r in all_rows] == [1, 2, 3]
    rows = {r[1]: r[1:] for r in all_rows}
    assert set(rows) == {"S1-001", "S1-002", "S2-001"}
    r1 = rows["S1-001"]
    assert r1[6:13] == ("Diameter", "Ø", 120, 0.2, -0.2, 120.2, 119.8)
    # Coordinates = centre of the annotation text as drawn (mm from the lower-left corner)
    text_rect = fitz.open(pdf)[0].search_for("Ø120 ±0.2")[0]
    assert r1[3] == pytest.approx((text_rect.x0 + text_rect.x1) / 2 * 25.4 / 72, abs=0.3)
    assert r1[4] == pytest.approx((841.89 - (text_rect.y0 + text_rect.y1) / 2) * 25.4 / 72, abs=0.3)
    assert r1[5] == "Front view"
    assert rows["S1-002"][18] == "CC5"  # critical tag next to the dimension
    assert rows["S1-002"][13] == "Unilateral (minus) tolerance"
    assert rows["S2-001"][17] == "Bracketed (reference)"

    notes = wb["Notes"]
    assert [c.value for c in notes[1]] == [name for name, _ in NOTES_COLUMNS]
    kinds = [(r[4], r[5]) for r in notes.iter_rows(min_row=2, values_only=True)]
    assert ("View label", "FRONT VIEW") in kinds
    assert ("Heading", "GENERAL NOTES") in kinds
    assert ("General note 1", "MATERIAL: EN AW-6082-T6 ALUMINIUM ALLOY.") in kinds
    assert ("General note 2", "REMOVE ALL BURRS AND SHARP EDGES.") in kinds
