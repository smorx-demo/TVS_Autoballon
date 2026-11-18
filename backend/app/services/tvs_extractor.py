"""
Drawing-level extractor for the TVS extraction report.

Reads a vector PDF drawing and returns every annotated feature and note, independent of
the balloon engine:

  1. primitives  – text lines/spans (with direction and size), segments, curves/circles,
                   filled/open triangles, quads (boxes, diamonds, parallelograms)
  2. regions     – sheet frame, ruled tables (title block, revision, fit limits, CC, …),
                   general-notes block, view labels and view areas
  3. annotations – claimed in priority order so graphics/text are never reused:
                   feature control frames → datum features → CC flags → surface texture
                   → thread callouts → dimensions (stacked/unilateral tolerances, fits,
                   basic/boxed, reference/bracketed, radii, chamfers, angles)
  4. mapping     – view, critical flag (CC flag proximity + CC table cross-check),
                   PCD / hole-pattern links

All rules are generic; nothing is keyed to a particular drawing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from app.services import iso286

PT_TO_MM = 25.4 / 72.0


# =============================================================================
# primitives
# =============================================================================

@dataclass
class Span:
    text: str
    rect: fitz.Rect
    size: float
    font: str


@dataclass
class TLine:
    text: str
    rect: fitz.Rect
    dir: tuple[float, float]
    size: float
    spans: list[Span]
    bold: bool
    used: bool = False

    @property
    def center(self) -> fitz.Point:
        return fitz.Point((self.rect.x0 + self.rect.x1) / 2, (self.rect.y0 + self.rect.y1) / 2)

    @property
    def horizontal(self) -> bool:
        return abs(self.dir[1]) < 0.05 and self.dir[0] > 0


@dataclass
class Seg:
    a: fitz.Point
    b: fitz.Point
    pid: int
    dashed: bool = False
    width: float = 0.0

    @property
    def length(self) -> float:
        return math.hypot(self.b.x - self.a.x, self.b.y - self.a.y)

    @property
    def angle(self) -> float:
        """0..180 degrees, y measured upwards."""
        a = math.degrees(math.atan2(-(self.b.y - self.a.y), self.b.x - self.a.x)) % 180.0
        return 0.0 if a > 179.5 else a

    @property
    def is_h(self) -> bool:
        return abs(self.a.y - self.b.y) <= 0.6

    @property
    def is_v(self) -> bool:
        return abs(self.a.x - self.b.x) <= 0.6

    @property
    def rect(self) -> fitz.Rect:
        return fitz.Rect(min(self.a.x, self.b.x), min(self.a.y, self.b.y), max(self.a.x, self.b.x), max(self.a.y, self.b.y))


@dataclass
class Curve:
    pid: int
    ends: list[fitz.Point]       # on-curve points
    rect: fitz.Rect
    closed: bool
    dashed: bool
    samples: list[fitz.Point] = field(default_factory=list)
    width: float = 0.0

    def circle(self) -> tuple[fitz.Point, float] | None:
        """Circle through first / middle / last sample points (arc geometry)."""
        pts = self.samples or self.ends
        if len(pts) < 3:
            return None
        a, b, c = pts[0], pts[len(pts) // 2], pts[-1]
        d = 2 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y))
        if abs(d) < 1e-6:
            return None
        ux = ((a.x ** 2 + a.y ** 2) * (b.y - c.y) + (b.x ** 2 + b.y ** 2) * (c.y - a.y) + (c.x ** 2 + c.y ** 2) * (a.y - b.y)) / d
        uy = ((a.x ** 2 + a.y ** 2) * (c.x - b.x) + (b.x ** 2 + b.y ** 2) * (a.x - c.x) + (c.x ** 2 + c.y ** 2) * (b.x - a.x)) / d
        ctr = fitz.Point(ux, uy)
        return ctr, math.hypot(a.x - ux, a.y - uy)

    @property
    def center(self) -> fitz.Point:
        xs = [p.x for p in self.ends]
        ys = [p.y for p in self.ends]
        if self.closed and len(self.ends) >= 2:
            return fitz.Point(sum(xs) / len(xs), sum(ys) / len(ys))
        return fitz.Point((self.rect.x0 + self.rect.x1) / 2, (self.rect.y0 + self.rect.y1) / 2)

    @property
    def radius(self) -> float:
        c = self.center
        if self.closed and len(self.ends) >= 2:
            return sum(math.hypot(p.x - c.x, p.y - c.y) for p in self.ends) / len(self.ends)
        return max(self.rect.width, self.rect.height) / 2


@dataclass
class Poly:
    pts: list[fitz.Point]
    rect: fitz.Rect
    filled: bool
    pid: int

    @property
    def axis_aligned(self) -> bool:
        n = len(self.pts)
        return all(abs(self.pts[i].x - self.pts[(i + 1) % n].x) <= 0.6 or abs(self.pts[i].y - self.pts[(i + 1) % n].y) <= 0.6
                   for i in range(n))

    @property
    def center(self) -> fitz.Point:
        return fitz.Point(sum(p.x for p in self.pts) / len(self.pts), sum(p.y for p in self.pts) / len(self.pts))


def _pt(p) -> fitz.Point:
    return fitz.Point(float(p.x), float(p.y))


def _close(p: fitz.Point, q: fitz.Point, tol: float = 1.0) -> bool:
    return abs(p.x - q.x) <= tol and abs(p.y - q.y) <= tol


def _norm(t: str) -> str:
    t = (t or "").replace("−", "-").replace("–", "-").replace("±", "±")
    t = t.replace("∅", "Ø").replace("⌀", "Ø")
    return re.sub(r"\s+", " ", t).strip()


class PageData:
    def __init__(self, page: fitz.Page, index: int):
        self.page = page
        self.index = index
        self.rect = page.rect
        self.lines: list[TLine] = []
        self.segs: list[Seg] = []
        self.curves: list[Curve] = []
        self.polys: list[Poly] = []
        self._load_text()
        self._load_paths()

    # -- text -------------------------------------------------------------------
    def _load_text(self) -> None:
        for block in self.page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = [
                    Span(s.get("text", ""), fitz.Rect(s["bbox"]), float(s.get("size") or 0), s.get("font", ""))
                    for s in line.get("spans", [])
                ]
                text = _norm("".join(s.text for s in spans))
                if not text:
                    continue
                size = max((s.size for s in spans if s.text.strip()), default=0.0)
                bold = any("bold" in s.font.lower() for s in spans if s.text.strip())
                d = line.get("dir") or (1.0, 0.0)
                self.lines.append(TLine(text, fitz.Rect(line["bbox"]), (float(d[0]), float(d[1])), size, spans, bold))

    # -- vector paths -----------------------------------------------------------------
    def _load_paths(self) -> None:
        for pid, d in enumerate(self.page.get_drawings()):
            dashed = bool(d.get("dashes")) and str(d.get("dashes")).strip() not in ("[] 0", "[]0", "")
            filled = d.get("fill") is not None
            # Stroked paths without an explicit width use the PDF default (1.0); fill-only paths have none
            width = float(d.get("width") or (1.0 if d.get("color") is not None else 0.0))
            items = d.get("items") or []
            lines_only = all(it[0] == "l" for it in items)
            curves_only = all(it[0] == "c" for it in items) and items
            if curves_only:
                ends = [_pt(items[0][1])] + [_pt(it[4]) for it in items]
                closed = _close(ends[0], ends[-1], 0.8)
                samples = [_pt(items[0][1])]
                for it in items:
                    p0, p1, p2, p3 = (_pt(q) for q in it[1:5])
                    samples.append(fitz.Point((p0.x + 3 * p1.x + 3 * p2.x + p3.x) / 8, (p0.y + 3 * p1.y + 3 * p2.y + p3.y) / 8))
                    samples.append(p3)
                self.curves.append(Curve(pid, ends[:-1] if closed else ends, fitz.Rect(d["rect"]), closed, dashed, samples, width))
                continue
            if lines_only and len(items) in (3, 4):
                pts = [_pt(it[1]) for it in items]
                closed = _close(_pt(items[-1][2]), pts[0], 0.8) and all(
                    _close(_pt(items[k][2]), _pt(items[k + 1][1]), 0.8) for k in range(len(items) - 1))
                if closed:
                    self.polys.append(Poly(pts, fitz.Rect(d["rect"]), filled, pid))
            for it in items:
                op = it[0]
                if op == "l":
                    self.segs.append(Seg(_pt(it[1]), _pt(it[2]), pid, dashed, width))
                elif op == "re":
                    r = fitz.Rect(it[1])
                    pts = [r.tl, r.tr, r.br, r.bl]
                    self.polys.append(Poly([_pt(p) for p in pts], fitz.Rect(r), filled, pid))
                    for k in range(4):
                        self.segs.append(Seg(_pt(pts[k]), _pt(pts[(k + 1) % 4]), pid, dashed))
                elif op == "qu":
                    q = it[1]
                    pts = [_pt(q.ul), _pt(q.ur), _pt(q.lr), _pt(q.ll)]
                    self.polys.append(Poly(pts, fitz.Rect(q.rect), filled, pid))
                    for k in range(4):
                        self.segs.append(Seg(pts[k], pts[(k + 1) % 4], pid, dashed))
                elif op == "c" and not curves_only:
                    self.curves.append(Curve(pid, [_pt(it[1]), _pt(it[4])], fitz.Rect(d["rect"]), False, dashed, width=width))

    # -- helpers -----------------------------------------------------------------------
    def words(self) -> list[TLine]:
        """Word-level text items (as TLines) for cell-by-cell reading."""
        if getattr(self, "_words", None) is None:
            self._words = []
            for x0, y0, x1, y1, w, *_ in self.page.get_text("words"):
                t = _norm(w)
                if t:
                    r = fitz.Rect(x0, y0, x1, y1)
                    self._words.append(TLine(t, r, (1.0, 0.0), r.height, [Span(t, r, r.height, "")], False))
        return self._words

    def thin_width(self) -> float | None:
        """Most common stroke weight on the sheet (dimension / leader lines)."""
        if getattr(self, "_thin", "unset") == "unset":
            from collections import Counter

            c = Counter(round(sg.width, 2) for sg in self.segs if sg.width > 0)
            self._thin = c.most_common(1)[0][0] if c else None
        return self._thin

    def strokes_in(self, area: fitz.Rect) -> tuple[list[Seg], list[Curve], list[Poly]]:
        segs = [s for s in self.segs if area.contains(s.a) and area.contains(s.b)]
        curves = [c for c in self.curves if area.contains(c.rect.tl) and area.contains(c.rect.br)]
        polys = [p for p in self.polys if area.contains(p.rect.tl) and area.contains(p.rect.br)]
        return segs, curves, polys

    def arrowheads(self) -> list[Poly]:
        return [p for p in self.polys if p.filled and len(p.pts) == 3 and max(p.rect.width, p.rect.height) <= 12]


# =============================================================================
# feature model
# =============================================================================

@dataclass
class Feature:
    sheet: int
    rect: fitz.Rect
    ftype: str
    text: str
    symbol: str = ""
    nominal: Any = None
    upper: float | None = None
    lower: float | None = None
    details: list[str] = field(default_factory=list)
    datums: list[str] = field(default_factory=list)
    flag: str = "-"
    critical: str = "-"
    notes: list[str] = field(default_factory=list)
    view: str = ""
    kind: str = ""                 # internal: fcf, datum, surface, thread, dim
    meta: dict = field(default_factory=dict)

    @property
    def center(self) -> fitz.Point:
        return fitz.Point((self.rect.x0 + self.rect.x1) / 2, (self.rect.y0 + self.rect.y1) / 2)

    @property
    def toleranced(self) -> bool:
        return self.upper is not None or self.lower is not None


@dataclass
class Note:
    sheet: int
    rect: fitz.Rect
    view: str
    kind: str
    text: str


# =============================================================================
# regions
# =============================================================================

def _inner_frame(pd: PageData) -> fitz.Rect:
    pr = pd.rect
    best = None
    for p in pd.polys:
        if p.axis_aligned and len(p.pts) == 4 and p.rect.width > pr.width * 0.7 and p.rect.height > pr.height * 0.7:
            if best is None or p.rect.width * p.rect.height < best.width * best.height:
                best = p.rect
    if best is None:
        # Frame drawn as separate long lines
        hs = sorted([s for s in pd.segs if s.is_h and s.length > pr.width * 0.7], key=lambda s: s.a.y)
        vs = sorted([s for s in pd.segs if s.is_v and s.length > pr.height * 0.7], key=lambda s: s.a.x)
        if len(hs) >= 2 and len(vs) >= 2:
            inner_h = [s for s in hs if pr.y0 + 5 < s.a.y < pr.y1 - 5]
            inner_v = [s for s in vs if pr.x0 + 5 < s.a.x < pr.x1 - 5]
            if len(inner_h) >= 2 and len(inner_v) >= 2:
                best = fitz.Rect(inner_v[0].a.x, inner_h[0].a.y, inner_v[-1].a.x, inner_h[-1].a.y)
    return best or fitz.Rect(pr)


@dataclass
class Table:
    rect: fitz.Rect
    rows: list[list[str]]          # cell texts per row (top → bottom)
    row_rects: list[fitz.Rect]
    kind: str
    lines: list[TLine]


def _merge_cover(intervals: list[tuple[float, float]]) -> float:
    total, cur0, cur1 = 0.0, None, None
    for a, b in sorted(intervals):
        if cur1 is None or a > cur1 + 0.8:
            if cur1 is not None:
                total += cur1 - cur0
            cur0, cur1 = a, b
        else:
            cur1 = max(cur1, b)
    if cur1 is not None:
        total += cur1 - cur0
    return total


def _components(segs: list[Seg], tol: float = 1.5) -> list[list[Seg]]:
    """Connected components of axis-aligned segments (touching / crossing)."""
    n = len(segs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rects = [s.rect + (-tol, -tol, tol, tol) for s in segs]
    order = sorted(range(n), key=lambda i: rects[i].x0)
    active: list[int] = []
    for i in order:
        active = [j for j in active if rects[j].x1 >= rects[i].x0]
        for j in active:
            if rects[i].intersects(rects[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
        active.append(i)
    groups: dict[int, list[Seg]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(segs[i])
    return list(groups.values())


def _seg_bbox(segs: list[Seg]) -> fitz.Rect:
    """Bounds of segments (explicit min/max: zero-area rects are 'empty' to fitz union)."""
    xs = [v for s in segs for v in (s.a.x, s.b.x)]
    ys = [v for s in segs for v in (s.a.y, s.b.y)]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def _find_tables(pd: PageData, frame: fitz.Rect) -> list[Table]:
    axis = [s for s in pd.segs if (s.is_h or s.is_v) and s.length >= 4 and not s.dashed
            and (frame + (-2, -2, 2, 2)).contains(fitz.Point((s.a.x + s.b.x) / 2, (s.a.y + s.b.y) / 2))]
    # Ignore the frame border itself
    axis = [s for s in axis if not ((s.is_h and s.length > frame.width * 0.9) or (s.is_v and s.length > frame.height * 0.9))]
    tables: list[Table] = []
    for comp in _components(axis):
        bbox = _seg_bbox(comp)
        if bbox.width < 40 or bbox.height < 20:
            continue
        # Full-width horizontal rules
        ys: dict[float, list[tuple[float, float]]] = {}
        for s in comp:
            if s.is_h:
                ys.setdefault(round(s.a.y * 2) / 2, []).append((min(s.a.x, s.b.x), max(s.a.x, s.b.x)))
        rules = sorted(y for y, iv in ys.items() if _merge_cover(iv) >= bbox.width * 0.95)
        rules = [y for k, y in enumerate(rules) if k == 0 or y - rules[k - 1] > 2.0]
        if len(rules) < 3:
            continue
        inside = [ln for ln in pd.lines if bbox.contains(ln.center)]
        rows, row_rects = [], []
        for y0, y1 in zip(rules, rules[1:]):
            band = fitz.Rect(bbox.x0, y0, bbox.x1, y1)
            cells_lines = sorted([ln for ln in inside if band.contains(ln.center)], key=lambda ln: (ln.rect.x0, ln.rect.y0))
            if not cells_lines:
                continue
            # Column boundaries: vertical rules crossing the band middle
            ym = (y0 + y1) / 2
            xs = sorted({round(s.a.x, 1) for s in comp if s.is_v and min(s.a.y, s.b.y) <= ym <= max(s.a.y, s.b.y)})
            cells: dict[int, list[TLine]] = {}
            for ln in cells_lines:
                col = sum(1 for x in xs if x <= ln.center.x)
                cells.setdefault(col, []).append(ln)
            row = [" ".join(l.text for l in sorted(cells[c], key=lambda l: (round(l.rect.y0), l.rect.x0))) for c in sorted(cells)]
            rows.append(row)
            row_rects.append(band)
        header_first = bool(rows) and all(not re.search(r"\d", c) and c.upper() == c for c in rows[0]) and len(rows[0]) >= 2
        if len(rows) < 3 and not (len(rows) == 2 and header_first):
            continue
        tables.append(Table(bbox, rows, row_rects, _table_kind(rows), inside))
    return tables


def _table_kind(rows: list[list[str]]) -> str:
    head = " ".join(" ".join(r) for r in rows[:2]).upper()
    allt = " ".join(" ".join(r) for r in rows).upper()
    if re.search(r"\bREV\b", head) and re.search(r"\bDATE\b|\bDESCRIPTION\b", head):
        return "revision"
    if re.search(r"\bFIT\b", head) and re.search(r"\bDEVIATION|\bMAX\b|\bMIN\b", head):
        return "fit"
    if re.search(r"\bCHARACTERISTIC", head):
        return "cc"
    if re.search(r"\bTITLE\b|\bDRAWING\s+NO\b|\bSCALE\b", allt) and re.search(r"\bSHEET\b|\bMATERIAL\b", allt):
        return "title"
    return "table"


VIEW_RE = re.compile(
    r"^\s*(?:"
    r"(?P<named>MAIN|FRONT|TOP|BOTTOM|SIDE|LEFT|RIGHT|REAR|BACK|END|PLAN|ISOMETRIC|AUXILIARY|PARTIAL|ENLARGED"
    r"|EXPLODED|SECTIONAL|ELEVATION)\s+VIEW"
    r"|SECTION\s+(?P<sec>[A-Z]{1,2}\s*-\s*[A-Z]{1,2})"
    r"|DETAIL\s+(?P<det>[A-Z]{1,2})\b"
    r"|VIEW\s+(?P<view>[A-Z]{1,2})\b"
    r")",
    re.IGNORECASE,
)


def view_name(m: re.Match) -> str:
    if m.group("named"):
        return f"{m.group('named').capitalize()} view"
    if m.group("sec"):
        return "Section " + re.sub(r"\s+", "", m.group("sec").upper())
    if m.group("det"):
        return "Detail " + m.group("det").upper()
    return "View " + m.group("view").upper()


@dataclass
class ViewLabel:
    name: str
    rect: fitz.Rect
    text: str
    scale: float | None   # drawing units per mm (e.g. 2.0 for SCALE 2:1)


def _view_labels(pd: PageData, frame: fitz.Rect, claimed: list[fitz.Rect]) -> list[ViewLabel]:
    out = []
    for ln in pd.lines:
        if ln.used or not frame.contains(ln.center) or any(r.contains(ln.center) for r in claimed):
            continue
        m = VIEW_RE.match(ln.text)
        if not m or not ln.horizontal:
            continue
        text, rect = ln.text, fitz.Rect(ln.rect)
        ln.used = True
        # Continuation lines directly below: "(…)" qualifiers or a "SCALE n:m" line
        for nx in sorted(pd.lines, key=lambda l: l.rect.y0):
            if nx.used or not (nx.text.startswith("(") or re.match(r"^SCALE\s+\d", nx.text, re.I)):
                continue
            if 0 <= nx.rect.y0 - rect.y1 <= max(6.0, ln.size) and nx.rect.x0 < rect.x1 and nx.rect.x1 > rect.x0:
                text = f"{text} {nx.text}"
                rect |= nx.rect
                nx.used = True
        sm = re.search(r"SCALE\s+(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)", text, re.IGNORECASE)
        scale = float(sm.group(1).replace(",", ".")) / float(sm.group(2).replace(",", ".")) if sm else None
        out.append(ViewLabel(view_name(m), rect, text, scale))
    return out


# =============================================================================
# annotation detectors
# =============================================================================

GDT_NAMES = {
    "position": ("GD&T - Position", "⌖", "Position"),
    "flatness": ("GD&T - Flatness", "⏥", "Flatness"),
    "straightness": ("GD&T - Straightness", "⏤", "Straightness"),
    "circularity": ("GD&T - Circularity", "○", "Circularity"),
    "cylindricity": ("GD&T - Cylindricity", "⌭", "Cylindricity"),
    "profile_surface": ("GD&T - Profile of a surface", "⌓", "Profile of surface"),
    "profile_line": ("GD&T - Profile of a line", "⌒", "Profile of line"),
    "perpendicularity": ("GD&T - Perpendicularity", "⊥", "Perpendicularity"),
    "parallelism": ("GD&T - Parallelism", "∥", "Parallelism"),
    "angularity": ("GD&T - Angularity", "∠", "Angularity"),
    "circular_runout": ("GD&T - Circular runout", "↗", "Circular runout"),
    "total_runout": ("GD&T - Total runout", "⌰", "Total runout"),
    "concentricity": ("GD&T - Concentricity", "◎", "Concentricity"),
    "symmetry": ("GD&T - Symmetry", "⌯", "Symmetry"),
}

# Unicode GD&T glyphs and common CAD "GDT" font letter codes (gdt.shx / GDT TrueType)
GLYPH_TO_GDT = {
    "⌖": "position", "⏥": "flatness", "⏤": "straightness", "○": "circularity", "⌭": "cylindricity",
    "⌓": "profile_surface", "⌒": "profile_line", "⊥": "perpendicularity", "∥": "parallelism", "//": "parallelism",
    "∠": "angularity", "↗": "circular_runout", "⌰": "total_runout", "◎": "concentricity", "⌯": "symmetry",
}
GDT_FONT_CODES = {
    "j": "position", "c": "flatness", "u": "straightness", "e": "circularity", "g": "cylindricity",
    "d": "profile_surface", "k": "profile_line", "b": "perpendicularity", "f": "parallelism",
    "a": "angularity", "h": "circular_runout", "t": "total_runout", "r": "concentricity", "i": "symmetry",
}


def symbol_strokes(pd: PageData, cell: fitz.Rect) -> tuple[list[Seg], list[Curve], list[Poly]]:
    """
    Strokes forming the symbol drawn in an FCF compartment. Symbol strokes may touch the
    compartment border (e.g. the slanted lines of cylindricity), so take everything within the
    cell plus 1 pt, minus lines that run along the cell's own edges (frame / dividers).
    """
    area = cell + (-1.0, -1.0, 1.0, 1.0)

    def on_edge(sg: Seg) -> bool:
        if sg.is_h:
            return min(abs(sg.a.y - cell.y0), abs(sg.a.y - cell.y1)) <= 0.8
        if sg.is_v:
            return min(abs(sg.a.x - cell.x0), abs(sg.a.x - cell.x1)) <= 0.8
        return False

    segs = [sg for sg in pd.segs if area.contains(sg.a) and area.contains(sg.b) and not on_edge(sg)]
    curves = [c for c in pd.curves if area.contains(c.rect.tl) and area.contains(c.rect.br)]
    polys = [q for q in pd.polys if area.contains(q.rect.tl) and area.contains(q.rect.br)
             and q.rect.width < cell.width * 0.95]
    return segs, curves, polys


def classify_gdt_symbol(segs: list[Seg], curves: list[Curve], polys: list[Poly]) -> str | None:
    """Recognise an ISO 1101 characteristic symbol from the strokes inside an FCF cell."""
    uniq_segs: list[Seg] = []
    for sg in segs:   # closed paths repeat a stroke in reverse: count each once
        if not any((_close(sg.a, u.a, 0.5) and _close(sg.b, u.b, 0.5)) or (_close(sg.a, u.b, 0.5) and _close(sg.b, u.a, 0.5))
                   for u in uniq_segs):
            uniq_segs.append(sg)
    segs = uniq_segs
    arrows = [p for p in polys if p.filled and len(p.pts) == 3]
    closed4 = [p for p in polys if len(p.pts) == 4 and not p.filled]
    circles = [c for c in curves if c.closed]
    arcs = [c for c in curves if not c.closed]
    loose = [s for s in segs if not any(s.pid == p.pid for p in arrows + closed4)]
    hs = [s for s in loose if s.is_h]
    vs = [s for s in loose if s.is_v]
    incl = [s for s in loose if not s.is_h and not s.is_v]

    if len(circles) >= 2:
        c0, c1 = circles[0], circles[1]
        if math.hypot(c0.center.x - c1.center.x, c0.center.y - c1.center.y) < 1.0:
            return "concentricity"
    if len(circles) == 1:
        c = circles[0]
        if len(incl) >= 2:
            return "cylindricity"
        crossing = [s for s in hs + vs if s.length >= c.radius * 1.6
                    and abs(((s.a.x + s.b.x) / 2) - c.center.x) < c.radius * 0.5
                    and abs(((s.a.y + s.b.y) / 2) - c.center.y) < c.radius * 0.5]
        if len(crossing) >= 2:
            return "position"
        if not loose:
            return "circularity"
    if arrows:
        if len(arrows) >= 2:
            return "total_runout"
        return "circular_runout"
    if arcs or (curves and not circles):
        return "profile_surface" if hs else "profile_line"
    for p in closed4:
        if not p.axis_aligned:
            return "flatness"
    if len(incl) == 4 and len(hs) == 0 and len(vs) == 0:
        # parallelogram drawn as loose lines
        return "flatness"
    if len(vs) == 1 and len(hs) == 1 and not incl:
        return "perpendicularity"
    if len(incl) == 2 and not hs and not vs:
        a0, a1 = incl[0].angle, incl[1].angle
        if abs(a0 - a1) < 3:
            return "parallelism"
        return "angularity"
    if len(incl) == 1 and len(hs) == 1:
        return "angularity"
    if len(hs) == 3 and not vs and not incl:
        return "symmetry"
    if len(hs) == 1 and not vs and not incl:
        return "straightness"
    return None


def _cell_glyph(lines: list[TLine]) -> str | None:
    for ln in lines:
        t = ln.text.strip()
        if t in GLYPH_TO_GDT:
            return GLYPH_TO_GDT[t]
        if any("gdt" in s.font.lower() for s in ln.spans) and t.lower() in GDT_FONT_CODES:
            return GDT_FONT_CODES[t.lower()]
    return None


def _compartment_rows(pd: PageData) -> list[list[fitz.Rect]]:
    """Rows of adjacent axis-aligned boxes sharing top/bottom edges (FCF strips)."""
    boxes = [p.rect for p in pd.polys if p.axis_aligned and len(p.pts) == 4 and not p.filled
             and 6 <= p.rect.height <= 30 and 4 <= p.rect.width <= 200]
    # A frame may be one outer box with internal vertical dividers: split it into compartments
    split: list[fitz.Rect] = []
    for b in boxes:
        xs = sorted({round(s.a.x, 1) for s in pd.segs
                     if s.is_v and b.x0 + 1.5 < s.a.x < b.x1 - 1.5
                     and b.y0 - 1.5 <= min(s.a.y, s.b.y) <= b.y0 + 1.0
                     and b.y1 - 1.0 <= max(s.a.y, s.b.y) <= b.y1 + 1.5})
        edges = [b.x0] + xs + [b.x1]
        split.extend(fitz.Rect(edges[k], b.y0, edges[k + 1], b.y1) for k in range(len(edges) - 1)
                     if edges[k + 1] - edges[k] > 2.0)
    boxes = split
    # De-duplicate
    uniq: list[fitz.Rect] = []
    for b in boxes:
        if not any(abs(b.x0 - u.x0) < 0.6 and abs(b.y0 - u.y0) < 0.6 and abs(b.x1 - u.x1) < 0.6 and abs(b.y1 - u.y1) < 0.6 for u in uniq):
            uniq.append(b)
    uniq.sort(key=lambda r: (round(r.y0), r.x0))
    rows: list[list[fitz.Rect]] = []
    used = [False] * len(uniq)
    for i, b in enumerate(uniq):
        if used[i]:
            continue
        row = [b]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j, c in enumerate(uniq):
                if used[j]:
                    continue
                if abs(c.y0 - row[0].y0) <= 1.0 and abs(c.y1 - row[0].y1) <= 1.0 and (
                        abs(c.x0 - row[-1].x1) <= 1.0 or abs(c.x1 - row[0].x0) <= 1.0):
                    row.append(c)
                    row.sort(key=lambda r: r.x0)
                    used[j] = True
                    changed = True
        rows.append(row)
    return rows


def detect_fcfs(pd: PageData, sheet: int, exclude: list[fitz.Rect]) -> tuple[list[Feature], list[Feature]]:
    """Feature control frames (+ datum feature boxes attached to their left)."""
    fcfs, datums = [], []
    for row in _compartment_rows(pd):
        if len(row) < 2:
            continue
        strip = fitz.Rect(row[0])
        for r in row[1:]:
            strip |= r
        if any(e.contains(strip.tl) and e.contains(strip.br) for e in exclude):
            continue
        cells = []
        strip_lines = [ln for ln in pd.lines if not ln.used and strip.contains(ln.center)]
        for r in row:
            inner = r + (0.8, 0.8, -0.8, -0.8)
            # Words, not lines: a datum letter set close to the value can share a text line with it
            lines = [w for w in pd.words() if inner.contains(w.center)]
            cells.append((r, lines))
        sym_idx, sym = None, None
        for k, (r, lines) in enumerate(cells):
            g = _cell_glyph(lines)
            if g:
                sym_idx, sym = k, g
                break
            if not lines:
                segs, curves, polys = symbol_strokes(pd, r)
                if segs or curves or polys:
                    g = classify_gdt_symbol(segs, curves, polys)
                    if g:
                        sym_idx, sym = k, g
                        break
        if sym_idx is None or sym_idx + 1 >= len(cells):
            continue
        tol_lines = cells[sym_idx + 1][1]
        if not tol_lines or not re.search(r"\d", " ".join(l.text for l in tol_lines)):
            continue
        tol_text = " ".join(l.text for l in sorted(tol_lines, key=lambda l: l.rect.x0))
        m = re.search(r"(Ø)?\s*(\d+(?:[.,]\d+)?)\s*([MLⓂⓁ])?", tol_text)
        value = float(m.group(2).replace(",", ".")) if m else None
        diam = bool(m and m.group(1))
        mod = ""
        if m and m.group(3):
            mod = "MMC" if m.group(3) in "MⓂ" else "LMC"
        datum_letters = []
        for r, lines in cells[sym_idx + 2:]:
            t = " ".join(l.text for l in lines).strip()
            if re.fullmatch(r"[A-Z]{1,2}(?:\s*-\s*[A-Z]{1,2})?(?:\s*[MLⓂⓁ])?", t):
                datum_letters.append(re.sub(r"\s+", "", t))
        for ln in strip_lines:
            ln.used = True
        ftype, symbol, label = GDT_NAMES[sym]
        frame = fitz.Rect(cells[sym_idx][0])
        for r, _ in cells[sym_idx + 1:]:
            frame |= r
        det = f"{label} {'Ø' if diam else ''}{_fmt(value)}"
        if mod:
            det += f" at {mod}"
        elif sym == "position":
            det += " (RFS)"
        for name, d in zip(("Primary", "Secondary", "Tertiary"), datum_letters):
            det += f" | {name} {d}"
        f = Feature(sheet, frame, ftype, _fcf_text(symbol, tol_text, datum_letters), symbol=symbol, nominal=value,
                    details=[det], datums=datum_letters[:3], kind="fcf",
                    notes=["Nominal = tolerance zone value (mm)"], meta={"gdt": sym, "diam": diam, "mod": mod})
        fcfs.append(f)
        # Datum feature identifiers attached to the left of the symbol compartment
        for r, lines in cells[:sym_idx]:
            t = " ".join(l.text for l in lines).strip()
            if re.fullmatch(r"[A-Z]{1,2}", t):
                datums.append(Feature(sheet, fitz.Rect(lines[0].rect), "Datum feature", t, nominal=t, kind="datum",
                                      details=[f"Datum {t}"], meta={"box": r}))
    return fcfs, datums


def _fcf_text(symbol: str, tol: str, datums: list[str]) -> str:
    tol = re.sub(r"\s+M$", " Ⓜ", tol.strip())
    tol = re.sub(r"\s+L$", " Ⓛ", tol)
    return " | ".join([f"{symbol} {tol}".strip()] + datums)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return f"{v:g}" if isinstance(v, float) else str(v)


def detect_datum_features(pd: PageData, sheet: int, exclude: list[fitz.Rect]) -> list[Feature]:
    """Letter in a square box with a (filled or open) datum triangle attached by a short leader."""
    out = []
    # Datum triangles are near-equilateral; dimension arrowheads are long and thin
    tris = [p for p in pd.polys if len(p.pts) == 3 and 4 <= max(p.rect.width, p.rect.height) <= 14
            and max(p.rect.width, p.rect.height) / max(min(p.rect.width, p.rect.height), 0.1) <= 1.7]
    for ln in pd.lines:
        if ln.used or not re.fullmatch(r"[A-Z]{1,2}", ln.text) or any(e.contains(ln.center) for e in exclude):
            continue
        box = None
        for p in pd.polys:
            if not (p.axis_aligned and len(p.pts) == 4) or p.filled:
                continue
            r = p.rect
            if r.contains(ln.center) and r.width <= ln.rect.width * 4 and r.height <= ln.rect.height * 2.2 \
                    and 0.6 <= r.width / max(r.height, 0.1) <= 1.7:
                box = r
                break
        if box is None:
            continue
        reach = max(box.width, box.height) * 3.5
        near = box + (-reach, -reach, reach, reach)

        def linked(tri: Poly) -> bool:
            # joined to the box by a line in any orientation (or touching it)
            return box.intersects(tri.rect + (-1.5, -1.5, 1.5, 1.5)) or any(
                (box + (-1.5, -1.5, 1.5, 1.5)).contains(s.a) and (tri.rect + (-2, -2, 2, 2)).contains(s.b)
                or (box + (-1.5, -1.5, 1.5, 1.5)).contains(s.b) and (tri.rect + (-2, -2, 2, 2)).contains(s.a)
                for s in pd.segs)

        cands = sorted((t for t in tris if near.intersects(t.rect) and linked(t)), key=lambda t: _rect_dist(t.rect, box))
        if not cands:
            continue
        tri = cands[0]
        ln.used = True
        out.append(Feature(sheet, fitz.Rect(ln.rect), "Datum feature", ln.text, nominal=ln.text, kind="datum",
                           details=[f"Datum {ln.text}"], meta={"box": box, "tri": fitz.Rect(tri.rect)}))
    return out


CC_RE = re.compile(r"^(CC|SC)\s*\d{1,3}$", re.IGNORECASE)


def detect_cc_flags(pd: PageData, exclude: list[fitz.Rect]) -> list[tuple[str, fitz.Rect]]:
    out = []
    for ln in pd.lines:
        if ln.used or not CC_RE.match(ln.text) or any(e.contains(ln.center) for e in exclude):
            continue
        ln.used = True
        shape = next((p.rect for p in pd.polys if len(p.pts) in (4, 6) and not p.axis_aligned and p.rect.contains(ln.center)),
                     None)
        out.append((re.sub(r"\s+", "", ln.text.upper()), shape or fitz.Rect(ln.rect)))
    return out


SURF_RE = re.compile(r"^(?:(?P<u>U)\s+)?(?P<k>Ra|Rz)\s*(?P<v>\d+(?:[.,]\d+)?)$|^N(?P<n>1[0-2]|[1-9])$")
N_GRADE_RA = {1: 0.025, 2: 0.05, 3: 0.1, 4: 0.2, 5: 0.4, 6: 0.8, 7: 1.6, 8: 3.2, 9: 6.3, 10: 12.5, 11: 25.0, 12: 50.0}


def _surface_symbol_near(pd: PageData, rect: fitz.Rect) -> list[int]:
    """Path ids of check-mark (√) strokes near a roughness label: 3+ joined lines incl. a V."""
    area = rect + (-30, -30, 30, 30)
    if getattr(pd, "_pid_segs", None) is None:
        pd._pid_segs = {}
        for s in pd.segs:
            pd._pid_segs.setdefault(s.pid, []).append(s)
        pd._filled_pids = {p.pid for p in pd.polys if p.filled}
    out = []
    for pid, ss in pd._pid_segs.items():
        if pid in pd._filled_pids or not 2 <= len(ss) <= 4:
            continue
        # the whole open check-mark path lies near the label (not a long outline polyline)
        if not all(area.contains(s.a) and area.contains(s.b) for s in ss):
            continue
        if any(not s.is_h and not s.is_v for s in ss):
            out.append(pid)
    return out


def detect_surface(pd: PageData, sheet: int, exclude: list[fitz.Rect]) -> list[Feature]:
    out = []
    lines = [ln for ln in pd.lines if not ln.used and not any(e.contains(ln.center) for e in exclude)]
    for ln in lines:
        if ln.used:
            continue
        m = SURF_RE.match(ln.text)
        if not m:
            continue
        rect = fitz.Rect(ln.rect)
        text = ln.text
        upper = lower = None
        if m.group("n"):
            g = int(m.group("n"))
            nominal, symbol, upper = f"N{g}", "N", N_GRADE_RA[g]
            det = f"ISO 1302 roughness grade N{g} (= Ra {_fmt(N_GRADE_RA[g])} µm)"
        else:
            kind, val = m.group("k"), float(m.group("v").replace(",", "."))
            symbol, nominal, upper = kind, val, val
            det = f"{kind} {_fmt(val)} µm max (upper limit)"
            if m.group("u"):
                # Two-limit spec: matching "L Ra x" line directly below
                low = next((o for o in lines if o is not ln and not o.used
                            and re.fullmatch(r"L\s+(?:Ra|Rz)\s*\d+(?:[.,]\d+)?", o.text)
                            and abs(o.rect.x0 - ln.rect.x0) < 6 and 0 <= o.rect.y0 - ln.rect.y0 <= ln.size * 2.2), None)
                if low is not None:
                    low.used = True
                    lv = float(re.search(r"(\d+(?:[.,]\d+)?)$", low.text).group(1).replace(",", "."))
                    upper, lower = val, lv
                    nominal = f"{kind} {_fmt(lv)}-{_fmt(val)}"
                    det = f"Upper limit {kind} {_fmt(val)} µm / lower limit {kind} {_fmt(lv)} µm"
                    text = f"{ln.text} / {low.text}"
                    rect |= low.rect
        if not _surface_symbol_near(pd, rect):
            continue
        ln.used = True
        f = Feature(sheet, rect, "Surface finish", text, symbol=symbol, nominal=nominal, upper=upper, lower=lower,
                    details=[det], kind="surface", notes=["Upper/Lower columns = roughness limits in µm"])
        if not ln.horizontal:
            ang = math.degrees(math.atan2(-ln.dir[1], ln.dir[0])) % 360
            f.notes.append("Symbol rotated 90°" if abs(abs(ang % 180) - 90) < 3 else f"Symbol rotated {ang % 180:.0f}°")
        out.append(f)
    return out


THREAD_RE = re.compile(
    r"^(?:(?P<qty>\d+)\s*[×xX]\s*)?M(?P<d>\d+(?:[.,]\d+)?)(?:\s*[×xX]\s*(?P<p>\d+(?:[.,]\d+)?))?"
    r"(?:\s*-\s*(?P<cls>\d[A-Ha-h]{1,2}(?:\d[A-Ha-h])?))?(?P<rest>.*)$"
)


def detect_threads(pd: PageData, sheet: int, exclude: list[fitz.Rect]) -> list[Feature]:
    out = []
    for ln in pd.lines:
        if ln.used or any(e.contains(ln.center) for e in exclude):
            continue
        m = THREAD_RE.match(ln.text)
        if not m:
            continue
        ln.used = True
        rect = fitz.Rect(ln.rect)
        parts = [ln.text]
        # Continuation lines of the same callout (tap drill, CSK, EQ SP …) directly below
        last = ln
        def _continues(o: TLine) -> bool:
            if o.used or not o.horizontal or o.size > ln.size * 1.05:
                return False
            if not 0 <= o.rect.y0 - last.rect.y1 <= last.size * 0.9:
                return False
            aligned = abs(o.rect.x0 - last.rect.x0) < 8 or abs(o.rect.x1 - last.rect.x1) < 8
            return aligned and bool(re.match(r"^(?:Ø|↧|CSK|CBORE|EQ|THRU|\d)", o.text))

        while True:
            nxt = next((o for o in pd.lines if _continues(o)), None)
            if nxt is None:
                break
            nxt.used = True
            parts.append(nxt.text)
            rect |= nxt.rect
            last = nxt
        full = " / ".join(parts)
        d = m.group("d")
        bits = [f"M{d}" + (f"×{m.group('p')}" if m.group("p") else " coarse")]
        if m.group("p"):
            bits[0] += " coarse" if _iso_coarse_pitch(float(d.replace(",", "."))) == float(m.group("p").replace(",", ".")) else " fine"
        if m.group("cls"):
            bits.append(m.group("cls"))
        depths = re.findall(r"↧\s*(\d+(?:[.,]\d+)?)|[×xX]?\s*(\d+(?:[.,]\d+)?)\s*DEEP\b", parts[0], re.I)
        depths = [a or b for a, b in depths]
        if depths:
            bits.append(f"thread depth {depths[0]}")
        if re.search(r"\bTHRU\b", full, re.I):
            bits.append("through")
        for extra in parts[1:]:
            dm = re.match(r"^Ø\s*(\d+(?:[.,]\d+)?)\s*(?:↧\s*(\d+(?:[.,]\d+)?)|[×xX]?\s*(\d+(?:[.,]\d+)?)\s*DEEP)?", extra, re.I)
            if dm:
                dd = dm.group(2) or dm.group(3)
                bits.append(f"tap drill Ø{dm.group(1)}" + (f" depth {dd}" if dd else ""))
            cm = re.search(r"CSK\s*Ø?\s*(\d+(?:[.,]\d+)?)\s*[×xX]\s*(\d+(?:[.,]\d+)?)\s*°", extra)
            if cm:
                bits.append(f"CSK Ø{cm.group(1)}×{cm.group(2)}°")
            if re.search(r"\bEQ\s*SP\b", extra, re.I):
                bits.append("equally spaced")
        f = Feature(sheet, rect, "Thread", full, symbol="M", nominal=f"M{d}", details=[", ".join(bits)], kind="thread")
        if depths:
            f.meta["thread_depth"] = _f(depths[0])
        for extra in parts[1:]:
            dm = re.match(r"^Ø\s*(\d+(?:[.,]\d+)?)\s*(?:↧\s*(\d+(?:[.,]\d+)?)|[×xX]?\s*(\d+(?:[.,]\d+)?)\s*DEEP)", extra, re.I)
            if dm:
                f.meta["drill"] = (_f(dm.group(1)), _f(dm.group(2) or dm.group(3)))
            cm = re.search(r"CSK\s*Ø?\s*(\d+(?:[.,]\d+)?)\s*[×xX]\s*(\d+(?:[.,]\d+)?)\s*°", extra)
            if cm:
                f.meta["csk"] = (_f(cm.group(1)), _f(cm.group(2)))
        if m.group("qty"):
            f.notes.append(f"Qty {m.group('qty')} ({m.group('qty')}×)")
            f.meta["qty"] = int(m.group("qty"))
        f.meta["size"] = d
        out.append(f)
    return out


_COARSE = {1: 0.25, 1.2: 0.25, 1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5,
           12: 1.75, 14: 2.0, 16: 2.0, 20: 2.5, 24: 3.0, 30: 3.5, 36: 4.0, 42: 4.5, 48: 5.0}


def _iso_coarse_pitch(d: float) -> float | None:
    return _COARSE.get(int(d) if d == int(d) else d)


# ----------------------------------------------------------------------------- dimensions

def _frame(ln: TLine) -> tuple[tuple[float, float], tuple[float, float]]:
    d = ln.dir
    return d, (-d[1], d[0])


def _uv(p: fitz.Point, d, n) -> tuple[float, float]:
    return p.x * d[0] + p.y * d[1], p.x * n[0] + p.y * n[1]


DEV_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$|^±\s*\d+(?:[.,]\d+)?$")


def _split_nominal(ln: TLine) -> tuple[str, list[Span], fitz.Rect]:
    """Leading full-size spans form the nominal; trailing smaller spans are stacked deviations."""
    big = ln.size * 0.85
    nom_spans, small = [], []
    for s in ln.spans:
        if not s.text.strip():
            (small if small else nom_spans).append(s)
            continue
        if s.size >= big and not small:
            nom_spans.append(s)
        else:
            small.append(s)
    nom = _norm("".join(s.text for s in nom_spans))
    rect = fitz.Rect(nom_spans[0].rect) if nom_spans else fitz.Rect(ln.rect)
    for s in nom_spans[1:]:
        rect |= s.rect
    return nom, [s for s in small if s.text.strip()], rect


def group_dimension_texts(pd: PageData, exclude: list[fitz.Rect]) -> list[tuple[str, list[str], fitz.Rect, TLine]]:
    """
    (nominal_text, [upper, lower] deviations, union rect, host line) for each dimension text.
    Stacked deviations are smaller text to the right of the nominal, upper over lower,
    in the text's own frame (works for any rotation).
    """
    out = []
    lines = [ln for ln in pd.lines if not ln.used and not any(e.contains(ln.center) for e in exclude)]
    for ln in lines:
        if ln.used:
            continue
        nom, small, nrect = _split_nominal(ln)
        if not re.search(r"\d", nom) and not small:
            continue
        if small and not all(DEV_RE.match(_norm(s.text)) for s in small):
            continue
        d, n = _frame(ln)
        devs: list[tuple[float, str, fitz.Rect]] = []
        for s in small:
            c = fitz.Point((s.rect.x0 + s.rect.x1) / 2, (s.rect.y0 + s.rect.y1) / 2)
            devs.append((_uv(c, d, n)[1], _norm(s.text), s.rect))
        if re.search(r"\d", nom) and (small or not re.search(r"±", nom)):
            # Look for separate smaller deviation lines just after the nominal
            corners_u = [_uv(p, d, n)[0] for p in (nrect.tl, nrect.tr, nrect.bl, nrect.br)]
            half_len = (max(corners_u) - min(corners_u)) / 2
            u_mid, v_mid = _uv(fitz.Point((nrect.x0 + nrect.x1) / 2, (nrect.y0 + nrect.y1) / 2), d, n)
            for o in lines:
                if o is ln or o.used or o.size > ln.size * 0.85 or not DEV_RE.match(o.text):
                    continue
                if abs(o.dir[0] - d[0]) > 0.05 or abs(o.dir[1] - d[1]) > 0.05:
                    continue
                ou, ov = _uv(o.center, d, n)
                # centre-to-centre along the text direction (robust for rotated bounding boxes)
                if 0 < ou - u_mid <= half_len + ln.size * 2.0 and abs(ov - v_mid) <= ln.size * 1.2:
                    devs.append((ov, o.text, o.rect))
                    o.used = True
        devs.sort(key=lambda t: t[0])
        rect = fitz.Rect(ln.rect)
        for _, _, r in devs:
            rect |= r
        ln.used = True
        out.append((nom, [t for _, t, _ in devs], rect, ln))
    return out


DIM_CORE = re.compile(
    r"^(?:(?P<qty>\d+)\s*[×xX]\s*)?"
    r"(?P<pre>Ø|S?R|SØ)?\s*(?P<num>\d+(?:[.,]\d+)?)\s*(?P<deg>°)?"
    r"(?:\s*(?P<fit>[A-Za-z]{1,2}\d{1,2}))?"
    r"(?:\s*±\s*(?P<pm>\d+(?:[.,]\d+)?)\s*°?)?"
    r"(?:\s*(?P<dev1>[+-]?\d+(?:[.,]\d+)?)\s*/\s*(?P<dev2>[+-]?\d+(?:[.,]\d+)?))?$"
)
_QTY = r"(?:(?P<qty>\d+)\s*[×xX]\s+)?"
CHAMFER_RE = re.compile(
    r"^" + _QTY + r"(?:(?P<a>\d+(?:[.,]\d+)?)\s*[×xX]\s*(?P<ang>\d+(?:[.,]\d+)?)\s*°"
    r"|(?P<ang2>\d+(?:[.,]\d+)?)\s*°\s*[×xX]\s*(?P<a2>\d+(?:[.,]\d+)?))$")


def _f(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    return float(str(s).replace(",", ".").replace("+", "").replace("±", ""))


def parse_dimension(nom: str, devs: list[str]) -> dict | None:
    """Interpret a nominal + stacked deviations → type, symbol, nominal, upper, lower, fit."""
    t = _norm(nom)
    ref = basic = False
    if re.fullmatch(r"\(.*\)", t):
        ref, t = True, t[1:-1].strip()
    cm = CHAMFER_RE.match(t)
    if cm and not devs:
        size, ang = cm.group("a") or cm.group("a2"), cm.group("ang") or cm.group("ang2")
        return {"type": "Chamfer", "symbol": "°", "nominal": _f(size), "upper": None, "lower": None,
                "detail": f"Chamfer {size} mm × {ang}°", "ref": ref,
                "qty": int(cm.group("qty")) if cm.group("qty") else None}
    m = DIM_CORE.match(t)
    if not m:
        return None
    pre = m.group("pre") or ""
    num = _f(m.group("num"))
    upper = lower = None
    if m.group("pm"):
        upper, lower = _f(m.group("pm")), -_f(m.group("pm"))
    elif m.group("dev1") is not None:
        a, b = _f(m.group("dev1")), _f(m.group("dev2"))
        upper, lower = max(a, b), min(a, b)
    if devs:
        if len(devs) == 1 and devs[0].startswith("±"):
            v = _f(devs[0])
            upper, lower = v, -v
        elif len(devs) == 2:
            a, b = _f(devs[0]), _f(devs[1])
            upper, lower = max(a, b), min(a, b)
        else:
            return None
    fit = m.group("fit")
    if fit and not iso286.parse_fit(fit):
        return None
    if pre.upper() in ("R", "SR"):
        typ, sym = "Radius", "R"
    elif m.group("deg"):
        typ, sym = "Angular dimension", "°"
    elif pre in ("Ø", "SØ"):
        typ, sym = "Diameter", "Ø"
    else:
        typ, sym = "Linear dimension", ""
    detail = []
    fit_dev = None
    if fit:
        pf = iso286.parse_fit(fit)
        typ = "Fit (hole)" if fit[0].isupper() else "Fit (shaft)"
        detail.append(f"ISO 286 {fit}")
        fit_dev = iso286.limits(pf[0], pf[1], num) if num else None
        if upper is None and fit_dev:
            upper, lower = fit_dev
            detail.append("limits from ISO 286-2 table")
        elif fit_dev and (abs(fit_dev[0] - upper) > 1e-6 or abs(fit_dev[1] - lower) > 1e-6):
            detail.append(f"printed limits differ from ISO 286-2 ({_fmt(fit_dev[0])}/{_fmt(fit_dev[1])})")
    return {"type": typ, "symbol": sym, "nominal": num, "upper": upper, "lower": lower, "detail": "; ".join(detail),
            "ref": ref, "qty": int(m.group("qty")) if m.group("qty") else None, "fit": fit}


def _tol_kind(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is None:
        return ""
    lo, hi = lo or 0.0, hi or 0.0
    if abs(hi + lo) < 1e-12 and hi > 0:
        return "Bilateral equal tolerance"
    if hi > 0 > lo:
        return "Unequal bilateral tolerance"
    if abs(lo) < 1e-12 and hi > 0:
        return "Unilateral (plus) tolerance"
    if abs(hi) < 1e-12 and lo < 0:
        return "Unilateral (minus) tolerance"
    if hi > 0 and lo > 0:
        return "Unilateral (plus) tolerance, both limits above nominal"
    if hi < 0 and lo < 0:
        return "Unilateral (minus) tolerance, both limits below nominal"
    return "Tolerance"


def _boxed(pd: PageData, rect: fitz.Rect, size: float) -> bool:
    """Value enclosed by a closed box hugging the text (level or rotated with it)."""
    c = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    for p in pd.polys:
        if len(p.pts) != 4 or p.filled or not p.rect.contains(c):
            continue
        r = p.rect
        if r.x0 <= rect.x0 + 2 and r.y0 <= rect.y0 + 2 and r.x1 >= rect.x1 - 2 and r.y1 >= rect.y1 - 2 \
                and r.width - rect.width <= size * 2.5 and r.height - rect.height <= size * 2.5:
            return True
    return False


def _arrowed_ends(sg: Seg, arrows: list[Poly]) -> int:
    tips = [a.center for a in arrows]
    return sum(1 for e in (sg.a, sg.b) if any(math.hypot(e.x - t.x, e.y - t.y) < 6 for t in tips))


def _dimension_line(pd: PageData, rect: fitz.Rect, arrows: list[Poly]) -> Seg | None:
    """The dimension line a text belongs to: a segment with arrowheads, running past the text."""
    c = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    best, best_d = None, 1e9
    tips = [a.center for a in arrows]
    for s in pd.segs:
        if s.length < 15 or s.dashed:
            continue
        ends_arrowed = sum(1 for e in (s.a, s.b) if any(math.hypot(e.x - t.x, e.y - t.y) < 6 for t in tips))
        if ends_arrowed == 0:
            continue
        vx, vy = s.b.x - s.a.x, s.b.y - s.a.y
        L2 = vx * vx + vy * vy
        tpar = ((c.x - s.a.x) * vx + (c.y - s.a.y) * vy) / L2
        if not 0.1 <= tpar <= 0.9:  # text lies along the line, not beyond an arrow tip
            continue
        px, py = s.a.x + tpar * vx, s.a.y + tpar * vy
        dist = math.hypot(c.x - px, c.y - py)
        lim = max(rect.width, rect.height) * 0.75 + 6
        if dist < lim and dist < best_d:
            best, best_d = s, dist
    return best


def detect_dimensions(pd: PageData, sheet: int, exclude: list[fitz.Rect]) -> list[Feature]:
    out = []
    arrows = pd.arrowheads()
    for nom, devs, rect, ln in group_dimension_texts(pd, exclude):
        info = parse_dimension(nom, devs)
        if info is None:
            ln.used = False
            continue
        nominal = info["nominal"]
        if nominal is None:
            continue
        shown = nom + (" " + "/".join(devs) if devs else "")
        f = Feature(sheet, rect, info["type"], shown, symbol=info["symbol"], nominal=nominal,
                    upper=info["upper"], lower=info["lower"], kind="dim", meta={"fit": info.get("fit")})
        if info["detail"]:
            f.details.append(info["detail"])
        if info["ref"]:
            f.ftype, f.symbol, f.flag = "Reference dimension", "", "Bracketed (reference)"
            f.upper = f.lower = None
            f.details = ["Reference only (not inspected)"]
        elif _boxed(pd, ln.rect, ln.size):
            is_ang = f.symbol == "°"
            f.ftype = "Basic angle" if is_ang else "Basic dimension"
            f.flag = "Boxed (basic)"
            f.upper = f.lower = None
            f.details = ["Theoretically exact"]
        else:
            kind = _tol_kind(f.lower, f.upper)
            if kind:
                f.details.append(kind)
            if f.toleranced and not info.get("fit") and f.ftype in ("Linear dimension", "Diameter"):
                eq = iso286.equivalent_classes(nominal, f.upper or 0.0, f.lower or 0.0)
                if eq:
                    f.details.append("ISO 286 equivalent " + " / ".join(eq))
            if f.ftype == "Linear dimension":
                dl = _dimension_line(pd, rect, arrows)
                # Inclined = the dimension line itself is non-orthogonal (|angle mod 90| > 2 deg) and
                # runs between two arrowheads; a one-arrow slanted line is a leader, not a dimension line.
                if dl is not None and min(dl.angle % 90, 90 - dl.angle % 90) > 2.0 and _arrowed_ends(dl, arrows) == 2:
                    f.ftype = "Inclined linear dimension"
                    f.notes.append(f"Dimension line inclined at {dl.angle:.0f}°")
        if info.get("qty"):
            f.notes.append(f"Qty {info['qty']}")
        if not ln.horizontal:
            ang = math.degrees(math.atan2(-ln.dir[1], ln.dir[0])) % 360
            f.notes.append("Rotated (vertical) text" if abs(abs(ang % 180) - 90) < 3 else f"Rotated text ({ang:.0f}°)")
        out.append(f)
    return out


# =============================================================================
# notes
# =============================================================================

NUMBERED = re.compile(r"^(\d{1,2})\.\s*(.*)$")


def _notes_block(pd: PageData, frame: fitz.Rect, exclude: list[fitz.Rect], sheet: int) -> tuple[list[Note], fitz.Rect | None]:
    head = next((ln for ln in pd.lines if not ln.used and re.fullmatch(r"(GENERAL\s+)?NOTES:?", ln.text, re.I)), None)
    if head is None:
        return [], None
    head.used = True
    notes = [Note(sheet, head.rect, "Notes block", "Heading", head.text.rstrip(":").upper())]
    region = fitz.Rect(head.rect)
    below = sorted([ln for ln in pd.lines if not ln.used and ln.horizontal and ln.rect.y0 > head.rect.y1 - 1
                    and ln.rect.x0 >= head.rect.x0 - 5 and not any(e.contains(ln.center) for e in exclude)],
                   key=lambda l: (round(l.rect.y0, 0), l.rect.x0))
    cur: Note | None = None
    last_y = head.rect.y1
    text_x: float | None = None   # left edge of note text (continuation lines align to it)
    i = 0
    while i < len(below):
        ln = below[i]
        if ln.rect.y0 - last_y > max(ln.size, head.size) * 2.2:
            break
        if NUMBERED.match(ln.text) and len(ln.text.split(None, 1)) == 1:
            nxt_txt = next((o for o in below if o is not ln and abs(o.rect.y0 - ln.rect.y0) < 1.5 and o.rect.x0 > ln.rect.x0), None)
            if nxt_txt is not None:
                text_x = nxt_txt.rect.x0
        elif not NUMBERED.match(ln.text) and cur is not None and text_x is not None and abs(ln.rect.x0 - text_x) > 4:
            break
        # number and text may be separate lines on the same baseline
        same = [o for o in below if o is not ln and abs(o.rect.y0 - ln.rect.y0) < 1.5 and o.rect.x0 > ln.rect.x0]
        text = " ".join([ln.text] + [o.text for o in sorted(same, key=lambda o: o.rect.x0)])
        rr = fitz.Rect(ln.rect)
        for o in same:
            rr |= o.rect
        m = NUMBERED.match(text)
        if m:
            cur = Note(sheet, rr, "General notes", f"General note {m.group(1)}", m.group(2).strip())
            notes.append(cur)
        elif cur is not None and ln.rect.x0 > head.rect.x0 + 5:
            cur.text = f"{cur.text} {text}"
            cur.rect |= rr
        else:
            break
        for o in [ln] + same:
            o.used = True
        region |= rr
        last_y = rr.y1
        i += 1
        while i < len(below) and below[i].used:
            i += 1
    return notes, region


def _table_notes(t: Table, sheet: int) -> list[Note]:
    out = []
    for k, (row, rr) in enumerate(zip(t.rows, t.row_rects)):
        txt = " | ".join(c for c in row if c)
        if t.kind == "title":
            continue
        is_header = all(not re.search(r"\d", c) and c.upper() == c for c in row) and k <= 1
        if t.kind == "revision":
            if is_header:
                out.append(Note(sheet, rr, "Revision table", "Heading", txt))
            else:
                out.append(Note(sheet, rr, "Revision table", f"Revision {row[0]}", txt))
        elif t.kind == "fit":
            out.append(Note(sheet, rr, "Fit limits table", "Heading" if is_header else "Fit limits", txt))
        elif t.kind == "cc":
            out.append(Note(sheet, rr, "CC table", "Heading" if is_header else "Critical characteristic", txt))
        else:
            out.append(Note(sheet, rr, "Table", "Heading" if is_header else "Table row", txt))
    if t.kind == "title":
        cells = [c for row in t.rows for c in row if c]
        out.append(Note(sheet, t.rect, "Title block", "Title block", " | ".join(cells)))
    return out


# =============================================================================
# view areas
# =============================================================================

def _view_regions(pd: PageData, frame: fitz.Rect, labels: list[ViewLabel], blocked: list[fitz.Rect]) -> list[tuple[fitz.Rect, str]]:
    cell = 4.0
    grow = 3
    nx, ny = int(pd.rect.width / cell) + 1, int(pd.rect.height / cell) + 1
    filled: set[tuple[int, int]] = set()

    def mark(r: fitz.Rect, outline: bool = False) -> None:
        i0, i1 = int((r.x0 - pd.rect.x0) / cell), int((r.x1 - pd.rect.x0) / cell)
        j0, j1 = int((r.y0 - pd.rect.y0) / cell), int((r.y1 - pd.rect.y0) / cell)
        if outline or (i1 - i0 + 1) * (j1 - j0 + 1) > 4000:
            for i in range(i0, i1 + 1):
                filled.add((i, j0)); filled.add((i, j1))
            for j in range(j0, j1 + 1):
                filled.add((i0, j)); filled.add((i1, j))
        else:
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    filled.add((i, j))

    def blocked_at(p: fitz.Point) -> bool:
        return not frame.contains(p) or any(b.contains(p) for b in blocked)

    for s in pd.segs:
        r = s.rect
        if r.width > frame.width * 0.5 or r.height > frame.height * 0.5:
            continue
        if blocked_at(s.a) or blocked_at(s.b):
            continue
        if s.length > 40:
            # rasterise along the segment
            steps = int(s.length / cell) + 1
            for k in range(steps + 1):
                t = k / steps
                filled.add((int((s.a.x + t * (s.b.x - s.a.x) - pd.rect.x0) / cell),
                            int((s.a.y + t * (s.b.y - s.a.y) - pd.rect.y0) / cell)))
        else:
            mark(r)
    for c in pd.curves:
        if blocked_at(c.rect.tl) or blocked_at(c.rect.br) or c.rect.width > frame.width * 0.5:
            continue
        mark(c.rect, outline=True)
    grown: set[tuple[int, int]] = set()
    for (i, j) in filled:
        for di in range(-grow, grow + 1):
            for dj in range(-grow, grow + 1):
                if 0 <= i + di < nx and 0 <= j + dj < ny:
                    grown.add((i + di, j + dj))
    seen: set[tuple[int, int]] = set()
    comps: list[fitz.Rect] = []
    comp_cells: list[set[tuple[int, int]]] = []
    for start in grown:
        if start in seen:
            continue
        stack, x0, x1, y0, y1, n = [start], start[0], start[0], start[1], start[1], 0
        seen.add(start)
        cells_here: set[tuple[int, int]] = set()
        while stack:
            i, j = stack.pop()
            cells_here.add((i, j))
            n += 1
            x0, x1, y0, y1 = min(x0, i), max(x1, i), min(y0, j), max(y1, j)
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb in grown and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if n >= 150:
            comps.append(fitz.Rect(pd.rect.x0 + x0 * cell, pd.rect.y0 + y0 * cell,
                                   pd.rect.x0 + (x1 + 1) * cell, pd.rect.y0 + (y1 + 1) * cell))
            comp_cells.append(cells_here)
    names = _assign_labels([(l.name, l.rect) for l in labels], comps)
    _CELLS[id(pd)] = (cell, pd.rect.x0, pd.rect.y0)
    return [(comp, names.get(k, ""), comp_cells[k]) for k, comp in enumerate(comps)]


_CELLS: dict[int, tuple[float, float, float]] = {}


def _label_cost(label: fitz.Rect, comp: fitz.Rect) -> float:
    c = fitz.Point((label.x0 + label.x1) / 2, (label.y0 + label.y1) / 2)
    if comp.contains(c):
        return min(c.x - comp.x0, comp.x1 - c.x, c.y - comp.y0, comp.y1 - c.y)
    dx = max(comp.x0 - label.x1, 0, label.x0 - comp.x1)
    dy = max(comp.y0 - label.y1, 0, label.y0 - comp.y1)
    return math.hypot(dx, dy)


def _assign_labels(views: list[tuple[str, fitz.Rect]], comps: list[fitz.Rect], max_cost: float = 90.0) -> dict[int, str]:
    if not views or not comps:
        return {}
    cost = [[_label_cost(lr, comp) for comp in comps] for _n, lr in views]
    best: dict[str, Any] = {"total": float("inf"), "pick": []}

    def search(i: int, used: set[int], total: float, pick: list) -> None:
        if total >= best["total"]:
            return
        if i == len(views):
            best["total"], best["pick"] = total, list(pick)
            return
        for k in sorted(range(len(comps)), key=lambda k: cost[i][k])[:6]:
            if k not in used and cost[i][k] <= max_cost:
                used.add(k); pick.append(k)
                search(i + 1, used, total + cost[i][k], pick)
                pick.pop(); used.discard(k)
        pick.append(None)
        search(i + 1, used, total + max_cost, pick)
        pick.pop()

    search(0, set(), 0.0, [])
    return {k: views[i][0] for i, k in enumerate(best["pick"]) if k is not None}


_LOG = __import__("logging").getLogger("tvs_extractor")


def _outline_width(pd: PageData) -> float | None:
    """Dominant visible-outline line weight: the most used weight clearly heavier than thin lines."""
    from collections import Counter

    c: Counter = Counter()
    for sg in pd.segs:
        if sg.width > 0:
            c[round(sg.width, 2)] += 1
    for cv in pd.curves:
        if cv.width > 0:
            c[round(cv.width, 2)] += 1
    if not c:
        return None
    # Reference weight = dimension/leader lines (the strokes that carry arrowheads)
    tips = [a.center for a in pd.arrowheads()]
    dim_w: Counter = Counter()
    for sg in pd.segs:
        if sg.width > 0 and sg.length > 10 and any(_close(e, t, 6.0) for e in (sg.a, sg.b) for t in tips):
            dim_w[round(sg.width, 2)] += 1
    thin = dim_w.most_common(1)[0][0] if dim_w else c.most_common(1)[0][0]
    heavy = [(n, w) for w, n in c.items() if w >= thin * 1.6 and n >= 15]
    return max(heavy)[1] if heavy else None


def _view_bodies(pd: PageData, frame: fitz.Rect, labels: list, blocked: list[fitz.Rect]):
    """[(view name, [cell centres])] for part-outline clusters that a view label claims."""
    w = _outline_width(pd)
    if w is None or not labels:
        return []
    cell, grow = 4.0, 2
    ox, oy = pd.rect.x0, pd.rect.y0

    def ok(q: fitz.Point) -> bool:
        return (frame + (-1, -1, 1, 1)).contains(q) and not any(b.contains(q) for b in blocked)

    filled: set[tuple[int, int]] = set()
    for sg in pd.segs:
        if abs(sg.width - w) > 0.05 or not (ok(sg.a) and ok(sg.b)):
            continue
        if sg.length > frame.width * 0.5:
            continue
        steps = int(sg.length / cell) + 1
        for k in range(steps + 1):
            t = k / steps
            filled.add((int((sg.a.x + t * (sg.b.x - sg.a.x) - ox) / cell), int((sg.a.y + t * (sg.b.y - sg.a.y) - oy) / cell)))
    for cv in pd.curves:
        if abs(cv.width - w) > 0.05 or not (ok(cv.rect.tl) and ok(cv.rect.br)):
            continue
        for q in (cv.samples or cv.ends):
            filled.add((int((q.x - ox) / cell), int((q.y - oy) / cell)))
    grown: set[tuple[int, int]] = set()
    for (i, j) in filled:
        for di in range(-grow, grow + 1):
            for dj in range(-grow, grow + 1):
                grown.add((i + di, j + dj))
    seen: set[tuple[int, int]] = set()
    comps: list = []
    for start in grown:
        if start in seen:
            continue
        stack, cells = [start], []
        seen.add(start)
        while stack:
            i, j = stack.pop()
            cells.append((i, j))
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb in grown and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(cells) < 40:
            continue
        own = [fitz.Point(ox + (i + 0.5) * cell, oy + (j + 0.5) * cell) for (i, j) in cells if (i, j) in filled]
        xs, ys = [q.x for q in own], [q.y for q in own]
        comps.append((fitz.Rect(min(xs), min(ys), max(xs), max(ys)), own))
    if not comps:
        return []
    names = _assign_labels([(l.name, l.rect) for l in labels], [c[0] for c in comps], max_cost=160.0)
    return [(names[k], comps[k][1]) for k in range(len(comps)) if k in names]


def _cells_dist(pts: list, a: fitz.Point) -> float:
    return min(math.hypot(q.x - a.x, q.y - a.y) for q in pts)


def _anchors(pd: PageData, f) -> list:
    """Points where an annotation attaches to the part: extension-line feet, leader tips, symbols."""
    pts: list = []
    if f.kind == "dim":
        dl = _dimension_line(pd, f.rect, pd.arrowheads())
        if dl is not None:
            pts += [dl.a, dl.b]
            for e in (dl.a, dl.b):
                for sg in pd.segs:
                    if sg is dl or sg.length < 6:
                        continue
                    if _close(sg.a, e, 3.0) or _close(sg.b, e, 3.0) or _seg_near(e, sg, 1.5):
                        far = max((sg.a, sg.b), key=lambda q: math.hypot(q.x - e.x, q.y - e.y))
                        pts.append(far)
    elif f.kind == "datum" and f.meta.get("tri") is not None:
        t = f.meta["tri"]
        pts.append(fitz.Point((t.x0 + t.x1) / 2, (t.y0 + t.y1) / 2))
    elif f.kind == "surface":
        for pid in _surface_symbol_near(pd, f.rect):
            pts += [q for sg in pd.segs if sg.pid == pid for q in (sg.a, sg.b)]
    if not pts:
        tip = _leader_end(pd, f.rect)
        if tip is not None:
            pts.append(tip)
    return pts or [f.center]


def _view_for(regions: list, labels: list[ViewLabel], p: fitz.Point, grid: tuple | None = None) -> str:
    if grid is not None:
        cell, ox, oy = grid
        key = (int((p.x - ox) / cell), int((p.y - oy) / cell))
        for comp, name, cells in regions:
            if name and key in cells:
                return name
    best, best_d = "", 1e18
    for comp, name, _cells in regions:
        if not name:
            continue
        if comp.contains(p):
            d = -1.0 / max(comp.width * comp.height, 1.0)
        else:
            d = math.hypot(max(comp.x0 - p.x, 0, p.x - comp.x1), max(comp.y0 - p.y, 0, p.y - comp.y1))
        if d < best_d:
            best, best_d = name, d
    if best and best_d < 40:
        return best
    # fallback: nearest label
    if labels:
        return min(labels, key=lambda l: math.hypot(l.rect.x0 - p.x, l.rect.y0 - p.y)).name
    return ""


# =============================================================================
# callout references (section / detail letters)
# =============================================================================

def _callouts(pd: PageData, frame: fitz.Rect, exclude: list[fitz.Rect], body_size: float) -> list[tuple[str, str, fitz.Rect]]:
    """('section'|'detail', letter, rect) for large single-letter view callouts."""
    letters = [ln for ln in pd.lines if not ln.used and re.fullmatch(r"[A-Z]", ln.text) and ln.size >= body_size * 1.1
               and frame.contains(ln.center) and not any(e.contains(ln.center) for e in exclude)]
    out = []
    seen: set[str] = set()
    for ln in letters:
        if ln.text in seen:
            continue
        same = [o for o in letters if o.text == ln.text]
        if len(same) >= 2:
            seen.add(ln.text)
            r = fitz.Rect(same[0].rect)
            for o in same[1:]:
                r |= o.rect
                o.used = True
            ln.used = True
            out.append(("section", ln.text, r))
        else:
            near = ln.rect + (-60, -60, 60, 60)
            if any(c.closed and near.intersects(c.rect) and c.radius > 8 for c in pd.curves):
                seen.add(ln.text)
                ln.used = True
                out.append(("detail", ln.text, fitz.Rect(ln.rect)))
    return out


# =============================================================================
# CC table cross-check
# =============================================================================

GDT_WORDS = {
    "position": "position", "⌖": "position", "⊥": "perpendicularity", "perpendicularity": "perpendicularity",
    "∥": "parallelism", "parallelism": "parallelism", "↗": "circular_runout", "circular runout": "circular_runout",
    "total runout": "total_runout", "⌰": "total_runout", "flatness": "flatness", "⏥": "flatness",
    "profile": "profile_surface", "⌓": "profile_surface", "cylindricity": "cylindricity", "⌭": "cylindricity",
    "runout": "circular_runout", "straightness": "straightness", "concentricity": "concentricity",
    "angularity": "angularity", "∠": "angularity", "symmetry": "symmetry", "⌯": "symmetry",
    "circularity": "circularity", "roundness": "circularity", "○": "circularity", "◎": "concentricity",
    "profile of a line": "profile_line", "⌒": "profile_line",
}


def _cc_table_refs(tables: list[Table]) -> dict[str, list[dict]]:
    """CC id → characteristics referenced in a CC table row."""
    refs: dict[str, list[dict]] = {}
    for t in tables:
        if t.kind != "cc":
            continue
        for row in t.rows:
            if not row or not CC_RE.match(row[0].replace(" ", "")):
                continue
            cid = row[0].replace(" ", "").upper()
            desc = row[1] if len(row) > 1 else ""
            where = row[2] if len(row) > 2 else ""
            sm = re.search(r"\bS(?:HEET)?\s*(\d+)", where, re.I)
            sheet = int(sm.group(1)) if sm else None
            items = []
            for part in re.split(r"\s\+\s", desc):
                p = part.strip()
                low = p.lower()
                g = None
                for w in sorted(GDT_WORDS, key=len, reverse=True):
                    if w in low or w in p:
                        g = GDT_WORDS[w]
                        break
                if g:
                    vm = re.search(r"(?:Ø\s*)?(\d+(?:[.,]\d+)?)(?![\d×x])(?=[^×x]*$)", p.split(next(w for w in sorted(GDT_WORDS, key=len, reverse=True) if w in low or w in p), 1)[-1])
                    items.append({"kind": "gdt", "gdt": g, "value": _f(vm.group(1)) if vm else None, "sheet": sheet})
                    continue
                dm = re.match(r"^(?:\d+\s*[×x]\s*)?(Ø|R)?\s*(\d+(?:[.,]\d+)?)\s*([A-Za-z]{1,2}\d{1,2})?", p)
                if dm:
                    items.append({"kind": "dim", "nominal": _f(dm.group(2)), "fit": dm.group(3), "sheet": sheet})
            refs[cid] = items
    return refs


# =============================================================================
# main entry
# =============================================================================

@dataclass
class Extraction:
    features: list[Feature]
    notes: list[Note]
    sheets: int
    page_rects: list[fitz.Rect]


def extract_drawing(pdf_path: str | Path) -> Extraction:
    doc = fitz.open(str(pdf_path))
    try:
        return _extract(doc)
    finally:
        doc.close()


def _extract(doc: fitz.Document) -> Extraction:
    pages = [PageData(doc[i], i) for i in range(doc.page_count)]
    all_features: list[Feature] = []
    all_notes: list[Note] = []
    all_tables: list[Table] = []
    cc_flags: list[tuple[int, str, fitz.Rect]] = []
    labels_by_sheet: dict[int, list[ViewLabel]] = {}
    callouts_by_sheet: dict[int, list] = {}
    regions_by_sheet: dict[int, list] = {}
    notes_region: dict[int, fitz.Rect | None] = {}
    grids_by_sheet: dict[int, tuple | None] = {}
    body_block: dict[int, list[fitz.Rect]] = {}

    for pd in pages:
        sheet = pd.index + 1
        frame = _inner_frame(pd)
        # Text outside the inner frame = sheet border / zone grid labels
        for ln in pd.lines:
            if not frame.contains(ln.center):
                ln.used = True
        tables = _find_tables(pd, frame)
        all_tables.extend(tables)
        table_rects = [t.rect for t in tables]
        for t in tables:
            for ln in t.lines:
                ln.used = True
            all_notes.extend(_table_notes(t, sheet))
        # Headings directly above tables
        for t in tables:
            for ln in pd.lines:
                if ln.used or not ln.horizontal:
                    continue
                if 0 <= t.rect.y0 - ln.rect.y1 <= 12 and t.rect.x0 - 5 <= ln.rect.x0 <= t.rect.x1:
                    ln.used = True
                    all_notes.append(Note(sheet, ln.rect, {"fit": "Fit limits table", "cc": "CC table"}.get(t.kind, "Table"),
                                          "Heading", ln.text))
        excl = list(table_rects)

        fcfs, fcf_datums = detect_fcfs(pd, sheet, excl)
        datums = detect_datum_features(pd, sheet, excl)
        for f in fcfs + fcf_datums + datums:
            all_features.append(f)
        for cid, r in detect_cc_flags(pd, excl):
            cc_flags.append((sheet, cid, r))

        nnotes, nreg = _notes_block(pd, frame, excl, sheet)
        all_notes.extend(nnotes)
        notes_region[sheet] = nreg
        if nreg is not None:
            excl.append(nreg)

        labels = _view_labels(pd, frame, excl)
        labels_by_sheet[sheet] = labels
        for l in labels:
            all_notes.append(Note(sheet, l.rect, l.name, "View label", l.text))

        surf = detect_surface(pd, sheet, excl)
        threads = detect_threads(pd, sheet, excl)
        dims = detect_dimensions(pd, sheet, excl)
        all_features.extend(surf + threads + dims)

        body = sorted(f.meta.get("size", 0) or 0 for f in dims)
        sizes = sorted(ln.size for ln in pd.lines if frame.contains(ln.center))
        body_size = sizes[len(sizes) // 2] if sizes else 9.0
        callouts_by_sheet[sheet] = _callouts(pd, frame, excl, body_size)

        # General surface texture: a surface symbol whose explanation says "general"
        for ln in pd.lines:
            if not ln.used and re.match(r"^=\s*GENERAL\s+SURFACE", ln.text, re.I):
                ln.used = True
                for f in surf:
                    if abs(f.center.y - ln.center.y) < 15 and f.rect.x1 < ln.rect.x0 + 5:
                        f.view = "General notes"
                        f.notes = ["General (default) requirement; Upper/Lower columns = roughness limits in µm"]
                        f.details = [d.replace("ISO 1302 roughness grade", "General surface texture") for d in f.details]
                        f.details.append("all over unless otherwise stated")

        blocked = excl + [l.rect for l in labels]
        body_block[sheet] = list(blocked) + [t.rect for t in tables]
        regions_by_sheet[sheet] = _view_regions(pd, frame, labels, blocked)
        grids_by_sheet[sheet] = _CELLS.pop(id(pd), None)

    # ---- views
    for f in all_features:
        if f.view:
            continue
        nreg = notes_region.get(f.sheet)
        if nreg is not None and (nreg + (-20, -20, 20, 60)).contains(f.center):
            f.view = "General notes"
            continue
        f.view = _view_for(regions_by_sheet.get(f.sheet, []), labels_by_sheet.get(f.sheet, []), f.center,
                           grids_by_sheet.get(f.sheet))
    # Outline-body assignment (overrides the whitespace clusters where outlines are distinguishable)
    for pd in pages:
        sheet = pd.index + 1
        bodies = _view_bodies(pd, _inner_frame(pd), labels_by_sheet.get(sheet, []), body_block.get(sheet, []))
        if not bodies:
            continue
        for f in all_features:
            if f.sheet != sheet or f.view == "General notes":
                continue
            anchors = _anchors(pd, f)
            best, best_d = None, 1e18
            for name, pts in bodies:
                d = min(_cells_dist(pts, a) for a in anchors)
                if d < best_d:
                    best, best_d = name, d
            if best is not None and best_d <= 120:
                f.view = best
    for f in all_features:
        if not f.view:
            f.view = "Unassigned"
            _LOG.warning("TVS extractor: no view for %r on sheet %s", f.text, f.sheet)

    # ---- callout reference notes
    label_index = {(l.name.upper()): (s, l) for s, ls in labels_by_sheet.items() for l in ls}
    for sheet, cos in callouts_by_sheet.items():
        for kind, letter, r in cos:
            if kind == "section":
                key = f"SECTION {letter}-{letter}"
                hit = label_index.get(key)
                if hit is None:
                    continue
                where = f"SECTION {letter}-{letter}, SHEET {hit[0]}" if hit[0] == sheet else f"SECTION {letter}-{letter} SHOWN ON SHEET {hit[0]}"
                all_notes.append(Note(sheet, r, _view_for(regions_by_sheet[sheet], labels_by_sheet[sheet], r.tl),
                                      "Section reference", f"{letter}-{letter} ({where})"))
            else:
                key = f"DETAIL {letter}"
                hit = label_index.get(key)
                if hit is None:
                    continue
                where = f"DETAIL {letter}, SHEET {hit[0]}" if hit[0] == sheet else f"DETAIL {letter} SHOWN ON SHEET {hit[0]}"
                all_notes.append(Note(sheet, r, _view_for(regions_by_sheet[sheet], labels_by_sheet[sheet], r.tl),
                                      "Detail reference", f"{letter} ({where})"))

    _link_hole_patterns(pages, all_features, labels_by_sheet)
    _link_sections(pages, all_features, callouts_by_sheet)
    # An FCF stacked under a hole callout belongs with that callout
    for f in all_features:
        t = f.meta.get("applies_to")
        if t is not None and t.view:
            f.view = t.view

    _assign_critical(all_features, cc_flags, _cc_table_refs(all_tables), pages)

    # Cross-check: every datum referenced by a feature control frame is defined exactly once
    referenced = {d for f in all_features if f.kind == "fcf" for d in f.datums}
    defined: dict[str, int] = {}
    for f in all_features:
        if f.kind == "datum":
            defined[f.text] = defined.get(f.text, 0) + 1
    for d in sorted(referenced):
        if defined.get(d, 0) != 1:
            _LOG.warning("TVS extractor: datum %s referenced by an FCF is defined %d times", d, defined.get(d, 0))

    all_notes.sort(key=lambda n: (n.sheet, n.rect.y0, n.rect.x0))
    return Extraction(all_features, all_notes, len(pages), [p.rect for p in pages])


# ----------------------------------------------------------------------------- links

def _seg_near(p: fitz.Point, s: Seg, tol: float) -> bool:
    vx, vy = s.b.x - s.a.x, s.b.y - s.a.y
    L2 = vx * vx + vy * vy or 1e-9
    t = max(0.0, min(1.0, ((p.x - s.a.x) * vx + (p.y - s.a.y) * vy) / L2))
    return math.hypot(p.x - (s.a.x + t * vx), p.y - (s.a.y + t * vy)) <= tol


def _leader(pd: PageData, rect: fitz.Rect) -> tuple[fitz.Point | None, list[Seg]]:
    """Tip of the leader leaving a text block (follows joined segments; arrowhead tip if any)."""
    area = rect + (-4, -4, 4, 4)
    thin = pd.thin_width()
    leader_ok = (lambda sg: 0 < sg.width <= thin * 1.25) if thin else (lambda sg: True)
    start = [s for s in pd.segs if not s.dashed and leader_ok(s) and (area.contains(s.a) or area.contains(s.b))
             and s.length > 6 and not ((rect + (-0.8, -0.8, 0.8, 0.8)).contains(s.a)
                                       and (rect + (-0.8, -0.8, 0.8, 0.8)).contains(s.b))]
    best, best_d, best_chain = None, 0.0, []
    for s in start:
        chain = [s]
        cur = s.b if area.contains(s.a) else s.a
        for _ in range(4):
            nxt = next((o for o in pd.segs if o is not s and not o.dashed and o.length > 3 and leader_ok(o)
                        and (_close(o.a, cur, 1.2) or _close(o.b, cur, 1.2))), None)
            if nxt is None:
                break
            cur = nxt.b if _close(nxt.a, cur, 1.2) else nxt.a
            s = nxt
            chain.append(s)
        d = math.hypot(cur.x - (rect.x0 + rect.x1) / 2, cur.y - (rect.y0 + rect.y1) / 2)
        if d > best_d:
            best, best_d, best_chain = cur, d, chain
    if best is not None:
        c = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        for a in pd.arrowheads():
            if math.hypot(a.center.x - best.x, a.center.y - best.y) < 8:
                best = max(a.pts, key=lambda q: math.hypot(q.x - c.x, q.y - c.y))
                break
    return best, best_chain


def _leader_end(pd: PageData, rect: fitz.Rect) -> fitz.Point | None:
    return _leader(pd, rect)[0]


def _link_hole_patterns(pages: list[PageData], feats: list[Feature], labels: dict[int, list[ViewLabel]]) -> None:
    """Basic Ø on a centre-line circle → PCD; thread callouts whose holes sit on it → 'n holes on PCD'."""
    for pd in pages:
        sheet = pd.index + 1
        pcds = [c for c in pd.curves if c.dashed and c.radius > 15]
        # merge dashed arcs belonging to the same circle
        circles: list[tuple[fitz.Point, float]] = []
        for c in pcds:
            cc = fitz.Point((c.rect.x0 + c.rect.x1) / 2, (c.rect.y0 + c.rect.y1) / 2)
            r = (c.rect.width + c.rect.height) / 4
            if not any(math.hypot(cc.x - o.x, cc.y - o.y) < 2 and abs(r - orr) < 2 for o, orr in circles):
                circles.append((cc, r))
        holes = [c for c in pd.curves if c.closed and not c.dashed and 2 < c.radius < 40]
        for f in feats:
            if f.sheet != sheet:
                continue
            if f.ftype == "Basic dimension" and f.symbol == "Ø" and isinstance(f.nominal, (int, float)):
                scale = next((l.scale for l in labels.get(sheet, []) if l.name == f.view and l.scale), 1.0)
                for cc, r in circles:
                    if abs(2 * r * PT_TO_MM / scale - f.nominal) < 0.6:
                        f.ftype = "Basic dimension (PCD)"
                        f.meta["pcd"] = (cc, r)
                        f.details = ["Pitch circle; theoretically exact"]
                        break
        pcd_feats = [f for f in feats if f.sheet == sheet and "pcd" in f.meta]
        for t in [f for f in feats if f.sheet == sheet and f.kind == "thread"]:
            end = _leader_end(pd, t.rect)
            if end is None:
                continue
            hole = min((h for h in holes if math.hypot(h.center.x - end.x, h.center.y - end.y) <= h.radius + 3),
                       key=lambda h: -h.radius, default=None)
            if hole is None:
                continue
            t.meta["holes"] = [hole]
            for p in pcd_feats:
                cc, r = p.meta["pcd"]
                if abs(math.hypot(hole.center.x - cc.x, hole.center.y - cc.y) - r) < 2.5:
                    qty = t.meta.get("qty") or 1
                    t.details[0] += f"; {qty} holes on PCD Ø{_fmt(p.nominal)}"
                    t.meta["holes"] = [h for h in holes if h.radius <= hole.radius + 0.5 and h.radius >= hole.radius - 0.5
                                       and abs(math.hypot(h.center.x - cc.x, h.center.y - cc.y) - r) < 2.5]
                    t.meta["pcd_center"] = cc
                    p.details = [f"Pitch circle of {qty}× M{t.meta['size']}; theoretically exact"]
        threads_here = [f for f in feats if f.sheet == sheet and f.kind == "thread" and f.meta.get("holes")]
        open_arcs = [c for c in pd.curves if not c.closed and not c.dashed and len(c.samples) >= 3]
        # Radius whose arc is concentric with a threaded hole
        for f in [f for f in feats if f.sheet == sheet and f.ftype == "Radius"]:
            tip = _leader_end(pd, f.rect)
            if tip is None:
                continue
            for arc in open_arcs:
                if min(math.hypot(q.x - tip.x, q.y - tip.y) for q in arc.samples) > 6:
                    continue
                circ = arc.circle()
                if circ is None:
                    continue
                for t in threads_here:
                    if any(math.hypot(h.center.x - circ[0].x, h.center.y - circ[0].y) < 2.5 for h in t.meta["holes"]):
                        f.details.append(f"concentric with M{t.meta['size']} hole")
                        break
        # Basic angle whose inclined leg passes through a threaded hole pattern
        for f in [f for f in feats if f.sheet == sheet and f.ftype == "Basic angle"]:
            near_arcs = [a for a in open_arcs if _rect_dist(a.rect, f.rect) < 25]
            done = False
            for arc in near_arcs:
                circ = arc.circle()
                if circ is None:
                    continue
                ctr = circ[0]
                for end in (arc.samples[0], arc.samples[-1]):
                    ang = math.degrees(math.atan2(-(end.y - ctr.y), end.x - ctr.x)) % 180
                    if min(ang % 90, 90 - ang % 90) < 3:
                        continue  # principal axis leg
                    for t in threads_here:
                        for h in t.meta["holes"]:
                            ha = math.degrees(math.atan2(-(h.center.y - ctr.y), h.center.x - ctr.x)) % 180
                            if abs(ha - ang) < 3:
                                f.details.append(f"Angular position of M{t.meta['size']} holes")
                                done = True
                                break
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
        # Datum feature whose triangle sits on a hole callout's leader = that hole's axis
        for t in [f for f in feats if f.sheet == sheet and f.kind == "thread"]:
            _tip, chain = _leader(pd, t.rect)
            for dfe in [f for f in feats if f.sheet == sheet and f.kind == "datum" and f.meta.get("tri") is not None]:
                tri = dfe.meta["tri"] + (-2, -2, 2, 2)
                if any(_seg_near(fitz.Point((tri.x0 + tri.x1) / 2, (tri.y0 + tri.y1) / 2), s, 6.0) for s in chain):
                    dfe.details = [f"Datum {dfe.text} = axis of M{t.meta['size']} hole"]
        # FCF placed directly under a hole callout applies to it
        for g in [f for f in feats if f.sheet == sheet and f.kind == "fcf"]:
            t = next((t for t in feats if t.sheet == sheet and t.kind == "thread"
                      and -2 <= g.rect.y0 - t.rect.y1 <= 8 and abs(g.rect.x0 - t.rect.x0) < 40), None)
            if t is not None:
                qty = t.meta.get("qty")
                g.details[0] += f"; applies to {str(qty) + '× ' if qty else ''}M{t.meta['size']}"
                g.meta["applies_to"] = t


def _link_sections(pages: list[PageData], feats: list[Feature], callouts: dict[int, list]) -> None:
    """A section cut through a callout's hole repeats that callout's values (depths, CSK)."""
    for sheet, cos in callouts.items():
        pd = pages[sheet - 1]
        widths = sorted(s.width for s in pd.segs if s.width > 0)
        if not widths:
            continue
        thick = widths[len(widths) // 2] * 1.8
        for kind, letter, r in cos:
            if kind != "section":
                continue
            ends = [s for s in pd.segs if s.width >= thick and s.length < 40 and _rect_dist(s.rect, r) < 45]
            if len(ends) < 2:
                continue
            m = [fitz.Point((s.a.x + s.b.x) / 2, (s.a.y + s.b.y) / 2) for s in ends]
            a, b = max(((p, q) for p in m for q in m), key=lambda pq: math.hypot(pq[0].x - pq[1].x, pq[0].y - pq[1].y))
            plane = Seg(a, b, -1)
            thread = None
            for t in [f for f in feats if f.sheet == sheet and f.kind == "thread"]:
                if any(_seg_near(h.center, plane, h.radius * 0.5 + 1.0) for h in t.meta.get("holes", [])):
                    thread = t
                    break
            if thread is None:
                continue
            size = thread.meta.get("size")
            view = f"Section {letter}-{letter}"
            for f in feats:
                if f.view != view or f.kind != "dim" or not isinstance(f.nominal, (int, float)):
                    continue
                if f.ftype in ("Linear dimension", "Inclined linear dimension"):
                    if thread.meta.get("thread_depth") == f.nominal:
                        f.symbol = "↧"
                        f.details.insert(0, f"M{size} full-thread depth")
                    elif thread.meta.get("drill") and thread.meta["drill"][1] == f.nominal:
                        f.symbol = "↧"
                        f.details.insert(0, f"Tap drill Ø{_fmt(thread.meta['drill'][0])} depth")
                elif thread.meta.get("csk"):
                    if f.ftype == "Diameter" and thread.meta["csk"][0] == f.nominal:
                        f.details.insert(0, f"Countersink diameter at M{size} entry")
                    elif f.ftype == "Angular dimension" and thread.meta["csk"][1] == f.nominal:
                        f.details.insert(0, f"Countersink included angle at M{size} entry")


def _assign_critical(feats: list[Feature], flags: list[tuple[int, str, fitz.Rect]], table: dict[str, list[dict]],
                     pages: list[PageData]) -> None:
    for sheet, cid, r in flags:
        tagged: list[Feature] = []
        refs = table.get(cid)
        if refs:
            for ref in refs:
                cands = [f for f in feats if (ref.get("sheet") in (None, f.sheet))]
                if ref["kind"] == "gdt":
                    hit = [f for f in cands if f.kind == "fcf" and f.meta.get("gdt") == ref["gdt"]
                           and (ref["value"] is None or abs((f.nominal or 0) - ref["value"]) < 1e-6)]
                else:
                    hit = [f for f in cands if f.kind == "dim" and isinstance(f.nominal, (int, float))
                           and ref["nominal"] is not None and abs(f.nominal - ref["nominal"]) < 1e-6
                           and (not ref.get("fit") or (f.meta.get("fit") or "").lower() == ref["fit"].lower())
                           and f.ftype not in ("Reference dimension",)]
                # prefer the occurrence closest to the CC flag
                if hit:
                    hit.sort(key=lambda f: (f.sheet != sheet, math.hypot(f.center.x - r.x0, f.center.y - r.y0)))
                    tagged.append(hit[0])
                else:
                    _LOG.error("TVS extractor: CC table entry %s (%s) matches no feature", cid, ref)
        if not tagged:
            near = [f for f in feats if f.sheet == sheet and f.kind in ("fcf", "dim", "surface", "thread")]
            if near:
                f = min(near, key=lambda f: _rect_dist(f.rect, r))
                if _rect_dist(f.rect, r) < 30:
                    tagged.append(f)
        for f in tagged:
            f.critical = cid
            f.meta["cc_owner"] = True
    # Surface texture attached to a critical feature (sits on its extension line / leader) inherits it
    for f in feats:
        if f.kind != "surface" or f.critical != "-":
            continue
        pd = pages[f.sheet - 1]
        host = _surface_host(pd, f, [g for g in feats if g.sheet == f.sheet and g.kind == "dim"])
        if host is not None and host.critical != "-":
            f.critical = host.critical


def _rect_dist(a: fitz.Rect, b: fitz.Rect) -> float:
    dx = max(a.x0 - b.x1, 0, b.x0 - a.x1)
    dy = max(a.y0 - b.y1, 0, b.y0 - a.y1)
    return math.hypot(dx, dy)


def _surface_host(pd: PageData, f: Feature, dims: list[Feature]) -> Feature | None:
    """Dimension whose extension line / dimension line the surface symbol stands on."""
    sym_pids = _surface_symbol_near(pd, f.rect)
    tips = []
    for s in pd.segs:
        if s.pid in sym_pids:
            tips.extend([s.a, s.b])
    if not tips:
        return None
    tip = max(tips, key=lambda p: p.y) if True else None
    # supporting segment through the tip (not the symbol itself)
    support = [s for s in pd.segs if s.pid not in sym_pids and s.length > 20 and _seg_near(tip, s, 1.5)]
    arrows = pd.arrowheads()
    best, best_d = None, 1e9
    for s in support:
        # the dimension whose dimension line meets this extension/leader line owns the surface
        for d in dims:
            dl = _dimension_line(pd, d.rect, arrows)
            if dl is None or dl is s:
                continue
            touch = (_seg_near(dl.a, s, 2.0) or _seg_near(dl.b, s, 2.0)
                     or _seg_near(s.a, dl, 2.0) or _seg_near(s.b, dl, 2.0))
            if touch:
                dd = math.hypot(d.center.x - tip.x, d.center.y - tip.y)
                if dd < best_d:
                    best, best_d = d, dd
    if best is not None:
        return best
    # Symbol on a leader: follow it to the surface it points at, then find the dimension whose
    # extension line is collinear with that point (same surface edge).
    lead = [s for s in pd.segs if s.pid not in sym_pids and s.length > 10 and (_close(s.a, tip, 1.5) or _close(s.b, tip, 1.5))]
    for s in lead:
        end = s.b if _close(s.a, tip, 1.5) else s.a
        for a in arrows:
            if math.hypot(a.center.x - end.x, a.center.y - end.y) < 8:
                end = max(a.pts, key=lambda q: math.hypot(q.x - tip.x, q.y - tip.y))
        for d in dims:
            dl = _dimension_line(pd, d.rect, arrows)
            if dl is None:
                continue
            for e in (dl.a, dl.b):
                for horiz in (True, False):
                    if horiz and abs(e.y - end.y) > 1.5 or not horiz and abs(e.x - end.x) > 1.5:
                        continue
                    # an extension line along that edge must bridge the two points
                    for x in pd.segs:
                        if (x.is_h if horiz else x.is_v) and abs((x.a.y if horiz else x.a.x) - (e.y if horiz else e.x)) <= 1.5:
                            lo_, hi_ = sorted(((x.a.x, x.b.x) if horiz else (x.a.y, x.b.y)))
                            pe, pn = (e.x, end.x) if horiz else (e.y, end.y)
                            if lo_ - 3 <= pe <= hi_ + 3 and min(abs(pn - lo_), abs(pn - hi_), 0 if lo_ <= pn <= hi_ else 1e9) <= 25:
                                dd = math.hypot(d.center.x - end.x, d.center.y - end.y)
                                if dd < best_d:
                                    best, best_d = d, dd
    if best is not None:
        return best
    # Shared surface edge: the symbol (or its leader tip) touches a part edge on which a
    # dimension's arrow also lands -> both describe the same surface.
    contacts = list(tips)
    for s in lead:
        end = s.b if _close(s.a, tip, 1.5) else s.a
        for a in arrows:
            if math.hypot(a.center.x - end.x, a.center.y - end.y) < 8:
                end = max(a.pts, key=lambda q: math.hypot(q.x - tip.x, q.y - tip.y))
        contacts.append(end)
    edges = [s for s in pd.segs if s.pid not in sym_pids and s.length > 8 and not s.dashed
             and any(_seg_near(c, s, 1.5) for c in contacts)]
    for d in dims:
        dl = _dimension_line(pd, d.rect, arrows)
        if dl is None:
            continue
        ends = [dl.a, dl.b] + [max(a.pts, key=lambda q: math.hypot(q.x - (dl.a.x + dl.b.x) / 2, q.y - (dl.a.y + dl.b.y) / 2))
                               for a in arrows if min(math.hypot(a.center.x - e.x, a.center.y - e.y) for e in (dl.a, dl.b)) < 6]
        for e in ends:
            if any(sg is not dl and _seg_near(e, sg, 2.0) for sg in edges):
                dd = min(math.hypot(e.x - c.x, e.y - c.y) for c in contacts)
                if dd < best_d:
                    best, best_d = d, dd
    return best
