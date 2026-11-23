"""
Read the general-tolerance standard, manufacturing process, sheet thickness, geometric class and
any printed general-tolerance table from a drawing's text (title block first, then notes).

Text comes from the PDF text layer; pages without one (text exported as vector strokes) go through
the existing OCR path (PyMuPDF + Tesseract, same as _ocr_enrich_pdf / the zoomed-OCR fallback).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import fitz

# ----------------------------------------------------------------------------- standards

_ISO = re.compile(
    r"\bISO\s*[-:]?\s*2768(?:\s*-\s*1(?![0-9]))?\s*[-:]?\s*(?P<cls>[fmcv])?(?:\s*-?\s*(?P<geo>[hkl]))?(?![a-z0-9])",
    re.I)
_IS = re.compile(r"\bIS\s*[:\-]?\s*2102(?:\s*[-(]?\s*(?:PART\s*[-]?\s*)?(?:I{1,2}|1|2)\)?)?"
                 r"(?:\s*[-:]?\s*(?P<cls>[fmcv])(?![a-z0-9]))?", re.I)
_CLASS_WORD = re.compile(r"\b(FINE|MEDIUM|COARSE|VERY\s+COARSE)\b", re.I)
_TSDS = re.compile(r"\bTSDS\s*[-]?\s*0407\b|TVS\s+SYSTEM\s+DESIGN\s+STANDARD.{0,80}UNSPECIFIED\s+TOL", re.I | re.S)
_GT_FIELD = re.compile(r"GENERAL\s+TOL|UNSPECIFIED\s+TOL|UNLESS\s+OTHERWISE|TOLERANCES?\b", re.I)

# ----------------------------------------------------------------------------- processes

PROCESSES = {
    "machining": "Machining",
    "casting_hpdc": "Casting – HPDC",
    "casting_shell": "Casting – shell",
    "casting_sand": "Casting – sand",
    "pressing": "Pressing",
    "forging": "Forging",
    "welding": "Welding",
    "plastic": "Plastic",
    "rubber_moulding": "Rubber – moulding",
    "rubber_extrusion": "Rubber – extrusion & others",
    "lost_wax": "Lost wax",
}
_PROC_WORDS: list[tuple[str, re.Pattern]] = [
    ("lost_wax", re.compile(r"\bINVESTMENT\s+CAST|\bLOST\s+WAX\b", re.I)),
    ("casting_hpdc", re.compile(r"\bH\.?P\.?D\.?C\b|\bPDC\b|PRESSURE\s+DIE[\s-]*CAST|\bDIE[\s-]*CAST", re.I)),
    ("casting_sand", re.compile(r"\bSAND\s+CAST", re.I)),
    ("casting_shell", re.compile(r"\bSHELL\s+(?:MOULD|MOLD|CAST)", re.I)),
    ("forging", re.compile(r"\bFORG(?:ED|ING)\b", re.I)),
    ("pressing", re.compile(r"\bPRESS(?:ED|ING)\b|\bSHEET\s+METAL\b|\bSTAMP(?:ED|ING)\b", re.I)),
    ("welding", re.compile(r"\bWELD(?:ED|ING|MENT)\b", re.I)),
    ("rubber_extrusion", re.compile(r"\bRUBBER\b.{0,40}\bEXTRU|\bEXTRUDED\s+RUBBER", re.I)),
    ("rubber_moulding", re.compile(r"\bRUBBER\b|\bNBR\b|\bEPDM\b|\bVULCANI[SZ]", re.I)),
    ("plastic", re.compile(r"\bPLASTIC\b|\bINJECTION\s+MOUL?D", re.I)),
    ("machining", re.compile(r"\bMACHIN(?:ED|ING)\b", re.I)),
]
_AS_CAST = re.compile(r"\bAS[\s-]+CAST\b|\bCASTING\b", re.I)
# Material designations that imply a process (only unambiguous ones)
_MATERIAL_PROC: list[tuple[str, re.Pattern]] = [
    ("casting_hpdc", re.compile(r"\bADC\s*-?\s*1[02]\b|\bA380\b|\bAlSi9Cu3\b|\bLM\s*24\b", re.I)),
    ("pressing", re.compile(r"\bSP(?:CC|CD|CE|HC)\b|\bCRCA\b|\bCR\s*[1-5]\b|\bSAPH\s*\d+|\bDC0[1-6]\b|\bIS\s*:?\s*(?:513|1079)\b", re.I)),
    ("plastic", re.compile(r"\b(?:PP|ABS|PA\s*6{1,2}|POM|PBT|PC|NYLON)\b", re.I)),
    ("rubber_moulding", re.compile(r"\bNBR\b|\bEPDM\b|\bSILICONE\s+RUBBER\b", re.I)),
]
# A forming / casting process is the part's base process; MACHINED features on it are per-feature overrides.
_BASE_ORDER = ["casting_hpdc", "casting_shell", "casting_sand", "lost_wax", "forging", "pressing", "welding",
               "plastic", "rubber_moulding", "rubber_extrusion", "machining"]

_THK = re.compile(r"(?:\bt\s*=\s*|\bTHK\.?\s*[:=]?\s*|\bTHICKNESS\s*[:=]?\s*)(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:mm\s*)?THK\b", re.I)
_GEO_CLASS = re.compile(r"\bGEOM(?:ETRIC(?:AL)?)?\.?\s*(?:TOL\w*\.?)?.{0,40}?\bCLASS\s*[-:]?\s*([123])\b|\bTSDS\s*-?\s*0407\b.{0,40}?\bCLASS\s*[-:]?\s*([123])\b", re.I | re.S)

# ----------------------------------------------------------------------------- printed tables

_NUM = r"(\d+(?:[.,]\d+)?)"
_ROW_RANGE = re.compile(r"(?:OVER\s*|ABOVE\s*|>\s*)?" + _NUM + r"\s*(?:-|–|~|TO|UPTO|UP\s*TO)\s*" + _NUM + r"\s*[:|]?\s*±\s*" + _NUM, re.I)
_ROW_UPTO = re.compile(r"(?:UP\s*TO|UPTO|≤|<=|BELOW)\s*" + _NUM + r"\s*[:|]?\s*±\s*" + _NUM, re.I)
_ROW_OVER = re.compile(r"(?:OVER|ABOVE|>)\s*" + _NUM + r"(?!\s*(?:-|–|~|TO|UPTO|UP\s*TO)\s*\d)\s*[:|]?\s*±\s*" + _NUM, re.I)
_ROW_ANGLE = re.compile(r"\bANG(?:LES?|ULAR)\b[^±]{0,20}±\s*(\d+(?:[.,]\d+)?)\s*°\s*(?:(\d+)\s*['′])?", re.I)


@dataclass
class StatedStandard:
    id: str                       # ISO2768 | TSDS0407 | DRAWING_TABLE
    cls: str | None = None        # ISO class f/m/c/v
    geo: str | None = None        # ISO 2768-2 class H/K/L (stored, never applied to sizes)
    via: str = ""                 # 'IS 2102' when stated as the Indian equivalent
    location: str = ""            # title block | notes
    text: str = ""

    def label(self) -> str:
        if self.id == "ISO2768":
            name = self.via or "ISO 2768"
            return f"{name}-{self.cls}{(self.geo or '').upper()}" if self.cls else name
        if self.id == "TSDS0407":
            return "TSDS0407"
        return "drawing table"


@dataclass
class DrawingGT:
    standards: list[StatedStandard] = field(default_factory=list)
    chosen: StatedStandard | None = None
    conflict: str = ""
    table: list[dict] = field(default_factory=list)        # printed general-tolerance table rows
    process: str | None = None
    process_source: str = ""
    process_candidates: list[str] = field(default_factory=list)
    process_hint: str = ""
    sheet_thickness: float | None = None
    geometric_class: int | None = None
    flags: list[str] = field(default_factory=list)
    text_source: str = "text layer"                        # text layer | OCR | none

    def to_dict(self) -> dict:
        d = asdict(self)
        d["chosen_label"] = self.chosen.label() if self.chosen else ""
        return d

    @staticmethod
    def from_dict(d: dict | None) -> "DrawingGT":
        if not d:
            return DrawingGT()
        out = DrawingGT(**{k: v for k, v in d.items() if k in DrawingGT.__dataclass_fields__ and k not in ("standards", "chosen")})
        out.standards = [StatedStandard(**s) for s in d.get("standards") or []]
        out.chosen = StatedStandard(**d["chosen"]) if d.get("chosen") else None
        return out


def _iso_class_from_words(text: str) -> str | None:
    m = _CLASS_WORD.search(text or "")
    if not m:
        return None
    return {"FINE": "f", "MEDIUM": "m", "COARSE": "c"}.get(m.group(1).upper(), "v")


def find_standards(text: str, location: str) -> list[StatedStandard]:
    out: list[StatedStandard] = []
    for m in _ISO.finditer(text or ""):
        cls = (m.group("cls") or "").lower() or None
        geo = (m.group("geo") or "").upper() or None
        out.append(StatedStandard("ISO2768", cls, geo, "", location, m.group(0).strip()))
    for m in _IS.finditer(text or ""):
        cls = (m.group("cls") or "").lower() or _iso_class_from_words(text[m.end(): m.end() + 30])
        out.append(StatedStandard("ISO2768", cls, None, "IS 2102", location, m.group(0).strip()))
    for m in _TSDS.finditer(text or ""):
        out.append(StatedStandard("TSDS0407", None, None, "", location, m.group(0).strip()))
    return out


def parse_printed_table(lines: list[str]) -> list[dict]:
    """
    Rows of a general-tolerance table printed on the drawing ("0-6 ±0.1", "OVER 30 UPTO 120 ±0.3",
    "ANGLES ±0°30'"). Needs a TOLERANCE heading within 6 lines above and at least two rows.
    """
    rows: list[dict] = []
    head_at = None
    for i, ln in enumerate(lines):
        t = ln.replace("−", "-").replace(",", ".")
        if re.search(r"TOLERANCE", t, re.I) and not _ISO.search(t) and not _TSDS.search(t) and not _IS.search(t):
            head_at = i
        if head_at is None or i - head_at > 12:
            continue
        found = False
        for m in _ROW_RANGE.finditer(t):
            rows.append({"min": float(m.group(1)), "max": float(m.group(2)), "pm": float(m.group(3)), "kind": "size",
                         "text": m.group(0).strip()})
            found = True
        if not found:
            m = _ROW_UPTO.search(t)
            if m:
                rows.append({"min": None, "max": float(m.group(1)), "pm": float(m.group(2)), "kind": "size", "text": m.group(0).strip()})
            m = _ROW_OVER.search(t)
            if m:
                rows.append({"min": float(m.group(1)), "max": None, "pm": float(m.group(2)), "kind": "size", "text": m.group(0).strip()})
        m = _ROW_ANGLE.search(t)
        if m:
            v = float(m.group(1)) + (float(m.group(2)) / 60 if m.group(2) else 0)
            rows.append({"min": None, "max": None, "pm": v, "kind": "angle", "text": m.group(0).strip()})
    size_rows = [r for r in rows if r["kind"] == "size"]
    return rows if len(size_rows) >= 2 else []


def detect_process(texts: list[str], material: str = "") -> tuple[str | None, str, list[str], str]:
    """(process, source, candidates, hint). Base process wins over MACHINED (machined features → override)."""
    joined = " \n ".join(texts)
    found: dict[str, str] = {}
    for pid, rx in _PROC_WORDS:
        m = rx.search(joined)
        if m and pid not in found:
            if pid == "rubber_moulding" and "rubber_extrusion" in found:
                continue
            found[pid] = f"note '{m.group(0).strip()}'"
    if material:
        for pid, rx in _MATERIAL_PROC:
            m = rx.search(material)
            if m and pid not in found:
                found[pid] = f"material '{m.group(0).strip()}'"
    if not found:
        hint = "casting stated but sub-process (HPDC / shell / sand) not stated" if _AS_CAST.search(joined) else ""
        return None, "", [], hint
    ordered = sorted(found, key=_BASE_ORDER.index)
    return ordered[0], found[ordered[0]], ordered, ""


def detect_from_text(title_lines: list[str], other_lines: list[str], material: str = "") -> DrawingGT:
    """Core detection on plain text (unit-testable without a PDF)."""
    out = DrawingGT()
    title = " | ".join(title_lines)
    others = "\n".join(other_lines)
    std = find_standards(title, "title block") + find_standards(others, "notes")
    # de-duplicate the same standard stated twice in one place
    seen = set()
    for s in std:
        k = (s.id, s.cls, s.via, s.location)
        if k not in seen:
            seen.add(k)
            out.standards.append(s)
    out.table = parse_printed_table(other_lines + title_lines)
    ids = {s.id for s in out.standards}
    if len(ids) > 1:
        tb = [s for s in out.standards if s.location == "title block"]
        tb_ids = {s.id for s in tb}
        if len(tb_ids) == 1:
            out.chosen = tb[0]
            out.conflict = (f"Drawing states {', '.join(sorted({s.label() for s in out.standards}))}; "
                            f"using {tb[0].label()} from the title block")
        else:
            out.conflict = f"Drawing states {', '.join(sorted({s.label() for s in out.standards}))}; ambiguous"
    elif out.standards:
        # same standard; prefer the title-block statement, then one that names a class
        out.chosen = sorted(out.standards, key=lambda s: (s.location != "title block", s.cls is None))[0]
        classes = {s.cls for s in out.standards if s.cls}
        if len(classes) > 1:
            out.conflict = f"Drawing states ISO 2768 classes {', '.join(sorted(classes))}; using {out.chosen.label()}"
    if out.chosen and out.chosen.id == "ISO2768" and not out.chosen.cls:
        out.flags.append(f"{out.chosen.label()}: tolerance class not stated")
    proc, src, cands, hint = detect_process(other_lines + title_lines, material)
    out.process, out.process_source, out.process_candidates, out.process_hint = proc, src, cands, hint
    if len(cands) > 1:
        out.flags.append(f"Several processes stated ({', '.join(PROCESSES[c] for c in cands)}); "
                         f"using {PROCESSES[proc]} — set a per-feature override for the others")
    m = _THK.search(" \n ".join(other_lines + title_lines + [material]))
    if m:
        out.sheet_thickness = float((m.group(1) or m.group(2)).replace(",", "."))
    m = _GEO_CLASS.search(" \n ".join(other_lines + title_lines))
    if m:
        out.geometric_class = int(m.group(1) or m.group(2))
    return out


# ----------------------------------------------------------------------------- PDF text

def _lines_from_words(words: list, rect: fitz.Rect | None = None, inside: bool = True) -> list[str]:
    rows: list[list] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if rect is not None and (rect.contains(fitz.Point(cx, cy)) != inside):
            continue
        h = max(1.0, w[3] - w[1])
        for r in rows:
            if abs(r[0] - cy) <= h * 0.45 and abs(r[2] - w[0]) < h * 4:
                r[1].append(w)
                r[2] = w[2]
                break
        else:
            rows.append([cy, [w], w[2]])
    out = []
    for _cy, ws, _x in sorted(rows, key=lambda r: (r[0], r[1][0][0])):
        out.append(" ".join(w[4] for w in sorted(ws, key=lambda w: w[0])))
    return out


def _ocr_words(page: fitz.Page) -> list | None:
    try:
        from app.services.autoballoon_engine import _resolve_tessdata
        tessdata = _resolve_tessdata()
    except Exception:
        tessdata = None
    if not tessdata:
        return None
    try:
        tp = page.get_textpage_ocr(dpi=300, full=True, language="eng", tessdata=tessdata)
        return page.get_text("words", textpage=tp)
    except Exception:
        return None


def _title_rects(page: fitz.Page) -> list[fitz.Rect]:
    try:
        from app.services.tvs_extractor import PageData, _find_tables, _inner_frame
        pd = PageData(page, page.number)
        return [fitz.Rect(t.rect) for t in _find_tables(pd, _inner_frame(pd)) if t.kind == "title"]
    except Exception:
        return []


def drawing_text(pdf_path: Path) -> tuple[list[str], list[str], str]:
    """(title-block lines, other lines, text source) for every page."""
    title: list[str] = []
    other: list[str] = []
    source = "text layer"
    doc = fitz.open(str(pdf_path))
    try:
        for page in doc:
            words = page.get_text("words")
            if len("".join(w[4] for w in words)) < 8:
                ocr = _ocr_words(page)
                if ocr is None:
                    source = "none (no text layer and OCR unavailable)"
                    continue
                words, source = ocr, "OCR"
            rects = _title_rects(page)
            if rects:
                for r in rects:
                    title.extend(_lines_from_words(words, r + (-1, -1, 1, 1), True))
                rest = [w for w in words if not any((r + (-1, -1, 1, 1)).contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for r in rects)]
                other.extend(_lines_from_words(rest))
            else:
                # No ruled title block found: the lower-right quarter stands in for it
                pr = page.rect
                tb = fitz.Rect(pr.x0 + pr.width * 0.55, pr.y0 + pr.height * 0.72, pr.x1, pr.y1)
                title.extend(_lines_from_words(words, tb, True))
                other.extend(_lines_from_words(words, tb, False))
    finally:
        doc.close()
    return title, other, source


def detect_drawing(pdf_path: Path, material: str = "") -> DrawingGT:
    title, other, source = drawing_text(Path(pdf_path))
    if not material:
        for ln in title:
            m = re.search(r"\bMATERIAL\b\s*[:]?\s*(.+)", ln, re.I)
            if m:
                material = m.group(1)
                break
    out = detect_from_text(title, other, material)
    out.text_source = source
    if source.startswith("none"):
        out.flags.append("Drawing has no text layer and OCR is not available: general tolerance not read")
    return out
