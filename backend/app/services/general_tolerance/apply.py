"""
Glue between the general-tolerance engine and the app's outputs:

  • jobs (inspection report rows)  — `refresh_job(meta, job_dir)`; called whenever job meta is saved
  • TVS extraction Excel           — `apply_to_extraction_rows(rows, feats, ctx, overrides)`
  • drawing-level context          — `context_for_job(meta, job_dir)` / `context_for_pdf(pdf, ...)`

Row fields written on inspection-report rows (existing fields untouched otherwise):
  tol_source  "Drawing" | "Manual" | "General tol – …" | "Rule: …" | "Org default – …" | "None – …"
  tol_flags   list[str]           tol_auto  bool (value came from a standard / rule)
  tol_angle   "45° ±1°" (chamfers) tol_category  feature category used for the lookup
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.services.general_tolerance import engine as E
from app.services.general_tolerance import store
from app.services.general_tolerance.classify import OpenDim, classify_text
from app.services.general_tolerance.detect import DrawingGT, detect_drawing

_LOG = logging.getLogger(__name__)
GT_VERSION = 1


def enabled_globally() -> bool:
    return os.environ.get("SMORX_GENERAL_TOL", "1").strip().lower() not in ("0", "false", "no", "off")


# ----------------------------------------------------------------------------- context

def org_for_meta(meta: dict) -> str:
    if meta.get("org_id"):
        return str(meta["org_id"])
    try:
        from app.services import auth_service

        u = auth_service.get_user(meta.get("owner")) if meta.get("owner") else None
        if u and u.get("org_id"):
            return str(u["org_id"])
    except Exception:
        pass
    return "default"


def drawing_key_for_meta(meta: dict) -> str:
    from app.services.drawing_versions import drawing_key_for

    files = meta.get("files") or [{}]
    name = (files[int(meta.get("active_file") or 0)] if len(files) > int(meta.get("active_file") or 0) else files[0]).get("name", "")
    return drawing_key_for(str(meta.get("part_number") or ""), name)[0]


def _features_from_pdf(pdf: Path) -> list[dict]:
    """Eligible open dimensions with their drawing-geometry class (extractor run once per file)."""
    try:
        from app.services.tvs_extractor import extract_drawing

        ext = extract_drawing(pdf)
    except Exception:
        _LOG.exception("general tolerance: extractor failed on %s", pdf)
        return []
    out = []
    for f in ext.features:
        gc = f.meta.get("gt_class")
        if gc:
            out.append({"page": f.sheet, "text": f.text, "rect": [f.rect.x0, f.rect.y0, f.rect.x1, f.rect.y1],
                        "balloon": f.balloon, "gt_class": gc})
    return out


def ensure_job_cache(meta: dict, job_dir: Path, *, force: bool = False) -> dict:
    """Detection + per-feature classes for the job's active file, cached in meta['general_tolerance']."""
    gt = meta.setdefault("general_tolerance", {})
    files = meta.get("files") or []
    idx = int(meta.get("active_file") or 0)
    if not files or idx >= len(files):
        return gt
    src = Path(job_dir) / files[idx]["path"]
    stamp = f"{files[idx]['path']}:{src.stat().st_mtime_ns if src.exists() else 0}:{GT_VERSION}"
    if not force and gt.get("stamp") == stamp and "detected" in gt:
        return gt
    if not src.exists():
        return gt
    try:
        det = detect_drawing(src, str(meta.get("material") or ""))
    except Exception:
        _LOG.exception("general tolerance: detection failed")
        det = DrawingGT(flags=["detection failed"])
    gt["detected"] = det.to_dict()
    gt["features"] = _features_from_pdf(src)
    gt["stamp"] = stamp
    return gt


def build_context(detected: DrawingGT, org_id: str, drawing_key: str, part_number: str) -> E.GTContext:
    org = store.get_org_settings(org_id)
    ds = store.get_drawing_settings(org_id, drawing_key) if drawing_key else dict(store.DEFAULT_DRAWING_SETTINGS)
    rules = store.list_rules(org_id, active_only=True)
    fam = store.family_key(part_number, org.get("family_pattern") or "")
    return E.GTContext(detected, org, ds, rules, drawing_key, fam, org_id)


def context_for_job(meta: dict, job_dir: Path) -> E.GTContext:
    gt = ensure_job_cache(meta, job_dir)
    det = DrawingGT.from_dict(gt.get("detected"))
    return build_context(det, org_for_meta(meta), drawing_key_for_meta(meta), str(meta.get("part_number") or ""))


