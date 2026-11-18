"""
Synthetic engineering drawings with their own answer keys (generalisation check for the
TVS extractor). Everything here is generated from a seed: values, positions, rotations,
GD&T symbols, datum sets, fits, sheet count. The answer key is written from the generator's
own ground truth — never from the extractor.

    python -m tests.synth_drawings out_dir [seed ...]
"""

from __future__ import annotations

import math
import random
import sys
from collections import Counter, OrderedDict
from pathlib import Path

import fitz
from openpyxl import Workbook

from app.services import iso286

W, H = 1190.55, 841.89
FRAME = fitz.Rect(56.7, 28.35, 1162.2, 813.5)
COLS, ROWS = 8, 6
MM = 25.4 / 72
FONT = "helv"

GDT_SET = ["position", "perpendicularity", "parallelism", "flatness", "circular_runout", "total_runout",
           "profile_surface", "profile_line", "straightness", "cylindricity", "angularity", "concentricity", "symmetry",
           "circularity"]
GDT_LABEL = {
    "position": ("GD&T - Position", "⌖", "Position"), "perpendicularity": ("GD&T - Perpendicularity", "⊥", "Perpendicularity"),
    "parallelism": ("GD&T - Parallelism", "∥", "Parallelism"), "flatness": ("GD&T - Flatness", "⏥", "Flatness"),
    "circular_runout": ("GD&T - Circular runout", "↗", "Circular runout"),
    "total_runout": ("GD&T - Total runout", "⌰", "Total runout"),
    "profile_surface": ("GD&T - Profile of a surface", "⌓", "Profile of surface"),
    "straightness": ("GD&T - Straightness", "⏤", "Straightness"), "cylindricity": ("GD&T - Cylindricity", "⌭", "Cylindricity"),
    "angularity": ("GD&T - Angularity", "∠", "Angularity"), "concentricity": ("GD&T - Concentricity", "◎", "Concentricity"),
    "symmetry": ("GD&T - Symmetry", "⌯", "Symmetry"), "circularity": ("GD&T - Circularity", "○", "Circularity"),
    "profile_line": ("GD&T - Profile of a line", "⌒", "Profile of line"),
}
N_RA = {5: 0.4, 6: 0.8, 7: 1.6, 8: 3.2, 9: 6.3}


def zone(x: float, y: float) -> str:
    c = min(COLS - 1, max(0, int((x - FRAME.x0) / (FRAME.width / COLS))))
    r = min(ROWS - 1, max(0, int((y - FRAME.y0) / (FRAME.height / ROWS))))
    return f"{'ABCDEF'[r]}{c + 1}"


def tw(text: str, size: float) -> float:
    return fitz.get_text_length(text, fontname=FONT, fontsize=size)


class _Shape:
    """Shape facade: every draw_* + finish pair is committed at once (no long-lived Shape objects)."""

    def __init__(self, sheet: "Sheet"):
        self.sheet = sheet
        self._cur = None

    def __getattr__(self, name):
        if name.startswith("draw_"):
            def call(*a, **k):
                self._cur = self.sheet.page.new_shape()
                return getattr(self._cur, name)(*a, **k)
            return call
        raise AttributeError(name)

    def finish(self, **k):
        self._cur.finish(**k)
        self._cur.commit()
        self._cur = None

    def commit(self):
        pass


class Sheet:
    def __init__(self, doc: fitz.Document, no: int):
        doc.new_page(width=W, height=H)
        self.doc = doc
        self.no = no
        self._idx = doc.page_count - 1
        self.shape = _Shape(self)
        self.boxes: list[fitz.Rect] = []   # annotation areas (keep background marks away)

    @property
    def page(self) -> fitz.Page:
        return self.doc[self._idx]

    def text(self, p, s, size=9.5, rotate=0, morph=None):
        self.page.insert_text(p, s, fontname=FONT, fontsize=size, rotate=rotate, morph=morph)
        # Ground-truth geometry: the bbox PyMuPDF reports for the text just drawn
        best = None
        for b in self.page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
              for sp in ln["spans"]:
                t = sp["text"].strip()
                r = fitz.Rect(sp["bbox"])
                if t == s.strip():
                    d = min(abs(r.x0 - p.x) + abs(r.y1 - p.y), abs(r.x1 - p.x) + abs(r.y1 - p.y),
                            abs(r.x0 - p.x) + abs(r.y0 - p.y), abs(r.x1 - p.x) + abs(r.y0 - p.y))
                    if best is None or d < best[0]:
                        best = (d, r)
        if best is not None:
            _MEASURED[(round(p.x, 2), round(p.y, 2), s, size)] = best[1]

    def line(self, a, b, width=0.7, dashes=None):
        self.shape.draw_line(a, b)
        self.shape.finish(width=width, color=(0, 0, 0), dashes=dashes, closePath=False)

    def rect(self, r, width=0.7):
        self.shape.draw_rect(r)
        self.shape.finish(width=width, color=(0, 0, 0))

    def poly(self, pts, fill=False, closed=True):
        self.shape.draw_polyline(pts + ([pts[0]] if closed else []))
        self.shape.finish(width=0.7, color=(0, 0, 0), fill=(0, 0, 0) if fill else None, closePath=False)

    def circle(self, c, r):
        self.shape.draw_circle(c, r)
        self.shape.finish(width=0.7, color=(0, 0, 0))

    def arrow(self, tip, frm, size=6):
        ang = math.atan2(tip.y - frm.y, tip.x - frm.x)
        b1 = fitz.Point(tip.x - size * math.cos(ang - 0.3), tip.y - size * math.sin(ang - 0.3))
        b2 = fitz.Point(tip.x - size * math.cos(ang + 0.3), tip.y - size * math.sin(ang + 0.3))
        self.poly([tip, b1, b2], fill=True)


