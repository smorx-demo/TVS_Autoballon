"""
Extended vector-PDF extraction: GD&T FCF, surface finish (N/Ra/Rz), drawing notes.

Runs alongside auto-ballooning; results are stored on job meta and exported to Excel.
"""

from __future__ import annotations

import io
import math
import os
import re
from dataclasses import dataclass

import fitz

# GDT characteristic tokens (garbled OCR / Latin substitutes on CAD exports)
_GDT_SYMBOL_ALIASES: dict[str, str] = {
    "n": "position",
    "⊥": "position",
    "i": "perpendicularity",
    "l": "perpendicularity",
    "g": "angularity",
    "∠": "angularity",
    "m": "concentricity",
    "o": "circularity",
    "r": "runout",
    "f": "flatness",
    "p": "profile",
    "⌯": "profile",
    "c": "cylindricity",
    "-": "straightness",
    "u": "parallelism",
}

_FCF_BOX_MIN_W = 48.0
_FCF_BOX_MAX_H = 52.0
_FCF_BOX_MIN_H = 6.0

_SURFACE_N_GRADE = re.compile(r"^N\s*([1-9]|1[0-2])$", re.IGNORECASE)
_SURFACE_RA = re.compile(
    r"^(?:Ra|R\.?A\.?)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(?:µm|um|μm)?$",
    re.IGNORECASE,
)
_SURFACE_RZ = re.compile(
    r"^(?:Rz|R\.?Z\.?)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(?:µm|um|μm)?$",
    re.IGNORECASE,
)
_SURFACE_RA_BARE = re.compile(
    r"^(?:0\.025|0\.05|0\.1|0\.2|0\.4|0\.8|1\.6|3\.2|6\.3|12\.5|25(?:\.0)?)$",
)

_NOTE_NUMBERED = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+)$", re.DOTALL)


