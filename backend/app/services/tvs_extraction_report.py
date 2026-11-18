"""
TVS extraction report — one Excel workbook per drawing, in the layout of the
SMX-DEMO-HF-001 expected-extraction answer key:

  • Summary     — drawing identity, counts by feature type / sheet, scope coverage
  • Extraction  — one row per ballooned feature (ID, sheet, zone, mm coordinates, view,
                  type, symbol, nominal, tolerances, limits, details, datums, flags, text)
  • Notes       — view labels, references, general notes, title block, other text

Built from the job's balloons (the user-curated feature list) and the source PDF.
"""

from __future__ import annotations

import io
import math
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

import fitz
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from app.services.report_pdf import parse_tolerance

PT_TO_MM = 25.4 / 72.0

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
WRAP = Alignment(wrap_text=True, vertical="top")

EXTRACTION_COLUMNS: list[tuple[str, float]] = [
    ("Feature ID", 10),
    ("Sheet", 7),
    ("Zone", 7),
    ("X (mm)", 8),
    ("Y (mm)", 8),
    ("View", 14),
    ("Feature type", 22),
    ("Symbol", 8),
    ("Nominal", 10),
    ("Upper tolerance", 10),
    ("Lower tolerance", 10),
    ("Max limit", 10),
    ("Min limit", 10),
    ("Fit / thread / finish / GD&T details (incl. datums)", 48),
    ("Primary datum", 9),
    ("Secondary datum", 9),
    ("Tertiary datum", 9),
    ("Boxed / reference flag", 16),
    ("Critical flag", 9),
    ("Notes", 34),
    ("Text as shown on drawing", 30),
]

NOTES_COLUMNS: list[tuple[str, float]] = [
    ("#", 5),
    ("Sheet", 7),
    ("Zone", 7),
    ("View / area", 18),
    ("Item type", 22),
    ("Text (verbatim)", 110),
]

GDT_SYMBOLS = {
    "position": "⌖",
    "true position": "⌖",
    "flatness": "⏥",
    "straightness": "⏤",
    "circularity": "○",
    "roundness": "○",
    "cylindricity": "⌭",
    "profile": "⌓",
    "profile of a surface": "⌓",
    "profile of a line": "⌒",
    "perpendicularity": "⊥",
    "parallelism": "∥",
    "angularity": "∠",
    "runout": "↗",
    "circular runout": "↗",
    "total runout": "⌰",
    "concentricity": "◎",
    "coaxiality": "◎",
    "symmetry": "⌯",
}

# ISO 1302 roughness grade → Ra (µm)
N_GRADE_RA = {
    1: 0.025, 2: 0.05, 3: 0.1, 4: 0.2, 5: 0.4, 6: 0.8,
    7: 1.6, 8: 3.2, 9: 6.3, 10: 12.5, 11: 25.0, 12: 50.0,
}

VIEW_RE = re.compile(
    r"^\s*(?:"
    r"(?P<named>FRONT|TOP|BOTTOM|SIDE|LEFT|RIGHT|REAR|BACK|END|PLAN|ISOMETRIC|AUXILIARY)\s+VIEW"
    r"|SECTION\s+(?P<sec>[A-Z]{1,2}\s*-\s*[A-Z]{1,2})"
    r"|DETAIL\s+(?P<det>[A-Z]{1,2})\b"
    r"|VIEW\s+(?P<view>[A-Z]{1,2})\b"
    r")",
    re.IGNORECASE,
)
CC_RE = re.compile(r"^\s*(CC\s*\d{1,3}|SC\s*\d{1,3})\s*$", re.IGNORECASE)
NUMBERED_NOTE_RE = re.compile(r"(?:(?<=\s)|^)(\d{1,2})\.\s+(?=[A-Z])")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(s: Any) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ".").replace("+", "").strip())
    except ValueError:
        return None


def _cell_num(v: float | None) -> float | int | None:
    if v is None:
        return None
    r = round(v, 4)
    return int(r) if r == int(r) else r


def _norm(text: str) -> str:
    t = (text or "").replace("−", "-").replace("–", "-").replace("±", "±")
    t = t.replace("∅", "Ø").replace("⌀", "Ø")
    return re.sub(r"\s+", " ", t).strip()


def _zone_short(ref: str) -> str:
    """'E-1' → 'E1'; page fallbacks ('P1') → ''."""
    if not ref or re.fullmatch(r"P\d+", ref):
        return ""
    return ref.replace("-", "")


def _view_name(m: re.Match) -> str:
    if m.group("named"):
        return f"{m.group('named').capitalize()} view"
    if m.group("sec"):
        return "Section " + re.sub(r"\s+", "", m.group("sec").upper())
    if m.group("det"):
        return "Detail " + m.group("det").upper()
    return "View " + m.group("view").upper()


# ---------------------------------------------------------------------------
# page context: text lines, view labels, CC tags, zones
# ---------------------------------------------------------------------------