_MEASURED: dict = {}


def text_rect(p: fitz.Point, s: str, size: float, rotate: int = 0) -> fitz.Rect:
    hit = _MEASURED.get((round(p.x, 2), round(p.y, 2), s, size))
    if hit is not None:
        return fitz.Rect(hit)
    w = tw(s, size)
    if rotate == 90:
        return fitz.Rect(p.x - size * 0.78, p.y - w, p.x + size * 0.22, p.y)
    return fitz.Rect(p.x, p.y - size * 0.78, p.x + w, p.y + size * 0.22)


# =============================================================================
# annotation writers — each returns (expected-row dict, occupied rect)
# =============================================================================

def _row(sheet, rect, view, ftype, text, symbol=None, nominal=None, upper=None, lower=None, details="",
         datums=(), flag="-", critical="-", notes=""):
    c = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    nom = nominal
    mx = mn = None
    if isinstance(nom, (int, float)) and (upper is not None or lower is not None) and ftype not in ("Surface finish",):
        mx, mn = round(nom + (upper or 0), 6), round(nom + (lower or 0), 6)
    d = (list(datums) + [None, None, None])[:3]
    return OrderedDict([
        ("Sheet", sheet.no), ("Zone", zone(c.x, c.y)), ("X (mm)", round(c.x * MM, 1)), ("Y (mm)", round((H - c.y) * MM, 1)),
        ("View", view), ("Feature type", ftype), ("Symbol", symbol), ("Nominal", nom), ("Upper tolerance", upper),
        ("Lower tolerance", lower), ("Max limit", mx), ("Min limit", mn),
        ("Fit / thread / finish / GD&T details (incl. datums)", details), ("Primary datum", d[0]),
        ("Secondary datum", d[1]), ("Tertiary datum", d[2]), ("Boxed / reference flag", flag), ("Critical flag", critical),
        ("Notes", notes), ("Text as shown on drawing", text), ("_rect", fitz.Rect(rect)), ("_zone_rect", fitz.Rect(rect)),
    ])


def stacked(sh: Sheet, p: fitz.Point, nom: str, up: str, lo: str, rotate: int, size=9.5):
    """Nominal with stacked upper/lower deviations (smaller text), horizontal or rotated 90°."""
    sm = size * 0.7
    wn = tw(nom + " ", size)
    sh.text(p, nom, size, rotate)
    r = text_rect(p, nom, size, rotate)
    if rotate == 90:
        pu = fitz.Point(p.x - size * 0.35, p.y - wn)
        pl = fitz.Point(p.x + size * 0.3, p.y - wn)
    else:
        pu = fitz.Point(p.x + wn, p.y - size * 0.4)
        pl = fitz.Point(p.x + wn, p.y + size * 0.3)
    for q, t in ((pu, up), (pl, lo)):
        sh.text(q, t, sm, rotate)
        r |= text_rect(q, t, sm, rotate)
    return r


def fmt(v: float) -> str:
    return ("+" if v > 0 else "") + (f"{v:g}" if v != 0 else "0")


# =============================================================================
# drawing
# =============================================================================

def _near_zone_boundary(r: fitz.Rect, tol: float = 2.5) -> bool:
    c = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
    xs = [FRAME.x0 + k * FRAME.width / COLS for k in range(1, COLS)]
    ys = [FRAME.y0 + k * FRAME.height / ROWS for k in range(1, ROWS)]
    return any(abs(c.x - x) < tol for x in xs) or any(abs(c.y - y) < tol for y in ys)


def make_drawing(seed: int, pdf_path: Path, key_path: Path) -> dict:
    """Generate a drawing whose annotation centres are never ambiguous between two zones."""
    for shift in (0, 7, 14, 21, 28):
        info = _make_drawing(seed, pdf_path, key_path, shift)
        if not info.pop("ambiguous"):
            return info
    return info


