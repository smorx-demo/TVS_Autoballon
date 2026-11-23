"""
General-tolerance standards as data: ISO 2768-1 / IS 2102 and TVS TSDS0407.

All values come from the JSON files in app/general_tolerances/. Cells are kept as the chart
prints them ("±0.1", "-0.1", "+0.2", "+0.4/-0.2", "±1°30'", "±0.8%", null) and parsed here, so
a unilateral value stays unilateral (never converted to ±) and a null cell is never filled in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "general_tolerances"
FILES = {"ISO2768": "iso2768.json", "TSDS0407": "tsds0407_general_tolerances.json"}
CONVENTIONS = ("upper_inclusive", "lower_inclusive")


@dataclass
class Lookup:
    """Result of one table lookup. `ok` False → blank + `reason` (never extrapolated)."""

    ok: bool
    upper: float | None = None
    lower: float | None = None
    unit: str = "mm"            # mm | deg
    cell: str = ""              # chart text, e.g. "±0.15", "-0.2", "±1°30'"
    table: str = ""
    column: str = ""
    band: str = ""
    reason: str = ""
    flags: list[str] = field(default_factory=list)


@lru_cache(maxsize=None)
def load(standard: str) -> dict:
    name = FILES.get(standard)
    if not name:
        raise KeyError(f"Unknown general-tolerance standard {standard!r}")
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def standards_summary() -> list[dict]:
    """Read-only view of the loaded standards (admin panel)."""
    out = []
    for sid in FILES:
        d = load(sid)
        out.append({"id": sid, "standard": d.get("standard"), "title": d.get("title"),
                    "transcription": d.get("transcription"), "range_convention": d.get("range_convention"),
                    "notes": d.get("notes", []), "tables": d.get("tables", {})})
    return out


_ANG = re.compile(r"^±\s*(\d+(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*['′])?$")
_PCT = re.compile(r"^±\s*(\d+(?:\.\d+)?)\s*%$")
_PM = re.compile(r"^±\s*(\d+(?:\.\d+)?)$")
_ASYM = re.compile(r"^([+-]\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)$")
_UNI = re.compile(r"^([+-])(\d+(?:\.\d+)?)$")
_BARE = re.compile(r"^(\d+(?:\.\d+)?)$")


def parse_cell(text: str | None) -> tuple[float | None, float | None, str] | None:
    """Chart text → (upper, lower, unit); unit is 'mm', 'deg' or '%'. None for a null cell."""
    if text is None:
        return None
    t = str(text).strip().replace("−", "-").replace("–", "-")
    if not t or t.upper() in ("NA", "—", "-"):
        return None
    m = _ANG.match(t)
    if m:
        v = float(m.group(1)) + (float(m.group(2)) / 60.0 if m.group(2) else 0.0)
        return v, -v, "deg"
    m = _PCT.match(t)
    if m:
        v = float(m.group(1))
        return v, -v, "%"
    m = _PM.match(t)
    if m:
        v = float(m.group(1))
        return v, -v, "mm"
    m = _ASYM.match(t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return max(a, b), min(a, b), "mm"
    m = _UNI.match(t)
    if m:
        v = float(m.group(2))
        return (v, 0.0, "mm") if m.group(1) == "+" else (0.0, -v, "mm")
    m = _BARE.match(t)
    if m:  # geometric tolerance-zone value
        return float(m.group(1)), None, "mm"
    raise ValueError(f"Unreadable tolerance cell {text!r}")


def _in_band(size: float, band: list, first: bool, last: bool, convention: str) -> bool:
    label, lo, hi = band[0], band[1], band[2]
    label = str(label).strip()
    if label.startswith("≤"):
        return size <= hi and (lo is None or size > lo)
    if label.startswith(">") and hi is None:
        return size > lo
    if label.startswith(">"):                       # ISO ">3-6": over 3 up to and including 6
        return lo < size <= hi
    if hi is None:
        return size > lo
    lo = 0.0 if lo is None else lo
    if convention == "lower_inclusive":
        return lo <= size < hi or (last and size == hi)
    return lo < size <= hi or (first and size == lo)


def band_for(standard: str, table: str, size: float, convention: str = "upper_inclusive") -> tuple[int, list] | None:
    bands = load(standard)["tables"][table]["bands"]
    for i, b in enumerate(bands):
        if _in_band(size, b, i == 0, i == len(bands) - 1, convention):
            return i, b
    return None


def band_limits(standard: str, table: str, size: float, convention: str = "upper_inclusive") -> tuple[float | None, float | None, str] | None:
    """(size_min exclusive, size_max inclusive, label) of the band containing size — default rule range."""
    hit = band_for(standard, table, size, convention)
    if hit is None:
        return None
    _, b = hit
    return b[1], b[2], str(b[0])


def lookup(standard: str, table: str, column: str, size: float | None, *,
           convention: str = "upper_inclusive", nominal: float | None = None) -> Lookup:
    """One cell. Out-of-band sizes and null cells come back ok=False with the reason."""
    d = load(standard)
    tname = d.get("standard", standard)
    tab = d["tables"].get(table)
    if tab is None or column not in tab["columns"]:
        return Lookup(False, table=table, column=column, reason=f"{tname}: no table {table}/{column}")
    col = tab["columns"][column]
    if "all_sizes" in col:
        p = parse_cell(col["all_sizes"])
        if p is None:
            return Lookup(False, table=table, column=column, reason=f"{tname}: not specified for this feature")
        return Lookup(True, p[0], p[1], p[2], str(col["all_sizes"]), table, column, "all sizes")
    if size is None:
        return Lookup(False, table=table, column=column, reason=f"{tname}: size unknown")
    hit = band_for(standard, table, size, convention)
    if hit is None:
        bands = tab["bands"]
        lo = bands[0][1] if bands[0][1] is not None else 0
        side = "below" if size <= (lo or 0) else "above"
        return Lookup(False, table=table, column=column,
                      reason=f"{tname}: {size:g} mm is {side} the range of {tab['title']} (outside range)")
    i, b = hit
    cell = col["values"][i]
    p = parse_cell(cell)
    if p is None:
        return Lookup(False, table=table, column=column, band=str(b[0]),
                      reason=f"{tname}: not specified for this size/feature ({tab['title']} {col['label']}, {b[0]})")
    up, lo_, unit = p
    if unit == "%":
        if nominal is None:
            return Lookup(False, table=table, column=column, band=str(b[0]), reason=f"{tname}: percentage row needs a nominal")
        v = round(abs(nominal) * up / 100.0, 6)
        return Lookup(True, v, -v, "mm", f"{cell} of nominal", table, column, str(b[0]))
    return Lookup(True, up, lo_, unit, str(cell), table, column, str(b[0]))


def fmt_deg(v: float) -> str:
    """1.5 → 1°30′"""
    v = abs(v)
    d = int(v + 1e-9)
    m = round((v - d) * 60)
    if m == 60:
        d, m = d + 1, 0
    return f"{d}°{m:02d}′" if m else f"{d}°"
