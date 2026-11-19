"""
Balloon placement that keeps balloons off the drawing content.

For each feature (in balloon-number order):
  1. annotation box = text runs of the feature + FCF frame / basic-dimension box / datum box and
     triangle / surface-texture symbol strokes (whatever hugs or belongs to the annotation)
  2. candidate centres around that box — left, right, above, below, the four diagonals and, for
     long boxes, positions along the sides — at increasing clearance (TVS_BALLOON_CLEARANCE_MM,
     default 2 mm, in steps up to TVS_BALLOON_MAX_GAP_MM, default 25 mm)
  3. hard rules: the circle stays inside the drawing frame, off every text run, every annotation
     box, every table / title block / revision table and every other balloon; its leader does
     not pass through another balloon and no earlier leader passes through it
  4. soft rules: no drawing geometry under the circle, the leader crosses no other leader and no
     text. The first candidate breaking no rule wins; otherwise the cheapest one that keeps the
     hard rules; otherwise the least-overlapping one, with a warning naming the balloon.
The leader runs from the balloon rim to the nearest point of the annotation box.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

import fitz

_LOG = logging.getLogger(__name__)

MM = 72.0 / 25.4


def _env_mm(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


CLEARANCE_MM = _env_mm("TVS_BALLOON_CLEARANCE_MM", 2.0)
MAX_GAP_MM = _env_mm("TVS_BALLOON_MAX_GAP_MM", 25.0)
TEXT_MARGIN_MM = 0.5        # gap kept to other text / annotation boxes / tables
BALLOON_GAP_MM = 1.0        # gap between two balloons

# soft-rule costs, in mm of extra leader length
_GEOM_COST = 6.0
_LEADER_CROSS_COST = 10.0
_TEXT_CROSS_COST = 15.0


@dataclass
class Placement:
    balloon: int
    sheet: int
    center: fitz.Point
    radius: float
    tip: fitz.Point
    box: fitz.Rect
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- geometry helpers

def _pt_seg_dist(p: fitz.Point, a: fitz.Point, b: fitz.Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    L2 = dx * dx + dy * dy
    if L2 <= 1e-12:
        return math.hypot(p.x - a.x, p.y - a.y)
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L2))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def _pt_rect_dist(p: fitz.Point, r: fitz.Rect) -> float:
    dx = max(r.x0 - p.x, 0.0, p.x - r.x1)
    dy = max(r.y0 - p.y, 0.0, p.y - r.y1)
    return math.hypot(dx, dy)


def nearest_on_rect(p: fitz.Point, r: fitz.Rect) -> fitz.Point:
    """Nearest point of the rectangle's boundary region to p (p is outside r)."""
    return fitz.Point(min(max(p.x, r.x0), r.x1), min(max(p.y, r.y0), r.y1))


