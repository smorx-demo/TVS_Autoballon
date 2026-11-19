"""
TVS extraction report for STEP AP242 PMI.

Values (nominal, tolerances, limits, details, datums, flags) come from the semantic PMI through the
same characteristic parser as drawings: the PMI is rendered as an annotation list (step_pmi) and read
back by the drawing extractor. That sheet is only a parsing medium — its layout never reaches the
report.

Two modes:

  paired     a 2D drawing (PDF, or DXF) of the same part is available — given explicitly or found by
             drawing / part number. Each STEP PMI is matched one-to-one (scored assignment) to a 2D
             characteristic; the drawing supplies Balloon No., Feature ID, Sheet, Zone, X/Y, View,
             Critical flag and Feature type. STEP-only PMI are numbered after the highest drawing
             balloon (Zone 'N/A (3D only)'); drawing characteristics without PMI are listed in the
             Summary with the reason.

  STEP-only  Sheet '3D', Zone 'N/A (3D model)', X/Y blank, View = saved view (camera model) or
             'Model'. Balloon numbers (deterministic): saved view in file order → PMI group
             (dimensions, GD&T, datums, surface finishes) → 3D anchor Z descending, Y descending,
             X ascending → entity #id.

STEP identity (PMI entity #id, saved view, faces, 3D anchor) is written to the Notes column.
"""

from __future__ import annotations

import logging
import math
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from app.services.step_pmi import PmiItem, PmiModel, read_pmi, render_pmi_sheet

_LOG = logging.getLogger(__name__)

PT_TO_MM = 25.4 / 72.0
ZONE_3D = "N/A (3D model)"
ZONE_3D_ONLY = "N/A (3D only)"
SHEET_3D = "3D"
INF = 1e6
_GROUP = {"dim": 0, "thread": 0, "chamfer": 0, "gdt": 1, "datum": 2, "surface": 3}

DATUM_COLS = ("Primary datum", "Secondary datum", "Tertiary datum")
DETAILS = "Fit / thread / finish / GD&T details (incl. datums)"


@dataclass
class StepReport:
    mode: str                                   # 'paired' | 'step-only'
    product: str
    features: list[dict]
    notes: list[dict]
    drawing: Path | None = None
    step_only: list[dict] = field(default_factory=list)       # paired mode: STEP PMI without a 2D match
    missing: list[tuple[dict, str]] = field(default_factory=list)  # paired mode: 2D rows without PMI, reason
    model: PmiModel | None = None


# ----------------------------------------------------------------------------- STEP rows

def _fmt3(p) -> str:
    return "(" + ", ".join(f"{v:.3f}".rstrip("0").rstrip(".") for v in p) + ")"


def step_context(it: PmiItem) -> str:
    bits = [f"STEP PMI #{it.eid}"]
    if it.view:
        bits.append(f"saved view {it.view}")
    if it.faces:
        bits.append("faces " + ", ".join(f"#{f}" for f in it.faces))
    if it.anchor is not None:
        bits.append(f"3D anchor {_fmt3(it.anchor)} mm ({it.anchor_src})")
    if it.source and it.kind == "surface":
        bits.append(f"found in: {it.source}")
    return "; ".join(bits)


def _strip_layout_notes(notes) -> list[str]:
    """Notes produced by the parsing sheet's layout (border proximity) mean nothing for 3D PMI."""
    out = []
    for n in str(notes or "").split("; "):
        if n and not n.startswith("Placed near"):
            out.append(n)
    return out


def step_rows(step_path: Path, model: PmiModel | None = None) -> tuple[list[dict], PmiModel]:
    """One report row per STEP PMI (values parsed exactly as for a drawing) with its PmiItem in '_item'."""
    from app.services.tvs_extraction_report import _extraction_rows

    model = model or read_pmi(step_path)
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "pmi.pdf"
        render_pmi_sheet(model, pdf)
        rows, _notes, _n, _size, _orph = _extraction_rows(pdf)
        import fitz
        doc = fitz.open(str(pdf))
        page_h = {i: doc[i].rect.height for i in range(doc.page_count)}
        doc.close()
    out = []
    taken: set[int] = set()
    for r in rows:
        pg = int(r["Sheet"]) - 1
        x = float(r["X (mm)"]) / PT_TO_MM
        y = page_h[pg] - float(r["Y (mm)"]) / PT_TO_MM
        it = next((i for i in model.items if i.slot and i.slot[0] == pg and i.slot[1].contains((x, y))
                   and id(i) not in taken), None)
        if it is None:
            _LOG.warning("STEP report: parsed characteristic %r maps to no PMI item", r["Text as shown on drawing"])
            continue
        taken.add(id(it))
        row = OrderedDict(r)
        row["_item"] = it
        out.append(row)
    for it in model.items:
        if id(it) not in taken:
            _LOG.warning("STEP report: PMI #%s (%s) was not parsed", it.eid, it.text or it.gdt)
    _thread_depths(out)
    return out, model