def _label_cost(label: fitz.Rect, comp: fitz.Rect) -> float:
    """Distance from a view label to a view's outline (labels sit just outside their view)."""
    c = fitz.Point((label.x0 + label.x1) / 2, (label.y0 + label.y1) / 2)
    if comp.contains(c):
        return min(c.x - comp.x0, comp.x1 - c.x, c.y - comp.y0, comp.y1 - c.y)
    dx = max(comp.x0 - label.x1, 0, label.x0 - comp.x1)
    dy = max(comp.y0 - label.y1, 0, label.y0 - comp.y1)
    return math.hypot(dx, dy)


def _assign_labels(views: list[tuple[str, fitz.Rect]], comps: list[fitz.Rect], max_cost: float = 90.0) -> dict[int, str]:
    """
    One label per view, minimising total label-to-outline distance (exhaustive search —
    a sheet has only a handful of views). Labels farther than max_cost stay unassigned.
    """
    n_l = len(views)
    if not n_l or not comps:
        return {}
    cost = [[_label_cost(lr, comp) for comp in comps] for _n, lr in views]
    best: dict[str, Any] = {"total": float("inf"), "pick": []}

    def search(i: int, used: set[int], total: float, pick: list[int | None]) -> None:
        if total >= best["total"]:
            return
        if i == n_l:
            best["total"], best["pick"] = total, list(pick)
            return
        options = sorted(range(len(comps)), key=lambda k: cost[i][k])[:6]
        for k in options:
            if k not in used and cost[i][k] <= max_cost:
                used.add(k)
                pick.append(k)
                search(i + 1, used, total + cost[i][k], pick)
                pick.pop()
                used.discard(k)
        pick.append(None)
        search(i + 1, used, total + max_cost, pick)
        pick.pop()

    search(0, set(), 0.0, [])
    return {k: views[i][0] for i, k in enumerate(best["pick"]) if k is not None}