def _seg_cross(a: fitz.Point, b: fitz.Point, c: fitz.Point, d: fitz.Point) -> bool:
    def orient(p, q, r):
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def _seg_hits_rect(a: fitz.Point, b: fitz.Point, r: fitz.Rect) -> bool:
    """Liang–Barsky clip: does segment a-b pass through rectangle r?"""
    t0, t1 = 0.0, 1.0
    dx, dy = b.x - a.x, b.y - a.y
    for p, q in ((-dx, a.x - r.x0), (dx, r.x1 - a.x), (-dy, a.y - r.y0), (dy, r.y1 - a.y)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


class _Grid:
    """Bucket index of rectangles -> items, for fast neighbourhood queries."""

    def __init__(self, cell: float = 24.0):
        self.cell = cell
        self.b: dict[tuple[int, int], list] = {}

    def _keys(self, r: fitz.Rect):
        c = self.cell
        for i in range(int(math.floor(r.x0 / c)), int(math.floor(r.x1 / c)) + 1):
            for j in range(int(math.floor(r.y0 / c)), int(math.floor(r.y1 / c)) + 1):
                yield i, j

    def add(self, r: fitz.Rect, item) -> None:
        for k in self._keys(r):
            self.b.setdefault(k, []).append(item)

    def query(self, r: fitz.Rect) -> list:
        seen, out = set(), []
        for k in self._keys(r):
            for it in self.b.get(k, ()):
                if id(it) not in seen:
                    seen.add(id(it))
                    out.append(it)
        return out


# ----------------------------------------------------------------------------- annotation box

def annotation_box(pd, f) -> fitz.Rect:
    """Full extent of a feature's annotation: its text runs plus frames, boxes, triangles and
    surface-texture symbols that belong to it."""
    from app.services.tvs_extractor import _surface_symbol_near

    box = fitz.Rect(f.rect)
    for v in f.meta.values():
        if isinstance(v, fitz.Rect):
            box |= v
    if f.kind == "surface":
        pids = set(_surface_symbol_near(pd, f.rect))
        for s in pd.segs:
            if s.pid in pids:
                box |= s.rect
                box.include_point(s.a)
                box.include_point(s.b)
    size = max(min(f.rect.width, f.rect.height), 4.0)
    # frames hugging the text: basic-dimension box, FCF compartments
    for _ in range(2):
        grown = fitz.Rect(box)
        for p in pd.polys:
            if p.filled or len(p.pts) != 4:
                continue
            r = p.rect
            if r.is_empty and (r.width == 0 or r.height == 0):
                continue
            hugs = (r.x0 <= box.x1 and r.x1 >= box.x0 and r.y0 <= box.y1 and r.y1 >= box.y0
                    and (r.width - box.width) <= size * 2.5 and (r.height - box.height) <= size * 2.5
                    and r.width <= box.width + size * 2.5 and r.height <= box.height + size * 2.5)
            if hugs and (r.contains(box) or box.contains((r.tl + r.br) / 2)):
                grown |= r
        if grown == box:
            break
        box = grown
    return box


# ----------------------------------------------------------------------------- layout

def balloon_radius(page: fitz.Page, label: str) -> float:
    from app.services.autoballoon_engine import _balloon_radius

    return _balloon_radius(page, label)


def _segments(pd, frame: fitz.Rect) -> list[tuple[fitz.Point, fitz.Point]]:
    out = []
    for s in pd.segs:
        if (s.is_h and s.length > frame.width * 0.9) or (s.is_v and s.length > frame.height * 0.9):
            continue  # frame border
        out.append((s.a, s.b))
    for c in pd.curves:
        pts = c.samples or c.ends
        if c.closed and pts:
            pts = list(pts) + [pts[0]]
        for a, b in zip(pts, pts[1:]):
            out.append((a, b))
    return out


def layout_sheet(pd, feats: list, *, page: fitz.Page | None = None) -> list[Placement]:
    """Place one balloon per feature on a sheet; features in balloon-number order."""
    from app.services.tvs_extractor import _find_tables, _inner_frame

    page = page or pd.page
    frame = _inner_frame(pd)
    tables = [t.rect for t in _find_tables(pd, frame)]
    clear = CLEARANCE_MM * MM
    max_gap = max(MAX_GAP_MM * MM, clear)
    tmargin = TEXT_MARGIN_MM * MM
    bgap = BALLOON_GAP_MM * MM

    boxes = {id(f): annotation_box(pd, f) for f in feats}
    texts = _Grid()
    for ln in pd.lines:
        texts.add(ln.rect, ("text", fitz.Rect(ln.rect)))
    for f in feats:
        texts.add(boxes[id(f)], ("anno", boxes[id(f)], id(f)))
    for r in tables:
        texts.add(r, ("table", fitz.Rect(r)))
    geom = _Grid()
    for a, b in _segments(pd, frame):
        geom.add(fitz.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)), (a, b))

    placed: list[Placement] = []
    leaders: list[tuple[fitz.Point, fitz.Point]] = []

    def evaluate(f, box: fitz.Rect, c: fitz.Point, r: float):
        hard, soft, cost = [], [], 0.0
        if not (frame.x0 + r + tmargin <= c.x <= frame.x1 - r - tmargin
                and frame.y0 + r + tmargin <= c.y <= frame.y1 - r - tmargin):
            hard.append("outside drawing frame")
        own_gap = _pt_rect_dist(c, box) - r
        if own_gap < clear - 0.05:
            hard.append("too close to its annotation")
        area = fitz.Rect(c.x - r - tmargin, c.y - r - tmargin, c.x + r + tmargin, c.y + r + tmargin)
        for it in texts.query(area):
            if it[0] == "anno" and it[2] == id(f):
                continue
            rr = it[1]
            if box.contains(rr):
                continue  # text of this annotation: covered by the clearance rule above
            if _pt_rect_dist(c, rr) < r + tmargin:
                hard.append(f"covers {it[0]}")
        for o in placed:
            if math.hypot(c.x - o.center.x, c.y - o.center.y) < r + o.radius + bgap:
                hard.append(f"overlaps balloon {o.balloon}")
        tip = nearest_on_rect(c, box)
        d = math.hypot(tip.x - c.x, tip.y - c.y) or 1.0
        start = fitz.Point(c.x + (tip.x - c.x) / d * r, c.y + (tip.y - c.y) / d * r)
        for o in placed:
            if _pt_seg_dist(o.center, start, tip) < o.radius + 0.5:
                hard.append(f"leader through balloon {o.balloon}")
        for a, b in leaders:
            if _pt_seg_dist(c, a, b) < r + 0.5:
                hard.append("balloon on another leader")
        # soft rules
        g = 0
        for a, b in geom.query(area):
            if _pt_seg_dist(c, a, b) < r + 0.5:
                g += 1
        if g:
            soft.append(f"{g} drawing strokes under balloon")
            cost += _GEOM_COST * min(g, 4)
        lc = sum(1 for a, b in leaders if _seg_cross(start, tip, a, b))
        if lc:
            soft.append(f"leader crosses {lc} leader(s)")
            cost += _LEADER_CROSS_COST * lc
        lrect = fitz.Rect(min(start.x, tip.x), min(start.y, tip.y), max(start.x, tip.x), max(start.y, tip.y))
        tc = 0
        for it in texts.query(lrect):
            if it[0] == "anno" and it[2] == id(f):
                continue
            rr = it[1]
            if box.contains(rr) or it[0] == "table":
                continue
            if _seg_hits_rect(start, tip, rr):
                tc += 1
        if tc:
            soft.append(f"leader crosses {tc} text item(s)")
            cost += _TEXT_CROSS_COST * tc
        cost += own_gap / MM
        return hard, soft, cost, tip

    def candidates(box: fitz.Rect, r: float):
        cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
        step = max(r * 0.6, 1.5 * MM)
        g = clear + 0.1
        while g <= max_gap + 1e-6:
            e = g + r
            q = e / math.sqrt(2.0)
            ring = [
                (box.x0 - e, cy), (box.x1 + e, cy), (cx, box.y0 - e), (cx, box.y1 + e),
                (box.x0 - q, box.y0 - q), (box.x1 + q, box.y0 - q),
                (box.x0 - q, box.y1 + q), (box.x1 + q, box.y1 + q),
            ]
            if box.width > 4 * r:
                ring += [(box.x0 + r, box.y0 - e), (box.x1 - r, box.y0 - e),
                         (box.x0 + r, box.y1 + e), (box.x1 - r, box.y1 + e)]
            if box.height > 4 * r:
                ring += [(box.x0 - e, box.y0 + r), (box.x0 - e, box.y1 - r),
                         (box.x1 + e, box.y0 + r), (box.x1 + e, box.y1 - r)]
            for x, y in ring:
                yield fitz.Point(x, y)
            g += step

    for f in feats:
        box = boxes[id(f)]
        r = balloon_radius(page, str(f.balloon))
        first_clean = None
        best_hardok = None
        best_any = None
        for c in candidates(box, r):
            hard, soft, cost, tip = evaluate(f, box, c, r)
            if not hard and not soft:
                first_clean = (c, tip, hard, soft)
                break
            if not hard and (best_hardok is None or cost < best_hardok[0]):
                best_hardok = (cost, c, tip, hard, soft)
            score = 1000.0 * len(hard) + cost
            if best_any is None or score < best_any[0]:
                best_any = (score, c, tip, hard, soft)
        if first_clean is not None:
            c, tip, hard, soft = first_clean
        elif best_hardok is not None:
            _, c, tip, hard, soft = best_hardok
        else:
            _, c, tip, hard, soft = best_any
            _LOG.warning("balloon %s (sheet %s, %r): no free position; least overlap: %s",
                         f.balloon, f.sheet, f.text, "; ".join(dict.fromkeys(hard)))
        d = math.hypot(tip.x - c.x, tip.y - c.y) or 1.0
        start = fitz.Point(c.x + (tip.x - c.x) / d * r, c.y + (tip.y - c.y) / d * r)
        leaders.append((start, tip))
        placed.append(Placement(f.balloon, f.sheet, c, r, tip, box, list(dict.fromkeys(hard)), soft))
    return placed


def layout_balloons(doc: fitz.Document, features: list) -> dict[int, Placement]:
    """Balloon number -> placement, for every feature of an extraction."""
    from app.services.tvs_extractor import PageData

    out: dict[int, Placement] = {}
    by_sheet: dict[int, list] = {}
    for f in features:
        by_sheet.setdefault(f.sheet, []).append(f)
    for sheet, feats in sorted(by_sheet.items()):
        pd = PageData(doc[sheet - 1], sheet - 1)
        for p in layout_sheet(pd, sorted(feats, key=lambda f: f.balloon)):
            out[p.balloon] = p
    return out
