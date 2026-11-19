"""
Ballooned PDF + TVS extraction Excel + inspection report for a drawing, the same way the app
builds them, plus a read-back of the ballooned PDF (balloon numbers, circles, leader end points)
used by the consistency / overlap checks.

    python -m tools.balloon_deliverables OUT_DIR drawing.pdf [drawing2.pdf ...]

writes, per drawing: <name>_ballooned.pdf, <name>_TVS_extraction_report.xlsx,
<name>_inspection_report.pdf, <name>_ballooned_sheet<N>.png, and prints the check results.
"""

from __future__ import annotations

import io
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz

BALLOON_FILL = (1.0, 0.97, 0.75)
BALLOON_RED = (0.85, 0.05, 0.05)


def _near(c, ref, tol=0.02) -> bool:
    return c is not None and len(c) == 3 and all(abs(a - b) <= tol for a, b in zip(c, ref))


# ----------------------------------------------------------------------------- pipeline

def build_all(src: Path) -> dict:
    """Run the app pipeline for one drawing; returns bytes of every output and the balloon list."""
    from app.services.autoballoon_engine import balloon_pdf
    from app.services.border_grid import zone_references_for_balloons
    from app.services.report_pdf import build_report_pdf, characteristics_to_report_rows, default_measure_columns
    from app.services.title_block_extract import extract_title_block_meta, merge_title_meta
    from app.services.tvs_extraction_report import build_tvs_extraction_xlsx

    out_pdf, chars, _log, _ext = balloon_pdf(src.read_bytes())
    balloons = [{"balloon": c.balloon, "page": c.page, "text": c.text, "x": c.x, "y": c.y, "width": c.width,
                 "height": c.height, "category": c.category, "pattern": c.pattern, "bx": c.bx, "by": c.by,
                 "tx": c.tx, "ty": c.ty, "arrow_size": c.arrow_size} for c in chars]
    rows = characteristics_to_report_rows(balloons, zone_references_for_balloons(src, balloons))
    meta: dict = {}
    merge_title_meta(meta, extract_title_block_meta(src))
    xlsx = build_tvs_extraction_xlsx(meta, src)
    payload = {k: meta.get(k, "") for k in ("part_number", "part_name", "revision", "material", "mass", "finish",
                                            "serial_number", "report_date", "company_name", "supplier_name",
                                            "customer_name", "authorized_signatory")}
    payload.update(rows=rows, measure_columns=default_measure_columns(), version_highlight={})
    return {"ballooned": out_pdf, "xlsx": xlsx, "report": build_report_pdf(payload), "meta": meta,
            "balloons": balloons, "report_rows": rows}


# ----------------------------------------------------------------------------- read-back

@dataclass
class DrawnBalloon:
    sheet: int
    number: int
    center: fitz.Point
    radius: float
    tip: fitz.Point | None = None
    start: fitz.Point | None = None


