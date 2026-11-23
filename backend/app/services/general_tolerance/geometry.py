"""
Drawing-geometry cues for open dimensions (runs inside the extractor, writes Feature.meta only):

  • diameter  → inside (hole) or outside (shaft) Ø
      1. the dimensioned circle: enclosed by a larger concentric circle or by part outline on all
         four sides → inside; the outermost circle with nothing around it → outside
      2. section views: hatching just inside both dimension ends (solid) → outside; hatching just
         outside both ends with none inside (the hole) → inside
  • angular   → length of the shorter leg (mm, view scale applied)

Undecided cases stay undecided (classify flags them); nothing here guesses.
"""

from __future__ import annotations

import math
import re

import fitz

PT_TO_MM = 25.4 / 72.0


def _dist(p: fitz.Point, q: fitz.Point) -> float:
    return math.hypot(p.x - q.x, p.y - q.y)


def _circles(pd) -> list[tuple[fitz.Point, float]]:
    out = []
    for c in pd.curves:
        if c.closed and not c.dashed and c.rect.width > 3 and abs(c.rect.width - c.rect.height) <= max(1.5, c.rect.width * 0.03):
            out.append((fitz.Point((c.rect.x0 + c.rect.x1) / 2, (c.rect.y0 + c.rect.y1) / 2), c.rect.width / 2))
    return out


def _outline_segs(pd) -> list:
    thin = pd.thin_width() if hasattr(pd, "thin_width") else 0
    # Drawing frame / border lines are not part outline
    big = min(pd.rect.width, pd.rect.height) * 0.6
    segs = [s for s in pd.segs if not s.dashed and s.length > 4 and s.length < big]
    if thin:
        thick = [s for s in segs if s.width > thin * 1.3]
        if thick:
            return thick
    return segs


def _ray_hits(p: fitz.Point, d: tuple[float, float], segs, r_min: float, r_max: float) -> bool:
    for s in segs:
        ax, ay, bx, by = s.a.x, s.a.y, s.b.x, s.b.y
        ex, ey = bx - ax, by - ay
        den = d[0] * ey - d[1] * ex
        if abs(den) < 1e-9:
            continue
        t = ((ax - p.x) * ey - (ay - p.y) * ex) / den      # distance along the ray
        u = ((ax - p.x) * d[1] - (ay - p.y) * d[0]) / den  # position on the segment
        if r_min < t <= r_max and -0.01 <= u <= 1.01:
            return True
    return False


def _side_from_circle(pd, ctr: fitz.Point, r: float, circles, segs) -> tuple[str | None, str]:
    for c2, r2 in circles:
        if r2 > r + 1.0 and _dist(c2, ctr) + r < r2 - 0.5:
            return "inside", "circle enclosed by a larger circle"
    dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))
    # Enclosed on all four sides (long reach) → a hole; nothing close around it (short reach) → a shaft
    if all(_ray_hits(ctr, d, segs, r + 1.0, r * 6 + 200) for d in dirs):
        return "inside", "circle enclosed by part outline"
    if not any(_ray_hits(ctr, d, segs, r + 1.0, r * 2 + 20) for d in dirs):
        return "outside", "outermost circle (nothing around it)"
    return None, ""


def _hatch_side(pd, a: fitz.Point, b: fitz.Point) -> tuple[str | None, str]:
    L = _dist(a, b)
    if L < 4:
        return None, ""
    ux, uy = (b.x - a.x) / L, (b.y - a.y) / L
    nx, ny = -uy, ux
    thin = pd.thin_width() if hasattr(pd, "thin_width") else 0
    between = outside = 0
    for s in pd.segs:
        if s.dashed or s.length > 40 or s.length < 2 or (thin and s.width > thin * 1.3):
            continue
        ang = s.angle % 90
        if not 25 <= ang <= 65:
            continue
        m = fitz.Point((s.a.x + s.b.x) / 2, (s.a.y + s.b.y) / 2)
        su = ((m.x - a.x) * ux + (m.y - a.y) * uy) / L
        h = (m.x - a.x) * nx + (m.y - a.y) * ny
        if abs(h) > 4 * L + 60:
            continue
        if 0.02 < su < 0.25 or 0.75 < su < 0.98:
            between += 1
        elif -0.25 < su < -0.02 or 1.02 < su < 1.25:
            outside += 1
    if between >= 2 and outside == 0:
        return "outside", "section hatching between the diameter edges"
    if outside >= 2 and between == 0:
        return "inside", "section hatching outside the diameter edges (hole)"
    return None, ""


