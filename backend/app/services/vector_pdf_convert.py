"""
Detect upload format and convert to vector PDF for the existing balloon pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import fitz

log = logging.getLogger(__name__)

SUPPORTED = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".step",
    ".stp",
    ".dxf",
    ".svg",
    ".pvz",
}

_KIND_MAP = {
    ".pdf": "pdf",
    ".step": "step",
    ".stp": "step",
    ".dxf": "dxf",
    ".svg": "svg",
    ".pvz": "pvz",
}


class ConversionError(RuntimeError):
    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def detect_format(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}:
        return "image"
    if ext in _KIND_MAP:
        return _KIND_MAP[ext]
    if ext == ".pdf":
        return "pdf"
    return "unknown"


def is_supported_upload(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in SUPPORTED


def _svg_to_pdf(src: Path, dest: Path) -> dict:
    doc = fitz.open(str(src))
    try:
        pdf_bytes = doc.convert_to_pdf()
    finally:
        doc.close()
    pdf = fitz.open("pdf", pdf_bytes)
    try:
        pdf.save(str(dest), deflate=True)
    finally:
        pdf.close()
    return {
        "converter": "pymupdf_svg",
        "pages": _page_count(dest),
        "vector_hint": True,
    }


def _page_count(pdf: Path) -> int:
    doc = fitz.open(pdf)
    try:
        return max(1, len(doc))
    finally:
        doc.close()


def _dxf_manifest(src: Path) -> dict:
    try:
        import ezdxf
    except ImportError:
        return {"text_entities": [], "dimensions": 0, "note": "ezdxf not installed"}
    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    texts: list[str] = []
    dims = 0
    for e in msp:
        t = e.dxftype()
        if t == "TEXT":
            texts.append(str(e.dxf.text or ""))
        elif t == "MTEXT":
            texts.append(str(e.text or ""))
        elif t == "DIMENSION":
            dims += 1
            texts.append(str(getattr(e.dxf, "text", "") or ""))
    return {
        "text_entities": [x for x in texts if x.strip()][:500],
        "dimension_entities": dims,
        "layout": doc.dxfversion,
    }


def _dxf_to_pdf(src: Path, dest: Path) -> dict:
    try:
        import ezdxf
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
    except ImportError as e:
        raise ConversionError(
            "DXF conversion requires ezdxf (pip install ezdxf)",
            hint="Add ezdxf to backend requirements and redeploy.",
        ) from e

    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    backend = PyMuPdfBackend()
    Frontend(RenderContext(doc), backend).draw_layout(msp)
    backend.save(dest)
    manifest = _dxf_manifest(src)
    manifest_path = dest.with_suffix(dest.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "converter": "ezdxf_pymupdf",
        "pages": _page_count(dest),
        "vector_hint": True,
        "manifest": manifest,
    }


def _run_freecad_step_to_dxf(step: Path, dxf: Path) -> dict:
    cmd = os.environ.get("FREECAD_CMD", "FreeCADCmd")
    if not shutil.which(cmd) and not Path(cmd).is_file():
        raise ConversionError(
            "STEP conversion needs FreeCAD (FreeCADCmd on PATH or FREECAD_CMD).",
            hint="Install FreeCAD or convert STEP to PDF/DXF offline.",
        )
    script = Path(__file__).resolve().parents[2] / "scripts" / "freecad_step_to_dxf.py"
    proc = subprocess.run(
        [cmd, str(script), str(step), str(dxf)],
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("SMORX_STEP_TIMEOUT", "180")),
    )
    if proc.returncode != 0:
        raise ConversionError(
            f"FreeCAD STEP export failed: {proc.stderr[-800:] or proc.stdout[-800:]}",
            hint="Verify STEP is valid and FreeCAD can import it.",
        )
    manifest = {}
    mpath = dxf.with_suffix(".manifest.json")
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return manifest


def _step_to_pdf(src: Path, dest: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="step2pdf_") as td:
        dxf = Path(td) / f"{src.stem}.dxf"
        step_manifest = _run_freecad_step_to_dxf(src, dxf)
        if not dxf.is_file():
            raise ConversionError("FreeCAD did not produce DXF")
        conv = _dxf_to_pdf(dxf, dest)
        conv["converter"] = "freecad_step→dxf→pdf"
        conv["step_manifest"] = step_manifest
        return conv


def _pvz_to_pdf(src: Path, dest: Path) -> dict:
    template = os.environ.get("SMORX_PVZ_CONVERTER", "").strip()
    if not template:
        raise ConversionError(
            "PVZ conversion is not configured.",
            hint="Set SMORX_PVZ_CONVERTER to a command with {in} and {out}, e.g. "
            "CreaView export tool, or convert PVZ → PDF with Creo View / publish.",
        )
    cmd_str = template.format(**{"in": str(src), "out": str(dest)})
    proc = subprocess.run(
        cmd_str,
        shell=True,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("SMORX_PVZ_TIMEOUT", "300")),
    )
    if proc.returncode != 0 or not dest.is_file():
        raise ConversionError(
            f"PVZ converter failed: {proc.stderr[-500:] or proc.stdout[-500:]}",
            hint="Check SMORX_PVZ_CONVERTER command and Creo View installation.",
        )
    return {
        "converter": "pvz_external",
        "pages": _page_count(dest),
        "vector_hint": True,
        "source": src.name,
    }


def _pdf_copy(src: Path, dest: Path) -> dict:
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return {"converter": "pdf", "pages": _page_count(dest), "vector_hint": True}


def pdf_text_sample(pdf_path: Path, *, max_pages: int = 20) -> list[dict]:
    doc = fitz.open(pdf_path)
    out: list[dict] = []
    try:
        for i in range(min(len(doc), max_pages)):
            text = doc[i].get_text("text") or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            out.append({"page": i + 1, "line_count": len(lines), "lines": lines[:120]})
    finally:
        doc.close()
    return out


def _mechanical_convert(
    src: Path,
    dest: Path,
    kind: str,
    *,
    image_to_pdf,
) -> dict:
    from app.services.anthropic_convert import anthropic_convert_enabled, anthropic_step_to_pdf

    if kind == "pdf":
        return _pdf_copy(src, dest)
    if kind == "image":
        image_to_pdf(src, dest)
        return {"converter": "image_to_pdf", "pages": _page_count(dest), "vector_hint": False}
    if kind == "svg":
        return _svg_to_pdf(src, dest)
    if kind == "dxf":
        return _dxf_to_pdf(src, dest)
    if kind == "step":
        use_ai = os.environ.get("SMORX_STEP_USE_ANTHROPIC", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if use_ai and anthropic_convert_enabled():
            return anthropic_step_to_pdf(src, dest)
        return _step_to_pdf(src, dest)
    if kind == "pvz":
        return _pvz_to_pdf(src, dest)
    raise ConversionError(f"Unsupported format: {kind}")


def convert_to_vector_pdf(
    src: Path,
    dest: Path,
    kind: str,
    *,
    image_to_pdf,
) -> dict:
    """
    Convert upload to vector PDF. `image_to_pdf` is injected from upload route (PyMuPDF wrap).
    Returns conversion metadata stored on the job.
    """
    from app.services.anthropic_convert import (
        anthropic_convert_enabled,
        anthropic_fallback_convert,
        maybe_enhance_with_anthropic,
    )

    kind = (kind or detect_format(src.name)).lower()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ai_kinds = {"dxf", "svg", "step", "pvz"}

    try:
        meta = _mechanical_convert(src, dest, kind, image_to_pdf=image_to_pdf)
    except (ConversionError, Exception) as exc:
        if anthropic_convert_enabled() and kind in ai_kinds:
            log.warning("Mechanical %s convert failed (%s); trying Anthropic", kind, exc)
            meta = anthropic_fallback_convert(src, dest, kind)
            meta["mechanical_error"] = str(exc)[:500]
        elif isinstance(exc, ConversionError):
            raise
        else:
            raise ConversionError(str(exc), hint="Install converter or set ANTHROPIC_API_KEY") from exc

    meta["source_kind"] = kind
    meta["source_name"] = src.name
    meta["pages"] = meta.get("pages") or _page_count(dest)
    meta["pdf_text"] = pdf_text_sample(dest)
    meta["has_selectable_text"] = any(p["line_count"] > 0 for p in meta["pdf_text"])
    meta = maybe_enhance_with_anthropic(src, dest, kind, meta)
    if meta.get("anthropic_repair"):
        meta["pages"] = _page_count(dest)
        meta["pdf_text"] = pdf_text_sample(dest)
        meta["has_selectable_text"] = any(p["line_count"] > 0 for p in meta["pdf_text"])
    return meta