def read_balloons(pdf_bytes: bytes) -> list[DrawnBalloon]:
    """Balloons as drawn in the PDF: circle (fill colour), number (text inside), leader end point."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out: list[DrawnBalloon] = []
    try:
        for pno, page in enumerate(doc, start=1):
            circles, leaders = [], []
            for d in page.get_drawings():
                items = d.get("items") or []
                if _near(d.get("fill"), BALLOON_FILL) and items and all(it[0] == "c" for it in items):
                    r = d["rect"]
                    circles.append((fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2), (r.width + r.height) / 4))
                elif (_near(d.get("color"), BALLOON_RED) and d.get("fill") is None and items
                      and all(it[0] == "l" for it in items)):
                    # one stroked line (PyMuPDF may close it back to its start: first item is the leader)
                    leaders.append((fitz.Point(items[0][1]), fitz.Point(items[0][2])))
            words = [(w[4], fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for w in page.get_text("words")]
            for c, r in circles:
                num = [t for t, p in words if t.isdigit() and math.hypot(p.x - c.x, p.y - c.y) < r * 0.8]
                if len(num) != 1:
                    continue
                b = DrawnBalloon(pno, int(num[0]), c, r)
                for a, z in leaders:
                    if abs(math.hypot(a.x - c.x, a.y - c.y) - r) <= 1.5:
                        b.start, b.tip = a, z
                out.append(b)
    finally:
        doc.close()
    return out


def annotation_boxes(src: Path) -> list[tuple]:
    """(sheet, balloon number, annotation box, text, centre) for every extracted feature."""
    from app.services.balloon_layout import annotation_box
    from app.services.tvs_extractor import PageData, _extract

    doc = fitz.open(str(src))
    try:
        ext = _extract(doc)
        pds = {}
        out = []
        for f in ext.features:
            pd = pds.setdefault(f.sheet, PageData(doc[f.sheet - 1], f.sheet - 1))
            out.append((f.sheet, f.balloon, annotation_box(pd, f), f.text, f.center))
        return out
    finally:
        doc.close()


def _rect_dist(p: fitz.Point, r: fitz.Rect) -> float:
    return math.hypot(max(r.x0 - p.x, 0, p.x - r.x1), max(r.y0 - p.y, 0, p.y - r.y1))


def consistency(src: Path, built: dict, tip_tol: float = 1.0) -> dict:
    """Excel balloon number vs the balloon whose leader ends on the feature's annotation box."""
    import openpyxl

    from app.services.tvs_extractor import PT_TO_MM

    drawn = read_balloons(built["ballooned"])
    boxes = annotation_boxes(src)
    ws = openpyxl.load_workbook(io.BytesIO(built["xlsx"]), data_only=True)["Extraction"]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    rows = [dict(zip(hdr, r)) for r in it]
    doc = fitz.open(str(src))
    page_rects = [p.rect for p in doc]
    doc.close()
    ok, bad = 0, []
    for row in rows:
        sheet = int(row["Sheet"])
        pr = page_rects[sheet - 1]
        # the Excel row's feature: same sheet, same centre (mm from lower-left), same text
        cand = [b for b in boxes if b[0] == sheet and b[3] == row["Text as shown on drawing"]
                and abs((b[4].x - pr.x0) * PT_TO_MM - float(row["X (mm)"])) <= 0.1
                and abs((pr.y1 - b[4].y) * PT_TO_MM - float(row["Y (mm)"])) <= 0.1]
        if len(cand) != 1:
            bad.append((row["Balloon No."], row["Text as shown on drawing"], "feature not identified"))
            continue
        box = cand[0][2]
        # leader that ends on this box (and on no nearer box)
        hits = []
        for b in drawn:
            if b.sheet != sheet or b.tip is None:
                continue
            d = _rect_dist(b.tip, box)
            nearest = min(_rect_dist(b.tip, o[2]) for o in boxes if o[0] == sheet)
            if d <= tip_tol and d <= nearest + 1e-6:
                hits.append(b.number)
        if hits == [row["Balloon No."]]:
            ok += 1
        else:
            bad.append((row["Balloon No."], row["Text as shown on drawing"], f"leaders ending on it: {hits}"))
    return {"rows": len(rows), "drawn": len(drawn), "ok": ok, "bad": bad}


def overlaps(src: Path, built: dict) -> list[str]:
    """Balloon circles over annotation boxes / text / other balloons; leaders ending on a balloon."""
    drawn = read_balloons(built["ballooned"])
    boxes = annotation_boxes(src)
    doc = fitz.open(str(src))
    texts = {p.number + 1: [fitz.Rect(w[:4]) for w in p.get_text("words")] for p in doc}
    doc.close()
    v = []
    for b in drawn:
        for s, n, box, text, _c in boxes:
            if s == b.sheet and _rect_dist(b.center, box) < b.radius:
                v.append(f"sheet {b.sheet} balloon {b.number} covers annotation of balloon {n} ({text})")
        for r in texts[b.sheet]:
            if _rect_dist(b.center, r) < b.radius:
                v.append(f"sheet {b.sheet} balloon {b.number} covers drawing text at {tuple(round(x) for x in r)}")
        for o in drawn:
            if o is b or o.sheet != b.sheet:
                continue
            if math.hypot(o.center.x - b.center.x, o.center.y - b.center.y) < o.radius + b.radius:
                v.append(f"sheet {b.sheet} balloons {b.number} and {o.number} overlap")
            if b.tip is not None and math.hypot(b.tip.x - o.center.x, b.tip.y - o.center.y) <= o.radius:
                v.append(f"sheet {b.sheet} leader of balloon {b.number} ends on balloon {o.number}")
    return sorted(set(v))


def render_png(pdf_bytes: bytes, out_dir: Path, stem: str, dpi: int = 110) -> list[Path]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    paths = []
    try:
        for i, page in enumerate(doc, start=1):
            p = out_dir / f"{stem}_ballooned_sheet{i}.png"
            page.get_pixmap(dpi=dpi).save(str(p))
            paths.append(p)
    finally:
        doc.close()
    return paths


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    for a in argv[1:]:
        src = Path(a)
        built = build_all(src)
        stem = src.stem
        (out_dir / f"{stem}_ballooned.pdf").write_bytes(built["ballooned"])
        (out_dir / f"{stem}_TVS_extraction_report.xlsx").write_bytes(built["xlsx"])
        (out_dir / f"{stem}_inspection_report.pdf").write_bytes(built["report"])
        pngs = render_png(built["ballooned"], out_dir, stem)
        c = consistency(src, built)
        ov = overlaps(src, built)
        m = built["meta"]
        print(f"{stem}: consistency {c['ok']}/{c['rows']} (balloons drawn {c['drawn']}); overlap violations {len(ov)}")
        for b in c["bad"]:
            print("   mismatch:", b)
        for x in ov:
            print("   overlap:", x)
        print(f"   title block: part name={m.get('part_name')!r} material={m.get('material')!r} mass={m.get('mass')!r}")
        print("   wrote:", ", ".join(p.name for p in pngs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
