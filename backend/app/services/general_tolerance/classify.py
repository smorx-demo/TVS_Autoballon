"""
Which dimensions are "open" (eligible for a general tolerance) and what kind of feature each is.

Eligible: linear, diameter, radius, chamfer (size + angle) or angular dimension with no printed
tolerance, fit class or limits. Never: basic / reference / fit / thread / GD&T / surface finish /
datum / notes / quantities / anything already toleranced.

Categories (column lookup):
  linear, thickness, outside_dia, inside_dia (hole_type bored | drilled | None), dia (side unknown),
  radius, chamfer (size; the angle is looked up alongside), angular.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

CATEGORIES = ["linear", "thickness", "outside_dia", "inside_dia", "radius", "chamfer_size", "chamfer_angle", "angular"]
CATEGORY_LABELS = {
    "linear": "linear", "thickness": "thickness", "outside_dia": "outside Ø", "inside_dia": "inside Ø",
    "dia": "Ø (side unknown)", "radius": "radius / corner round", "chamfer": "chamfer",
    "chamfer_size": "chamfer size", "chamfer_angle": "chamfer angle", "angular": "angular",
}

_NEVER = ("basic", "reference", "fit", "thread", "gd&t", "surface", "datum", "note", "flag", "text")
_DRILL = re.compile(r"\bDRILL(?:ED|ING)?\b|\bDRL\b", re.I)
_BORE = re.compile(r"\b(?:C'?BORE|COUNTER\s*BORE|BORED?|BORING|REAM(?:ED)?|SPOT\s*FACE)\b|⌴", re.I)
_HOLE = re.compile(r"\bTHRU\b|\bHOLES?\b|↧|\bDEEP\b|\bDEPTH\b|\bPCD\b|\bEQ\.?\s*SP", re.I)
_THK = re.compile(r"\bTHK\b|\bTHICK(?:NESS)?\b|^\s*t\s*=", re.I)


@dataclass
class OpenDim:
    category: str                 # linear | thickness | dia | outside_dia | inside_dia | radius | chamfer | angular
    size: float                   # the value looked up (count prefix ignored)
    hole_type: str | None = None  # bored | drilled (inside_dia only)
    angle: float | None = None    # chamfer angle / angular value (deg)
    leg: float | None = None      # shorter leg (mm) for angular lookups
    leg_estimated: bool = False
    side_source: str = ""         # how outside / inside was decided
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict | None) -> "OpenDim | None":
        if not d:
            return None
        keys = OpenDim.__dataclass_fields__.keys()
        return OpenDim(**{k: d[k] for k in keys if k in d})


def hole_type_from_text(*texts: str) -> tuple[str | None, bool]:
    """(hole_type, is_hole) from callout / note words."""
    t = " ".join(x for x in texts if x)
    if _DRILL.search(t):
        return "drilled", True
    if _BORE.search(t):
        return "bored", True
    return None, bool(_HOLE.search(t))


def classify_feature(ftype: str, text: str, nominal, upper=None, lower=None, *, qty_note: bool = False,
                     fit: str | None = None, context_text: str = "", side: str | None = None,
                     side_source: str = "", leg: float | None = None) -> OpenDim | None:
    """
    OpenDim for an eligible open dimension, else None.
    `side` ('outside' | 'inside') comes from drawing geometry when known; text cues win over it.
    """
    ft = (ftype or "").strip().lower()
    if not ft or any(ft.startswith(w) for w in _NEVER):
        return None
    if upper is not None or lower is not None or fit:
        return None
    if not isinstance(nominal, (int, float)):
        return None
    size = float(nominal)
    if ft in ("linear dimension", "inclined linear dimension"):
        cat = "thickness" if _THK.search(text or "") or _THK.search(context_text or "") else "linear"
        return OpenDim(cat, size)
    if ft == "diameter":
        ht, is_hole = hole_type_from_text(text, context_text)
        if ht or is_hole:
            src = "callout text" if (hole_type_from_text(text)[0] or hole_type_from_text(text)[1]) else "linked note"
            return OpenDim("inside_dia", size, hole_type=ht, side_source=src)
        if side == "inside":
            return OpenDim("inside_dia", size, side_source=side_source or "drawing geometry")
        if side == "outside":
            return OpenDim("outside_dia", size, side_source=side_source or "drawing geometry")
        return OpenDim("dia", size, flags=["Ø inside/outside not determined"])
    if ft == "radius":
        return OpenDim("radius", size)
    if ft == "chamfer":
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*°", text or "")
        ang = float(m.group(1).replace(",", ".")) if m else 45.0
        return OpenDim("chamfer", size, angle=ang, leg=size)
    if ft == "angular dimension":
        if leg is not None:
            return OpenDim("angular", size, angle=size, leg=leg)
        return OpenDim("angular", size, angle=size, leg=None, leg_estimated=True)
    return None


def classify_text(text: str, category: str = "") -> OpenDim | None:
    """
    Fallback when only the balloon text is known (legacy detector / old jobs): reuse the extractor's
    dimension grammar. Boxed (basic) status is unknown from text alone, so `category` must not be
    'basic' / 'reference' / 'gdt' / 'surface' / 'thread' / 'datum'.
    """
    cat = (category or "").lower()
    if cat in ("basic", "reference", "gdt", "surface", "thread", "datum", "fit", "note"):
        return None
    from app.services.tvs_extractor import parse_dimension

    t = (text or "").strip()
    if not t:
        return None
    if re.match(r"^\[.*\]$", t):           # [25] = basic
        return None
    info = parse_dimension(t, [])
    if info is None or info.get("ref"):
        return None
    return classify_feature(info["type"], t, info["nominal"], info["upper"], info["lower"], fit=info.get("fit"))
