"""
Native DXF -> vector PDF (PyMuPDF), built for downstream extraction:

  • TEXT / MTEXT become real, selectable PDF text (rotation, height, alignment kept), so
    dimensions can be read without OCR
  • lines, polylines (incl. bulges), circles, arcs, ellipses and splines are drawn as true
    vector paths; curves stay Bézier curves (circles remain detectable)
  • line weight and line type (centre / hidden -> dashed) follow the entity or its layer
  • SOLID / filled HATCH become filled shapes (arrowheads, datum triangles)
  • INSERT blocks and DIMENSION entities are exploded into their geometry and text
  • several sheets laid out side by side in model space become separate pages

Units: 1 drawing unit = 1 mm (true scale). Font: TVS_DXF_FONT, else DejaVu Sans, else
Segoe UI Symbol, else Helvetica (glyphs outside Latin-1 may then be lost; a warning is logged).
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

import fitz

log = logging.getLogger(__name__)

MM = 72.0 / 25.4
DEFAULT_LW_MM = 0.25
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
]
_CAP_RATIO = 0.73   # DXF text height = cap height; PDF font size is the em size


def _font_file() -> str | None:
    env = os.environ.get("TVS_DXF_FONT", "").strip()
    for f in ([env] if env else []) + _FONT_CANDIDATES:
        if f and Path(f).is_file():
            return f
    log.warning("dxf_to_pdf: no Unicode TrueType font found; using Helvetica (set TVS_DXF_FONT)")
    return None


def _lineweight_mm(e, doc) -> float:
    lw = e.dxf.get("lineweight", -1)
    if lw is None or lw < 0:  # BYLAYER / BYBLOCK / DEFAULT
        layer = doc.layers.get(e.dxf.get("layer", "0")) if doc.layers.has_entry(e.dxf.get("layer", "0")) else None
        lw = layer.dxf.get("lineweight", -3) if layer is not None else -3
    return lw / 100.0 if lw is not None and lw > 0 else DEFAULT_LW_MM


def _dashes(e, doc, lw_pt: float) -> str | None:
    lt = (e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
    if lt in ("BYLAYER", "BYBLOCK"):
        layer = doc.layers.get(e.dxf.get("layer", "0")) if doc.layers.has_entry(e.dxf.get("layer", "0")) else None
        lt = (layer.dxf.get("linetype", "CONTINUOUS") if layer is not None else "CONTINUOUS").upper()
    if "CENTER" in lt or "DASHDOT" in lt or "PHANTOM" in lt:
        return "[ 22.68 4.25 4.25 4.25 ] 0"
    if "HIDDEN" in lt or "DASH" in lt:
        return "[ 8.5 4.25 ] 0"
    return None


class _Sheet:
    def __init__(self, page: fitz.Page, x0: float, y_top: float):
        self.page = page
        self.x0 = x0
        self.y_top = y_top

    def pt(self, x: float, y: float) -> fitz.Point:
        return fitz.Point((x - self.x0) * MM, (self.y_top - y) * MM)


def _draw_path(sheet: _Sheet, path, *, width: float, dashes: str | None, fill: bool) -> None:
    """ezdxf Path -> one PyMuPDF shape path (lines + cubic Béziers)."""
    from ezdxf.path import Command

    sh = sheet.page.new_shape()
    cur = path.start
    drawn = False
    for cmd in path.commands():
        if cmd.type == Command.LINE_TO:
            sh.draw_line(sheet.pt(cur.x, cur.y), sheet.pt(cmd.end.x, cmd.end.y))
            drawn = True
        elif cmd.type == Command.CURVE4_TO:
            sh.draw_bezier(sheet.pt(cur.x, cur.y), sheet.pt(cmd.ctrl1.x, cmd.ctrl1.y),
                           sheet.pt(cmd.ctrl2.x, cmd.ctrl2.y), sheet.pt(cmd.end.x, cmd.end.y))
            drawn = True
        elif cmd.type == Command.CURVE3_TO:
            c = cmd.ctrl
            c1 = (cur.x + 2 / 3 * (c.x - cur.x), cur.y + 2 / 3 * (c.y - cur.y))
            c2 = (cmd.end.x + 2 / 3 * (c.x - cmd.end.x), cmd.end.y + 2 / 3 * (c.y - cmd.end.y))
            sh.draw_bezier(sheet.pt(cur.x, cur.y), sheet.pt(*c1), sheet.pt(*c2), sheet.pt(cmd.end.x, cmd.end.y))
            drawn = True
        cur = cmd.end
    if not drawn:
        return
    if fill:
        sh.finish(fill=(0, 0, 0), color=None, closePath=True)
    else:
        sh.finish(width=width, color=(0, 0, 0), dashes=dashes, closePath=False)
    sh.commit()


def _text(sheet: _Sheet, text: str, ins, height: float, rotation: float, fontfile: str | None) -> None:
    if not text.strip():
        return
    size = max(height * MM / _CAP_RATIO, 1.0)
    p = sheet.pt(ins[0], ins[1])
    kw = {"fontsize": size}
    if fontfile:
        kw.update(fontname="dxf0", fontfile=fontfile)
    else:
        kw.update(fontname="helv")
    rot = round(rotation % 360, 3)
    if rot in (0.0, 90.0, 180.0, 270.0):
        sheet.page.insert_text(p, text, rotate=int(rot), **kw)
    else:
        sheet.page.insert_text(p, text, morph=(p, fitz.Matrix(rot)), **kw)


def _text_anchor(e) -> tuple[tuple[float, float], float]:
    """Baseline-left insertion point of a TEXT entity (alignment resolved via ezdxf)."""
    d = e.dxf
    halign, valign = d.get("halign", 0), d.get("valign", 0)
    if halign or valign:
        try:
            from ezdxf.tools.text import TextLine
            from ezdxf.fonts import fonts

            p = e.get_placement()
            ins, align = p[1], p[0]
            font = fonts.make_font("DejaVuSans.ttf", d.height)
            tl = TextLine(d.text, font)
            corners = tl.corner_vertices(ins, align, rotation=math.radians(d.get("rotation", 0)))
            return (corners[0].x, corners[0].y), d.height
        except Exception:
            ap = d.get("align_point", d.insert)
            return (ap[0], ap[1]), d.height
    return (d.insert[0], d.insert[1]), d.height


def _render(entity, doc, sheet: _Sheet, fontfile: str | None, depth: int = 0) -> None:
    import ezdxf.path as ezpath

    t = entity.dxftype()
    if t == "TEXT":
        ins, h = _text_anchor(entity)
        _text(sheet, entity.plain_text() if hasattr(entity, "plain_text") else entity.dxf.text, ins, h,
              entity.dxf.get("rotation", 0.0), fontfile)
        return
    if t == "MTEXT":
        h = entity.dxf.get("char_height", 2.5)
        rot = entity.get_rotation() if hasattr(entity, "get_rotation") else entity.dxf.get("rotation", 0.0)
        lines = entity.plain_text().split("\n")
        ins = entity.dxf.insert
        ang = math.radians(rot)
        step = h * entity.dxf.get("line_spacing_factor", 1.0) * 1.667
        for k, ln in enumerate(lines):
            # MTEXT insert is the top-left (default attachment); first baseline one cap height below
            off = h + k * step
            x = ins[0] + off * math.sin(ang)
            y = ins[1] - off * math.cos(ang)
            _text(sheet, ln, (x, y), h, rot, fontfile)
        return
    if t in ("INSERT", "DIMENSION", "LEADER", "MLEADER", "MULTILEADER") and depth < 8:
        try:
            for sub in entity.virtual_entities():
                _render(sub, doc, sheet, fontfile, depth + 1)
        except Exception as ex:
            log.debug("dxf_to_pdf: cannot explode %s: %s", t, ex)
        return
    if t in ("SOLID", "TRACE", "3DFACE"):
        vs = [entity.dxf.get(f"vtx{i}") for i in range(4)]
        vs = [v for v in vs if v is not None]
        order = [vs[0], vs[1], vs[3], vs[2]] if len(vs) == 4 and t != "3DFACE" else vs
        uniq = []
        for v in order:
            if not uniq or (abs(v[0] - uniq[-1][0]) > 1e-9 or abs(v[1] - uniq[-1][1]) > 1e-9):
                uniq.append(v)
        if len(uniq) > 1 and abs(uniq[0][0] - uniq[-1][0]) < 1e-9 and abs(uniq[0][1] - uniq[-1][1]) < 1e-9:
            uniq.pop()
        sh = sheet.page.new_shape()
        pts = [sheet.pt(v[0], v[1]) for v in uniq]
        sh.draw_polyline(pts + [pts[0]])
        sh.finish(fill=(0, 0, 0), color=None, closePath=False)
        sh.commit()
        return
    if t == "HATCH":
        if entity.dxf.get("solid_fill", 0):
            for p in ezpath.from_hatch(entity):
                _draw_path(sheet, p, width=0, dashes=None, fill=True)
        else:
            try:
                for sub in entity.virtual_entities():
                    _render(sub, doc, sheet, fontfile, depth + 1)
            except Exception:
                pass
        return
    if t in ("LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"):
        lw = _lineweight_mm(entity, doc) * MM
        dashes = _dashes(entity, doc, lw)
        try:
            p = ezpath.make_path(entity)
        except Exception as ex:
            log.debug("dxf_to_pdf: skip %s: %s", t, ex)
            return
        _draw_path(sheet, p, width=lw, dashes=dashes, fill=False)
        return
    if t == "POINT":
        return


def _sheet_ranges(msp) -> list[tuple[float, float, float, float]]:
    """Split model space into sheets separated by empty vertical gaps (side-by-side layouts)."""
    from ezdxf import bbox

    spans = []
    for e in msp:
        try:
            b = bbox.extents([e])
        except Exception:
            continue
        if b.has_data:
            spans.append((b.extmin.x, b.extmax.x, b.extmin.y, b.extmax.y))
    if not spans:
        return []
    spans.sort()
    sheets = []
    x0, x1, y0, y1 = spans[0]
    for a, b, c, d in spans[1:]:
        if a > x1 + 5.0:  # empty band -> next sheet
            sheets.append((x0, x1, y0, y1))
            x0, x1, y0, y1 = a, b, c, d
        else:
            x1, y0, y1 = max(x1, b), min(y0, c), max(y1, d)
    sheets.append((x0, x1, y0, y1))
    return sheets


def dxf_to_pdf(src: Path, dest: Path) -> dict:
    import ezdxf
    from ezdxf import bbox

    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    fontfile = _font_file()
    out = fitz.open()
    ranges = _sheet_ranges(msp)
    if not ranges:
        raise ValueError("DXF model space is empty")
    for (x0, x1, y0, y1) in ranges:
        page = out.new_page(width=(x1 - x0) * MM, height=(y1 - y0) * MM)
        sheet = _Sheet(page, x0, y1)
        for e in msp:
            try:
                b = bbox.extents([e])
            except Exception:
                continue
            if not b.has_data:
                continue
            cx = (b.extmin.x + b.extmax.x) / 2
            if x0 - 1e-6 <= cx <= x1 + 1e-6:
                _render(e, doc, sheet, fontfile)
    out.save(str(dest), garbage=3, deflate=True)
    pages = out.page_count
    out.close()
    return {"converter": "dxf_native_pymupdf", "pages": pages, "vector_hint": True,
            "font": Path(fontfile).name if fontfile else "helv"}
