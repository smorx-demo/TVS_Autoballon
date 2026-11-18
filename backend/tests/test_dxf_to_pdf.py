"""DXF -> PDF keeps real text, curves, line weights, line types and sheets."""

from __future__ import annotations

import ezdxf
import fitz

from app.services.dxf_to_pdf import dxf_to_pdf


def test_dxf_converts_to_text_bearing_vector_pdf(tmp_path):
    doc = ezdxf.new(units=4)
    doc.layers.add("CENTER", linetype="CENTER")
    msp = doc.modelspace()
    for x0 in (0, 450):                         # two A3 sheets side by side
        msp.add_lwpolyline([(x0, 0), (x0 + 420, 0), (x0 + 420, 297), (x0, 297)], close=True)
    msp.add_line((50, 50), (150, 50), dxfattribs={"lineweight": 50})
    msp.add_circle((100, 150), 20, dxfattribs={"layer": "CENTER"})
    msp.add_text("Ø25 ±0.1", height=2.5, dxfattribs={"insert": (60, 100)})
    msp.add_text("M8×1.25-6H ↧10", height=2.5, dxfattribs={"insert": (500, 100), "rotation": 90})
    src = tmp_path / "t.dxf"
    doc.saveas(src)
    meta = dxf_to_pdf(src, tmp_path / "t.pdf")
    pdf = fitz.open(tmp_path / "t.pdf")
    assert meta["pages"] == pdf.page_count == 2
    assert round(pdf[0].rect.width * 25.4 / 72) == 420           # true scale, mm
    assert "Ø25 ±0.1" in pdf[0].get_text()
    lines = [l for b in pdf[1].get_text("dict")["blocks"] for l in b.get("lines", [])]
    assert any("M8×1.25-6H" in "".join(s["text"] for s in l["spans"]) and abs(l["dir"][1]) > 0.9 for l in lines)
    d = pdf[0].get_drawings()
    assert any(any(it[0] == "c" for it in p["items"]) and p.get("dashes") not in (None, "[] 0") for p in d)  # dashed circle
    assert any(abs((p.get("width") or 0) - 0.5 * 72 / 25.4) < 0.05 for p in d)                           # 0.5 mm weight