def _make_drawing(seed: int, pdf_path: Path, key_path: Path, shift: float) -> dict:
    rng = random.Random(seed)
    n_sheets = 1 + seed % 3
    doc = fitz.open()
    sheets = [Sheet(doc, i + 1) for i in range(n_sheets)]
    rows: list[dict] = []
    notes: list[tuple[int, str, str, str]] = []   # (sheet, view/area, item type, text)
    used_vals: set[float] = set()
    cc_list: list[tuple[str, dict, str]] = []     # (cc id, row, table description)

    def uniq(lo, hi, step=1.0):  # uniq.used is the drawing-wide set of taken values
        while True:
            v = round(rng.randrange(int(lo / step), int(hi / step)) * step, 3)
            if v not in used_vals:
                used_vals.add(v)
                return v

    uniq.used = used_vals
    view_names = ["FRONT VIEW", "TOP VIEW", "SIDE VIEW", "SECTION A-A", "SECTION B-B", "DETAIL C  SCALE 2:1",
                  "MAIN VIEW  SCALE 2:1", "DETAIL D  SCALE 3:1"]
    rng.shuffle(view_names)
    for sh in sheets:
        _frame(sh, rng, n_sheets)
        notes.append((sh.no, "Revision table", "Heading", "REVISIONS"))
        notes.append((sh.no, "Revision table", "Heading", "REV | ZONE | DESCRIPTION | DATE | BY"))
        for rv in sh._revs:
            notes.append((sh.no, "Revision table", f"Revision {rv[0]}",
                          " | ".join(f"zone {v}" if i == 1 else v for i, v in enumerate(rv))))
        notes.append((sh.no, "Title block", "Title block", " | ".join(sh._title)))
        last = sh.no == n_sheets
        boxes = [fitz.Rect(66, 117 + shift, 420, 597 + shift), fitz.Rect(450, 117 + shift, 790, 597 + shift)]
        for vbox in boxes:
            label = view_names.pop()
            vname = _view_name(label)
            above = rng.random() < 0.4
            lp = fitz.Point(vbox.x0 + 40, vbox.y0 - 10 if above else vbox.y1 + 25)
            if "SCALE" in label and rng.random() < 0.5:
                # split run: title on one line, SCALE on the next
                head, scale = label.split("  SCALE ")
                sh.text(lp, head, 8.5)
                sh.text(fitz.Point(lp.x - 2, lp.y + 11), f"SCALE {scale}", 8.5)
                if above:
                    lp = fitz.Point(lp.x, lp.y - 11)
            else:
                sh.text(lp, label, 8.5)
            notes.append((sh.no, vname, "View label", label))
            slots = [fitz.Point(vbox.x0 + 20 + c * 160, vbox.y0 + 30 + r * 62) for r in range(7) for c in range(2)]
            rng.shuffle(slots)
            kinds = ["lin_pm", "stack", "dia_pm", "fit_print", "fit_bare", "radius", "angle", "chamfer",
                     "basic", "ref", "fcf", "datum", "surface", "thread", "inclined", "chamfer_qty", "vert_dim"]
            rng.shuffle(kinds)
            free = list(slots)
            for kind in kinds[: rng.randint(11, 14)]:
                slot = None
                for cand in free:
                    # Dry run on a scratch page: skip slots whose annotation centre would sit
                    # on a zone boundary (zone ambiguous by construction).
                    st, uv = rng.getstate(), set(used_vals)
                    scratch = Sheet(fitz.open(), 1)
                    scratch.no = sh.no
                    trial = _place(scratch, kind, cand, vname, rng, uniq)
                    rng.setstate(st)
                    used_vals.clear()
                    used_vals.update(uv)
                    if trial is not None and not _near_zone_boundary(trial["_zone_rect"]):
                        slot = cand
                        break
                if slot is None:
                    continue
                free.remove(slot)
                row = _place(sh, kind, slot, vname, rng, uniq)
                if row is None:
                    continue
                rows.append(row)
                # Critical-characteristic flags on a few dimension / FCF rows (needs the CC table)
                if row["Feature type"] in ("Linear dimension", "Diameter", "Fit (hole)", "Fit (shaft)", "Surface finish") \
                        or row["Feature type"].startswith("GD&T"):
                    row.setdefault("_cc_desc", row["Text as shown on drawing"])
                    if rng.random() < 0.3 and len(cc_list) < 5:
                        cid = f"CC{len(cc_list) + 1}"
                        r0 = row["_rect"]
                        dc = fitz.Point(r0.x1 + 18, (r0.y0 + r0.y1) / 2)
                        sh.poly([fitz.Point(dc.x - 12, dc.y), fitz.Point(dc.x, dc.y - 6.5), fitz.Point(dc.x + 12, dc.y),
                                 fitz.Point(dc.x, dc.y + 6.5)])
                        sh.text(fitz.Point(dc.x - 6.5, dc.y + 2.2), cid, 5.5)
                        row["Critical flag"] = cid
                        cc_list.append((cid, row, row["_cc_desc"]))
            _background(sh, vbox, [r["_rect"] for r in rows if r["Sheet"] == sh.no])
    # Free-text cross-reference on every sheet but the last
    for sh in sheets[:-1]:
        sh.text(fitz.Point(830, 300), "GENERAL NOTES AND REVISIONS:", 7)
        sh.text(fitz.Point(830, 310), f"SEE SHEET {n_sheets}.", 7)
        notes.append((sh.no, "Free text", "Cross-reference note", f"GENERAL NOTES AND REVISIONS: SEE SHEET {n_sheets}."))
    # Last sheet: general notes, fit table, CC table
    sh = sheets[-1]
    _general_notes(sh, rng, notes)
    if cc_list:
        _cc_table(sh, cc_list, notes)
    for sh in sheets:
        sh.shape.commit()
    doc.save(str(pdf_path))
    doc.close()
    _write_key(rows, notes, key_path)
    return {"sheets": n_sheets, "features": len(rows), "notes": len(notes),
            "ambiguous": any(_near_zone_boundary(r["_zone_rect"]) for r in rows)}


def _view_name(label: str) -> str:
    import re

    m = re.match(r"(FRONT|TOP|SIDE|MAIN) VIEW", label)
    if m:
        return f"{m.group(1).capitalize()} view"
    m = re.match(r"SECTION (\w-\w)", label)
    if m:
        return f"Section {m.group(1)}"
    m = re.match(r"DETAIL (\w)", label)
    return f"Detail {m.group(1)}"


