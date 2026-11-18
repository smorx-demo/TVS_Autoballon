"""Unit tests for the SMX-DEMO-002 bug classes (generic rules, built on tiny in-memory drawings)."""

from __future__ import annotations

import math

import fitz
import pytest

from app.services import tvs_extractor as T


def _page():
    doc = fitz.open()
    return doc, doc.new_page(width=1190.55, height=841.89)


def _line(page, a, b, width=0.7):
    sh = page.new_shape()
    sh.draw_line(a, b)
    sh.finish(width=width, color=(0, 0, 0), closePath=False)
    sh.commit()


def _arrow(page, tip, frm, size=6):
    ang = math.atan2(tip.y - frm.y, tip.x - frm.x)
    pts = [tip, fitz.Point(tip.x - size * math.cos(ang - 0.3), tip.y - size * math.sin(ang - 0.3)),
           fitz.Point(tip.x - size * math.cos(ang + 0.3), tip.y - size * math.sin(ang + 0.3))]
    sh = page.new_shape()
    sh.draw_polyline(pts + [pts[0]])
    sh.finish(fill=(0, 0, 0), color=None, closePath=False)
    sh.commit()


# 1. view-label grammar --------------------------------------------------------------------
@pytest.mark.parametrize("text, name", [
    ("MAIN VIEW  SCALE 2:1", "Main view"), ("FRONT VIEW", "Front view"), ("SECTION C-C  SCALE 2:1", "Section C-C"),
    ("DETAIL D", "Detail D"), ("VIEW X (FROM LEFT END)  SCALE 2:1", "View X"), ("ISOMETRIC VIEW", "Isometric view"),
])
def test_view_label_grammar(text, name):
    m = T.VIEW_RE.match(text)
    assert m and T.view_name(m) == name


@pytest.mark.parametrize("text", ["C", "A", "SEE SHEET 2.", "GENERAL NOTES"])
def test_non_titles_are_not_view_labels(text):
    assert not T.VIEW_RE.match(text)


def test_split_label_keeps_scale_line():
    doc, page = _page()
    page.insert_text((300, 400), "DETAIL D", fontsize=9)
    page.insert_text((298, 412), "SCALE 3:1", fontsize=9)
    pd = T.PageData(page, 0)
    labels = T._view_labels(pd, page.rect, [])
    assert [(l.name, l.text, l.scale) for l in labels] == [("Detail D", "DETAIL D SCALE 3:1", 3.0)]


# 2. datum feature symbols in four orientations -------------------------------------------
@pytest.mark.parametrize("direction", ["left", "right", "up", "down"])
def test_datum_symbol_any_orientation(direction):
    doc, page = _page()
    box = fitz.Rect(300, 300, 317, 317)
    sh = page.new_shape()
    sh.draw_rect(box)
    sh.finish(width=0.7, color=(0, 0, 0))
    sh.commit()
    page.insert_text((305, 312), "B", fontsize=9.5)
    c = fitz.Point(308.5, 308.5)
    off = {"left": (-30, 0), "right": (30, 0), "up": (0, -30), "down": (0, 30)}[direction]
    apex = fitz.Point(c.x + off[0], c.y + off[1])
    edge = fitz.Point(c.x + off[0] * 8.5 / 30, c.y + off[1] * 8.5 / 30)
    _line(page, edge, apex)
    _arrow(page, apex, edge, size=8)        # filled, but near-equilateral like a datum triangle
    _arrow(page, fitz.Point(360, 309), fitz.Point(390, 309), size=9)  # a long thin dimension arrowhead nearby
    pd = T.PageData(page, 0)
    found = T.detect_datum_features(pd, 1, [])
    assert [f.text for f in found] == ["B"]


# 3. chamfer grammar ------------------------------------------------------------------------
@pytest.mark.parametrize("text, size, qty", [
    ("2× 0.5 × 45°", 0.5, 2), ("2X 1 x 30°", 1.0, 2), ("3 x 0.5x45°", 0.5, 3), ("45° × 1.5", 1.5, None), ("1 × 45°", 1.0, None),
])
def test_chamfer_grammar(text, size, qty):
    info = T.parse_dimension(text, [])
    assert info["type"] == "Chamfer" and info["nominal"] == size and info.get("qty") == qty


