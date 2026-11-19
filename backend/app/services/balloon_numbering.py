"""
Balloon numbering — the single source of truth for balloon numbers.

`assign_balloon_numbers` runs once, at the end of extraction, and writes `Feature.balloon`.
Every writer (ballooned PDF, TVS extraction Excel, inspection report) reads that field and
none of them renumbers.

Order (TVS_BALLOON_ORDER):
  zone      (default) sheet → zone row letter (A→Z) → zone column number (1→n) →
            top-to-bottom, then left-to-right inside the zone
  position  sheet → top-to-bottom, then left-to-right (zones ignored)
"""

from __future__ import annotations

import logging
import os
import re

import fitz

_LOG = logging.getLogger(__name__)

ORDERS = ("zone", "position")
_ZONE_RE = re.compile(r"^\s*([A-Za-z]+)\s*-?\s*(\d+)\s*$")


def balloon_order() -> str:
    o = os.environ.get("TVS_BALLOON_ORDER", "zone").strip().lower()
    return o if o in ORDERS else "zone"


def zone_of(page: fitz.Page, page_no: int, x: float, y: float, marks=None) -> str:
    """Border-grid zone of a point, e.g. 'B3' ('' when the sheet has no zone grid)."""
    try:
        from app.services.border_grid import zone_reference_for_point

        ref = zone_reference_for_point(page, x, y, page_no=page_no, marks=marks)
    except Exception:
        return ""
    m = _ZONE_RE.match(ref or "")
    return f"{m.group(1).upper()}{m.group(2)}" if m else ""


def zone_key(zone: str) -> tuple[str, int]:
    m = _ZONE_RE.match(zone or "")
    return (m.group(1).upper(), int(m.group(2))) if m else ("", 0)


def _reading_order(items: list, center, row_tol: float) -> list:
    """Top-to-bottom rows (centres within row_tol share a row), left-to-right inside a row."""
    out: list = []
    rows: list[list] = []
    for it in sorted(items, key=lambda i: center(i).y):
        if rows and center(it).y - center(rows[-1][0]).y <= row_tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    for row in rows:
        out.extend(sorted(row, key=lambda i: center(i).x))
    return out


def assign_balloon_numbers(features: list, doc: fitz.Document, order: str | None = None) -> None:
    """Number features 1..N (continuous over sheets) in place: `f.balloon`, and `f.meta['zone']`."""
    order = order or balloon_order()
    marks: dict[int, object] = {}
    for f in features:
        page = doc[f.sheet - 1]
        if f.sheet not in marks:
            try:
                from app.services.border_grid import _collect_display_marks

                marks[f.sheet] = _collect_display_marks(page)
            except Exception:
                marks[f.sheet] = None
        c = f.center
        f.meta["zone"] = zone_of(page, f.sheet, c.x, c.y, marks[f.sheet])

    heights = sorted(f.rect.height for f in features if f.rect.height > 0)
    row_tol = max(2.0, (heights[len(heights) // 2] if heights else 8.0) * 0.5)

    groups: dict[tuple, list] = {}
    for f in features:
        key = (f.sheet,) + (zone_key(f.meta["zone"]) if order == "zone" else ("", 0))
        groups.setdefault(key, []).append(f)
    n = 0
    for key in sorted(groups):
        for f in _reading_order(groups[key], lambda i: i.center, row_tol):
            n += 1
            f.balloon = n
