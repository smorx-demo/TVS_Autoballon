"""GD&T feature control frame parsing from vector PDF graphics."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import fitz

_GDT_SYMBOLS = {
    "position": "⌖",
    "perpendicularity": "⊥",
    "parallelism": "∥",
    "angularity": "∠",
    "flatness": "⏤",
    "straightness": "—",
    "circularity": "○",
    "cylindricity": "⌭",
    "profile": "⌓",
    "runout": "↗",
    "total_runout": "↗↗",
}

_MODIFIERS = {"MMC": "Ⓜ", "LMC": "Ⓛ", "RFS": ""}


@dataclass
class FcfRecord:
    page: int
    x: float
    y: float
    width: float
    height: float
    symbol: str
    characteristic: str
    tolerance: str
    modifier: str = ""
    datum_primary: str = ""
    datum_secondary: str = ""
    datum_tertiary: str = ""
    raw_cells: list[str] | None = None
    frame_text: str = ""
    source: str = "graphics"


def _clean(text: str) -> str:
    from app.services.autoballoon_engine import _clean

    return _clean(text)


def _iter_lines(page: fitz.Page):
    from app.services.autoballoon_engine import _iter_text_lines

    yield from _iter_text_lines(page)


def _fcf_boxes(page: fitz.Page) -> list[fitz.Rect]:
    boxes: list[fitz.Rect] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return boxes
    horiz: list[fitz.Rect] = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = float(r.width), float(r.height)
        if w >= 50 and 5 <= h <= 45 and w / max(h, 0.1) >= 2.5:
            horiz.append(fitz.Rect(r))
        elif h >= 50 and 5 <= w <= 45 and h / max(w, 0.1) >= 2.5:
            horiz.append(fitz.Rect(r))
    for r in horiz:
        merged = False
        for i, b in enumerate(boxes):
            if (b | r).width < b.width + r.width + 8:
                boxes[i] = b | r
                merged = True
                break
        if not merged:
            boxes.append(r)
    return boxes


def _split_fcf_row(cells: list[str]) -> FcfRecord | None:
    if not cells:
        return None
    joined = " | ".join(_clean(c) for c in cells if _clean(c))
    sym = ""
    characteristic = ""
    tol = ""
    modifier = ""
    datums: list[str] = []

    for i, c in enumerate(cells):
        t = _clean(c)
        if not t:
            continue
        if i == 0:
            sym = t
            characteristic = t.lower()[:24]
            for name, glyph in _GDT_SYMBOLS.items():
                if glyph in t or name.startswith(t.lower()[:4]):
                    characteristic = name
                    break
            if re.match(r"^[a-z]$", t.lower()):
                _map = {
                    "n": "position",
                    "p": "profile",
                    "f": "flatness",
                    "c": "circularity",
                    "g": "angularity",
                }
                characteristic = _map.get(t.lower(), characteristic)
            continue
        if re.match(r"^(?:0\.)?\d+(?:[.,]\d+)?$", t.replace(",", ".")):
            tol = t.replace(",", ".")
            continue
        if t.upper() in {"MMC", "LMC", "RFS"}:
            modifier = t.upper()
            continue
        letters = re.findall(r"[A-Z]", t.upper())
        datums.extend(letters[:3])

    if not tol and len(cells) >= 2:
        m = re.search(r"(0\.\d+|\d+\.\d+)", joined.replace(",", "."))
        if m:
            tol = m.group(1)

    if not tol:
        return None

    d1 = datums[0] if len(datums) > 0 else ""
    d2 = datums[1] if len(datums) > 1 else ""
    d3 = datums[2] if len(datums) > 2 else ""
    frame_text = " | ".join(
        x for x in [characteristic or sym, tol, modifier, d1, d2, d3] if x
    )
    return FcfRecord(
        page=0,
        x=0,
        y=0,
        width=0,
        height=0,
        symbol=sym,
        characteristic=characteristic or sym,
        tolerance=tol,
        modifier=modifier,
        datum_primary=d1,
        datum_secondary=d2,
        datum_tertiary=d3,
        raw_cells=[_clean(c) for c in cells],
        frame_text=frame_text,
        source="graphics",
    )


def extract_fcf_from_page(page: fitz.Page, page_index: int) -> list[FcfRecord]:
    records: list[FcfRecord] = []
    seen: set[str] = set()

    for box in _fcf_boxes(page):
        pad = 4.0
        search = fitz.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad)
        spans: list[tuple[str, fitz.Rect]] = []
        for text, rect, *_ in _iter_lines(page):
            if search.intersects(rect):
                spans.append((_clean(text), rect))
        if not spans:
            continue
        spans.sort(key=lambda s: (round(s[1].y0, 1), s[1].x0))

        rows: dict[float, list[tuple[str, fitz.Rect]]] = {}
        for text, rect in spans:
            key = round((rect.y0 + rect.y1) / 2, 0)
            rows.setdefault(key, []).append((text, rect))

        for row_spans in rows.values():
            row_spans.sort(key=lambda s: s[1].x0)
            cells = [t for t, _ in row_spans if t]
            rec = _split_fcf_row(cells)
            if rec is None:
                continue
            urect = fitz.Rect(row_spans[0][1])
            for _, r in row_spans:
                urect |= r
            rec.page = page_index + 1
            rec.x = float(urect.x0)
            rec.y = float(urect.y0)
            rec.width = float(urect.width)
            rec.height = float(urect.height)
            key = f"{rec.page}:{rec.frame_text}"
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)

    for text, rect, *_ in _iter_lines(page):
        m = re.match(
            r"^([⌖⊥∥∠⏤⌓○⌭↗n_gf-p c]{1,3})\s*"
            r"((?:0\.)?\d+(?:[.,]\d+)?)\s*"
            r"(?:(MMC|LMC|RFS)\s*)?"
            r"([A-Z](?:[-\s][A-Z]){0,2})?$",
            _clean(text),
            re.I,
        )
        if not m:
            continue
        sym, tol, mod, d = m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""
        letters = re.findall(r"[A-Z]", d.upper())
        rec = FcfRecord(
            page=page_index + 1,
            x=float(rect.x0),
            y=float(rect.y0),
            width=float(rect.width),
            height=float(rect.height),
            symbol=sym,
            characteristic=sym,
            tolerance=tol.replace(",", "."),
            modifier=(mod or "").upper(),
            datum_primary=letters[0] if letters else "",
            datum_secondary=letters[1] if len(letters) > 1 else "",
            datum_tertiary=letters[2] if len(letters) > 2 else "",
            raw_cells=[sym, tol, mod, d],
            frame_text=_clean(text),
            source="text+graphics",
        )
        key = f"{rec.page}:{rec.frame_text}"
        if key not in seen:
            seen.add(key)
            records.append(rec)

    return records


def records_to_dicts(records: list[FcfRecord]) -> list[dict]:
    out = []
    for r in records:
        out.append(
            {
                "page": r.page,
                "x": r.x,
                "y": r.y,
                "width": r.width,
                "height": r.height,
                "symbol": r.symbol,
                "characteristic": r.characteristic,
                "tolerance": r.tolerance,
                "modifier": r.modifier,
                "datum_primary": r.datum_primary,
                "datum_secondary": r.datum_secondary,
                "datum_tertiary": r.datum_tertiary,
                "frame_text": r.frame_text,
                "source": r.source,
                "raw_cells": r.raw_cells or [],
            }
        )
    return out