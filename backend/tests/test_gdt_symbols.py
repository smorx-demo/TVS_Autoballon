"""GD&T symbol recognition, including look-alike pairs and strokes touching the cell border."""

from __future__ import annotations

import fitz
import pytest

from app.services import tvs_extractor as T
from tests import synth_drawings as SD

PAIRS = [
    ("cylindricity", "circularity"),
    ("symmetry", "concentricity"),
    ("circular_runout", "total_runout"),
    ("flatness", "parallelism"),
    ("profile_surface", "profile_line"),
    ("perpendicularity", "angularity"),
]


def _draw_profile_line(sh, cell):
    c = fitz.Point((cell.x0 + cell.x1) / 2, (cell.y0 + cell.y1) / 2)
    h = cell.height * 0.26
    sh.shape.draw_curve(fitz.Point(c.x - h * 1.2, c.y + h * 0.6), fitz.Point(c.x, c.y - h * 1.4),
                        fitz.Point(c.x + h * 1.2, c.y + h * 0.6))
    sh.shape.finish(width=0.7, color=(0, 0, 0), closePath=False)


def _classify(kind: str, draw_h: float, cell_h: float) -> str | None:
    """Draw `kind` sized for a draw_h-tall cell, then read it back from a cell_h-tall compartment."""
    doc = fitz.open()
    sh = SD.Sheet(doc, 1)
    centre = fitz.Point(100, 100)
    draw_cell = fitz.Rect(centre.x - 10, centre.y - draw_h / 2, centre.x + 10, centre.y + draw_h / 2)
    if kind == "profile_line":
        _draw_profile_line(sh, draw_cell)
    else:
        SD._gdt_symbol(sh, draw_cell, kind)
    pd = T.PageData(doc[0], 0)
    cell = fitz.Rect(centre.x - 10, centre.y - cell_h / 2, centre.x + 10, centre.y + cell_h / 2)
    segs, curves, polys = T.symbol_strokes(pd, cell)
    return T.classify_gdt_symbol(segs, curves, polys)


@pytest.mark.parametrize("a, b", PAIRS)
@pytest.mark.parametrize("cell_h", [17.0, 13.0])  # 13: strokes reach the compartment border
def test_lookalike_pairs_are_distinguished(a, b, cell_h):
    assert _classify(a, 17.0, cell_h) == a
    assert _classify(b, 17.0, cell_h) == b


@pytest.mark.parametrize("kind", ["position", "straightness"])
def test_other_symbols(kind):
    assert _classify(kind, 17.0, 17.0) == kind


def test_unicode_and_gdt_font_glyphs():
    assert T.GLYPH_TO_GDT["⌭"] == "cylindricity" and T.GLYPH_TO_GDT["○"] == "circularity"
    assert T.GDT_FONT_CODES["g"] == "cylindricity" and T.GDT_FONT_CODES["e"] == "circularity"