_THREAD_DEPTH = re.compile(r"^(?:\d+\s*×\s*)?M(?P<size>\d+(?:[.,]\d+)?)[^↧/]*↧\s*(?P<depth>\d+(?:[.,]\d+)?)")
_DRILL_DEPTH = re.compile(r"Ø\s*(?P<d>\d+(?:[.,]\d+)?)\s*↧\s*(?P<depth>\d+(?:[.,]\d+)?)")


def _thread_depths(rows: list[dict]) -> None:
    """A linear location on the same hole axes as a thread callout whose value is that thread's
    full-thread depth or tap-drill depth is a depth dimension (↧) — the 3D counterpart of the drawing
    rule that links section dimensions to the thread they cut."""
    threads = [r for r in rows if r["_item"].kind == "thread" and r["_item"].axes]
    for r in rows:
        it = r["_item"]
        if it.kind != "dim" or r["Feature type"] not in ("Linear dimension", "Inclined linear dimension") \
                or r.get("Symbol") or not isinstance(r["Nominal"], (int, float)):
            continue
        for t in threads:
            if not set(t["_item"].axes) <= set(it.axes):
                continue
            txt = str(t["Text as shown on drawing"])
            m = _THREAD_DEPTH.match(txt)
            dm = _DRILL_DEPTH.search(txt)
            size = re.sub(r"[.,]0+$", "", m.group("size")) if m else ""
            if m and float(m.group("depth")) == float(r["Nominal"]):
                note = f"M{size} full-thread depth"
            elif dm and float(dm.group("depth")) == float(r["Nominal"]):
                note = f"M{size} tap drill Ø{dm.group('d')} depth"
            else:
                continue
            r["Symbol"] = "↧"
            r[DETAILS] = "; ".join(x for x in (note, r[DETAILS]) if x)
            break


def _three_d_fields(row: dict, zone: str) -> None:
    it: PmiItem = row["_item"]
    row["Sheet"] = SHEET_3D
    row["Zone"] = zone
    row["X (mm)"] = None
    row["Y (mm)"] = None
    row["View"] = it.view or "Model"
    if it.inclined and row["Feature type"] == "Linear dimension":
        row["Feature type"] = "Inclined linear dimension"
    if it.pcd and str(row["Feature type"]).startswith("Basic dimension") and row["Feature type"] != "Basic dimension (PCD)":
        row["Feature type"] = "Basic dimension (PCD)"
        row[DETAILS] = "; ".join(x for x in (f"Pitch circle of {it.pcd}× feature pattern", "theoretically exact") if x)
    row["Critical flag"] = it.critical or "-"
    row["Notes"] = "; ".join(_strip_layout_notes(row["Notes"]) + [step_context(it)]) or None


def step_order_key(it: PmiItem) -> tuple:
    """Documented STEP-only numbering order (deterministic for a given file)."""
    view = it.view_order if it.view_order else 10 ** 6
    a = it.anchor or (0.0, 0.0, 0.0)
    return (view, _GROUP.get(it.kind, 9), -round(a[2], 4), -round(a[1], 4), round(a[0], 4), it.eid)


# ----------------------------------------------------------------------------- matching

def family(ftype: str) -> str:
    t = str(ftype or "")
    if t.startswith("GD&T"):
        return "gdt"
    if t == "Datum feature":
        return "datum"
    if t == "Surface finish":
        return "surface"
    if t == "Thread":
        return "thread"
    return "dim"


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _same_value(a, b) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None or nb is not None:
        return na is not None and nb is not None and abs(na - nb) <= 1e-6
    return re.sub(r"\s+", "", str(a or "")).upper() == re.sub(r"\s+", "", str(b or "")).upper()


_QTY = re.compile(r"^\s*(\d+)\s*[×xX]\s*")
_DESIG = re.compile(r"\bM\d+(?:[.,]\d+)?(?:\s*[×xX]\s*\d+(?:[.,]\d+)?)?(?:-\w+)?|\b[A-Za-z]{1,2}\d{1,2}\b(?=\s|$|[+\-/])")


def quantity(text: str) -> int:
    m = _QTY.match(str(text or ""))
    return int(m.group(1)) if m else 1