def extended_extract_enabled() -> bool:
    return os.environ.get("SMORX_EXTENDED_EXTRACT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def balloon_gdt_surface_enabled() -> bool:
    return os.environ.get("SMORX_BALLOON_GDT_SURFACE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _clean(text: str) -> str:
    from app.services.autoballoon_engine import _clean as eng_clean

    return eng_clean(text)


@dataclass
class GdtFcfRecord:
    page: int
    x: float
    y: float
    width: float
    height: float
    characteristic: str
    tolerance: str
    datum_primary: str = ""
    datum_secondary: str = ""
    datum_tertiary: str = ""
    raw: str = ""
    frame_text: str = ""


@dataclass
class SurfaceFinishRecord:
    page: int
    x: float
    y: float
    kind: str  # n_grade | ra | rz | ra_bare
    value: str
    raw: str


@dataclass
class NoteRecord:
    page: int
    x: float
    y: float
    note_no: str
    text: str
    zone: str = "notes"


def _iter_page_lines(page: fitz.Page):
    from app.services.autoballoon_engine import _iter_text_lines

    yield from _iter_text_lines(page)


def _page_zones(page: fitz.Page):
    from app.services.autoballoon_engine import _build_page_zones

    return _build_page_zones(page)


def _point_in_rects(x: float, y: float, rects: list[fitz.Rect]) -> bool:
    from app.services.autoballoon_engine import _point_in_rects as pip

    return pip(x, y, rects)


def _fcf_box_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Horizontal FCF-like boxes from vector strokes."""
    out: list[fitz.Rect] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        if w < _FCF_BOX_MIN_W or h < _FCF_BOX_MIN_H or h > _FCF_BOX_MAX_H:
            continue
        if w / max(h, 0.1) < 3.0:
            continue
        out.append(fitz.Rect(r))
    return _merge_overlapping_rects(out, pad=4.0)


def _merge_overlapping_rects(rects: list[fitz.Rect], pad: float = 0.0) -> list[fitz.Rect]:
    if not rects:
        return []
    merged: list[fitz.Rect] = []
    for r in sorted(rects, key=lambda x: (x.y0, x.x0)):
        hit = False
        for i, m in enumerate(merged):
            e = fitz.Rect(m.x0 - pad, m.y0 - pad, m.x1 + pad, m.y1 + pad)
            if e.intersects(r):
                merged[i] = m | r
                hit = True
                break
        if not hit:
            merged.append(fitz.Rect(r))
    return merged


def _spans_in_rect(
    page: fitz.Page,
    box: fitz.Rect,
    *,
    pad: float = 3.0,
) -> list[tuple[str, fitz.Rect, float]]:
    search = fitz.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad)
    items: list[tuple[str, fitz.Rect, float]] = []
    for text, rect, size, *_ in _iter_page_lines(page):
        if search.intersects(rect):
            items.append((text, rect, size))
    items.sort(key=lambda t: (t[1].x0, t[1].y0))
    return items


def _parse_gdt_symbol(token: str) -> str:
    t = _clean(token).lower()
    if not t:
        return ""
    if t in _GDT_SYMBOL_ALIASES:
        return _GDT_SYMBOL_ALIASES[t]
    if len(t) == 1 and t.isalpha():
        return _GDT_SYMBOL_ALIASES.get(t, t)
    for ch in t:
        if ch in _GDT_SYMBOL_ALIASES:
            return _GDT_SYMBOL_ALIASES[ch]
    return t


def _split_datums(s: str) -> tuple[str, str, str]:
    s = _clean(s).upper().replace(" ", "")
    if not s:
        return "", "", ""
    if "|" in s:
        parts = [p for p in s.split("|") if p]
    else:
        parts = list(re.sub(r"[^A-Z]", "", s))
    d1 = parts[0] if len(parts) > 0 else ""
    d2 = parts[1] if len(parts) > 1 else ""
    d3 = parts[2] if len(parts) > 2 else ""
    return d1, d2, d3


def _parse_fcf_cells(cells: list[str]) -> GdtFcfRecord | None:
    if not cells:
        return None
    raw = " ".join(cells)
    sym = ""
    tol = ""
    datums = ""
    if len(cells) >= 3:
        sym, tol, datums = cells[0], cells[1], " ".join(cells[2:])
    elif len(cells) == 2:
        m = re.match(
            r"^([^\d]*?)\s*((?:0\.)?\d+(?:[.,]\d+)?)\s*(.*)$",
            " ".join(cells),
            re.IGNORECASE,
        )
        if m:
            sym, tol, datums = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
        else:
            sym, tol = cells[0], cells[1]
    else:
        m = re.match(
            r"^([^\d]*?)\s*((?:0\.)?\d+(?:[.,]\d+)?)\s*([A-Za-z|\s]*)$",
            cells[0],
            re.IGNORECASE,
        )
        if not m:
            return None
        sym, tol, datums = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
    tol = tol.replace(",", ".")
    if not tol:
        return None
    characteristic = _parse_gdt_symbol(sym)
    d1, d2, d3 = _split_datums(datums)
    frame_text = " | ".join(
        x for x in [characteristic or sym, tol, d1, d2, d3] if x
    )
    return GdtFcfRecord(
        page=0,
        x=0,
        y=0,
        width=0,
        height=0,
        characteristic=characteristic or sym,
        tolerance=tol,
        datum_primary=d1,
        datum_secondary=d2,
        datum_tertiary=d3,
        raw=raw,
        frame_text=frame_text,
    )


def extract_gdt_fcf(page: fitz.Page, page_index: int) -> list[GdtFcfRecord]:
    boxes = _fcf_box_rects(page)
    records: list[GdtFcfRecord] = []
    seen: set[str] = set()

    for box in boxes:
        spans = _spans_in_rect(page, box)
        if not spans:
            continue
        cells: list[str] = []
        tol_rect = fitz.Rect(spans[0][1])
        for text, rect, _size in spans:
            cells.append(_clean(text))
            tol_rect |= rect
        rec = _parse_fcf_cells(cells)
        if rec is None:
            continue
        rec.page = page_index + 1
        rec.x = float(tol_rect.x0)
        rec.y = float(tol_rect.y0)
        rec.width = float(tol_rect.width)
        rec.height = float(tol_rect.height)
        key = f"{rec.page}:{rec.frame_text}:{round(rec.x)}:{round(rec.y)}"
        if key in seen:
            continue
        seen.add(key)
        records.append(rec)

    # Merged single-span FCF rows (no visible box)
    for text, rect, _size, *_ in _iter_page_lines(page):
        m = re.match(
            r"^([n_Il1g⊥∠⌯⏤\u2212\- a-z]{0,3})\s*"
            r"((?:0\.)?\d+(?:[.,]\d+)?)\s*"
            r"([A-Z](?:\s*[|\-]\s*[A-Z]){0,2})?$",
            _clean(text),
            re.IGNORECASE,
        )
        if not m:
            continue
        tol = m.group(2).replace(",", ".")
        if tol in {"0.1", "0.4", "0.025"} and not m.group(3):
            continue
        datums = (m.group(3) or "").strip()
        sym = (m.group(1) or "").strip()
        characteristic = _parse_gdt_symbol(sym)
        d1, d2, d3 = _split_datums(datums)
        frame_text = " | ".join(x for x in [characteristic or sym, tol, d1, d2, d3] if x)
        key = f"{page_index + 1}:{frame_text}"
        if key in seen:
            continue
        seen.add(key)
        records.append(
            GdtFcfRecord(
                page=page_index + 1,
                x=float(rect.x0),
                y=float(rect.y0),
                width=float(rect.width),
                height=float(rect.height),
                characteristic=characteristic or sym,
                tolerance=tol,
                datum_primary=d1,
                datum_secondary=d2,
                datum_tertiary=d3,
                raw=text,
                frame_text=frame_text,
            )
        )
    return records


def classify_surface_text(text: str) -> tuple[str, str] | None:
    t = _clean(text)
    if not t:
        return None
    m = _SURFACE_N_GRADE.match(t)
    if m:
        return "n_grade", f"N{m.group(1)}"
    m = _SURFACE_RA.match(t)
    if m:
        return "ra", m.group(1).replace(",", ".")
    m = _SURFACE_RZ.match(t)
    if m:
        return "rz", m.group(1).replace(",", ".")
    if _SURFACE_RA_BARE.match(t):
        return "ra_bare", t.replace(",", ".")
    m = re.match(r"^([1-4])$", t)
    if m:
        return "n_grade", f"N{m.group(1)}"
    return None


def extract_surface_finish(
    page: fitz.Page,
    page_index: int,
    *,
    require_symbol_anchor: bool = False,
) -> list[SurfaceFinishRecord]:
    from app.services.autoballoon_engine import _page_bound, _roughness_anchors

    bound = _page_bound(page)
    anchors = _roughness_anchors(page) if require_symbol_anchor else []
    out: list[SurfaceFinishRecord] = []
    seen: set[str] = set()

    for text, rect, size, *_ in _iter_page_lines(page):
        cls = classify_surface_text(text)
        if cls is None:
            continue
        kind, value = cls
        if rect.x0 < bound.x0 + 25 and rect.x1 > bound.x1 - 25:
            if kind == "ra_bare":
                continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        if require_symbol_anchor and anchors:
            if not any(math.hypot(cx - a.x, cy - a.y) <= max(size, 6.0) * 3.5 for a in anchors):
                if kind in {"ra_bare", "n_grade"} and kind == "n_grade" and not re.match(r"^N([6-9]|1[0-2])", value, re.I):
                    continue
        key = f"{page_index + 1}:{kind}:{value}:{round(cx)}:{round(cy)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            SurfaceFinishRecord(
                page=page_index + 1,
                x=float(rect.x0),
                y=float(rect.y0),
                kind=kind,
                value=value,
                raw=text,
            )
        )
    return out


def extract_drawing_notes(page: fitz.Page, page_index: int) -> list[NoteRecord]:
    zones = _page_zones(page)
    notes_out: list[NoteRecord] = []
    seen: set[str] = set()

    blocks_lines: list[tuple[str, fitz.Rect, str]] = []
    for text, rect, _size, _dir, _bn, _li, raw in _iter_page_lines(page):
        tcx = (rect.x0 + rect.x1) / 2
        tcy = (rect.y0 + rect.y1) / 2
        zone = "drawing"
        if zones and _point_in_rects(tcx, tcy, zones.notes):
            zone = "notes"
        elif zones and _point_in_rects(tcx, tcy, zones.bom):
            zone = "bom"
        elif zones and _point_in_rects(tcx, tcy, zones.title):
            zone = "title"
        if zone not in {"notes", "bom"}:
            continue
        line = _clean(raw) or _clean(text)
        if len(line) < 2:
            continue
        blocks_lines.append((line, rect, zone))

    paragraph_buf: list[str] = []
    buf_rect = None
    buf_zone = "notes"
    for line, rect, zone in blocks_lines:
        m = _NOTE_NUMBERED.match(line)
        if m and paragraph_buf and buf_rect is not None:
            body = " ".join(paragraph_buf).strip()
            key = f"{page_index + 1}:{body[:48]}"
            if key not in seen and len(body) > 3:
                seen.add(key)
                notes_out.append(
                    NoteRecord(
                        page=page_index + 1,
                        x=float(buf_rect.x0),
                        y=float(buf_rect.y0),
                        note_no="",
                        text=body,
                        zone=buf_zone,
                    )
                )
            paragraph_buf = []
            buf_rect = None
        if m:
            note_no, body = m.group(1), m.group(2).strip()
            key = f"{page_index + 1}:{note_no}:{body[:40]}"
            if key not in seen:
                seen.add(key)
                notes_out.append(
                    NoteRecord(
                        page=page_index + 1,
                        x=float(rect.x0),
                        y=float(rect.y0),
                        note_no=note_no,
                        text=body,
                        zone=zone,
                    )
                )
            continue
        paragraph_buf.append(line)
        buf_rect = rect if buf_rect is None else (buf_rect | rect)
        buf_zone = zone

    if paragraph_buf and buf_rect is not None:
        body = " ".join(paragraph_buf).strip()
        key = f"{page_index + 1}:{body[:48]}"
        if key not in seen and len(body) > 8:
            seen.add(key)
            notes_out.append(
                NoteRecord(
                    page=page_index + 1,
                    x=float(buf_rect.x0),
                    y=float(buf_rect.y0),
                    note_no="",
                    text=body,
                    zone=buf_zone,
                )
            )
    return notes_out


def extract_vector_drawing_data(doc: fitz.Document) -> dict:
    gdt: list[dict] = []
    surface: list[dict] = []
    notes: list[dict] = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        for rec in extract_gdt_fcf(page, page_index):
            gdt.append(
                {
                    "page": rec.page,
                    "x": rec.x,
                    "y": rec.y,
                    "width": rec.width,
                    "height": rec.height,
                    "characteristic": rec.characteristic,
                    "tolerance": rec.tolerance,
                    "datum_primary": rec.datum_primary,
                    "datum_secondary": rec.datum_secondary,
                    "datum_tertiary": rec.datum_tertiary,
                    "frame_text": rec.frame_text,
                    "raw": rec.raw,
                }
            )
        for rec in extract_surface_finish(page, page_index, require_symbol_anchor=False):
            surface.append(
                {
                    "page": rec.page,
                    "x": rec.x,
                    "y": rec.y,
                    "kind": rec.kind,
                    "value": rec.value,
                    "raw": rec.raw,
                }
            )
        for rec in extract_drawing_notes(page, page_index):
            notes.append(
                {
                    "page": rec.page,
                    "x": rec.x,
                    "y": rec.y,
                    "note_no": rec.note_no,
                    "text": rec.text,
                    "zone": rec.zone,
                }
            )
    return {"gdt_fcf": gdt, "surface_finish": surface, "notes": notes}


def characteristics_from_extraction(
    extraction: dict,
    existing_texts: set[str],
) -> list:
    """Build extra Characteristics for GD&T + surface (for ballooning)."""
    from app.services.autoballoon_engine import Characteristic

    out = []
    for row in extraction.get("gdt_fcf") or []:
        label = str(row.get("frame_text") or row.get("raw") or "").strip()
        if not label or label in existing_texts:
            continue
        existing_texts.add(label)
        out.append(
            Characteristic(
                balloon=0,
                page=int(row.get("page") or 1),
                text=label,
                x=float(row.get("x") or 0),
                y=float(row.get("y") or 0),
                width=float(row.get("width") or 24.0),
                height=12.0,
                category="gdt",
                pattern="gdt_fcf",
            )
        )
    for row in extraction.get("surface_finish") or []:
        kind = str(row.get("kind") or "")
        val = str(row.get("value") or "")
        raw = str(row.get("raw") or val)
        label = raw if raw else f"{kind}:{val}"
        if label in existing_texts:
            continue
        existing_texts.add(label)
        out.append(
            Characteristic(
                balloon=0,
                page=int(row.get("page") or 1),
                text=label,
                x=float(row.get("x") or 0),
                y=float(row.get("y") or 0),
                width=20.0,
                height=12.0,
                category="surface",
                pattern="surface_finish_ext",
            )
        )
    return out


def append_extraction_sheets(wb, extraction: dict | None) -> None:
    if not extraction:
        return
    from openpyxl.styles import Alignment, Font

    def _sheet(name: str, headers: list[str], rows: list[list]):
        ws = wb.create_sheet(title=name[:31])
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append(row)
        for i, h in enumerate(headers, start=1):
            from openpyxl.utils import get_column_letter

            ws.column_dimensions[get_column_letter(i)].width = min(60, max(12, len(h) + 2))
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    gdt_rows = []
    for r in extraction.get("gdt_fcf") or []:
        gdt_rows.append(
            [
                r.get("page"),
                round(float(r.get("x") or 0), 1),
                round(float(r.get("y") or 0), 1),
                r.get("characteristic"),
                r.get("tolerance"),
                r.get("datum_primary"),
                r.get("datum_secondary"),
                r.get("datum_tertiary"),
                r.get("frame_text"),
            ]
        )
    if gdt_rows:
        _sheet(
            "GD&T FCF",
            [
                "Sheet",
                "X",
                "Y",
                "Characteristic",
                "Tolerance",
                "Datum 1",
                "Datum 2",
                "Datum 3",
                "Frame",
            ],
            gdt_rows,
        )

    surf_rows = []
    for r in extraction.get("surface_finish") or []:
        surf_rows.append(
            [
                r.get("page"),
                round(float(r.get("x") or 0), 1),
                round(float(r.get("y") or 0), 1),
                r.get("kind"),
                r.get("value"),
                r.get("raw"),
            ]
        )
    if surf_rows:
        _sheet(
            "Surface Finish",
            ["Sheet", "X", "Y", "Type", "Value", "Raw text"],
            surf_rows,
        )

    note_rows = []
    for r in extraction.get("notes") or []:
        note_rows.append(
            [
                r.get("page"),
                r.get("note_no"),
                r.get("zone"),
                round(float(r.get("x") or 0), 1),
                round(float(r.get("y") or 0), 1),
                r.get("text"),
            ]
        )
    if note_rows:
        _sheet(
            "Drawing Notes",
            ["Sheet", "Note #", "Zone", "X", "Y", "Text"],
            note_rows,
        )


def build_extraction_xlsx(extraction: dict) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["GD&T FCF count", len(extraction.get("gdt_fcf") or [])])
    ws.append(["Surface finish count", len(extraction.get("surface_finish") or [])])
    ws.append(["Drawing notes count", len(extraction.get("notes") or [])])
    append_extraction_sheets(wb, extraction)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()