def context_for_pdf(pdf: Path, *, org_id: str = "default", part_number: str = "", file_name: str = "",
                    material: str = "") -> E.GTContext:
    from app.services.drawing_versions import drawing_key_for

    det = detect_drawing(Path(pdf), material)
    key = drawing_key_for(part_number, file_name or Path(pdf).name)[0]
    return build_context(det, org_id, key, part_number)


def banner(ctx: E.GTContext) -> dict:
    """Per-drawing banner: 'General tolerance: TSDS0407 – machining (from title block)'."""
    std, flags = ctx.stated()
    d = ctx.drawing
    proc, pflags = ctx.process_for(None)
    from app.services.general_tolerance.detect import PROCESSES

    if d.table and not (ctx.drawing_settings or {}).get("standard"):
        text = "General tolerance: table printed on the drawing"
        source = "drawing"
    elif std is not None:
        where = {"title block": "from title block", "notes": "from drawing notes", "set by user": "set for this drawing"}.get(std.location, std.location)
        text = f"General tolerance: {std.label()}"
        if std.id == "TSDS0407":
            ps = "set for this drawing" if (ctx.drawing_settings or {}).get("process") else (
                d.process_source and f"from {d.process_source}" or "assumed")
            text += f" – {PROCESSES.get(proc, proc).lower()} ({where}; process {ps})"
        else:
            text += f" ({where})"
        source = std.location
    elif (ctx.org_settings or {}).get("default_standard"):
        os_ = ctx.org_settings
        text = f"General tolerance: organisation default {os_['default_standard']}" + (
            f" – {PROCESSES.get(proc, proc).lower()}" if os_["default_standard"] == "TSDS0407" else f"-{os_.get('default_iso_class')}")
        source = "org default"
    else:
        text = "General tolerance: none stated on this drawing"
        source = "none"
    needs_process = bool(std is not None and std.id == "TSDS0407" and not (ctx.drawing_settings or {}).get("process")
                         and not d.process)
    return {"text": text, "source": source, "standard": std.label() if std else "",
            "standard_id": std.id if std else "", "iso_class": std.cls if std else None,
            "process": proc if (std is None or std.id == "TSDS0407") else None,
            "needs_process": needs_process, "process_hint": d.process_hint, "flags": flags + d.flags,
            "conflict": d.conflict, "sheet_thickness": ctx.sheet_thickness, "geometric_class": d.geometric_class,
            "drawing_key": ctx.drawing_key, "family_key": ctx.family_key, "text_source": d.text_source,
            "detected_standards": [s.label() + f" ({s.location})" for s in d.standards],
            "table_rows": d.table, "enabled": (ctx.org_settings or {}).get("enabled", True) and enabled_globally()}


# ----------------------------------------------------------------------------- matching balloons to features

