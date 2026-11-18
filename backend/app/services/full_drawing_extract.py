"""
Full drawing extraction: all dimensions, all text/notes, GD&T, surface, critical/major, 3D manifests.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import fitz

from app.services.critical_parameters import classify_collection
from app.services.drawing_extraction import (
    extract_drawing_notes,
    extract_surface_finish,
    extract_vector_drawing_data,
)
from app.services.gdt_fcf_graphics import extract_fcf_from_page, records_to_dicts
from app.services.model_manifest import build_model_manifests


def full_extract_enabled() -> bool:
    return os.environ.get("SMORX_FULL_EXTRACT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def harvest_all_text(doc: fitz.Document) -> list[dict]:
    """Every text line on every page with coordinates (minimal noise filter)."""
    from app.services.autoballoon_engine import _build_page_zones, _iter_text_lines, _point_in_rects

    skip = re.compile(
        r"^[A-Z]$|^\d$|^SHEET\s+\d|^SCALE\b|^DWG\b",
        re.I,
    )
    out: list[dict] = []
    seen: set[str] = set()
    for page_index in range(len(doc)):
        page = doc[page_index]
        zones = _build_page_zones(page)
        for text, rect, size, *_ in _iter_text_lines(page):
            t = text.strip()
            if len(t) < 1 or skip.match(t):
                continue
            tcx = (rect.x0 + rect.x1) / 2
            tcy = (rect.y0 + rect.y1) / 2
            zone = "drawing"
            if zones:
                if _point_in_rects(tcx, tcy, zones.notes):
                    zone = "notes"
                elif _point_in_rects(tcx, tcy, zones.title):
                    zone = "title"
                elif _point_in_rects(tcx, tcy, zones.bom):
                    zone = "bom"
            key = f"{page_index + 1}:{t}:{round(tcx)}:{round(tcy)}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "page": page_index + 1,
                    "x": round(float(rect.x0), 2),
                    "y": round(float(rect.y0), 2),
                    "width": round(float(rect.width), 2),
                    "height": round(float(rect.height), 2),
                    "text": t,
                    "zone": zone,
                    "font_size": round(float(size), 2),
                    "item_type": "text",
                }
            )
    return out


def harvest_all_notes(doc: fitz.Document) -> list[dict]:
    """Notes zones + any text line matching numbered note pattern anywhere on sheet."""
    note_pat = re.compile(r"^\s*\d{1,3}[.)]\s+.+", re.DOTALL)
    out: list[dict] = []
    seen: set[str] = set()

    for page_index in range(len(doc)):
        page = doc[page_index]
        for rec in extract_drawing_notes(page, page_index):
            d = {
                "page": rec.page,
                "x": rec.x,
                "y": rec.y,
                "note_no": rec.note_no,
                "text": rec.text,
                "zone": rec.zone,
                "item_type": "note",
            }
            key = f"{d['page']}:{d['text'][:60]}"
            if key not in seen:
                seen.add(key)
                out.append(d)

    from app.services.autoballoon_engine import _iter_text_lines

    for page_index in range(len(doc)):
        page = doc[page_index]
        for text, rect, *_ in _iter_text_lines(page):
            if not note_pat.match(text):
                continue
            key = f"{page_index + 1}:{text[:60]}"
            if key in seen:
                continue
            seen.add(key)
            m = re.match(r"^\s*(\d{1,3})[.)]\s+(.+)", text, re.DOTALL)
            out.append(
                {
                    "page": page_index + 1,
                    "x": float(rect.x0),
                    "y": float(rect.y0),
                    "note_no": m.group(1) if m else "",
                    "text": m.group(2).strip() if m else text,
                    "zone": "drawing",
                    "item_type": "note",
                }
            )
    return out


def harvest_all_dimensions(doc: fitz.Document) -> list[dict]:
    """Maximum-recall dimension harvest (all zones, multipass)."""
    from app.services.autoballoon_engine import detect_dimensions_multipass

    passes = int(os.environ.get("SMORX_FULL_EXTRACT_PASSES", "5"))
    chars, _log = detect_dimensions_multipass(
        doc,
        passes=passes,
        use_zones=False,
        use_tiles=True,
        use_ocr=os.environ.get("SMORX_OCR_FALLBACK", "1").strip().lower()
        not in {"0", "false", "no", "off"},
    )
    out: list[dict] = []
    for c in chars:
        out.append(
            {
                "page": c.page,
                "x": round(c.x, 2),
                "y": round(c.y, 2),
                "width": round(c.width, 2),
                "height": round(c.height, 2),
                "text": c.text,
                "category": c.category,
                "pattern": c.pattern,
                "item_type": "dimension",
            }
        )
    return out


def extract_full_gdt(doc: fitz.Document) -> list[dict]:
    """Merge regex + graphics FCF parsing."""
    all_recs: list[dict] = []
    seen: set[str] = set()
    base = extract_vector_drawing_data(doc).get("gdt_fcf") or []
    for row in base:
        k = f"{row.get('page')}:{row.get('frame_text')}"
        seen.add(k)
        all_recs.append({**row, "item_type": "gdt"})
    for page_index in range(len(doc)):
        page = doc[page_index]
        for rec in extract_fcf_from_page(page, page_index):
            d = records_to_dicts([rec])[0]
            d["item_type"] = "gdt"
            k = f"{d.get('page')}:{d.get('frame_text')}"
            if k in seen:
                continue
            seen.add(k)
            all_recs.append(d)
    return all_recs


def extract_full_surface(doc: fitz.Document) -> list[dict]:
    out: list[dict] = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        for rec in extract_surface_finish(page, page_index, require_symbol_anchor=False):
            out.append(
                {
                    "page": rec.page,
                    "x": rec.x,
                    "y": rec.y,
                    "kind": rec.kind,
                    "value": rec.value,
                    "text": rec.raw,
                    "raw": rec.raw,
                    "item_type": "surface_finish",
                }
            )
    return out


def run_full_extraction(
    pdf_path: Path,
    job_dir: Path,
    meta: dict,
) -> dict[str, Any]:
    if not full_extract_enabled():
        return meta.get("full_extraction") or {}

    doc = fitz.open(pdf_path)
    try:
        dimensions = harvest_all_dimensions(doc)
        all_text = harvest_all_text(doc)
        notes = harvest_all_notes(doc)
        gdt = extract_full_gdt(doc)
        surface = extract_full_surface(doc)

        pool: list[dict] = []
        pool.extend(dimensions)
        pool.extend(gdt)
        pool.extend(surface)
        pool.extend(notes)
        for t in all_text:
            if t.get("zone") in {"notes", "bom"}:
                continue
            pool.append(
                {
                    "page": t["page"],
                    "x": t["x"],
                    "y": t["y"],
                    "text": t["text"],
                    "category": t.get("zone", ""),
                    "pattern": "",
                    "item_type": "text",
                }
            )

        critical_major = classify_collection(pool)
        critical_only = [r for r in critical_major if r["priority"] in {"critical", "major"}]

        model_manifest = build_model_manifests(job_dir, meta.get("conversion_log"))

        return {
            "dimensions": dimensions,
            "all_text": all_text,
            "notes": notes,
            "gdt_fcf": gdt,
            "surface_finish": surface,
            "critical_major": critical_major,
            "critical_major_highlighted": critical_only,
            "model_3d_manifest": model_manifest,
            "counts": {
                "dimensions": len(dimensions),
                "text_lines": len(all_text),
                "notes": len(notes),
                "gdt_fcf": len(gdt),
                "surface_finish": len(surface),
                "critical": sum(1 for r in critical_major if r["priority"] == "critical"),
                "major": sum(1 for r in critical_major if r["priority"] == "major"),
            },
        }
    finally:
        doc.close()