def _frame(sh: Sheet, rng: random.Random, n: int):
    sh.rect(fitz.Rect(0, 0, W, H), 0.5)
    sh.rect(FRAME, 1.4)
    for c in range(COLS):
        x = FRAME.x0 + (c + 0.5) * FRAME.width / COLS
        for y in (18, H - 10):
            sh.text(fitz.Point(x - 3, y), str(c + 1), 9.5)
    for r in range(ROWS):
        y = FRAME.y0 + (r + 0.5) * FRAME.height / ROWS + 3
        for x in (30, W - 22):
            sh.text(fitz.Point(x, y), "ABCDEF"[r], 9.5)
    # Revision table (top right)
    rx0, ry0 = 822.0, FRAME.y0
    cols = [rx0, 860, 900, 1050, 1110, FRAME.x1]
    rh = 17.0
    revs = [("A", "-", "INITIAL RELEASE", f"0{rng.randint(1, 9)}-0{rng.randint(1, 9)}-2027", "QA")]
    if rng.random() < 0.6:
        revs.append(("B", zone(rng.uniform(100, 700), rng.uniform(100, 600)), "UPDATED TOLERANCES", "11-11-2027", "QA"))
    nrows = 2 + len(revs)
    for k in range(nrows + 1):
        sh.line(fitz.Point(rx0, ry0 + k * rh), fitz.Point(FRAME.x1, ry0 + k * rh))
    sh.line(fitz.Point(rx0, ry0), fitz.Point(rx0, ry0 + nrows * rh))
    for x in cols[1:-1]:
        sh.line(fitz.Point(x, ry0 + rh), fitz.Point(x, ry0 + nrows * rh))
    sh.text(fitz.Point(rx0 + 140, ry0 + 12), "REVISIONS", 7.5)
    head = ["REV", "ZONE", "DESCRIPTION", "DATE", "BY"]
    for i, t in enumerate(head):
        sh.text(fitz.Point(cols[i] + 3, ry0 + rh + 12), t, 7)
    for j, rv in enumerate(revs):
        for i, t in enumerate(rv):
            sh.text(fitz.Point(cols[i] + 3, ry0 + (2 + j) * rh + 12), t, 7)
    # Title block (bottom right)
    tx0, ty0 = 652.0, 658.0
    tr = [ty0, 700, 740, 780, FRAME.y1]
    for y in tr:
        sh.line(fitz.Point(tx0, y), fitz.Point(FRAME.x1, y))
    sh.line(fitz.Point(tx0, ty0), fitz.Point(tx0, FRAME.y1))
    for y0, y1, xs in ((700, 740, [900]), (740, 780, [820, 990]), (780, FRAME.y1, [900])):
        for x in xs:
            sh.line(fitz.Point(x, y0), fitz.Point(x, y1))
    part = f"PART {rng.randint(100, 999)}"
    cells = [
        (tx0, ty0, "TITLE", f"{part} BRACKET"),
        (tx0, 700, "DRAWING NO.", f"DWG-{rng.randint(1000, 9999)}"), (900, 700, "MATERIAL", "STEEL"),
        (tx0, 740, "SCALE", "1:1"), (820, 740, "REV", "B"), (990, 740, "SHEET", f"{sh.no} OF {n}"),
        (tx0, 780, "DRAWN", "QA"), (900, 780, "UNITS", "mm"),
    ]
    for x, y, lab, val in cells:
        sh.text(fitz.Point(x + 3, y + 9), lab, 5)
        sh.text(fitz.Point(x + 3, y + 28), val, 9)
    sh._title = [f"{lab} {val}" for _x, _y, lab, val in cells]
    sh._revs = revs


def _background(sh: Sheet, vbox: fitz.Rect, keep_out: list[fitz.Rect]):
    """Light construction marks so each view reads as one cluster of geometry."""
    y = vbox.y0
    while y <= vbox.y1:
        x = vbox.x0
        while x <= vbox.x1:
            p = fitz.Point(x, y)
            if not any((r + (-10, -10, 10, 10)).contains(p) for r in keep_out):
                sh.line(fitz.Point(x - 3, y), fitz.Point(x + 3, y), 0.4)
                sh.line(fitz.Point(x, y - 3), fitz.Point(x, y + 3), 0.4)
            x += 24
        y += 24


def _gdt_symbol(sh: Sheet, cell: fitz.Rect, kind: str):
    c = fitz.Point((cell.x0 + cell.x1) / 2, (cell.y0 + cell.y1) / 2)
    h = cell.height * 0.26
    L = sh.line
    if kind == "position":
        sh.circle(c, h * 0.75); L(fitz.Point(c.x - h * 1.6, c.y), fitz.Point(c.x + h * 1.6, c.y)); L(fitz.Point(c.x, c.y - h * 1.6), fitz.Point(c.x, c.y + h * 1.6))
    elif kind == "perpendicularity":
        L(fitz.Point(c.x - h * 1.3, c.y + h), fitz.Point(c.x + h * 1.3, c.y + h)); L(fitz.Point(c.x, c.y + h), fitz.Point(c.x, c.y - h * 1.2))
    elif kind == "parallelism":
        L(fitz.Point(c.x - h, c.y + h), fitz.Point(c.x - h * 0.2, c.y - h)); L(fitz.Point(c.x + h * 0.2, c.y + h), fitz.Point(c.x + h, c.y - h))
    elif kind == "flatness":
        sh.poly([fitz.Point(c.x - h * 1.3, c.y + h * 0.6), fitz.Point(c.x + h * 0.7, c.y + h * 0.6),
                 fitz.Point(c.x + h * 1.3, c.y - h * 0.6), fitz.Point(c.x - h * 0.7, c.y - h * 0.6)])
    elif kind == "circular_runout":
        a, b = fitz.Point(c.x - h, c.y + h), fitz.Point(c.x + h, c.y - h)
        L(a, b); sh.arrow(fitz.Point(b.x + 1, b.y - 1), a, 4)
    elif kind == "total_runout":
        for dx in (-h * 0.8, h * 0.6):
            a, b = fitz.Point(c.x + dx - h * 0.5, c.y + h), fitz.Point(c.x + dx + h * 0.5, c.y - h)
            L(a, b); sh.arrow(fitz.Point(b.x + 0.5, b.y - 1), a, 4)
    elif kind == "profile_surface":
        sh.shape.draw_curve(fitz.Point(c.x - h * 1.2, c.y + h * 0.6), fitz.Point(c.x, c.y - h * 1.4), fitz.Point(c.x + h * 1.2, c.y + h * 0.6))
        sh.shape.finish(width=0.7, color=(0, 0, 0))
        L(fitz.Point(c.x - h * 1.2, c.y + h * 0.6), fitz.Point(c.x + h * 1.2, c.y + h * 0.6))
    elif kind == "profile_line":
        sh.shape.draw_curve(fitz.Point(c.x - h * 1.2, c.y + h * 0.6), fitz.Point(c.x, c.y - h * 1.4), fitz.Point(c.x + h * 1.2, c.y + h * 0.6))
        sh.shape.finish(width=0.7, color=(0, 0, 0), closePath=False)
    elif kind == "straightness":
        L(fitz.Point(c.x - h * 1.3, c.y), fitz.Point(c.x + h * 1.3, c.y))
    elif kind == "cylindricity":
        sh.circle(c, h * 0.7); L(fitz.Point(c.x - h * 1.4, c.y + h), fitz.Point(c.x - h * 0.2, c.y - h)); L(fitz.Point(c.x + h * 0.2, c.y + h), fitz.Point(c.x + h * 1.4, c.y - h))
    elif kind == "angularity":
        L(fitz.Point(c.x - h * 1.2, c.y + h), fitz.Point(c.x + h * 1.2, c.y + h)); L(fitz.Point(c.x - h * 1.2, c.y + h), fitz.Point(c.x + h * 1.0, c.y - h))
    elif kind == "concentricity":
        sh.circle(c, h * 0.5); sh.circle(c, h * 1.0)
    elif kind == "symmetry":
        L(fitz.Point(c.x - h * 0.8, c.y - h * 0.7), fitz.Point(c.x + h * 0.8, c.y - h * 0.7))
        L(fitz.Point(c.x - h * 1.3, c.y), fitz.Point(c.x + h * 1.3, c.y))
        L(fitz.Point(c.x - h * 0.8, c.y + h * 0.7), fitz.Point(c.x + h * 0.8, c.y + h * 0.7))
    elif kind == "circularity":
        sh.circle(c, h * 0.9)


