"""Critical / major parameter classification for extracted drawing data."""

from __future__ import annotations

import re
from typing import Any

CRITICAL_PHRASES = (
    r"\bCRITICAL\b",
    r"\bCTQ\b",
    r"\bKEY\s+CHARACTERISTIC",
    r"\bSAFETY\b",
    r"\bCLASS\s*1\b",
    r"\bFAI\b",
    r"\b100\s*%\s*INSPECT",
    r"\bDO\s+NOT\s+USE\s+IF",
    r"\bFIT\s+FOR\s+FUNCTION\b",
    r"\bTORQUE\b",
    r"\bPRESS\s*FIT\b",
    r"\bINTERFERENCE\s+FIT\b",
)

MAJOR_PHRASES = (
    r"\bMAJOR\b",
    r"\bSIGNIFICANT\b",
    r"\bCLASS\s*2\b",
    r"\bFUNCTIONAL\b",
    r"\bMATING\b",
    r"\bSEAL\b",
    r"\bBEARING\b",
    r"\bBORE\b",
    r"\bPOSITION\b",
    r"\bPROFILE\b",
)

TIGHT_TOL = re.compile(r"±\s*0?\.0{1,2}[1-9]|±\s*0\.0[0-4]\d", re.I)
GDT_CRITICAL = frozenset({"position", "profile", "runout", "perpendicularity", "flatness"})
FIT_CRITICAL = re.compile(r"\b[HhGg][678]\s*/?\s*[a-z]{1,2}[567]\b|[Hh]7|[gG]6|[pP]6", re.I)
THREAD_CRITICAL = re.compile(r"\bM\d{1,2}\b.*\b(DEEP|PITCH|CLASS)\b", re.I)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    t = text or ""
    return any(re.search(p, t, re.I) for p in patterns)


def classify_parameter(
    text: str,
    *,
    category: str = "",
    pattern: str = "",
    item_type: str = "dimension",
    gdt: dict | None = None,
) -> dict[str, Any]:
    """
    Returns priority: critical | major | standard, score 0-1, reasons[].
    """
    reasons: list[str] = []
    score = 0.0
    t = (text or "").strip()

    if _matches_any(t, CRITICAL_PHRASES):
        score += 0.55
        reasons.append("critical_keyword")

    if _matches_any(t, MAJOR_PHRASES):
        score += 0.35
        reasons.append("major_keyword")

    if TIGHT_TOL.search(t):
        score += 0.25
        reasons.append("tight_tolerance")

    if category in {"gdt", "feature"} or item_type == "gdt":
        char = (gdt or {}).get("characteristic") or t
        if any(x in str(char).lower() for x in GDT_CRITICAL):
            score += 0.4
            reasons.append("gdt_characteristic")

    if category == "fit" or FIT_CRITICAL.search(t):
        score += 0.3
        reasons.append("fit_class")

    if category == "thread" or THREAD_CRITICAL.search(t):
        score += 0.35
        reasons.append("thread_spec")

    if category in {"surface", "surface_finish"} and re.search(r"\bN[678]\b|Ra\s*0?\.\d", t, re.I):
        score += 0.25
        reasons.append("surface_finish_spec")

    if pattern in {"gdt_fcf", "gdt_tolerance"}:
        score += 0.35
        reasons.append("gdt_fcf_pattern")

    if re.search(r"\b\d+\s*[°xX×]\s*\d+\s*°", t):
        score += 0.15
        reasons.append("chamfer_or_angle")

    if score >= 0.55:
        priority = "critical"
    elif score >= 0.3:
        priority = "major"
    else:
        priority = "standard"

    return {
        "text": t,
        "category": category,
        "pattern": pattern,
        "item_type": item_type,
        "priority": priority,
        "score": round(min(1.0, score), 3),
        "reasons": reasons,
    }


def classify_collection(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        row = classify_parameter(
            str(it.get("text") or it.get("raw") or it.get("value") or ""),
            category=str(it.get("category") or ""),
            pattern=str(it.get("pattern") or ""),
            item_type=str(it.get("item_type") or "dimension"),
            gdt=it if it.get("characteristic") else None,
        )
        row.update(
            {
                "page": it.get("page"),
                "x": it.get("x"),
                "y": it.get("y"),
            }
        )
        out.append(row)
    return out