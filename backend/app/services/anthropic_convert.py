"""
Anthropic (Claude) assisted conversion → vector PDF (SVG intermediate).

Used when mechanical converters fail or are not installed (FreeCAD, PVZ CLI, broken DXF/SVG).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import fitz

from app.services.ai_fallback import _http_json

log = logging.getLogger(__name__)


def anthropic_convert_enabled() -> bool:
    if os.environ.get("SMORX_ANTHROPIC_CONVERT", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _model() -> str:
    return os.environ.get(
        "ANTHROPIC_CONVERT_MODEL",
        os.environ.get("ANTHROPIC_VERIFY_MODEL", "claude-sonnet-4-20250514"),
    ).strip()


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing")
    return key


def _extract_svg(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"```(?:svg|xml)?\s*([\s\S]*?)```", raw, re.I)
    if m:
        raw = m.group(1).strip()
    m = re.search(r"(<svg[\s\S]*?</svg>)", raw, re.I)
    if not m:
        raise ValueError("Claude response did not contain SVG")
    svg = m.group(1)
    if "xmlns" not in svg[:300]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


def _svg_file_to_pdf(svg_path: Path, dest: Path) -> None:
    doc = fitz.open(str(svg_path))
    try:
        pdf_bytes = doc.convert_to_pdf()
    finally:
        doc.close()
    pdf = fitz.open("pdf", pdf_bytes)
    try:
        pdf.save(str(dest), deflate=True)
    finally:
        pdf.close()


def _claude_text(prompt: str, *, max_tokens: int = 16000) -> str:
    data = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "Content-Type": "application/json",
            "x-api-key": _api_key(),
            "anthropic-version": "2023-06-01",
        },
        {
            "model": _model(),
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    parts = data.get("content") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _claude_vision(prompt: str, png_bytes: bytes, *, max_tokens: int = 16000) -> str:
    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    data = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "Content-Type": "application/json",
            "x-api-key": _api_key(),
            "anthropic-version": "2023-06-01",
        },
        {
            "model": _model(),
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        },
    )
    parts = data.get("content") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _step_preview_png(step_path: Path) -> bytes | None:
    try:
        import trimesh
    except ImportError:
        return None
    try:
        scene = trimesh.load(str(step_path), force="mesh")
        if scene is None:
            return None
        if isinstance(scene, trimesh.Scene):
            png = scene.save_image(resolution=(1400, 1000))
        else:
            png = scene.save_image(resolution=(1400, 1000))
        return png
    except Exception as exc:
        log.debug("trimesh STEP preview failed: %s", exc)
        return None


def _read_text_source(path: Path, *, limit: int = 120_000) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")[:limit]


def anthropic_dxf_to_pdf(src: Path, dest: Path) -> dict[str, Any]:
    excerpt = _read_text_source(src)
    if len(excerpt) > 100_000:
        excerpt = excerpt[:50_000] + "\n...\n" + excerpt[-40_000:]

    prompt = f"""You convert AutoCAD DXF into one SVG page (vector) for inspection ballooning.

Rules:
- Output ONLY a single <svg>...</svg> document (no markdown).
- Preserve ALL TEXT, MTEXT, and DIMENSION text as <text> elements (use translate if needed).
- Draw LINE, LWPOLYLINE, CIRCLE, ARC as vector paths.
- viewBox should fit all geometry; units = mm.
- White background.

DXF input ({src.name}):
{excerpt}
"""
    raw = _claude_text(prompt)
    svg = _extract_svg(raw)
    with tempfile.TemporaryDirectory(prefix="claude_dxf_") as td:
        svg_path = Path(td) / "out.svg"
        svg_path.write_text(svg, encoding="utf-8")
        _svg_file_to_pdf(svg_path, dest)

    return {
        "converter": "anthropic_dxf→svg→pdf",
        "vector_hint": True,
        "ai_model": _model(),
        "manifest": {
            "source": src.name,
            "excerpt_chars": len(excerpt),
        },
    }


def anthropic_svg_to_pdf(src: Path, dest: Path) -> dict[str, Any]:
    svg_raw = _read_text_source(src, limit=150_000)
    prompt = f"""Fix/normalize this SVG for PDF export. Keep all visible text and geometry.
Output ONLY corrected <svg>...</svg> (no markdown). File: {src.name}