def _place(sh: Sheet, kind: str, p: fitz.Point, view: str, rng: random.Random, uniq) -> dict | None:
    size = 9.5
    if kind == "lin_pm":
        n, t = uniq(10, 200), rng.choice([0.05, 0.1, 0.2, 0.5])
        s = f"{n:g} ±{t:g}"
        sh.text(p, s, size)
        r = text_rect(p, s, size)
        row = _row(sh, r, view, "Linear dimension", s, None, n, t, -t, "Bilateral equal tolerance")
        row["_cc_desc"] = f"{n:g} ±{t:g} length"
    elif kind == "stack":
        n = uniq(5, 150)
        up, lo = rng.choice([(rng.choice([0.1, 0.2, 0.05]), 0.0), (0.0, -rng.choice([0.1, 0.2, 0.3])),
                             (rng.choice([0.05, 0.1]), -rng.choice([0.02, 0.05]))])
        rot = rng.choice([0, 90])
        r = stacked(sh, p if rot == 0 else fitz.Point(p.x + 10, p.y + 40), f"{n:g}", fmt(up), fmt(lo), rot)
        row = _row(sh, r, view, "Linear dimension", f"{n:g} {fmt(up)}/{fmt(lo)}", None, n, up, lo,
                   notes="Rotated (vertical) text" if rot else "")
        row["_cc_desc"] = f"{n:g} {fmt(up)}/{fmt(lo)}"
    elif kind == "dia_pm":
        n, t = uniq(5, 120), rng.choice([0.1, 0.2])
        s = f"Ø{n:g} ±{t:g}"
        rot = rng.choice([0, 90])
        q = p if rot == 0 else fitz.Point(p.x + 10, p.y + 45)
        sh.text(q, s, size, rot)
        row = _row(sh, text_rect(q, s, size, rot), view, "Diameter", s, "Ø", n, t, -t,
                   notes="Rotated (vertical) text" if rot else "")
        row["_cc_desc"] = f"Ø{n:g} bore"
    elif kind in ("fit_print", "fit_bare"):
        n = uniq(10, 120)
        letter = rng.choice(["H", "g", "h", "p", "f", "k", "m", "n", "G", "K", "N"])
        grade = rng.choice([6, 7, 8]) if letter not in ("k", "m", "n", "p") else rng.choice([6, 7])
        lim = iso286.limits(letter, grade, n)
        if lim is None:
            return None
        up, lo = lim
        code = f"{letter}{grade}"
        typ = "Fit (hole)" if letter.isupper() else "Fit (shaft)"
        if kind == "fit_print":
            rot = 90
            r = stacked(sh, fitz.Point(p.x + 10, p.y + 50), f"Ø{n:g} {code}", fmt(up), fmt(lo), rot)
            row = _row(sh, r, view, typ, f"Ø{n:g} {code} {fmt(up)}/{fmt(lo)}", "Ø", n, up, lo, f"ISO 286 {code}",
                       notes="Rotated (vertical) text")
        else:
            s = f"Ø{n:g} {code}"
            sh.text(p, s, size)
            row = _row(sh, text_rect(p, s, size), view, typ, s, "Ø", n, up, lo, f"ISO 286 {code}")
        row["_cc_desc"] = f"Ø{n:g} {code}"
    elif kind == "radius":
        n = uniq(1, 30)
        q = rng.choice([None, 2, 4])
        s = (f"{q}× " if q else "") + f"R{n:g}"
        sh.text(p, s, size)
        row = _row(sh, text_rect(p, s, size), view, "Radius", s, "R", n, notes=f"Qty {q}" if q else "")
    elif kind == "angle":
        a, t = rng.choice([15, 20, 30, 60, 75]), rng.choice([0.5, 1])
        s = f"{a}° ±{t:g}°"
        sh.text(p, s, size)
        row = _row(sh, text_rect(p, s, size), view, "Angular dimension", s, "°", a, t, -t)
    elif kind == "chamfer":
        a = rng.choice([0.5, 1, 1.5, 2, 3])
        s = f"{a:g} × 45°"
        sh.text(p, s, size)
        row = _row(sh, text_rect(p, s, size), view, "Chamfer", s, "°", a, details=f"Chamfer {a:g} mm × 45°")
    elif kind == "chamfer_qty":
        q, a, ang = rng.choice([2, 3, 4]), rng.choice([0.3, 0.5, 1]), rng.choice([30, 45])
        s = f"{q}× {a:g} × {ang}°"
        sh.text(p, s, size)
        row = _row(sh, text_rect(p, s, size), view, "Chamfer", s, "°", a, details=f"Chamfer {a:g} mm × {ang}°",
                   notes=f"Qty {q}")
    elif kind == "vert_dim":
        n, t = uniq(20, 120), rng.choice([0.05, 0.1, 0.2])
        a, b = fitz.Point(p.x + 14, p.y - 20), fitz.Point(p.x + 14, p.y + 45)
        sh.line(a, b)
        sh.arrow(a, b)
        sh.arrow(b, a)
        s = f"{n:g} ±{t:g}"
        q = fitz.Point(p.x + 10, p.y + 12 + tw(s, size) / 2)
        sh.text(q, s, size, 90)
        r = text_rect(q, s, size, 90)
        row = _row(sh, r, view, "Linear dimension", s, None, n, t, -t, notes="Rotated (vertical) text")
        row["_rect"] = r | fitz.Rect(a.x - 4, a.y, b.x + 4, b.y)
        row["_cc_desc"] = f"{n:g} ±{t:g}"
        return row
    elif kind == "basic":
        ang = rng.random() < 0.4
        n = rng.choice([30, 45, 60]) if ang else uniq(20, 160)
        s = f"{n:g}°" if ang else f"Ø{n:g}"
        sh.text(p, s, size)
        r = text_rect(p, s, size)
        sh.rect(fitz.Rect(r.x0 - 3, r.y0 - 2, r.x1 + 3, r.y1 + 2))
        row = _row(sh, r, view, "Basic angle" if ang else "Basic dimension", f"[{s}]", "°" if ang else "Ø", n,
                   flag="Boxed (basic)")
    elif kind == "ref":
        n = uniq(5, 300, 0.1)
        s = f"({n:g})"
        rot = rng.choice([0, 90])
        q = p if rot == 0 else fitz.Point(p.x + 10, p.y + 35)
        sh.text(q, s, size, rot)
        row = _row(sh, text_rect(q, s, size, rot), view, "Reference dimension", s, None, n, flag="Bracketed (reference)",
                   notes="Rotated (vertical) text" if rot else "")
    elif kind == "fcf":
        g = rng.choice(GDT_SET)
        val = rng.choice([0.01, 0.02, 0.03, 0.05, 0.1, 0.15, 0.2])
        diam = g in ("position", "perpendicularity", "concentricity") and rng.random() < 0.7
        mod = g == "position" and rng.random() < 0.5
        nd = 0 if g in ("flatness", "straightness", "cylindricity", "circularity", "profile_line") else rng.randint(1, 3)
        datums = ["A", "B", "C"][:nd]
        tol = f"{'Ø' if diam else ''}{val:g}" + (" M" if mod else "")
        hgt, sw = 17.0, 20.0
        tw_ = tw(tol, 9) + 10
        x = p.x
        cells = [fitz.Rect(x, p.y - 12, x + sw, p.y - 12 + hgt)]
        x += sw
        cells.append(fitz.Rect(x, p.y - 12, x + tw_, p.y - 12 + hgt))
        x += tw_
        for _ in datums:
            cells.append(fitz.Rect(x, p.y - 12, x + 17, p.y - 12 + hgt))
            x += 17
        frame = fitz.Rect(cells[0].x0, cells[0].y0, cells[-1].x1, cells[-1].y1)
        sh.rect(frame)
        for c in cells[1:]:
            sh.line(fitz.Point(c.x0, c.y0), fitz.Point(c.x0, c.y1))
        _gdt_symbol(sh, cells[0], g)
        sh.text(fitz.Point(cells[1].x0 + 5, p.y + 1), tol, 9)
        for c, d in zip(cells[2:], datums):
            sh.text(fitz.Point(c.x0 + 5, p.y + 1), d, 9)
        ftype, sym, label = GDT_LABEL[g]
        det = f"{label} {'Ø' if diam else ''}{val:g}" + (" at MMC" if mod else "")
        row = _row(sh, frame, view, ftype, f"{sym} {tol} | " + " | ".join(datums), sym, val, details=det, datums=datums)
        row["_cc_desc"] = f"{label.lower()} {'Ø' if diam else ''}{val:g} {' '.join(datums)}"
        row["_rect"] = frame
        return row | {"_rect": frame}
    elif kind == "datum":
        # each datum letter is defined once per drawing (FCFs reference A, B, C)
        free = [l for l in "ABC" if f"datum:{l}" not in uniq.used]
        if not free:
            return None
        letter = free[0]
        uniq.used.add(f"datum:{letter}")
        box = fitz.Rect(p.x, p.y - 13, p.x + 17, p.y + 4)
        sh.rect(box)
        sh.text(fitz.Point(box.x0 + 5, box.y1 - 5), letter, 9.5)
        cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
        d = rng.choice(["right", "left", "up", "down"])
        ux, uy = {"right": (1, 0), "left": (-1, 0), "up": (0, -1), "down": (0, 1)}[d]
        edge = fitz.Point(cx + ux * 8.5, cy + uy * 8.5)
        apex = fitz.Point(cx + ux * 18.5, cy + uy * 18.5)
        sh.line(edge, apex)
        base = fitz.Point(apex.x + ux * 8, apex.y + uy * 8)
        px, py = -uy * 4.5, ux * 4.5
        sh.poly([apex, fitz.Point(base.x + px, base.y + py), fitz.Point(base.x - px, base.y - py)], fill=True)
        r = text_rect(fitz.Point(box.x0 + 5, box.y1 - 5), letter, 9.5)
        row = _row(sh, r, view, "Datum feature", letter, None, letter)
        row["_rect"] = box | fitz.Rect(min(apex.x, base.x) - 5, min(apex.y, base.y) - 5, max(apex.x, base.x) + 5,
                                       max(apex.y, base.y) + 5)
        return row
    elif kind == "surface":
        k = rng.choice(["Ra", "Rz", "N", "UL"])
        q = fitz.Point(p.x + 30, p.y)
        sym_pts = [fitz.Point(q.x - 24, q.y - 4), fitz.Point(q.x - 18, q.y + 8), fitz.Point(q.x - 6, q.y - 13),
                   fitz.Point(q.x + 34, q.y - 13)]
        sh.poly(sym_pts, closed=False)
        if k == "UL":
            hi, lo = rng.choice([(3.2, 0.8), (1.6, 0.4), (6.3, 1.6)])
            s1, s2 = f"U Ra {hi:g}", f"L Ra {lo:g}"
            sh.text(q, s1, 7)
            q2 = fitz.Point(q.x, q.y + 9)
            sh.text(q2, s2, 7)
            r = text_rect(q, s1, 7) | text_rect(q2, s2, 7)
            row = _row(sh, r, view, "Surface finish", f"{s1} / {s2}", "Ra", f"Ra {lo:g}-{hi:g}", hi, lo,
                       f"Upper limit Ra {hi:g} µm / lower limit Ra {lo:g} µm")
        elif k == "N":
            g = rng.choice(list(N_RA))
            s = f"N{g}"
            sh.text(q, s, 7)
            row = _row(sh, text_rect(q, s, 7), view, "Surface finish", s, "N", s, N_RA[g],
                       details=f"ISO 1302 roughness grade N{g} (= Ra {N_RA[g]:g} µm)")
        else:
            v = rng.choice([0.4, 0.8, 1.6, 3.2, 6.3])
            s = f"{k} {v:g}"
            sh.text(q, s, 7)
            row = _row(sh, text_rect(q, s, 7), view, "Surface finish", s, k, v, v, details=f"{k} {v:g} µm max")
        row["_rect"] = row_rect = fitz.Rect(sym_pts[0].x, sym_pts[2].y, sym_pts[3].x, sym_pts[1].y) | text_rect(q, "Ra 0.0", 7)
        return row
    elif kind == "thread":
        d, pitch = rng.choice([(5, 0.8), (6, 1), (8, 1.25), (10, 1.5), (12, 1.75)])
        qty = rng.choice([None, 2, 3, 4])
        depth = d + rng.randint(2, 8)
        l1 = (f"{qty}× " if qty else "") + f"M{d}×{pitch:g}-6H x {depth} DEEP"
        drill = round(d - pitch, 1)
        l2 = f"Ø{drill:g} x {depth + 3} DEEP"
        sh.text(p, l1, 8.5)
        q2 = fitz.Point(p.x, p.y + 13)
        sh.text(q2, l2, 8.5)
        r = text_rect(p, l1, 8.5) | text_rect(q2, l2, 8.5)
        row = _row(sh, r, view, "Thread", f"{l1} / {l2}", "M", f"M{d}",
                   details=f"M{d}×{pitch:g} coarse, 6H, thread depth {depth}, tap drill Ø{drill:g} depth {depth + 3}",
                   notes=f"Qty {qty} ({qty}×)" if qty else "")
    elif kind == "inclined":
        n, t = uniq(20, 150), rng.choice([0.05, 0.1])
        ang = math.radians(rng.choice([25, 30, 35, 40, 50, 55]))
        a = fitz.Point(p.x, p.y + 40)
        b = fitz.Point(p.x + 120 * math.cos(ang), p.y + 40 - 120 * math.sin(ang))
        sh.line(a, b)
        sh.arrow(a, b)
        sh.arrow(b, a)
        s = f"{n:g} ±{t:g}"
        mid = fitz.Point((a.x + b.x) / 2 - tw(s, size) / 2, (a.y + b.y) / 2 - 6)
        sh.text(mid, s, size)
        r = text_rect(mid, s, size)
        row = _row(sh, r, view, "Inclined linear dimension", s, None, n, t, -t)
        row["_rect"] = r | fitz.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
        row["_cc_desc"] = f"{n:g} ±{t:g} inclined"
        return row
    else:
        return None
    return row