def _overlap(a: list[float], b: list[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def open_dim_for_balloon(b: dict, features: list[dict]) -> OpenDim | None:
    text = str(b.get("text") or "").strip()
    page = int(b.get("page") or 1)
    x, y = float(b.get("x") or 0), float(b.get("y") or 0)
    box = [x, y, x + float(b.get("width") or 0), y + float(b.get("height") or 0)]
    best, best_ov = None, 0.0
    for f in features:
        if f["page"] != page:
            continue
        ov = _overlap(box, f["rect"])
        same = f["text"].replace(" ", "") == text.replace(" ", "")
        if same and (ov > 0 or (abs(f["rect"][0] - x) < 3 and abs(f["rect"][1] - y) < 3)):
            ov += 1e6
        if ov > best_ov:
            best, best_ov = f, ov
    if best is not None and best_ov > 0:
        return OpenDim.from_dict(best["gt_class"])
    cat = str(b.get("category") or "")
    return classify_text(text, cat)


# ----------------------------------------------------------------------------- inspection report rows

AUTO_PREFIXES = ("General tol", "Rule:", "Org default", "None –")


def _printed(row_text: str) -> tuple[str, str]:
    from app.services.report_pdf import parse_tolerance

    _n, lo, hi = parse_tolerance(row_text)
    return lo, hi


def apply_rows(rows: list[dict], balloons: list[dict], ctx: E.GTContext, features: list[dict]) -> dict:
    """Fill open-dimension tolerances on report rows in place. Returns counts for the summary."""
    by_key = {(int(b.get("page") or 1), int(b.get("balloon") or 0)): b for b in balloons}
    manual_store = (ctx.drawing_settings or {}).get("manual") or {}
    on = (ctx.org_settings or {}).get("enabled", True) and (ctx.drawing_settings or {}).get("enabled", True) and enabled_globally()
    counts = {"open": 0, "standard": 0, "rule": 0, "manual": 0, "unresolved": [], "drawing": 0, "manual_updates": {}}
    for row in rows:
        b = by_key.get((int(row.get("page") or 1), int(row.get("balloon") or 0)))
        if b is None:
            continue
        text = str(b.get("text") or "")
        plo, phi = _printed(text)
        src = str(row.get("tol_source") or "")
        has_vals = bool(str(row.get("tol_low") or "").strip() or str(row.get("tol_high") or "").strip())
        if src == "Reset":                      # inspector asked to go back to the automatic value
            counts["manual_updates"][str(int(row.get("balloon") or 0))] = None
            manual_store = {k: v for k, v in manual_store.items() if k != str(int(row.get("balloon") or 0))}
            row["tol_low"] = row["tol_high"] = ""
            src, has_vals = "", False
            row.pop("tol_source", None)
        elif src == "Manual" and not has_vals:  # values blanked by a balloon refresh: restore from the store
            src = ""
        if plo or phi:
            # 1. printed on the drawing (an explicit edit of a printed value is still a manual value)
            if src != "Manual":
                row["tol_source"] = "Drawing"
                row.pop("tol_auto", None)
                row.pop("tol_flags", None)
            counts["drawing"] += 1
            continue
        od = open_dim_for_balloon(b, features)
        if od is None:
            if (row.get("tol_low") or row.get("tol_high")) and not src.startswith(AUTO_PREFIXES):
                row["tol_source"] = "Manual"
            elif src.startswith(AUTO_PREFIXES):
                row["tol_low"] = row["tol_high"] = ""
                for k in ("tol_source", "tol_auto", "tol_flags", "tol_angle", "tol_category", "tol_size"):
                    row.pop(k, None)
            continue
        counts["open"] += 1
        bno = int(row.get("balloon") or 0)
        row["tol_size"] = od.size
        manual = None
        if src == "Manual" or (has_vals and not src):
            manual = {"upper": _f(row.get("tol_high")), "lower": _f(row.get("tol_low")), "unit": "mm"}
            m = manual_store.get(str(bno)) or {}
            if (str(m.get("tol_low")), str(m.get("tol_high")), m.get("text")) != (str(row.get("tol_low")), str(row.get("tol_high")), text):
                counts["manual_updates"][str(bno)] = {"text": text, "tol_low": str(row.get("tol_low") or ""),
                                                      "tol_high": str(row.get("tol_high") or "")}
        else:
            m = manual_store.get(str(bno))
            if m and str(m.get("text") or "").replace(" ", "") == text.replace(" ", ""):
                manual = {"upper": _f(m.get("tol_high")), "lower": _f(m.get("tol_low")), "unit": "mm"}
        if manual is not None:
            if src != "Manual":
                row["tol_low"], row["tol_high"] = _s(manual["lower"]), _s(manual["upper"])
            row["tol_source"] = "Manual"
            row["tol_auto"] = False
            row["tol_category"] = od.category
            counts["manual"] += 1
            continue
        if not on:
            if src.startswith(AUTO_PREFIXES):
                row["tol_low"] = row["tol_high"] = ""
                for k in ("tol_source", "tol_auto", "tol_flags", "tol_angle"):
                    row.pop(k, None)
            continue
        res = E.resolve(od, ctx, balloon=bno)
        lo, hi = E.tol_strings(res.primary)
        row["tol_low"], row["tol_high"] = lo, hi
        row["tol_source"] = res.primary.source
        row["tol_auto"] = res.primary.ok
        row["tol_flags"] = list(dict.fromkeys(res.flags))
        row["tol_category"] = od.category
        if res.angle is not None:
            row["tol_angle"] = (f"{res.angle_value:g}° {E.describe(res.angle)}" if res.angle.ok
                                else f"{res.angle_value:g}°: {res.angle.source}")
        else:
            row.pop("tol_angle", None)
        if not res.primary.ok:
            counts["unresolved"].append(bno)
        elif res.primary.source.startswith("Rule:"):
            counts["rule"] += 1
        else:
            counts["standard"] += 1
    return counts


def _f(v) -> float | None:
    try:
        s = str(v).strip().replace(",", ".")
        return float(s) if s not in ("", "None") else None
    except (TypeError, ValueError):
        return None


def _s(v: float | None) -> str:
    if v is None:
        return ""
    if abs(v) < 1e-12:
        return "0"
    return f"{v:+.4f}".rstrip("0").rstrip(".")


def refresh_job(meta: dict, job_dir: Path) -> None:
    """Recompute auto-filled tolerances on the job's report rows (idempotent)."""
    rows = meta.get("report_rows") or []
    balloons = meta.get("balloons") or []
    if not rows or not balloons:
        return
    meta.setdefault("org_id", org_for_meta(meta))
    ctx = context_for_job(meta, job_dir)
    counts = apply_rows(rows, balloons, ctx, (meta.get("general_tolerance") or {}).get("features") or [])
    updates = counts.pop("manual_updates", {})
    if updates and ctx.drawing_key:
        # Manual overrides persist by balloon number for this drawing (all revisions while the feature is unchanged)
        manual = dict((ctx.drawing_settings or {}).get("manual") or {})
        for k, v in updates.items():
            if v is None:
                manual.pop(k, None)
            else:
                manual[k] = v
        try:
            ctx.drawing_settings = store.put_drawing_settings(ctx.org_id, ctx.drawing_key, {"manual": manual},
                                                              str(meta.get("owner_username") or ""))
        except Exception:
            _LOG.exception("general tolerance: could not save manual overrides")
    gt = meta.setdefault("general_tolerance", {})
    gt["banner"] = banner(ctx)
    gt["counts"] = counts


# ----------------------------------------------------------------------------- TVS extraction Excel

def apply_to_extraction_rows(rows: list[dict], feats: list, ctx: E.GTContext, overrides: dict | None = None) -> dict:
    """
    Fill Upper/Lower/Max/Min of open dimensions on TVS Extraction rows (columns unchanged) and append
    the source + flags to Notes. `feats` are the extractor Features in row order; `overrides` maps
    balloon → manual {tol_low, tol_high, text} from the inspection report.
    """
    from app.services.tvs_extraction_report import _cell_num

    counts = {"open": 0, "standard": 0, "rule": 0, "manual": 0, "unresolved": []}
    on = (ctx.org_settings or {}).get("enabled", True) and enabled_globally()
    manual_store = (ctx.drawing_settings or {}).get("manual") or {}
    for row, f in zip(rows, feats):
        od = OpenDim.from_dict(f.meta.get("gt_class"))
        if od is None:
            continue
        counts["open"] += 1
        bno = int(f.balloon or 0)
        ov = (overrides or {}).get(bno) or manual_store.get(str(bno))
        manual = None
        if ov and (ov.get("tol_low") or ov.get("tol_high")) and str(ov.get("tol_source", "Manual")) == "Manual" \
                and str(ov.get("text", f.text)).replace(" ", "") == f.text.replace(" ", ""):
            manual = {"upper": _f(ov.get("tol_high")), "lower": _f(ov.get("tol_low")), "unit": "mm"}
        if manual is None and not on:
            continue
        res = E.resolve(od, ctx, balloon=bno, manual=manual)
        p = res.primary
        if p.ok:
            row["Upper tolerance"] = _cell_num(p.upper)
            row["Lower tolerance"] = _cell_num(p.lower)
            nom = f.nominal if isinstance(f.nominal, (int, float)) else None
            if nom is not None and p.unit == "mm":
                row["Max limit"] = _cell_num(nom + (p.upper or 0.0))
                row["Min limit"] = _cell_num(nom + (p.lower or 0.0))
            row["_toleranced"] = True
            if p.source == "Manual":
                counts["manual"] += 1
            elif p.source.startswith("Rule:"):
                counts["rule"] += 1
            else:
                counts["standard"] += 1
        else:
            counts["unresolved"].append(bno)
        note = E.summary_note(res)
        row["Notes"] = "; ".join(x for x in (row.get("Notes"), note) if x)
        row["_tol_source"] = p.source
        row["_tol_auto"] = p.ok and p.source != "Manual"
    return counts


def summary_section(ctx: E.GTContext, counts: dict) -> tuple[str, list[str], list[list]]:
    """Rows for the TVS Summary sheet."""
    b = banner(ctx)
    rows = [
        ["General-tolerance standard used", "", b["text"].replace("General tolerance: ", "")],
        ["Process (TSDS0407)", "", b["process"] or "-"],
        ["Open dimensions found", counts["open"], ""],
        ["Auto-filled from the standard", counts["standard"], ""],
        ["Auto-filled by a rule", counts["rule"], ""],
        ["Manual tolerance (inspector)", counts.get("manual", 0), ""],
        ["Unresolved (blank, see Notes)", len(counts["unresolved"]), ", ".join(str(n) for n in sorted(counts["unresolved"])) or "none"],
    ]
    if b["conflict"]:
        rows.append(["Standard conflict", "", b["conflict"]])
    if b["geometric_class"]:
        rows.append(["Geometric tolerance class (TSDS0407)", "", f"class {b['geometric_class']} (not applied to sizes)"])
    return "General tolerances (open dimensions)", ["Item", "Count", "Details"], rows
