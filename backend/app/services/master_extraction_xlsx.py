"""Single master Excel — full extraction + coordinates (15-point deliverable)."""

from __future__ import annotations

import io
import json
from typing import Any


def _sheet(wb, title: str, headers: list[str], rows: list[list[Any]]):
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title=title[:31])
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
    for row in rows:
        ws.append(row)
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(55, max(10, len(str(h)) + 2))
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap


def _tolerance_lookup(meta: dict) -> dict:
    """(page, text) -> report row of its balloon (tolerance + source)."""
    rows = {(int(r.get("page") or 1), int(r.get("balloon") or 0)): r for r in meta.get("report_rows") or []}
    out = {}
    for b in meta.get("balloons") or []:
        r = rows.get((int(b.get("page") or 1), int(b.get("balloon") or 0)))
        if r is not None:
            out.setdefault((int(b.get("page") or 1), str(b.get("text") or "").replace(" ", "")), r)
    return out


def build_master_extraction_xlsx(meta: dict, full: dict | None = None) -> bytes:
    from openpyxl import Workbook

    full = full or meta.get("full_extraction") or {}
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    counts = full.get("counts") or {}
    summary.append(["Field", "Value"])
    summary.append(["Part Number", meta.get("part_number", "")])
    summary.append(["Part Name", meta.get("part_name", "")])
    summary.append(["Revision", meta.get("revision", "")])
    summary.append(["Dimensions extracted", counts.get("dimensions", 0)])
    summary.append(["Text lines", counts.get("text_lines", 0)])
    summary.append(["Notes", counts.get("notes", 0)])
    summary.append(["GD&T FCF", counts.get("gdt_fcf", 0)])
    summary.append(["Surface finish", counts.get("surface_finish", 0)])
    summary.append(["Critical parameters", counts.get("critical", 0)])
    summary.append(["Major parameters", counts.get("major", 0)])
    mm = full.get("model_3d_manifest") or {}
    summary.append(["3D/PVZ manifest", mm.get("summary", "")])

    from app.services.report_pdf import tolerance_source_label

    tol_by = _tolerance_lookup(meta)
    dim_rows, auto_rows = [], []
    for d in full.get("dimensions") or []:
        r = tol_by.get((int(d.get("page") or 1), str(d.get("text") or "").replace(" ", ""))) or {}
        dim_rows.append([
            d.get("page"),
            d.get("x"),
            d.get("y"),
            d.get("text"),
            d.get("category"),
            d.get("pattern"),
            r.get("tol_low", ""),
            r.get("tol_high", ""),
            tolerance_source_label(r) if r else "",
        ])
        auto_rows.append(bool(r.get("tol_auto")))
    _sheet(
        wb,
        "All_Dimensions",
        ["Sheet", "X", "Y", "Text", "Category", "Pattern", "Tol low", "Tol high", "Tolerance source"],
        dim_rows,
    )
    from openpyxl.styles import Font

    ws_dims = wb["All_Dimensions"]
    for i, auto in enumerate(auto_rows, start=2):
        if auto:
            for col in (7, 8, 9):
                ws_dims.cell(row=i, column=col).font = Font(italic=True, color="1F4E79")

    gdt_rows = [
        [
            g.get("page"),
            g.get("x"),
            g.get("y"),
            g.get("characteristic"),
            g.get("tolerance"),
            g.get("modifier"),
            g.get("datum_primary"),
            g.get("datum_secondary"),
            g.get("datum_tertiary"),
            g.get("frame_text"),
            g.get("source"),
        ]
        for g in full.get("gdt_fcf") or []
    ]
    _sheet(
        wb,
        "GD&T_FCF",
        [
            "Sheet",
            "X",
            "Y",
            "Characteristic",
            "Tolerance",
            "Modifier",
            "Datum 1",
            "Datum 2",
            "Datum 3",
            "Frame",
            "Source",
        ],
        gdt_rows,
    )

    sf_rows = [
        [s.get("page"), s.get("x"), s.get("y"), s.get("kind"), s.get("value"), s.get("text")]
        for s in full.get("surface_finish") or []
    ]
    _sheet(wb, "Surface_Finish", ["Sheet", "X", "Y", "Type", "Value", "Raw"], sf_rows)

    note_rows = [
        [n.get("page"), n.get("x"), n.get("y"), n.get("note_no"), n.get("zone"), n.get("text")]
        for n in full.get("notes") or []
    ]
    _sheet(wb, "All_Notes", ["Sheet", "X", "Y", "Note #", "Zone", "Text"], note_rows)

    text_rows = [
        [t.get("page"), t.get("x"), t.get("y"), t.get("zone"), t.get("font_size"), t.get("text")]
        for t in full.get("all_text") or []
    ]
    _sheet(wb, "All_Text", ["Sheet", "X", "Y", "Zone", "Font", "Text"], text_rows)

    crit_rows = [
        [
            c.get("page"),
            c.get("x"),
            c.get("y"),
            c.get("priority"),
            c.get("score"),
            ",".join(c.get("reasons") or []),
            c.get("text"),
            c.get("category"),
        ]
        for c in full.get("critical_major") or []
        if c.get("priority") in {"critical", "major"}
    ]
    _sheet(
        wb,
        "Critical_Major",
        ["Sheet", "X", "Y", "Priority", "Score", "Reasons", "Text", "Category"],
        crit_rows,
    )

    manifest = full.get("model_3d_manifest") or {}
    man_rows = [[json.dumps(s, ensure_ascii=False)[:32000]] for s in manifest.get("sources") or []]
    if not man_rows:
        man_rows = [["(no STEP/PVZ source — PDF-only job)"]]
    _sheet(wb, "Model_3D_PVZ", ["Source manifest JSON"], man_rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()