def _general_notes(sh: Sheet, rng: random.Random, notes):
    x, y = 830.0, 130.0
    sh.text(fitz.Point(x, y), "GENERAL NOTES", 9.5)
    notes.append((sh.no, "Notes block", "Heading", "GENERAL NOTES"))
    pool = [
        ["MATERIAL: STEEL BAR, CERTIFIED TO SUPPLIER SPECIFICATION."],
        ["DIMENSIONS IN mm. UNLESS OTHERWISE SPECIFIED, GENERAL", "TOLERANCES TO ISO 2768-m."],
        ["REMOVE ALL BURRS AND SHARP EDGES."],
        ["SURFACE TEXTURE Ra 3.2 ALL OVER UNLESS", "OTHERWISE STATED."],
        ["HEAT TREAT AND TEMPER AFTER MACHINING."],
    ]
    chosen = rng.sample(pool, rng.randint(3, 5))
    y += 22
    for i, lines in enumerate(chosen, start=1):
        sh.text(fitz.Point(x, y), f"{i}.", 7.4)
        for j, ln in enumerate(lines):
            sh.text(fitz.Point(x + 17, y + j * 13), ln, 7.4)
        notes.append((sh.no, "General notes", f"General note {i}", " ".join(lines)))
        y += 13 * len(lines)


