"""
STEP AP242 semantic PMI -> model-based-definition sheet (vector PDF).

Reads the semantic PMI of a STEP file (ISO 10303-21 text) — dimensional size/location and
angular dimensions with plus/minus tolerances, geometric tolerances (characteristic, value,
diametral zone, MMC/LMC, datum system), datum features, and descriptive notes (BASIC,
REFERENCE, ISO 286 fit, THREAD, CHAMFER) — and draws every characteristic the way a 2D
drawing shows it: values as real text, basic dimensions boxed, references in brackets, GD&T as
feature control frames with vector symbols, datum features as boxed letters with triangles.

The existing drawing extractor, ballooning and TVS report then work on STEP-PMI unchanged.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

# =============================================================================
# ISO 10303-21 parser (enough for AP242 PMI)
# =============================================================================


class Ref(int):
    pass


@dataclass
class Enum:
    name: str


@dataclass
class Typed:
    type: str
    value: object


def _tokenize(s: str):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c == "'":
            j, buf = i + 1, []
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            yield ("str", "".join(buf))
            i = j + 1
        elif c == "#":
            j = i + 1
            while j < n and s[j].isdigit():
                j += 1
            yield ("ref", Ref(int(s[i + 1:j])))
            i = j
        elif c == ".":
            j = s.find(".", i + 1)
            word = s[i + 1:j]
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", word):
                yield ("enum", Enum(word))
                i = j + 1
            else:  # number like .5
                m = re.match(r"[-+]?[0-9]*\.?[0-9]*(?:[Ee][-+]?[0-9]+)?", s[i:])
                yield ("num", float(m.group(0)))
                i += len(m.group(0))
        elif c in "()," or c in "$*":
            yield (c, c)
            i += 1
        elif c in "-+" or c.isdigit():
            m = re.match(r"[-+]?[0-9]+\.?[0-9]*(?:[Ee][-+]?[0-9]+)?", s[i:])
            yield ("num", float(m.group(0)))
            i += len(m.group(0))
        else:
            m = re.match(r"[A-Z_][A-Z0-9_]*", s[i:])
            if not m:
                i += 1
                continue
            yield ("kw", m.group(0))
            i += len(m.group(0))


def _parse_value(toks, k):
    kind, val = toks[k]
    if kind == "(":
        out, k = [], k + 1
        while toks[k][0] != ")":
            v, k = _parse_value(toks, k)
            out.append(v)
            if toks[k][0] == ",":
                k += 1
        return out, k + 1
    if kind == "kw":
        if k + 1 < len(toks) and toks[k + 1][0] == "(":
            args, k2 = _parse_value(toks, k + 1)
            return Typed(val, args[0] if len(args) == 1 else args), k2
        return Enum(val), k + 1
    if kind in ("$", "*"):
        return None, k + 1
    return val, k + 1


def _parse_entity(body: str) -> dict[str, list]:
    """'TYPE(args)' or '( A(args) B(args) )' -> {TYPE: [args]}."""
    toks = list(_tokenize(body))
    out: dict[str, list] = {}
    k = 0
    if toks and toks[0][0] == "(":
        k = 1
    while k < len(toks):
        kind, val = toks[k]
        if kind == "kw":
            if k + 1 < len(toks) and toks[k + 1][0] == "(":
                args, k = _parse_value(toks, k + 1)
                out[val] = args
            else:
                out[val] = []
                k += 1
        else:
            k += 1
    return out


def parse_step(path: str | Path) -> dict[int, dict[str, list]]:
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    data = txt[txt.find("DATA;") + 5:]
    ents: dict[int, dict[str, list]] = {}
    for m in re.finditer(r"#(\d+)\s*=\s*(.*?);\s*(?=#\d+\s*=|ENDSEC)", data, re.S):
        try:
            ents[int(m.group(1))] = _parse_entity(m.group(2))
        except Exception:
            continue
    return ents


# =============================================================================
# PMI model
# =============================================================================

GT_TYPES = {
    "POSITION_TOLERANCE": "position", "FLATNESS_TOLERANCE": "flatness", "STRAIGHTNESS_TOLERANCE": "straightness",
    "ROUNDNESS_TOLERANCE": "circularity", "CYLINDRICITY_TOLERANCE": "cylindricity",
    "SURFACE_PROFILE_TOLERANCE": "profile_surface", "LINE_PROFILE_TOLERANCE": "profile_line",
    "PERPENDICULARITY_TOLERANCE": "perpendicularity", "PARALLELISM_TOLERANCE": "parallelism",
    "ANGULARITY_TOLERANCE": "angularity", "CIRCULAR_RUNOUT_TOLERANCE": "circular_runout",
    "TOTAL_RUNOUT_TOLERANCE": "total_runout", "CONCENTRICITY_TOLERANCE": "concentricity",
    "COAXIALITY_TOLERANCE": "concentricity", "SYMMETRY_TOLERANCE": "symmetry",
}


@dataclass
class PmiItem:
    kind: str                       # dim | gdt | datum | thread | chamfer
    text: str = ""                  # annotation as a drawing would show it
    lines: list[str] = field(default_factory=list)
    gdt: str = ""
    tol: str = ""                   # FCF tolerance cell, e.g. 'Ø0.2 M'
    datums: list[str] = field(default_factory=list)
    boxed: bool = False
    note: str = ""


@dataclass
class PmiModel:
    product: str
    items: list[PmiItem]


def has_semantic_pmi(path: str | Path) -> bool:
    head = Path(path).read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"DIMENSIONAL_CHARACTERISTIC_REPRESENTATION|GEOMETRIC_TOLERANCE|DATUM_FEATURE", head))


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _dev(v: float) -> str:
    return "0" if abs(v) < 1e-12 else (("+" if v > 0 else "") + _fmt(v))


class _Resolver:
    def __init__(self, ents: dict[int, dict[str, list]]):
        self.e = ents
        self.angle_unit: dict[int, float] = {}   # unit entity -> degrees per unit
        for i, d in ents.items():
            if "PLANE_ANGLE_UNIT" in d or "CONVERSION_BASED_UNIT" in d and "PLANE_ANGLE_UNIT" in d:
                si = d.get("SI_UNIT")
                if si is not None:
                    self.angle_unit[i] = 180.0 / math.pi
                cb = d.get("CONVERSION_BASED_UNIT")
                if cb and isinstance(cb[0], str) and "DEG" in cb[0].upper():
                    self.angle_unit[i] = 1.0

    def get(self, r) -> dict[str, list]:
        return self.e.get(int(r), {}) if isinstance(r, int) else {}

    def measure(self, r) -> tuple[float | None, bool]:
        """(value, is_angle) of a measure entity, angles converted to degrees."""
        d = self.get(r)
        mwu = d.get("MEASURE_WITH_UNIT") or d.get("LENGTH_MEASURE_WITH_UNIT") or d.get("PLANE_ANGLE_MEASURE_WITH_UNIT")
        if not mwu:
            return None, False
        raw, unit = mwu[0], mwu[1] if len(mwu) > 1 else None
        val = raw.value if isinstance(raw, Typed) else raw
        if isinstance(val, list):
            val = val[0]
        if not isinstance(val, (int, float)):
            return None, False
        is_angle = (isinstance(raw, Typed) and "ANGLE" in raw.type) or (isinstance(unit, int) and int(unit) in self.angle_unit)
        if is_angle:
            return float(val) * self.angle_unit.get(int(unit) if isinstance(unit, int) else -1, 180.0 / math.pi), True
        return float(val), False

    def name_of(self, r) -> str:
        d = self.get(r)
        ri = d.get("REPRESENTATION_ITEM")
        if ri and isinstance(ri[0], str):
            return ri[0]
        return ""


def read_pmi(path: str | Path) -> PmiModel:
    ents = parse_step(path)
    R = _Resolver(ents)
    product = next((d["PRODUCT"][0] for d in ents.values() if "PRODUCT" in d and d["PRODUCT"]), Path(path).stem)
    items: list[PmiItem] = []

    tol_by_dim: dict[int, tuple[float, float]] = {}
    for d in ents.values():
        pmt = d.get("PLUS_MINUS_TOLERANCE")
        if pmt:
            rng = R.get(pmt[0])
            tv = rng.get("TOLERANCE_VALUE")
            if tv:
                lo, _ = R.measure(tv[0])
                hi, _ = R.measure(tv[1])
                if lo is not None and hi is not None:
                    tol_by_dim[int(pmt[1])] = (lo, hi)

    # ---- dimensions
    for i, d in sorted(ents.items()):
        dcr = d.get("DIMENSIONAL_CHARACTERISTIC_REPRESENTATION")
        if not dcr:
            continue
        dim_id, rep = int(dcr[0]), R.get(dcr[1])
        dim = R.get(dim_id)
        sdr = rep.get("SHAPE_DIMENSION_REPRESENTATION")
        nominal = is_angle = None
        notes: list[str] = []
        for it in (sdr[1] if sdr and isinstance(sdr[1], list) else []):
            ed = R.get(it)
            if "DESCRIPTIVE_REPRESENTATION_ITEM" in ed:
                notes.append(str(ed["DESCRIPTIVE_REPRESENTATION_ITEM"][1]))
                continue
            v, ang = R.measure(it)
            if v is not None and (nominal is None or R.name_of(it).lower().startswith("nominal")):
                nominal, is_angle = v, ang
        if nominal is None:
            continue
        note = " ".join(notes)
        dtype = ""
        for t in ("DIMENSIONAL_SIZE", "ANGULAR_SIZE", "DIMENSIONAL_LOCATION", "ANGULAR_LOCATION"):
            if t in dim:
                dtype = t
                args = dim[t]
                sub = (args[1] if t in ("DIMENSIONAL_SIZE", "ANGULAR_SIZE") else args[0]) if args else ""
                break
        sub = (sub or "").lower() if isinstance(sub, str) else ""
        angle = is_angle or dtype.startswith("ANGULAR")
        tol = tol_by_dim.get(dim_id)

        # a THREAD note names a designation: 'THREAD [n x] M...' ('full-thread depth' is not one)
        th = re.search(r"\bTHREAD\s+((?:\d+\s*x\s*)?M\d[^;]*)", note)
        if th:
            callout = _thread_text(th.group(1))
            items.append(PmiItem("thread", text=" / ".join(callout), lines=callout, note=_rest(note, th)))
            continue
        ch = re.search(r"\bCHAMFER\s+(?:(\d+)\s*x\s+)?(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*deg", note, re.I)
        if ch:
            qty = f"{ch.group(1)}× " if ch.group(1) else ""
            items.append(PmiItem("chamfer", text=f"{qty}{ch.group(2)} × {ch.group(3)}°", note=note))
            continue
        pre = "Ø" if "diameter" in sub else ("R" if "radius" in sub else "")
        if "spherical" in sub:
            pre = "S" + pre
        val = f"{pre}{_fmt(nominal)}" + ("°" if angle else "")
        fit = re.search(r"\bFIT\s+([A-Za-z]{1,2}\d{1,2})\b", note)
        if fit:
            val += f" {fit.group(1)}"
        basic = bool(re.search(r"\bBASIC\b|THEORETICALLY EXACT", note, re.I))
        ref = bool(re.search(r"\bREFERENCE\b", note, re.I))
        if ref:
            text = f"({_fmt(nominal)}{'°' if angle else ''})"
        elif basic:
            text = val
        elif tol is not None:
            lo, hi = tol
            if abs(hi + lo) < 1e-12 and hi > 0:
                text = f"{val} ±{_fmt(hi)}{'°' if angle else ''}"
            else:
                text = f"{val} {_dev(hi)}/{_dev(lo)}"
        else:
            text = val
        items.append(PmiItem("dim", text=text, boxed=basic and not ref, note=note))

    # ---- datum letters
    datum_letter = {i: d["DATUM"][4] for i, d in ents.items() if "DATUM" in d and len(d["DATUM"]) > 4}
    feature_letter: dict[int, str] = {}
    for d in ents.values():
        rel = d.get("SHAPE_ASPECT_RELATIONSHIP")
        if rel and len(rel) >= 4:
            a, b = int(rel[2]) if isinstance(rel[2], int) else -1, int(rel[3]) if isinstance(rel[3], int) else -1
            if a in datum_letter and "DATUM_FEATURE" in R.get(b):
                feature_letter[b] = datum_letter[a]
            elif b in datum_letter and "DATUM_FEATURE" in R.get(a):
                feature_letter[a] = datum_letter[b]
    df_ids = [i for i, d in sorted(ents.items()) if "DATUM_FEATURE" in d]
    letters_left = [l for l in sorted(datum_letter.values()) if l not in feature_letter.values()]
    for i in df_ids:
        letter = feature_letter.get(i) or (letters_left.pop(0) if letters_left else "")
        if letter:
            items.append(PmiItem("datum", text=letter))

    # ---- geometric tolerances
    zone_form: dict[int, str] = {}
    for d in ents.values():
        tz = d.get("TOLERANCE_ZONE")
        if tz and len(tz) >= 6:
            form = R.get(tz[5]).get("TOLERANCE_ZONE_FORM")
            for gt in (tz[4] if isinstance(tz[4], list) else []):
                if form:
                    zone_form[int(gt)] = str(form[0])
    for i, d in sorted(ents.items()):
        kind = next((GT_TYPES[t] for t in d if t in GT_TYPES), None)
        if kind is None:
            continue
        g = d.get("GEOMETRIC_TOLERANCE") or next((d[t] for t in d if t in GT_TYPES and d[t]), None)
        if not g or len(g) < 3:
            continue
        mag, _ = R.measure(g[2])
        if mag is None:
            continue
        diam = "circular" in zone_form.get(i, "") or "cylindrical" in zone_form.get(i, "")
        mods = d.get("GEOMETRIC_TOLERANCE_WITH_MODIFIERS")
        mod = ""
        if mods:
            names = {m.name for m in (mods[0] if isinstance(mods[0], list) else []) if isinstance(m, Enum)}
            mod = " M" if "MAXIMUM_MATERIAL_REQUIREMENT" in names else (" L" if "LEAST_MATERIAL_REQUIREMENT" in names else "")
        datums: list[str] = []
        wdr = d.get("GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE")
        if wdr:
            for ds in (wdr[0] if isinstance(wdr[0], list) else []):
                sysd = R.get(ds).get("DATUM_SYSTEM")
                for comp in (sysd[4] if sysd and len(sysd) > 4 and isinstance(sysd[4], list) else []):
                    c = R.get(comp).get("DATUM_REFERENCE_COMPARTMENT")
                    base = c[4] if c and len(c) > 4 else None
                    if isinstance(base, int) and int(base) in datum_letter:
                        datums.append(datum_letter[int(base)])
                    elif isinstance(base, list):
                        datums.append("-".join(datum_letter.get(int(x), "?") for x in base if isinstance(x, int)))
        tol = f"{'Ø' if diam else ''}{_fmt(mag)}{mod}"
        items.append(PmiItem("gdt", gdt=kind, tol=tol, datums=datums))
    return PmiModel(str(product), items)


def _thread_text(callout: str) -> list[str]:
    t = callout.strip()
    t = re.sub(r"\bDIA\s*", "Ø", t)
    t = re.sub(r"^(\d+)\s*x\s*(?=M)", r"\1× ", t)   # quantity prefix: '4x M8' -> '4× M8'
    t = re.sub(r"(\d)\s*x\s*(?=\d)", r"\1×", t)      # size x pitch, CSK dia x angle
    t = re.sub(r"(\d)deg\b", r"\1°", t)
    t = re.sub(r"\bDEPTH\s*(\d+(?:\.\d+)?)", r"↧\1", t)
    return [p.strip() for p in t.split("/") if p.strip()]


def _rest(note: str, m) -> str:
    return note[m.end():].lstrip("; ").strip()


# =============================================================================
# sheet renderer
# =============================================================================

W, H = 1190.55, 841.89
FRAME = fitz.Rect(56.7, 28.35, 1162.2, 813.5)


def _draw_symbol(page: fitz.Page, cell: fitz.Rect, kind: str) -> None:
    c = fitz.Point((cell.x0 + cell.x1) / 2, (cell.y0 + cell.y1) / 2)
    h = cell.height * 0.26

    def line(a, b):
        sh = page.new_shape()
        sh.draw_line(a, b)
        sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
        sh.commit()

    def circle(r):
        sh = page.new_shape()
        sh.draw_circle(c, r)
        sh.finish(width=0.7, color=(0, 0, 0))
        sh.commit()

    def poly(pts, fill=False):
        sh = page.new_shape()
        sh.draw_polyline(pts + [pts[0]])
        sh.finish(width=0.7, color=None if fill else (0, 0, 0), fill=(0, 0, 0) if fill else None, closePath=False)
        sh.commit()

    def arrow(tip, frm, size=4):
        ang = math.atan2(tip.y - frm.y, tip.x - frm.x)
        poly([tip, fitz.Point(tip.x - size * math.cos(ang - 0.3), tip.y - size * math.sin(ang - 0.3)),
              fitz.Point(tip.x - size * math.cos(ang + 0.3), tip.y - size * math.sin(ang + 0.3))], fill=True)

    P = fitz.Point
    if kind == "position":
        circle(h * 0.75); line(P(c.x - h * 1.6, c.y), P(c.x + h * 1.6, c.y)); line(P(c.x, c.y - h * 1.6), P(c.x, c.y + h * 1.6))
    elif kind == "perpendicularity":
        line(P(c.x - h * 1.3, c.y + h), P(c.x + h * 1.3, c.y + h)); line(P(c.x, c.y + h), P(c.x, c.y - h * 1.2))
    elif kind == "parallelism":
        line(P(c.x - h, c.y + h), P(c.x - h * 0.2, c.y - h)); line(P(c.x + h * 0.2, c.y + h), P(c.x + h, c.y - h))
    elif kind == "flatness":
        poly([P(c.x - h * 1.3, c.y + h * 0.6), P(c.x + h * 0.7, c.y + h * 0.6), P(c.x + h * 1.3, c.y - h * 0.6), P(c.x - h * 0.7, c.y - h * 0.6)])
    elif kind == "circular_runout":
        a, b = P(c.x - h, c.y + h), P(c.x + h, c.y - h)
        line(a, b); arrow(P(b.x + 1, b.y - 1), a)
    elif kind == "total_runout":
        for dx in (-h * 0.8, h * 0.6):
            a, b = P(c.x + dx - h * 0.5, c.y + h), P(c.x + dx + h * 0.5, c.y - h)
            line(a, b); arrow(P(b.x + 0.5, b.y - 1), a)
    elif kind in ("profile_surface", "profile_line"):
        sh = page.new_shape()
        sh.draw_curve(P(c.x - h * 1.2, c.y + h * 0.6), P(c.x, c.y - h * 1.4), P(c.x + h * 1.2, c.y + h * 0.6))
        sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
        sh.commit()
        if kind == "profile_surface":
            line(P(c.x - h * 1.2, c.y + h * 0.6), P(c.x + h * 1.2, c.y + h * 0.6))
    elif kind == "straightness":
        line(P(c.x - h * 1.3, c.y), P(c.x + h * 1.3, c.y))
    elif kind == "cylindricity":
        circle(h * 0.7); line(P(c.x - h * 1.4, c.y + h), P(c.x - h * 0.2, c.y - h)); line(P(c.x + h * 0.2, c.y + h), P(c.x + h * 1.4, c.y - h))
    elif kind == "angularity":
        line(P(c.x - h * 1.2, c.y + h), P(c.x + h * 1.2, c.y + h)); line(P(c.x - h * 1.2, c.y + h), P(c.x + h * 1.0, c.y - h))
    elif kind == "concentricity":
        circle(h * 0.5); circle(h * 1.0)
    elif kind == "symmetry":
        line(P(c.x - h * 0.8, c.y - h * 0.7), P(c.x + h * 0.8, c.y - h * 0.7))
        line(P(c.x - h * 1.3, c.y), P(c.x + h * 1.3, c.y))
        line(P(c.x - h * 0.8, c.y + h * 0.7), P(c.x + h * 0.8, c.y + h * 0.7))
    elif kind == "circularity":
        circle(h * 0.9)


def render_pmi_sheet(model: PmiModel, dest: Path) -> int:
    from app.services.dxf_to_pdf import _font_file

    fontfile = _font_file()
    doc = fitz.open()

    def text(page, p, s, size=9.5, bold=False):
        kw = {"fontsize": size}
        if fontfile:
            kw.update(fontname="pmi0", fontfile=fontfile)
        else:
            kw.update(fontname="hebo" if bold else "helv")
            s = s.replace("↧", "DEPTH ")
        page.insert_text(p, s, **kw)

    def rect(page, r, width=0.7):
        sh = page.new_shape()
        sh.draw_rect(r)
        sh.finish(width=width, color=(0, 0, 0))
        sh.commit()

    def width_of(s, size=9.5):
        f = fitz.Font(fontfile=fontfile) if fontfile else fitz.Font("helv")
        return f.text_length(s, fontsize=size)

    def new_page(n):
        page = doc.new_page(width=W, height=H)
        rect(page, FRAME, 1.4)
        for c in range(8):
            x = FRAME.x0 + (c + 0.5) * FRAME.width / 8
            for y in (18, H - 10):
                text(page, fitz.Point(x - 3, y), str(c + 1))
        for r in range(6):
            y = FRAME.y0 + (r + 0.5) * FRAME.height / 6 + 3
            for x in (30, W - 22):
                text(page, fitz.Point(x, y), "ABCDEF"[r])
        text(page, fitz.Point(90, 75), f"MODEL-BASED DEFINITION (STEP AP242 PMI): {model.product}", 9.5)
        text(page, fitz.Point(470, 770), "MODEL VIEW (SEMANTIC PMI)", 8.5)
        return page

    cols, x0s, row_h = 4, [100, 340, 580, 820], 42.0
    y_start, y_end = 120.0, 720.0
    rows = int((y_end - y_start) // row_h)
    page = new_page(1)
    slot = 0
    for it in model.items:
        if slot >= rows * cols:
            page = new_page(doc.page_count + 1)
            slot = 0
        x = x0s[slot % cols]
        y = y_start + (slot // cols) * row_h
        slot += 1
        if it.kind == "gdt":
            hgt, sw = 17.0, 20.0
            tw = width_of(it.tol, 9) + 10
            cells = [fitz.Rect(x, y - 12, x + sw, y + 5), fitz.Rect(x + sw, y - 12, x + sw + tw, y + 5)]
            cx = x + sw + tw
            for _ in it.datums:
                cells.append(fitz.Rect(cx, y - 12, cx + 17, y + 5))
                cx += 17
            frame = fitz.Rect(cells[0].x0, cells[0].y0, cells[-1].x1, cells[-1].y1)
            rect(page, frame)
            for c in cells[1:]:
                sh = page.new_shape()
                sh.draw_line(fitz.Point(c.x0, c.y0), fitz.Point(c.x0, c.y1))
                sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
                sh.commit()
            _draw_symbol(page, cells[0], it.gdt)
            text(page, fitz.Point(cells[1].x0 + 5, y + 1), it.tol, 9)
            for c, dl in zip(cells[2:], it.datums):
                text(page, fitz.Point(c.x0 + 5, y + 1), dl, 9)
        elif it.kind == "datum":
            box = fitz.Rect(x, y - 13, x + 17, y + 4)
            rect(page, box)
            text(page, fitz.Point(box.x0 + 5, box.y1 - 5), it.text)
            apex = fitz.Point(box.x1 + 10, (box.y0 + box.y1) / 2)
            sh = page.new_shape()
            sh.draw_line(fitz.Point(box.x1, apex.y), apex)
            sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
            sh.commit()
            sh = page.new_shape()
            pts = [apex, fitz.Point(apex.x + 8, apex.y - 4.5), fitz.Point(apex.x + 8, apex.y + 4.5)]
            sh.draw_polyline(pts + [pts[0]])
            sh.finish(fill=(0, 0, 0), color=None, closePath=False)
            sh.commit()
        elif it.kind == "thread":
            for k, ln in enumerate(it.lines):
                text(page, fitz.Point(x, y + k * 11), ln, 8.5)
        else:
            text(page, fitz.Point(x, y), it.text)
            if it.boxed:
                w = width_of(it.text)
                rect(page, fitz.Rect(x - 3, y - 9.5 * 0.95, x + w + 3, y + 9.5 * 0.3))
    n = doc.page_count
    doc.save(str(dest), garbage=3, deflate=True)
    doc.close()
    return n


def step_pmi_to_pdf(src: Path, dest: Path) -> dict:
    model = read_pmi(src)
    if not model.items:
        raise ValueError("STEP file has no semantic PMI")
    pages = render_pmi_sheet(model, dest)
    counts: dict[str, int] = {}
    for it in model.items:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    return {"converter": "step_ap242_pmi", "pages": pages, "vector_hint": True, "product": model.product,
            "pmi_counts": counts}