{svg_raw}
"""
    raw = _claude_text(prompt)
    svg = _extract_svg(raw)
    with tempfile.TemporaryDirectory(prefix="claude_svg_") as td:
        svg_path = Path(td) / "fixed.svg"
        svg_path.write_text(svg, encoding="utf-8")
        _svg_file_to_pdf(svg_path, dest)
    return {
        "converter": "anthropic_svg→pdf",
        "vector_hint": True,
        "ai_model": _model(),
    }


def anthropic_step_to_pdf(src: Path, dest: Path) -> dict[str, Any]:
    preview = _step_preview_png(src)
    if preview:
        prompt = (
            f"You receive a render of 3D model `{src.name}` (STEP). "
            "Produce a vector SVG (one page) with: (1) labeled orthographic wireframe "
            "or projected edges, (2) title block text with filename, "
            "(3) any dimension-like labels you can infer from the image as <text>. "
            "Output ONLY <svg>...</svg>, white background, engineering style."
        )
        raw = _claude_vision(prompt, preview)
    else:
        header = _read_text_source(src, limit=80_000)
        prompt = f"""STEP file `{src.name}` could not be meshed locally. From this ISO-10303 text excerpt,
produce a single SVG page: title block with filename, note "3D STEP — verify drawing PDF for PMI",
and a simple placeholder diagram. Output ONLY <svg>...</svg>.

{header}
"""
        raw = _claude_text(prompt)

    svg = _extract_svg(raw)
    with tempfile.TemporaryDirectory(prefix="claude_step_") as td:
        svg_path = Path(td) / "step.svg"
        svg_path.write_text(svg, encoding="utf-8")
        _svg_file_to_pdf(svg_path, dest)

    return {
        "converter": "anthropic_step→svg→pdf",
        "vector_hint": True,
        "ai_model": _model(),
        "step_manifest": {
            "source": src.name,
            "used_vision_preview": preview is not None,
        },
    }


def anthropic_pvz_to_pdf(src: Path, dest: Path) -> dict[str, Any]:
    """Try PVZ as zip; else ask Claude to extract structure hints and build placeholder SVG."""
    with tempfile.TemporaryDirectory(prefix="pvz_") as td:
        td_path = Path(td)
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as zf:
                for name in zf.namelist():
                    low = name.lower()
                    if low.endswith(".pdf"):
                        zf.extract(name, td_path)
                        shutil_pdf = td_path / name
                        shutil.copy2(shutil_pdf, dest)
                        return {
                            "converter": "pvz_zip_embedded_pdf",
                            "vector_hint": True,
                        }
                    if low.endswith(".svg"):
                        zf.extract(name, td_path)
                        svg_path = td_path / name
                        _svg_file_to_pdf(svg_path, dest)
                        return {
                            "converter": "pvz_zip_embedded_svg",
                            "vector_hint": True,
                        }

        excerpt = src.read_bytes()[:60_000]
        b64 = base64.standard_b64encode(excerpt).decode("ascii")
        prompt = f"""PVZ package `{src.name}` (Creo View). Binary/zip excerpt (base64 first 60k bytes): {b64[:500]}...

If you cannot decode, output an SVG placeholder page listing filename and instruction to use Creo View publish.
If you can infer sheet structure, include as text elements.
Output ONLY <svg>...</svg>.
"""
        raw = _claude_text(prompt, max_tokens=8000)
        svg = _extract_svg(raw)
        svg_path = td_path / "pvz.svg"
        svg_path.write_text(svg, encoding="utf-8")
        _svg_file_to_pdf(svg_path, dest)

    return {
        "converter": "anthropic_pvz→svg→pdf",
        "vector_hint": False,
        "ai_model": _model(),
        "note": "Placeholder; configure SMORX_PVZ_CONVERTER for production PVZ",
    }


def anthropic_fallback_convert(src: Path, dest: Path, kind: str) -> dict[str, Any]:
    kind = kind.lower()
    if kind == "dxf":
        return anthropic_dxf_to_pdf(src, dest)
    if kind == "svg":
        return anthropic_svg_to_pdf(src, dest)
    if kind == "step":
        return anthropic_step_to_pdf(src, dest)
    if kind == "pvz":
        return anthropic_pvz_to_pdf(src, dest)
    raise RuntimeError(f"Anthropic conversion not implemented for {kind}")


def maybe_enhance_with_anthropic(
    src: Path,
    dest: Path,
    kind: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """
    Optional second pass: if mechanical PDF has almost no text, ask Claude to rebuild from source.
    """
    if not anthropic_convert_enabled():
        return meta
    if kind not in {"dxf", "step", "svg"}:
        return meta
    if meta.get("has_selectable_text"):
        return meta
    if os.environ.get("SMORX_ANTHROPIC_REPAIR", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return meta
    try:
        repaired = anthropic_fallback_convert(src, dest, kind)
        repaired["repair_of"] = meta.get("converter")
        repaired["anthropic_repair"] = True
        return repaired
    except Exception as exc:
        log.warning("Anthropic repair skipped: %s", exc)
        meta["anthropic_repair_error"] = str(exc)[:300]
        return meta