class _PageCtx:
    def __init__(self, page: fitz.Page, page_no: int):
        self.page = page
        self.page_no = page_no
        self.rect = page.rect
        self.lines: list[tuple[str, fitz.Rect, tuple[float, float]]] = []
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                if text:
                    self.lines.append((text, fitz.Rect(line["bbox"]), tuple(line.get("dir") or (1.0, 0.0))))
        self.views: list[tuple[str, fitz.Rect]] = []
        self.cc_tags: list[tuple[str, fitz.Point]] = []
        for text, r, _d in self.lines:
            m = VIEW_RE.match(text)
            if m:
                self.views.append((_view_name(m), r))
        # CC / SC tags per word: they may share a text line with the dimension they mark
        for wx0, wy0, wx1, wy1, word, *_ in page.get_text("words"):
            c = CC_RE.match(word)
            if c:
                self.cc_tags.append((re.sub(r"\s+", "", c.group(1).upper()),
                                     fitz.Point((wx0 + wx1) / 2, (wy0 + wy1) / 2)))
        self._marks = None
        self._regions: list[tuple[fitz.Rect, str]] | None = None

    # -- view regions: cluster drawing geometry, then attach each view label to its cluster --
    def _build_regions(self) -> list[tuple[fitz.Rect, str]]:
        pr = self.rect
        cell = 4.0
        grow = 3  # cells of dilation (~12 pt): whitespace wider than this separates views
        nx, ny = int(pr.width / cell) + 1, int(pr.height / cell) + 1
        border = min(pr.width, pr.height) * 0.06
        inner = fitz.Rect(pr.x0 + border, pr.y0 + border, pr.x1 - border, pr.y1 - border)
        filled: set[tuple[int, int]] = set()
        try:
            drawings = self.page.get_drawings()
        except Exception:
            drawings = []
        for d in drawings:
            r = d.get("rect")
            if r is None or r.is_empty and r.width == 0 and r.height == 0:
                continue
            if r.width > pr.width * 0.5 or r.height > pr.height * 0.5:
                continue  # sheet frame / long border lines
            if not inner.contains(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)):
                continue
            if (r.x0 + r.x1) / 2 > pr.x0 + pr.width * 0.55 and (r.y0 + r.y1) / 2 > pr.y0 + pr.height * 0.78:
                continue  # title block
            i0, i1 = int((r.x0 - pr.x0) / cell), int((r.x1 - pr.x0) / cell)
            j0, j1 = int((r.y0 - pr.y0) / cell), int((r.y1 - pr.y0) / cell)
            if (i1 - i0 + 1) * (j1 - j0 + 1) > 4000:
                # large outline: rasterise its edges only
                for i in range(i0, i1 + 1):
                    filled.add((i, j0)); filled.add((i, j1))
                for j in range(j0, j1 + 1):
                    filled.add((i0, j)); filled.add((i1, j))
            else:
                for i in range(i0, i1 + 1):
                    for j in range(j0, j1 + 1):
                        filled.add((i, j))
        grown: set[tuple[int, int]] = set()
        for (i, j) in filled:
            for di in range(-grow, grow + 1):
                for dj in range(-grow, grow + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny:
                        grown.add((ii, jj))
        seen: set[tuple[int, int]] = set()
        comps: list[fitz.Rect] = []
        for start in grown:
            if start in seen:
                continue
            stack = [start]
            seen.add(start)
            x0 = x1 = start[0]
            y0 = y1 = start[1]
            n = 0
            while stack:
                i, j = stack.pop()
                n += 1
                x0, x1, y0, y1 = min(x0, i), max(x1, i), min(y0, j), max(y1, j)
                for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                    if nb in grown and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            if n >= 150:  # ignore specks (symbols, small tables)
                comps.append(fitz.Rect(pr.x0 + x0 * cell, pr.y0 + y0 * cell,
                                       pr.x0 + (x1 + 1) * cell, pr.y0 + (y1 + 1) * cell))
        names = _assign_labels(self.views, comps)
        return [(comp, names.get(k, "")) for k, comp in enumerate(comps)]

    def zone(self, x: float, y: float) -> str:
        try:
            from app.services.border_grid import _collect_display_marks, zone_reference_for_point

            if self._marks is None:
                self._marks = _collect_display_marks(self.page)
            return _zone_short(zone_reference_for_point(self.page, x, y, page_no=self.page_no, marks=self._marks))
        except Exception:
            return ""

    def view_for(self, x: float, y: float) -> str:
        """View containing the point (geometry clusters); falls back to the nearest label."""
        if not self.views:
            return ""
        if self._regions is None:
            self._regions = self._build_regions()
        p = fitz.Point(x, y)
        best_r, best_rd = "", 1e18
        for comp, name in self._regions:
            if not name:
                continue
            dx = max(comp.x0 - x, 0, x - comp.x1)
            dy = max(comp.y0 - y, 0, y - comp.y1)
            d = math.hypot(dx, dy)
            if comp.contains(p):
                d = -1.0 / max(comp.width * comp.height, 1.0)  # innermost/smallest containing view wins
            if d < best_rd:
                best_r, best_rd = name, d
        if best_r and best_rd < 40:
            return best_r
        best, best_cost = "", 1e18
        for name, r in self.views:
            cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            dx, dy = abs(cx - x), cy - y
            cost = dx + (dy * 0.6 if dy >= 0 else (-dy) * 3.0 + 200)
            if cost < best_cost:
                best, best_cost = name, cost
        return best

    def text_direction(self, rect: fitz.Rect) -> tuple[float, float] | None:
        best, best_area = None, 0.0
        for _t, r, d in self.lines:
            inter = fitz.Rect(r) & rect
            if inter.is_empty:
                continue
            area = inter.width * inter.height
            if area > best_area:
                best, best_area = d, area
        return best

    def cc_for(self, rect: fitz.Rect, reach: float = 28.0) -> str:
        best, best_d = "", reach
        for tag, p in self.cc_tags:
            dx = max(rect.x0 - p.x, 0, p.x - rect.x1)
            dy = max(rect.y0 - p.y, 0, p.y - rect.y1)
            dist = math.hypot(dx, dy)
            if dist < best_d:
                best, best_d = tag, dist
        return best

    def near_border(self, x: float, y: float) -> str:
        w, h = self.rect.width, self.rect.height
        m = 0.08
        sides = []
        if x < self.rect.x0 + w * m:
            sides.append("left")
        if x > self.rect.x1 - w * m:
            sides.append("right")
        if y < self.rect.y0 + h * m:
            sides.append("top")
        if y > self.rect.y1 - h * m:
            sides.append("bottom")
        return f"Placed near {'/'.join(sides)} border" if sides else ""


# ---------------------------------------------------------------------------
# feature classification
# ---------------------------------------------------------------------------

def _tol_kind(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is None:
        return ""
    lo = lo or 0.0
    hi = hi or 0.0
    if abs(hi + lo) < 1e-12 and hi > 0:
        return "Bilateral equal tolerance"
    if hi > 0 and lo < 0:
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


def _parse_gdt(text: str) -> dict | None:
    """'position | Ø0.2 M | A | B | C' or 'Ø0.2 M A B C' → GD&T parts."""
    t = _norm(text)
    parts = [p.strip() for p in t.split("|")] if "|" in t else []
    name = ""
    if parts and re.fullmatch(r"[A-Za-z ]+", parts[0]):
        name = parts[0].lower()
        parts = parts[1:]
    if not parts:
        m = re.fullmatch(r"(Ø?\s*\d+(?:[.,]\d+)?)\s*([MLⓂⓁ])?((?:\s+[A-Z]){1,3})", t)
        if not m:
            return None
        parts = [((m.group(1) or "") + (" " + m.group(2) if m.group(2) else "")).strip()] + m.group(3).split()
    tol_tok = next((p for p in parts if re.search(r"\d", p)), "")
    if not tol_tok:
        return None
    datums = [p for p in parts[parts.index(tol_tok) + 1:] if re.fullmatch(r"[A-Z]{1,2}", p)][:3]
    m = re.search(r"\d+(?:[.,]\d+)?", tol_tok)
    value = _num(m.group(0)) if m else None
    mod = ""
    if re.search(r"(?:\s|^)(M|Ⓜ)(?:\s|$)", tol_tok):
        mod = "at MMC"
    elif re.search(r"(?:\s|^)(L|Ⓛ)(?:\s|$)", tol_tok):
        mod = "at LMC"
    return {
        "name": name,
        "value": value,
        "diametral": "Ø" in tol_tok,
        "modifier": mod,
        "datums": datums,
        "tol_text": tol_tok,
    }


def _parse_thread(text: str) -> dict | None:
    t = _norm(text)
    m = re.search(r"(?:(\d+)\s*[x×]\s*)?M(\d+(?:[.,]\d+)?)(?:\s*[x×]\s*(\d+(?:[.,]\d+)?))?", t)
    if not m:
        return None
    if not re.match(r"^(?:\d+\s*[x×]\s*)?M\d", t):
        return None
    cls = re.search(r"-\s*(\d[A-Ha-h]{1,2})\b", t)
    depth = re.search(r"↧\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*DEEP", t, re.IGNORECASE)
    return {
        "qty": int(m.group(1)) if m.group(1) else None,
        "size": m.group(2),
        "pitch": m.group(3),
        "cls": cls.group(1) if cls else "",
        "depth": (depth.group(1) or depth.group(2)) if depth else "",
        "thru": bool(re.search(r"\bTHRU\b|\bTHROUGH\b", t, re.IGNORECASE)),
    }


def _parse_surface(text: str) -> dict | None:
    t = _norm(text)
    ul = re.search(r"U\s*(Ra|Rz)\s*(\d+(?:[.,]\d+)?)\s*L\s*(?:Ra|Rz)\s*(\d+(?:[.,]\d+)?)", t, re.IGNORECASE)
    if ul:
        kind = ul.group(1).capitalize()
        return {"symbol": kind, "nominal": f"{kind} {ul.group(3)}-{ul.group(2)}",
                "upper": _num(ul.group(2)), "lower": _num(ul.group(3)),
                "detail": f"Upper limit {kind} {ul.group(2)} µm / lower limit {kind} {ul.group(3)} µm",
                "note": "Two-limit (U/L) specification; Upper/Lower columns = roughness limits in µm"}
    m = re.search(r"\b(Ra|Rz)\s*(\d+(?:[.,]\d+)?)", t, re.IGNORECASE)
    if m:
        kind = m.group(1).capitalize()
        v = _num(m.group(2))
        return {"symbol": kind, "nominal": v, "upper": v, "lower": None,
                "detail": f"{kind} {m.group(2)} µm max (upper limit)",
                "note": "Upper/Lower columns = roughness limits in µm"}
    m = re.search(r"\bN\s*(1[0-2]|[1-9])\b", t)
    if m:
        g = int(m.group(1))
        return {"symbol": "N", "nominal": f"N{g}", "upper": N_GRADE_RA[g], "lower": None,
                "detail": f"ISO 1302 roughness grade N{g} (= Ra {N_GRADE_RA[g]} µm)",
                "note": "Upper/Lower columns = roughness limits in µm"}
    return None


def classify_feature(text: str, category: str = "", pattern: str = "") -> dict:
    """
    Map a ballooned characteristic to answer-key columns.
    Returns feature_type, symbol, nominal, upper, lower, detail, datums[], flag, note.
    """
    t = _norm(text)
    cat = (category or "").lower()
    out: dict[str, Any] = {
        "feature_type": "", "symbol": "", "nominal": None, "upper": None, "lower": None,
        "detail": "", "datums": [], "flag": "-", "note": "", "toleranced": False,
    }

    # --- GD&T feature control frame ---
    gdt = _parse_gdt(t) if (cat == "gdt" or "|" in t or cat in {"manual", ""}) else None
    if gdt and (cat == "gdt" or "|" in t or gdt["datums"]):
        name = gdt["name"]
        out["feature_type"] = f"GD&T - {name.capitalize()}" if name else "GD&T"
        out["symbol"] = GDT_SYMBOLS.get(name, "")
        out["nominal"] = gdt["value"]
        bits = []
        if name:
            bits.append(
                f"{name.capitalize()} {'Ø' if gdt['diametral'] else ''}{gdt['tol_text'].replace('Ø', '').split()[0]}"
                + (f" {gdt['modifier']}" if gdt["modifier"] else "")
            )
        else:
            bits.append(f"Tolerance {gdt['tol_text']}")
        for label, d in zip(("Primary", "Secondary", "Tertiary"), gdt["datums"]):
            bits.append(f"{label} {d}")
        out["detail"] = " | ".join(bits)
        out["datums"] = gdt["datums"]
        out["note"] = "Nominal = tolerance zone value (mm)"
        return out

    # --- surface finish ---
    if cat in {"surface", "surface_finish"} or re.search(r"\b(Ra|Rz)\s*\d|\bN(1[0-2]|[1-9])\b", t):
        sf = _parse_surface(t)
        if sf:
            out.update(feature_type="Surface finish", symbol=sf["symbol"], nominal=sf["nominal"],
                       upper=sf["upper"], lower=sf["lower"], detail=sf["detail"], note=sf["note"])
            return out

    # --- thread ---
    th = _parse_thread(t)
    if th or cat == "thread":
        out["feature_type"], out["symbol"] = "Thread", "M"
        if th:
            out["nominal"] = f"M{th['size']}"
            bits = [f"M{th['size']}" + (f"×{th['pitch']}" if th["pitch"] else " coarse")]
            if th["cls"]:
                bits.append(th["cls"])
            if th["depth"]:
                bits.append(f"thread depth {th['depth']}")
            if th["thru"]:
                bits.append("through")
            out["detail"] = ", ".join(bits)
            if th["qty"]:
                out["note"] = f"Qty {th['qty']} ({th['qty']}×)"
        return out

    # --- chamfer ---
    ch = re.match(r"^(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*°", t)
    if cat == "chamfer" or ch:
        out["feature_type"], out["symbol"] = "Chamfer", "°"
        if ch:
            out["nominal"] = _num(ch.group(1))
            out["detail"] = f"Chamfer {ch.group(1)} mm × {ch.group(2)}°"
        return out

    # --- dimensions (basic / reference / fit / diameter / radius / angle / linear) ---
    nominal, lo, hi = parse_tolerance(t)
    out["nominal"] = _num(nominal)
    out["upper"], out["lower"] = _num(hi), _num(lo)
    out["toleranced"] = out["upper"] is not None or out["lower"] is not None
    is_angle = "°" in t
    is_dia = t.startswith("Ø") or t.upper().startswith("DIA")
    is_rad = bool(re.match(r"^(?:\d+\s*[x×]\s*)?S?R\s*\d", t))
    if is_rad:
        qty = re.match(r"^(\d+)\s*[x×]", t)
        if qty:
            out["note"] = f"Qty {qty.group(1)} ({qty.group(1)}×)"
            rm = re.search(r"R\s*(\d+(?:[.,]\d+)?)", t)
            out["nominal"] = _num(rm.group(1)) if rm else out["nominal"]
    fit = re.search(r"\d\s*([A-Za-z]{1,2})(\d{1,2})\b", t.replace("Ø", ""))
    fit_code = ""
    if fit and not is_rad and fit.group(1).upper() not in {"X", "MM", "M"}:
        fit_code = fit.group(1) + fit.group(2)

    if t.startswith("[") and t.endswith("]") or cat == "basic":
        out["feature_type"] = "Basic angle" if is_angle else "Basic dimension"
        out["symbol"] = "°" if is_angle else ("Ø" if "Ø" in t else "")
        out["flag"] = "Boxed (basic)"
        out["detail"] = "Theoretically exact; tolerance comes from the related feature control frame"
        out["upper"] = out["lower"] = None
        out["toleranced"] = False
        return out
    if re.fullmatch(r"\(.*\)", t) or cat == "reference":
        out["feature_type"] = "Reference dimension"
        out["flag"] = "Bracketed (reference)"
        out["detail"] = "Reference only (not inspected)"
        out["upper"] = out["lower"] = None
        out["toleranced"] = False
        return out
    if fit_code:
        out["feature_type"] = "Fit (hole)" if fit_code[0].isupper() else "Fit (shaft)"
        out["symbol"] = "Ø" if is_dia else ""
        out["detail"] = f"ISO 286 {fit_code}"
        if not out["toleranced"]:
            out["detail"] += "; limits not printed on the drawing"
    elif is_rad:
        out["feature_type"], out["symbol"] = "Radius", "R"
    elif is_angle:
        out["feature_type"], out["symbol"] = "Angular dimension", "°"
    elif is_dia:
        out["feature_type"], out["symbol"] = "Diameter", "Ø"
    else:
        out["feature_type"] = "Linear dimension"
    kind = _tol_kind(out["lower"], out["upper"])
    if kind:
        out["detail"] = f"{out['detail']}; {kind}" if out["detail"] else kind
    elif not fit_code:
        out["detail"] = "No tolerance on the dimension (general tolerance applies)"
    return out


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

def _row_overrides(rows: list[dict]) -> dict[tuple[int, int], dict]:
    out: dict[tuple[int, int], dict] = {}
    for r in rows or []:
        try:
            out[(int(r.get("page") or 1), int(r.get("balloon") or 0))] = r
        except (TypeError, ValueError):
            continue
    return out


def build_feature_rows(doc: fitz.Document, balloons: list[dict], report_rows: list[dict] | None = None,
                       zones: dict[int, str] | None = None) -> list[dict]:
    """One dict per balloon in answer-key column order (see EXTRACTION_COLUMNS)."""
    ctxs: dict[int, _PageCtx] = {}
    overrides = _row_overrides(report_rows or [])
    per_sheet: Counter = Counter()
    out: list[dict] = []
    ordered = sorted(balloons, key=lambda b: (int(b.get("page") or 1), int(b.get("balloon") or 0)))
    for b in ordered:
        page_no = int(b.get("page") or 1)
        if page_no < 1 or page_no > doc.page_count:
            continue
        ctx = ctxs.get(page_no)
        if ctx is None:
            ctx = ctxs[page_no] = _PageCtx(doc[page_no - 1], page_no)
        text = str(b.get("text") or "").strip()
        if not text:
            continue
        x, y = float(b.get("x") or 0), float(b.get("y") or 0)
        w, h = float(b.get("width") or 0), float(b.get("height") or 0)
        rect = fitz.Rect(x, y, x + max(w, 1), y + max(h, 1))
        cx, cy = x + w / 2, y + h / 2
        f = classify_feature(text, b.get("category") or "", b.get("pattern") or "")

        # Inspection-report edits win for dimension tolerances
        ov = overrides.get((page_no, int(b.get("balloon") or 0)))
        if ov and f["feature_type"] and not f["feature_type"].startswith(("GD&T", "Surface", "Thread", "Basic", "Reference")):
            lo, hi = _num(ov.get("tol_low")), _num(ov.get("tol_high"))
            if lo is not None or hi is not None:
                f["lower"], f["upper"] = lo, hi
                f["toleranced"] = True
            n = _num(ov.get("nominal"))
            if n is not None:
                f["nominal"] = n

        nom = f["nominal"] if isinstance(f["nominal"], (int, float)) else None
        max_lim = min_lim = None
        if f["toleranced"] and nom is not None:
            max_lim = nom + (f["upper"] or 0.0)
            min_lim = nom + (f["lower"] or 0.0)

        notes = [f["note"]] if f["note"] else []
        d = ctx.text_direction(rect)
        if d is not None and abs(d[1]) > 0.05:
            ang = abs(math.degrees(math.atan2(-d[1], d[0])))
            notes.append("Rotated (vertical) text" if abs(ang - 90) < 3 else f"Inclined text ({ang:.0f}°)")
        border = ctx.near_border(cx, cy)
        if border:
            notes.append(border)

        cc = ctx.cc_for(rect) or "-"

        per_sheet[page_no] += 1
        zone = _zone_short((zones or {}).get(int(b.get("balloon") or 0), "")) or ctx.zone(cx, cy)
        datums = (f["datums"] + ["", "", ""])[:3]
        out.append(OrderedDict([
            ("Feature ID", f"S{page_no}-{per_sheet[page_no]:03d}"),
            ("Sheet", page_no),
            ("Zone", zone),
            ("X (mm)", round((cx - ctx.rect.x0) * PT_TO_MM, 1)),
            ("Y (mm)", round((ctx.rect.y1 - cy) * PT_TO_MM, 1)),
            ("View", ctx.view_for(cx, cy)),
            ("Feature type", f["feature_type"] or "Other"),
            ("Symbol", f["symbol"] or None),
            ("Nominal", _cell_num(f["nominal"]) if isinstance(f["nominal"], (int, float)) else f["nominal"]),
            ("Upper tolerance", _cell_num(f["upper"])),
            ("Lower tolerance", _cell_num(f["lower"])),
            ("Max limit", _cell_num(max_lim)),
            ("Min limit", _cell_num(min_lim)),
            ("Fit / thread / finish / GD&T details (incl. datums)", f["detail"] or None),
            ("Primary datum", datums[0] or None),
            ("Secondary datum", datums[1] or None),
            ("Tertiary datum", datums[2] or None),
            ("Boxed / reference flag", f["flag"]),
            ("Critical flag", cc),
            ("Notes", "; ".join(notes) or None),
            ("Text as shown on drawing", text),
            ("_balloon", int(b.get("balloon") or 0)),
            ("_toleranced", f["toleranced"]),
            ("_tol_kind", _tol_kind(f["lower"], f["upper"]) if f["toleranced"] else ""),
        ]))
    return out


REVISION_RE = re.compile(r"^([A-Z])\s+(?:-|[A-H]\d{1,2})\s+.*\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")
FIT_ROW_RE = re.compile(r"^Ø?\s*\d+(?:[.,]\d+)?\s*[A-Za-z]{1,2}\d{1,2}\s+[+-]?\d")
CC_ROW_RE = re.compile(r"^(CC|SC)\s*\d{1,3}\b", re.IGNORECASE)
GRID_RE = re.compile(r"^(?:[A-Z]|\d{1,2})(?:\s+(?:[A-Z]|\d{1,2}))*$")


def _is_prose(text: str) -> bool:
    """Real words, not a dimension / GD&T fragment ('Ø5 ↧11, EQ SP', '0.5 A B C')."""
    words = re.findall(r"[A-Za-z]{3,}", text)
    if len(words) >= 2:
        return True
    return not re.search(r"\d", text) and any(len(w) >= 4 for w in words)


def build_note_rows(doc: fitz.Document, balloon_texts: set[str] | None = None) -> list[dict]:
    """Drawing text that is not a ballooned feature: views, references, notes, tables, title block."""
    from app.services.autoballoon_engine import _is_dimension

    balloon_texts = {_norm(t) for t in (balloon_texts or set())}
    rows: list[dict] = []
    for pi in range(doc.page_count):
        page = doc[pi]
        ctx = _PageCtx(page, pi + 1)
        pr = page.rect
        title_bits: list[str] = []
        title_pt: tuple[float, float] | None = None
        area = ""  # current table / block context (set by headings)
        last: dict | None = None
        last_rect: fitz.Rect | None = None
        for x0, y0, x1, y1, raw, *_ in page.get_text("blocks"):
            text = _norm(str(raw).replace("\n", " "))
            br = fitz.Rect(x0, y0, x1, y1)
            if len(re.sub(r"[^A-Za-z]", "", text)) < 2 or GRID_RE.match(text):
                continue
            if text in balloon_texts or CC_RE.match(text) or _is_dimension(text):
                continue
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if cx > pr.x0 + pr.width * 0.55 and cy > pr.y0 + pr.height * 0.78:
                title_bits.append(text)
                if title_pt is None:
                    title_pt = (cx, cy)
                continue
            # Wrapped continuation of the previous note / label: same column, directly below
            if (
                last is not None and last_rect is not None
                and last["type"].startswith(("General note", "View label"))
                and not NUMBERED_NOTE_RE.match(text)
                and abs(x0 - last_rect.x0) < 40 and -2 <= y0 - last_rect.y1 < 12
            ):
                last["text"] = f"{last['text']} {text}"
                last_rect = last_rect | br
                continue
            zone = ctx.zone(cx, cy)

            def add(view: str, kind: str, body: str) -> dict:
                row = {"sheet": pi + 1, "zone": zone, "view": view, "type": kind, "text": body}
                rows.append(row)
                return row

            vm = VIEW_RE.match(text)
            if vm:
                last, last_rect = add(_view_name(vm), "View label", text), br
                continue
            if CC_ROW_RE.match(text):
                last, last_rect = add("CC table", "Critical characteristic", text), br
                continue
            rv = REVISION_RE.match(text)
            if rv:
                last, last_rect = add("Revision table", f"Revision {rv.group(1)}", text), br
                continue
            if FIT_ROW_RE.match(text):
                last, last_rect = add("Fit limits table", "Fit limits", text), br
                continue
            if not _is_prose(text):
                continue
            head = re.match(r"^\s*((?:GENERAL\s+)?NOTES?)\s*:?\s*", text, re.IGNORECASE)
            if head:
                add("Notes block", "Heading", head.group(1).upper())
                text = text[head.end():]
                area = "General notes"
            parts = NUMBERED_NOTE_RE.split(text)
            if len(parts) >= 3:
                for k in range(1, len(parts) - 1, 2):
                    body = parts[k + 1].strip()
                    if body:
                        last, last_rect = add("General notes", f"General note {parts[k]}", body), br
                continue
            if not text:
                continue
            if text.upper() == text and (
                not re.search(r"\d", text) or re.search(r"\b(?:LIMITS|TABLE|CHARACTERISTICS|REVISIONS)\b", text)
            ):
                # Table titles / column headers set the context for the rows below
                up = text.upper()
                if "REVISION" in up:
                    area = "Revision table"
                elif "FIT" in up:
                    area = "Fit limits table"
                elif "CRITICAL" in up:
                    area = "CC table"
                last, last_rect = add(area or ctx.view_for(cx, cy) or "Drawing", "Heading", text), br
                continue
            last, last_rect = add(ctx.view_for(cx, cy) or "Drawing", "Text", text), br
        if title_bits:
            zone = ctx.zone(*title_pt) if title_pt else ""
            rows.append({"sheet": pi + 1, "zone": zone, "view": "Title block", "type": "Title block",
                         "text": " | ".join(title_bits)})
    return rows


# ---------------------------------------------------------------------------
# workbook
# ---------------------------------------------------------------------------

def _header(ws, columns: list[tuple[str, float]]) -> None:
    from openpyxl.utils import get_column_letter

    for i, (name, width) in enumerate(columns, start=1):
        c = ws.cell(row=1, column=i, value=name)
        c.font, c.fill, c.alignment = HEADER_FONT, HEADER_FILL, WRAP
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"


def _scope_coverage(features: list[dict], notes: list[dict]) -> list[tuple[str, list[str]]]:
    def ids(pred) -> list[str]:
        return [f["Feature ID"] for f in features if pred(f)]

    def txt(f) -> str:
        return str(f["Text as shown on drawing"])

    def ftype(f) -> str:
        return str(f["Feature type"])

    return [
        ("Dimensions with tolerances", ids(lambda f: f["_toleranced"] and not ftype(f).startswith(("Surface", "GD&T")))),
        ("Ø diameter symbol", ids(lambda f: "Ø" in txt(f))),
        ("R radius symbol", ids(lambda f: bool(re.search(r"(?:^|[\s×x])S?R\s*\d", txt(f))))),
        ("° angle symbol", ids(lambda f: "°" in txt(f))),
        ("Inclined / rotated / non-aligned dimensions & annotations",
         ids(lambda f: bool(re.search(r"Rotated|Inclined", str(f["Notes"] or ""))))),
        ("Unilateral tolerances", ids(lambda f: f["_tol_kind"].startswith("Unilateral"))),
        ("Bilateral tolerances (equal or unequal)",
         ids(lambda f: f["_tol_kind"] in {"Bilateral equal tolerance", "Unequal bilateral tolerance"})),
        ("GD&T feature control frames", ids(lambda f: ftype(f).startswith("GD&T"))),
        ("FCFs with primary + secondary + tertiary datums", ids(lambda f: bool(f["Tertiary datum"]))),
        ("Surface finish (N6/N7/N8/Ra/Rz)", ids(lambda f: ftype(f) == "Surface finish")),
        ("Threads M6 / M8 / M10 with pitch and depth", ids(lambda f: ftype(f) == "Thread")),
        ("Fits H7 / h6 / g6 / p7", ids(lambda f: ftype(f).startswith("Fit"))),
        ("Chamfers (size × angle)", ids(lambda f: ftype(f) == "Chamfer")),
        ("Boxed (basic) dimensions", ids(lambda f: f["Boxed / reference flag"] == "Boxed (basic)")),
        ("Bracketed (reference) dimensions", ids(lambda f: f["Boxed / reference flag"] == "Bracketed (reference)")),
        ("Annotations near border / title block", ids(lambda f: "border" in str(f["Notes"] or ""))),
        ("Critical characteristics (CC)", ids(lambda f: f["Critical flag"] not in {"-", "", None})),
        ("Items on sheet 2+ (multi-sheet mapping)", ids(lambda f: int(f["Sheet"]) > 1)),
    ]


def build_tvs_extraction_xlsx(meta: dict, src_pdf: Path, report_rows: list[dict] | None = None,
                              balloons: list[dict] | None = None) -> bytes:
    from app.services.border_grid import zone_references_for_balloons

    balloons = list(balloons if balloons is not None else (meta.get("balloons") or []))
    doc = fitz.open(str(src_pdf))
    try:
        zones = zone_references_for_balloons(src_pdf, balloons)
        features = build_feature_rows(doc, balloons, report_rows, zones)
        notes = build_note_rows(doc, {str(b.get("text") or "") for b in balloons})
        sheet_count = doc.page_count
        size = ""
        if doc.page_count:
            r = doc[0].rect
            size = f"{round(r.width * PT_TO_MM)} x {round(r.height * PT_TO_MM)} mm"
    finally:
        doc.close()

    wb = Workbook()
    # --- Summary ---
    ws = wb.active
    ws.title = "Summary"
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 120
    ident = " ".join(
        p for p in [
            f"Drawing {meta.get('part_number')}" if meta.get("part_number") else "",
            f"rev {meta.get('revision')}" if meta.get("revision") else "",
            f"- {meta.get('part_name')}" if meta.get("part_name") else "",
        ] if p
    ) or f"Drawing {((meta.get('files') or [{}])[0]).get('name', '')}"
    ws.append(["TVS extraction report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"{ident}, {sheet_count} sheet(s){', ' + size if size else ''}."])
    ws.append(["Coordinates: mm from the lower-left corner of the sheet, at the centre of the annotation text. "
               "Zones: sheet border grid (letter row + number column)."])
    ws.append([])
    ws.append(["Features by type"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append(["Feature type", "Count"])
    for c in ws[ws.max_row][:2]:
        c.font = Font(bold=True)
    for name, n in Counter(f["Feature type"] for f in features).most_common():
        ws.append([name, n])
    ws.append(["TOTAL features", len(features)])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append([])
    ws.append(["Features by sheet"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append(["Sheet", "Count"])
    for c in ws[ws.max_row][:2]:
        c.font = Font(bold=True)
    for sheet, n in sorted(Counter(f["Sheet"] for f in features).items()):
        ws.append([sheet, n])
    ws.append(["Notes / text items (Notes tab)", len(notes)])
    ws.append([])
    ws.append(["Scope coverage"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append(["Scope item", "Count", "Feature IDs"])
    for c in ws[ws.max_row][:3]:
        c.font = Font(bold=True)
    for label, fids in _scope_coverage(features, notes):
        ws.append([label, len(fids), ", ".join(fids)])
    ws.append(["Notes & textual information", len(notes), "See Notes tab"])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    # --- Extraction ---
    ws = wb.create_sheet("Extraction")
    _header(ws, EXTRACTION_COLUMNS)
    keys = [name for name, _ in EXTRACTION_COLUMNS]
    for f in features:
        ws.append([f[k] for k in keys])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = WRAP
    ws.auto_filter.ref = f"A1:{ws.cell(row=max(ws.max_row, 1), column=len(keys)).coordinate}"

    # --- Notes ---
    ws = wb.create_sheet("Notes")
    _header(ws, NOTES_COLUMNS)
    for i, n in enumerate(notes, start=1):
        ws.append([i, n["sheet"], n["zone"], n["view"], n["type"], n["text"]])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = WRAP
    ws.auto_filter.ref = f"A1:{ws.cell(row=max(ws.max_row, 1), column=len(NOTES_COLUMNS)).coordinate}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