# 5. CC table rows resolve to several features ----------------------------------------------
def test_cc_table_row_resolves_to_dimension_and_gdt():
    t = T.Table(fitz.Rect(0, 0, 10, 10), [["ID", "CHARACTERISTIC"], ["CC1", "Ø30 h6 + cylindricity 0.005 (S1)"],
                                          ["CC2", "Ø35 p7 + ↗ 0.02 B (S1)"]], [fitz.Rect()] * 3, "cc", [])
    refs = T._cc_table_refs([t])
    assert {(r["kind"], r.get("nominal"), r.get("fit"), r.get("gdt"), r.get("value")) for r in refs["CC1"]} == {
        ("dim", 30.0, "h6", None, None), ("gdt", None, None, "cylindricity", 0.005)}
    assert {r.get("gdt") for r in refs["CC2"]} == {None, "circular_runout"}


# 6. depth symbol ---------------------------------------------------------------------------
def test_depth_symbol_prefix_kept():
    assert T._norm("↧10") == "↧10"
    info = T.parse_dimension("↧10 ±0.3", [])
    assert info["symbol"] == "↧" and info["nominal"] == 10 and (info["upper"], info["lower"]) == (0.3, -0.3)


# 7. inclined only for a non-orthogonal two-arrow dimension line ----------------------------
@pytest.mark.parametrize("case, expected", [
    ("vertical_text", "Linear dimension"), ("inclined_dimline", "Inclined linear dimension"),
    ("one_arrow_leader", "Linear dimension"),
])
def test_inclined_vs_rotated(case, expected):
    doc, page = _page()
    if case == "vertical_text":
        a, b = fitz.Point(300, 300), fitz.Point(300, 420)
        _line(page, a, b); _arrow(page, a, b); _arrow(page, b, a)
        page.insert_text((296, 380), "48 ±0.1", fontsize=9.5, rotate=90)
    else:
        a, b = fitz.Point(300, 420), fitz.Point(400, 340)
        _line(page, a, b)
        _arrow(page, a, b)
        if case == "inclined_dimline":
            _arrow(page, b, a)
        page.insert_text((330, 372), "48 ±0.1", fontsize=9.5)
    pd = T.PageData(page, 0)
    dims = T.detect_dimensions(pd, 1, [])
    assert [d.ftype for d in dims] == [expected]


# 8. near-border rule -------------------------------------------------------------------------
def test_near_border_fixed_distance():
    frame = fitz.Rect(56.7, 28.35, 1162.2, 813.5)
    ext = T.Extraction([], [], 1, [fitz.Rect(0, 0, 1190.55, 841.89)], {1: frame},
                       {1: [("title block", fitz.Rect(652, 658, 1162.2, 813.5))]})
    mm = 1 / T.PT_TO_MM
    near = fitz.Rect(frame.x0 + 10 * mm, 300, frame.x0 + 30 * mm, 310)
    far = fitz.Rect(frame.x0 + 20 * mm, 300, frame.x0 + 40 * mm, 310)
    assert T.near_border_note(ext, 1, near, 15) == "Placed near left border"
    assert T.near_border_note(ext, 1, far, 15) == ""
    assert "title block" in T.near_border_note(ext, 1, fitz.Rect(600, 640, 640, 650), 15)


# 9. bounding-box centre of a multi-line callout ---------------------------------------------
def test_multiline_callout_centre_is_union_bbox():
    doc, page = _page()
    page.insert_text((300, 300), "4× M8×1.25-6H x 10 DEEP", fontsize=8.5)
    page.insert_text((300, 313), "Ø6.8 x 13 DEEP", fontsize=8.5)
    pd = T.PageData(page, 0)
    th = T.detect_threads(pd, 1, [])
    assert len(th) == 1
    l1, l2 = [l.rect for l in pd.lines]
    u = l1 | l2
    assert abs(th[0].center.x - (u.x0 + u.x1) / 2) < 0.01 and abs(th[0].center.y - (u.y0 + u.y1) / 2) < 0.01


# 11. text coverage + cross-reference notes ---------------------------------------------------
def test_free_text_becomes_note_and_nothing_is_orphaned():
    doc, page = _page()
    page.insert_text((800, 400), "GENERAL NOTES AND REVISIONS:", fontsize=7)
    page.insert_text((800, 409), "SEE SHEET 2.", fontsize=7)
    ext = T._extract(doc)
    assert [(n.kind, n.text) for n in ext.notes] == [("Cross-reference note", "GENERAL NOTES AND REVISIONS: SEE SHEET 2.")]
    assert ext.orphans == []
