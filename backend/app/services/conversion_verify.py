"""
Anthropic verification agents after format → vector PDF conversion.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import fitz

from app.services.ai_fallback import _http_json
from app.services.vector_pdf_convert import pdf_text_sample


def verify_enabled() -> bool:
    if os.environ.get("SMORX_VERIFY_CONVERSION", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_VERIFY_MODEL", "claude-sonnet-5-5").strip()


def _render_page_png(pdf: Path, page: int = 0, scale: float = 1.25) -> bytes:
    doc = fitz.open(pdf)
    try:
        pg = doc[min(page, len(doc) - 1)]
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _call_verify_agent(
    *,
    agent: str,
    source_manifest: dict[str, Any],
    pdf_path: Path,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {
            "agent": agent,
            "status": "skipped",
            "reason": "ANTHROPIC_API_KEY not set",
        }

    pdf_text = pdf_text_sample(pdf_path)
    png = _render_page_png(pdf_path)
    b64 = base64.standard_b64encode(png).decode("ascii")

    prompt = f"""You are the {agent} conversion verification agent for engineering drawings.

The system converted a {agent.upper()} source file into a vector PDF for auto-ballooning.

Source manifest (from converter):
{json.dumps(source_manifest, indent=2, ensure_ascii=False)[:12000]}

Extracted text from converted PDF (sample):
{json.dumps(pdf_text, indent=2, ensure_ascii=False)[:12000]}

Inspect the attached PDF page image. Return ONLY valid JSON:
{{
  "status": "ok" | "warning" | "fail",
  "completeness_score": 0.0-1.0,
  "missing": ["list of likely missing dimensions, notes, GD&T, title data, views"],
  "found": ["notable items present in PDF"],
  "recommendation": "one short paragraph"
}}

Focus on whether a 2D inspection drawing workflow can proceed (dimensions, notes, title block).
For STEP: flag if only 3D edge export with no PMI/dimensions.
For DXF: compare manifest text/dimensions vs PDF text.
For PVZ: flag missing sheets or empty views.
"""

    data = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        {
            "model": _anthropic_model(),
            "max_tokens": 2048,
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
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"agent": agent, "status": "warning", "raw": text[:2000]}
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"agent": agent, "status": "warning", "raw": text[:2000]}
    parsed["agent"] = agent
    return parsed


def verify_step_conversion(conversion_meta: dict[str, Any], pdf_path: Path) -> dict[str, Any]:
    manifest = {
        "format": "step",
        "step_manifest": conversion_meta.get("step_manifest") or {},
        "converter": conversion_meta.get("converter"),
    }
    return _call_verify_agent(agent="step", source_manifest=manifest, pdf_path=pdf_path)


def verify_dxf_conversion(conversion_meta: dict[str, Any], pdf_path: Path) -> dict[str, Any]:
    manifest = {
        "format": "dxf",
        "manifest": conversion_meta.get("manifest") or {},
        "converter": conversion_meta.get("converter"),
    }
    return _call_verify_agent(agent="dxf", source_manifest=manifest, pdf_path=pdf_path)


def verify_pvz_conversion(conversion_meta: dict[str, Any], pdf_path: Path) -> dict[str, Any]:
    manifest = {
        "format": "pvz",
        "converter": conversion_meta.get("converter"),
        "source": conversion_meta.get("source_name"),
    }
    return _call_verify_agent(agent="pvz", source_manifest=manifest, pdf_path=pdf_path)


def verify_pdf_upload(conversion_meta: dict[str, Any], pdf_path: Path) -> dict[str, Any]:
    """Optional sanity check when upload is already PDF."""
    manifest = {
        "format": "pdf",
        "has_selectable_text": conversion_meta.get("has_selectable_text"),
        "pages": conversion_meta.get("pages"),
    }
    return _call_verify_agent(agent="pdf", source_manifest=manifest, pdf_path=pdf_path)


def run_conversion_verification(
    source_kind: str,
    conversion_meta: dict[str, Any],
    pdf_path: Path,
) -> dict[str, Any]:
    if not verify_enabled():
        return {"status": "skipped", "reason": "SMORX_VERIFY_CONVERSION off or no API key"}

    kind = (source_kind or "").lower()
    try:
        if kind == "step":
            report = verify_step_conversion(conversion_meta, pdf_path)
        elif kind == "dxf":
            report = verify_dxf_conversion(conversion_meta, pdf_path)
        elif kind == "pvz":
            report = verify_pvz_conversion(conversion_meta, pdf_path)
        elif kind == "pdf":
            report = verify_pdf_upload(conversion_meta, pdf_path)
        else:
            return {
                "status": "skipped",
                "reason": f"No verify agent for kind={kind}",
            }
        report["verified_at"] = "conversion"
        return report
    except Exception as exc:
        return {"status": "error", "agent": kind, "error": str(exc)[:500]}