def _cc_table(sh: Sheet, cc_list, notes):
    x0, y0, x1 = 830.0, 420.0, 1150.0
    cols = [x0, 870, 1060, x1]
    rh = 17.0
    sh.text(fitz.Point(x0, y0 - 6), "CRITICAL CHARACTERISTICS (CC)", 9)
    n = 1 + len(cc_list)
    for k in range(n + 1):
        sh.line(fitz.Point(x0, y0 + k * rh), fitz.Point(x1, y0 + k * rh))
    for x in cols:
        sh.line(fitz.Point(x, y0), fitz.Point(x, y0 + n * rh))
    for i, t in enumerate(["ID", "CHARACTERISTIC", "SHEET / VIEW"]):
        sh.text(fitz.Point(cols[i] + 4, y0 + 12), t, 7)
    notes.append((sh.no, "CC table", "Heading", "ID | CHARACTERISTIC | SHEET / VIEW"))
    for j, (cid, row, desc) in enumerate(cc_list, start=1):
        where = f"S{row['Sheet']} / {row['View'].upper()}"
        for i, t in enumerate([cid, desc, where]):
            sh.text(fitz.Point(cols[i] + 4, y0 + j * rh + 12), t, 7)
        notes.append((sh.no, "CC table", "Critical characteristic", f"{cid}: {desc} ({where})"))