def diameter_side(pd, f, arrows) -> tuple[str | None, str]:
    from app.services.tvs_extractor import _dimension_line, _leader_end

    dl = _dimension_line(pd, f.rect, arrows)
    circles = _circles(pd)
    segs = _outline_segs(pd)
    tip = None
    try:
        tip = _leader_end(pd, f.rect)
    except Exception:
        tip = None
    for ctr, r in circles:
        hit = False
        if dl is not None:
            mid = fitz.Point((dl.a.x + dl.b.x) / 2, (dl.a.y + dl.b.y) / 2)
            if abs(2 * r - dl.length) <= max(2.0, 0.05 * r) and _dist(mid, ctr) <= r * 0.15 + 2:
                hit = True
        if not hit and tip is not None and abs(_dist(tip, ctr) - r) <= 2.0:
            hit = True
        if hit:
            side, why = _side_from_circle(pd, ctr, r, circles, segs)
            if side:
                return side, why
    if dl is not None:
        return _hatch_side(pd, dl.a, dl.b)
    return None, ""


_SCALE = re.compile(r"SCALE\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)


def _view_scale(view: str, labels) -> float:
    for l in labels or []:
        if l.name == view:
            m = _SCALE.search(l.text or "")
            if m and float(m.group(2)):
                return float(m.group(1)) / float(m.group(2))
    return 1.0


def angle_short_leg(pd, f, labels) -> float | None:
    """Shorter leg (mm) of an angular dimension: the arc's centre is the vertex; the legs are the
    two outline segments leaving it in the arc-end directions."""
    best = None
    for c in pd.curves:
        if c.closed or c.dashed:
            continue
        circ = c.circle()
        if circ is None:
            continue
        ctr, r = circ
        if r < 5 or _dist(ctr, f.center) > r * 1.6:
            continue
        pts = c.samples or c.ends
        if len(pts) < 2:
            continue
        legs = []
        for end in (pts[0], pts[-1]):
            d = (end.x - ctr.x, end.y - ctr.y)
            n = math.hypot(*d) or 1.0
            d = (d[0] / n, d[1] / n)
            L = 0.0
            for s in pd.segs:
                if s.dashed:
                    continue
                for p, q in ((s.a, s.b), (s.b, s.a)):
                    if _dist(p, ctr) <= 2.0:
                        v = (q.x - p.x, q.y - p.y)
                        lv = math.hypot(*v) or 1.0
                        if (v[0] * d[0] + v[1] * d[1]) / lv > 0.995:
                            L = max(L, lv)
            legs.append(L)
        if all(legs) and (best is None or _dist(ctr, f.center) < best[0]):
            best = (_dist(ctr, f.center), min(legs))
    if best is None:
        return None
    return round(best[1] * PT_TO_MM / _view_scale(f.view, labels), 2)


def annotate_features(pages, feats, labels_by_sheet: dict | None = None) -> None:
    """Store the general-tolerance class of every eligible open dimension in Feature.meta['gt_class']."""
    from app.services.general_tolerance.classify import classify_feature

    by_sheet = {pd.index + 1: pd for pd in pages}
    arrows_cache: dict[int, list] = {}
    for f in feats:
        if f.kind != "dim":
            continue
        pd = by_sheet.get(f.sheet)
        context = " ".join(list(f.notes) + list(f.details))
        link = f.meta.get("applies_to")
        if link is not None and getattr(link, "text", ""):
            context += " " + link.text
        side, why, leg = None, "", None
        if pd is not None and f.ftype == "Diameter" and not f.toleranced:
            if f.sheet not in arrows_cache:
                arrows_cache[f.sheet] = pd.arrowheads()
            try:
                side, why = diameter_side(pd, f, arrows_cache[f.sheet])
            except Exception:
                side, why = None, ""
        if pd is not None and f.ftype == "Angular dimension" and not f.toleranced:
            try:
                leg = angle_short_leg(pd, f, (labels_by_sheet or {}).get(f.sheet))
            except Exception:
                leg = None
        od = classify_feature(f.ftype, f.text, f.nominal, f.upper, f.lower, fit=f.meta.get("fit"),
                              context_text=context, side=side, side_source=why, leg=leg)
        if od is not None:
            f.meta["gt_class"] = od.to_dict()
