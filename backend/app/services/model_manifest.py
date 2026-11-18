"""STEP / PVZ / 3D source manifests (beyond PDF conversion)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any


def parse_step_manifest(step_path: Path) -> dict[str, Any]:
    raw = step_path.read_bytes()
    text = raw.decode("utf-8", errors="ignore")
    if len(text) < 50:
        text = raw.decode("latin-1", errors="ignore")

    products = re.findall(r"PRODUCT\s*\(\s*'([^']+)'", text, re.I)
    shapes = len(re.findall(r"MANIFOLD_SOLID_BREP|CLOSED_SHELL|ADVANCED_BREP", text, re.I))
    assemblies = len(re.findall(r"NEXT_ASSEMBLY_USAGE_OCCURREN", text, re.I))
    pmis = len(re.findall(r"TOLERANCE|DIMENSIONAL|TOLERANCED_GEOMETR", text, re.I))
    header_name = ""
    m = re.search(r"FILE_NAME\s*\(\s*'([^']+)'", text, re.I)
    if m:
        header_name = m.group(1)

    bbox_hint = None
    try:
        import trimesh

        scene = trimesh.load(str(step_path), force="mesh")
        if scene is not None:
            if hasattr(scene, "bounds"):
                bb = scene.bounds
                bbox_hint = {
                    "min": [float(x) for x in bb[0]],
                    "max": [float(x) for x in bb[1]],
                }
            elif hasattr(scene, "extents"):
                bbox_hint = {"extents_mm": [float(x) for x in scene.extents]}
    except Exception:
        pass

    return {
        "format": "step",
        "file": step_path.name,
        "file_description": header_name,
        "products": products[:50],
        "solid_features": shapes,
        "assembly_links": assemblies,
        "pmi_keyword_hits": pmis,
        "has_pmi_hints": pmis > 0,
        "bbox_hint": bbox_hint,
        "interpretation": (
            "3D B-Rep model. Dimensions/GD&T on drawings are read from converted PDF; "
            "PMI in STEP is flagged by keyword scan — use native drawing PDF for full GD&T."
            if pmis == 0
            else "STEP contains PMI-related entities; verify against converted PDF."
        ),
    }


def parse_pvz_manifest(pvz_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "format": "pvz",
        "file": pvz_path.name,
        "is_zip": zipfile.is_zipfile(pvz_path),
        "entries": [],
        "embedded_pdfs": [],
        "embedded_svg": [],
        "embedded_models": [],
    }
    if not out["is_zip"]:
        out["interpretation"] = "Binary PVZ — use Creo View publish or SMORX_PVZ_CONVERTER for full structure."
        return out

    with zipfile.ZipFile(pvz_path) as zf:
        for info in zf.infolist():
            low = info.filename.lower()
            row = {
                "name": info.filename,
                "size": info.file_size,
                "compressed": info.compress_size,
            }
            out["entries"].append(row)
            if low.endswith(".pdf"):
                out["embedded_pdfs"].append(info.filename)
            elif low.endswith(".svg"):
                out["embedded_svg"].append(info.filename)
            elif low.endswith((".pvs", ".ol", ".idx", ".u3d", ".prc", ".step", ".stp")):
                out["embedded_models"].append(info.filename)

    out["sheet_estimate"] = max(1, len(out["embedded_pdfs"]) or len(out["embedded_svg"]) or 1)
    out["interpretation"] = (
        "PVZ package parsed: "
        f"{len(out['entries'])} files, "
        f"{len(out['embedded_pdfs'])} PDF(s), "
        f"{len(out['embedded_models'])} model asset(s)."
    )
    return out


def build_model_manifests(job_dir: Path, conversion_log: list[dict] | None) -> dict[str, Any]:
    manifests: dict[str, Any] = {"sources": []}
    if not conversion_log:
        return manifests

    for i, entry in enumerate(conversion_log):
        kind = (entry.get("source_kind") or "").lower()
        name = entry.get("source_name") or f"upload_{i}"
        candidates = list(job_dir.glob(f"upload_*_{name}")) + list(job_dir.glob(f"upload_{i}_*"))
        src = candidates[0] if candidates else None
        if src is None or not src.is_file():
            continue
        if kind == "step":
            m = parse_step_manifest(src)
            m["conversion"] = entry.get("converter")
            manifests["sources"].append(m)
        elif kind == "pvz":
            m = parse_pvz_manifest(src)
            m["conversion"] = entry.get("converter")
            manifests["sources"].append(m)
        elif kind in {"dxf", "svg", "pdf"}:
            manifests["sources"].append(
                {
                    "format": kind,
                    "file": name,
                    "conversion": entry.get("converter"),
                    "interpretation": "2D drawing source — full text mined from converted PDF.",
                }
            )

    if manifests["sources"]:
        manifests["summary"] = (
            f"{len(manifests['sources'])} source file(s) interpreted; "
            "inspection data is unified in master extraction Excel."
        )
    return manifests