# =============================================================================
# answer key
# =============================================================================

NEAR_MM = 15.0
TITLE_BLOCK = fitz.Rect(652.0, 658.0, FRAME.x1, FRAME.y1)


def _near_border(r: fitz.Rect) -> bool:
    """Same rule as the extractor: text box within NEAR_MM of the frame or the title block / revision table."""
    lim = NEAR_MM / MM
    if min(r.x0 - FRAME.x0, FRAME.x1 - r.x1, r.y0 - FRAME.y0, FRAME.y1 - r.y1) <= lim:
        return True
    rev = fitz.Rect(822.0, FRAME.y0, FRAME.x1, FRAME.y0 + 4 * 17.0)
    for b in (TITLE_BLOCK, rev):
        dx = max(b.x0 - r.x1, 0, r.x0 - b.x1)
        dy = max(b.y0 - r.y1, 0, r.y0 - b.y1)
        if math.hypot(dx, dy) <= lim:
            return True
    return False


def _write_key(rows: list[dict], notes, key_path: Path):
    from app.services.tvs_extraction_report import _scope_coverage

    rows = sorted(rows, key=lambda r: (r["Sheet"], -r["Y (mm)"], r["X (mm)"]))
    per = Counter()
    feats = []
    for r in rows:
        per[r["Sheet"]] += 1
        f = OrderedDict([("Feature ID", f"S{r['Sheet']}-{per[r['Sheet']]:03d}")])
        f.update((k, v) for k, v in r.items() if not k.startswith("_"))
        if _near_border(r["_zone_rect"]):  # rule is on the annotation's text box
            f["Notes"] = ((f.get("Notes") or "") + "; Placed near border").strip("; ")
        f["_toleranced"] = r["Feature type"] not in ("Surface finish",) and not r["Feature type"].startswith("GD&T") \
            and (r["Upper tolerance"] is not None or r["Lower tolerance"] is not None)
        f["_tol_kind"] = ""
        feats.append(f)
    # Title-block and revision notes come from the sheets' tables
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Synthetic answer key"])
    ws.append([])
    ws.append(["Features by type"])
    ws.append(["Feature type", "Count"])
    for name, n in Counter(f["Feature type"] for f in feats).most_common():
        ws.append([name, n])
    ws.append(["TOTAL features", len(feats)])
    ws.append([])
    ws.append(["Features by sheet"])
    ws.append(["Sheet", "Count"])
    for s, n in sorted(Counter(f["Sheet"] for f in feats).items()):
        ws.append([s, n])
    ws.append([])
    ws.append(["Scope coverage"])
    ws.append(["Scope item", "Count", "Feature IDs"])
    for label, fids in _scope_coverage(feats, []):
        ws.append([label, len(fids), ", ".join(fids)])
    ws2 = wb.create_sheet("Expected Extraction")
    keys = [k for k in feats[0] if not k.startswith("_")] if feats else []
    ws2.append(keys)
    for f in feats:
        ws2.append([f[k] for k in keys])
    ws3 = wb.create_sheet("Notes")
    ws3.append(["#", "Sheet", "Zone", "View / area", "Item type", "Text (verbatim)"])
    for i, (s, v, t, txt) in enumerate(notes, start=1):
        ws3.append([i, s, "", v, t, txt])
    wb.save(str(key_path))


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for sd in [int(a) for a in sys.argv[2:]] or [1, 2, 3]:
        info = make_drawing(sd, out / f"synth_{sd}.pdf", out / f"synth_{sd}_key.xlsx")
        print(sd, info)
