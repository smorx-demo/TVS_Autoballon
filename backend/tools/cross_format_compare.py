"""
Cross-format comparison: the same part's TVS extraction from PDF (reference), DXF and STEP AP242 PMI.

    python -m tools.cross_format_compare OUT_DIR drawing.pdf drawing.dxf part_v1.step [part_v3.step ...]
           [--old OLD_STEP_REPORT.xlsx]

Rows are paired with the PDF report by the scored one-to-one matcher (values, never text). Per field,
the table counts how many of the PDF report's characteristics the other output reproduces exactly.
Writes OUT_DIR/cross_format_comparison.xlsx and prints the summary.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from app.services.step_report import build_step_report, drawing_pdf, match, step_report_xlsx
from app.services.tvs_extraction_report import _extraction_rows

FIELDS = ["Balloon No.", "Sheet", "Zone", "View", "Feature type", "Symbol", "Nominal", "Upper tolerance",
          "Lower tolerance", "Max limit", "Min limit", "Primary datum", "Secondary datum", "Tertiary datum",
          "Boxed / reference flag", "Critical flag"]


def _xlsx_rows(path: Path) -> list[dict]:
    ws = openpyxl.load_workbook(path, data_only=True)["Extraction"]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    return [dict(zip(hdr, r)) for r in it]


def compare(ref: list[dict], other: list[dict]) -> tuple[dict[str, int], int, list[tuple]]:
    pairs = match(other, ref)
    agree = {f: 0 for f in FIELDS}
    detail = []
    for i, j, _c in pairs:
        o, r = other[i], ref[j]
        for f in FIELDS:
            same = str(o.get(f)) == str(r.get(f))
            agree[f] += same
            if not same:
                detail.append((r["Balloon No."], r["Text as shown on drawing"], f, r.get(f), o.get(f)))
    return agree, len(pairs), detail


def main(argv: list[str]) -> int:
    old = None
    if "--old" in argv:
        k = argv.index("--old")
        old = Path(argv[k + 1])
        argv = argv[:k] + argv[k + 2:]
    out_dir, pdf, dxf, steps = Path(argv[0]), Path(argv[1]), Path(argv[2]), [Path(a) for a in argv[3:]]
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_rows = _extraction_rows(pdf)
    ref = ref_rows[0]
    columns: list[tuple[str, list[dict]]] = []
    with tempfile.TemporaryDirectory() as td:
        columns.append(("DXF", _extraction_rows(drawing_pdf(dxf, Path(td)))[0]))
    if old is not None:
        columns.append(("STEP (old report)", _xlsx_rows(old)))
    for st in steps:
        tag = st.stem.replace(pdf.stem, "").strip("_") or st.stem
        paired = build_step_report(st, pdf, drawing_rows=ref_rows)
        alone = build_step_report(st)
        (out_dir / f"{st.stem}_paired_PDF_TVS_extraction_report.xlsx").write_bytes(step_report_xlsx(paired, {}, pdf))
        (out_dir / f"{st.stem}_paired_DXF_TVS_extraction_report.xlsx").write_bytes(
            step_report_xlsx(build_step_report(st, dxf), {}, pdf))
        (out_dir / f"{st.stem}_STEP_only_TVS_extraction_report.xlsx").write_bytes(step_report_xlsx(alone, {}))
        columns.append((f"STEP {tag} paired", [{k: v for k, v in r.items() if k != "_item"} for r in paired.features]))
        columns.append((f"STEP {tag} STEP-only", [{k: v for k, v in r.items() if k != "_item"} for r in alone.features]))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Agreement"
    ws.append([f"Agreement with the PDF report ({len(ref)} characteristics); rows paired by value (one-to-one)"])
    ws["A1"].font = Font(bold=True)
    ws.append(["Field"] + [name for name, _ in columns])
    results = [(name, *compare(ref, rows)) for name, rows in columns]
    ws.append(["Characteristics paired"] + [f"{n}/{len(ref)}" for _name, _agree, n, _d in results])
    for f in FIELDS:
        ws.append([f] + [f"{agree[f]}/{len(ref)}" for _name, agree, _n, _d in results])
    print("\t".join(["Field"] + [name for name, _ in columns]))
    for row in ws.iter_rows(min_row=3, values_only=True):
        print("\t".join(str(x) for x in row))
    for name, _agree, _n, detail in results:
        ds = wb.create_sheet(name[:31])
        ds.append(["Balloon No. (PDF)", "Text", "Field", "PDF", name])
        for d in detail:
            ds.append([str(x) if x is not None else None for x in d])
    wb.save(out_dir / "cross_format_comparison.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