def designations(row: dict) -> set[str]:
    txt = f"{row.get('Text as shown on drawing') or ''}"
    return {re.sub(r"\s+", "", d).upper() for d in _DESIG.findall(txt)}


def modifiers(row: dict) -> set[str]:
    t = f"{row.get('Text as shown on drawing') or ''} {row.get(DETAILS) or ''}"
    out = set()
    if re.search(r"Ⓜ|\bMMC\b|maximum material", t, re.I):
        out.add("MMC")
    if re.search(r"Ⓛ|\bLMC\b|least material", t, re.I):
        out.add("LMC")
    return out


def drawn_symbol(row: dict) -> str:
    """The characteristic's symbol as an annotation shows it. A depth symbol (↧) inferred from context
    (a section through a thread) is not part of the annotation unless the text itself carries it."""
    sym = row.get("Symbol") or ""
    if sym == "↧" and "↧" not in str(row.get("Text as shown on drawing") or ""):
        return ""
    return sym


def critical_agree(a: dict, b: dict) -> bool:
    """Nominal, tolerances, symbol and datum references must agree exactly (and the family)."""
    if family(a["Feature type"]) != family(b["Feature type"]):
        return False
    if not _same_value(a["Nominal"], b["Nominal"]):
        return False
    for k in ("Upper tolerance", "Lower tolerance"):
        if not _same_value(a.get(k), b.get(k)):
            return False
    if drawn_symbol(a) != drawn_symbol(b):
        return False
    return all((a.get(k) or "") == (b.get(k) or "") for k in DATUM_COLS)


def pair_cost(a: dict, b: dict) -> float:
    """Cost of pairing STEP row a with 2D row b; INF when a critical field differs.
    Never based on text equality alone: only typed fields are compared."""
    if not critical_agree(a, b):
        return INF
    c = 0.0
    if designations(a) != designations(b):
        c += 3.0
    if modifiers(a) != modifiers(b):
        c += 2.0
    if quantity(a.get("Text as shown on drawing")) != quantity(b.get("Text as shown on drawing")):
        c += 2.0
    if (a.get("Boxed / reference flag") or "-") != (b.get("Boxed / reference flag") or "-"):
        c += 2.0
    if a["Feature type"] != b["Feature type"]:
        c += 1.0
    return c


def hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost assignment for an n×m matrix (n ≤ m): column index per row."""
    n = len(cost)
    m = len(cost[0]) if n else 0
    if n == 0:
        return []
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [math.inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    out = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


def match(step: list[dict], drawing: list[dict]) -> list[tuple[int, int, float]]:
    """One-to-one STEP → 2D pairs (i, j, cost); pairs breaking a critical field are dropped."""
    if not step or not drawing:
        return []
    n, m = len(step), len(drawing)
    size = max(n, m)
    cost = [[(pair_cost(step[i], drawing[j]) if i < n and j < m else INF) for j in range(size)] for i in range(size)]
    # deterministic tie-break between equal pairs: prefer the drawing order of first appearance
    for i in range(n):
        for j in range(m):
            if cost[i][j] < INF:
                cost[i][j] += 1e-9 * j + 1e-12 * i
    assign = hungarian(cost)
    return [(i, j, cost[i][j]) for i, j in enumerate(assign) if i < n and 0 <= j < m and cost[i][j] < INF]


# ----------------------------------------------------------------------------- pairing

def drawing_pdf(path: Path, workdir: Path) -> Path:
    """A 2D drawing as PDF (DXF converted with the app's converter)."""
    if path.suffix.lower() == ".dxf":
        from app.services.dxf_to_pdf import dxf_to_pdf

        out = workdir / f"{path.stem}.pdf"
        dxf_to_pdf(path, out)
        return out
    return path


def _norm_id(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def step_part_ids(step_path: Path) -> set[str]:
    """Product id / name of the STEP file (normalised)."""
    from app.services.step_pmi import decode_step_string, parse_step

    ids = set()
    for d in parse_step(step_path).values():
        pr = d.get("PRODUCT")
        if pr:
            for v in pr[:2]:
                if isinstance(v, str) and v.strip():
                    ids.add(_norm_id(decode_step_string(v)))
    return {i for i in ids if i}


def drawing_part_id(pdf: Path) -> str:
    from app.services.title_block_extract import extract_title_block_meta

    try:
        return _norm_id(extract_title_block_meta(pdf).get("part_number", ""))
    except Exception:
        return ""


def find_pairing(step_path: Path, candidates: list[Path]) -> Path | None:
    """First candidate drawing whose drawing / part number equals the STEP product id or name."""
    ids = step_part_ids(step_path)
    with tempfile.TemporaryDirectory() as td:
        for c in candidates:
            try:
                pid = drawing_part_id(drawing_pdf(Path(c), Path(td)))
            except Exception:
                continue
            if pid and pid in ids:
                return Path(c)
    return None


# ----------------------------------------------------------------------------- report

def _missing_reason(row: dict, model: PmiModel, step: list[dict]) -> str:
    fam = family(row["Feature type"])
    kinds = {i.kind for i in model.items}
    if fam == "surface" and "surface" not in kinds:
        return "not in the STEP file as PMI (the file carries no surface-finish data)"
    near = [s for s in step if family(s["Feature type"]) == fam and _same_value(s["Nominal"], row["Nominal"])]
    if near:
        return "STEP PMI with the same nominal differs: " + "; ".join(str(s["Text as shown on drawing"]) for s in near[:3])
    return "not in the STEP file as semantic PMI"


def build_step_report(step_path: Path, drawing: Path | None = None, *, model: PmiModel | None = None,
                      drawing_rows: tuple | None = None) -> StepReport:
    """STEP report rows; paired with `drawing` (PDF / DXF) when given."""
    from app.services.tvs_extraction_report import _extraction_rows

    rows, model = step_rows(Path(step_path), model)
    if drawing is None and drawing_rows is None:
        for r in rows:
            _three_d_fields(r, ZONE_3D)
        rows.sort(key=lambda r: step_order_key(r["_item"]))
        for n, r in enumerate(rows, start=1):
            r["Balloon No."] = n
            r["Feature ID"] = f"3D-{n:03d}"
        notes = [{"sheet": SHEET_3D, "zone": ZONE_3D, "view": "Model", "type": k, "text": t} for k, t in model.notes]
        return StepReport("step-only", model.product, rows, notes, model=model)

    if drawing_rows is None:
        with tempfile.TemporaryDirectory() as td:
            pdf = drawing_pdf(Path(drawing), Path(td))
            drawing_rows = _extraction_rows(pdf)
    d_rows, d_notes = drawing_rows[0], drawing_rows[1]
    pairs = match(rows, d_rows)
    matched_step = {i for i, _j, _c in pairs}
    matched_2d = {j for _i, j, _c in pairs}
    out: list[dict] = []
    for i, j, _c in pairs:
        s, d = rows[i], d_rows[j]
        it: PmiItem = s["_item"]
        r = OrderedDict(s)
        for k in ("Balloon No.", "Feature ID", "Sheet", "Zone", "X (mm)", "Y (mm)", "View", "Critical flag", "Feature type"):
            r[k] = d[k]
        notes = _strip_layout_notes(d.get("Notes"))
        crit_2d = d["Critical flag"] if d["Critical flag"] not in (None, "", "-") else ""
        if it.critical and crit_2d and it.critical != crit_2d:
            notes.append(f"CC conflict: STEP {it.critical} vs drawing {crit_2d}")
        elif it.critical and not crit_2d:
            notes.append(f"CC conflict: STEP {it.critical}, drawing none")
        notes.append(step_context(it))
        r["Notes"] = "; ".join(dict.fromkeys(n for n in notes if n)) or None
        out.append(r)
    top = max((int(d["Balloon No."]) for d in d_rows), default=0)
    extra = sorted((rows[i] for i in range(len(rows)) if i not in matched_step), key=lambda r: step_order_key(r["_item"]))
    for k, r in enumerate(extra, start=1):
        _three_d_fields(r, ZONE_3D_ONLY)
        r["Balloon No."] = top + k
        r["Feature ID"] = f"3D-{k:03d}"
        out.append(r)
    out.sort(key=lambda r: int(r["Balloon No."]))
    missing = [(d_rows[j], _missing_reason(d_rows[j], model, rows)) for j in range(len(d_rows)) if j not in matched_2d]
    notes = list(d_notes) + [{"sheet": SHEET_3D, "zone": ZONE_3D, "view": "Model", "type": f"STEP {k}", "text": t}
                             for k, t in model.notes]
    return StepReport("paired", model.product, out, notes, Path(drawing) if drawing else None, extra, missing, model)


def step_report_xlsx(rep: StepReport, meta: dict | None = None, drawing_pdf_path: Path | None = None) -> bytes:
    """Excel in the TVS report layout; Summary carries the STEP-only and missing sections."""
    from app.services.tvs_extraction_report import _title_fields, build_tvs_extraction_xlsx

    meta = dict(meta or {})
    if drawing_pdf_path is not None:
        title = _title_fields(drawing_pdf_path, meta)
    else:
        title = {"part_number": rep.product, "revision": meta.get("revision", ""), "part_name": meta.get("part_name", ""),
                 "material": meta.get("material", ""), "mass": meta.get("mass", "")}
    sections = []
    if rep.mode == "paired":
        sections.append(("STEP-only PMI (no drawing characteristic)", ["Balloon No.", "STEP PMI", "Text"],
                         [(r["Balloon No."], f"#{r['_item'].eid}", r["Text as shown on drawing"]) for r in rep.step_only]
                         or [("none", "", "")]))
        sections.append(("Missing from STEP (drawing characteristic without PMI)", ["Balloon No.", "Text", "Reason"],
                         [(d["Balloon No."], d["Text as shown on drawing"], why) for d, why in rep.missing]
                         or [("none", "", "")]))
        ident = f"(STEP AP242 PMI paired with drawing {rep.drawing.name if rep.drawing else ''})"
        coords = ("Balloon No., Sheet, Zone, X/Y, View and Critical flag come from the matched drawing "
                  "characteristic; values come from the STEP PMI. STEP entity #id, saved view, faces and 3D anchor "
                  "are in the Notes column.")
    else:
        ident = "(STEP AP242 PMI, no drawing)"
        coords = ("STEP-only: no drawing sheet, so Sheet = 3D, Zone = N/A (3D model) and X/Y are blank. View = saved "
                  "view. 3D anchor (model mm) and face #ids are in the Notes column. Balloon order: saved view (file "
                  "order) → dimensions, GD&T, datums, surface finishes → anchor Z descending, Y descending, X ascending.")
    features = []
    for r in rep.features:
        f = OrderedDict((k, v) for k, v in r.items() if k != "_item")
        features.append(f)
    prepared = {"features": features, "notes": rep.notes, "sheet_count": 0 if rep.mode == "step-only" else None,
                "size": "", "orphans": [], "title": title, "ident": ident, "coordinates": coords, "sections": sections}
    if rep.mode == "paired":
        prepared["sheet_count"] = len({f["Sheet"] for f in features if str(f["Sheet"]).isdigit()})
    return build_tvs_extraction_xlsx(meta, drawing_pdf_path or Path(f"{rep.product}.step"), prepared=prepared)


# ----------------------------------------------------------------------------- app integration

STEP_CONVERTER = "step_ap242_pmi"


def job_step_source(job_dir: Path, meta: dict) -> Path | None:
    """The uploaded STEP file of a job whose drawing was produced from STEP AP242 PMI (single upload)."""
    files = meta.get("files") or []
    if not files:
        return None
    f = files[int(meta.get("active_file") or 0)]
    names = f.get("source_names") or []
    log = f.get("conversion_log") or meta.get("conversion_log") or []
    if len(names) != 1 or not log or log[0].get("converter") != STEP_CONVERTER:
        return None
    p = Path(job_dir) / f"upload_0_{names[0]}"
    return p if p.is_file() else None


def job_drawing_source(job_dir: Path, meta: dict) -> Path | None:
    """A job's 2D drawing (PDF, or the PDF converted from its DXF); None for STEP jobs."""
    if job_step_source(job_dir, meta) is not None:
        return None
    files = meta.get("files") or []
    if not files:
        return None
    p = Path(job_dir) / files[int(meta.get("active_file") or 0)].get("path", "")
    return p if p.is_file() and p.suffix.lower() == ".pdf" else None


def find_paired_job(step_path: Path, jobs_root: Path, exclude: str = "") -> tuple[str, Path] | None:
    """Most recent other job whose drawing / part number equals the STEP product id or name."""
    import json

    ids = step_part_ids(step_path)
    best = None
    for mp in Path(jobs_root).glob("*/meta.json"):
        jd = mp.parent
        if jd.name == exclude:
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _norm_id(meta.get("part_number", "")) not in ids:
            continue
        src = job_drawing_source(jd, meta)
        if src is None:
            continue
        t = mp.stat().st_mtime
        if best is None or t > best[0]:
            best = (t, jd.name, src)
    return (best[1], best[2]) if best else None


def balloon_number_lookup(rep: StepReport):
    """(sheet, point) on the rendered PMI sheet -> report balloon number, so the ballooned PDF of a
    STEP job shows the same numbers as its report."""
    slots = [(r["_item"].slot, int(r["Balloon No."])) for r in rep.features if r["_item"].slot]

    def number_of(sheet: int, pt) -> int | None:
        for (pg, rect), n in slots:
            if pg == sheet - 1 and rect.contains(pt):
                return n
        return None

    return number_of
