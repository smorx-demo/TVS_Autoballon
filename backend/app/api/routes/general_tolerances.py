"""
General tolerances for open dimensions: standards (read-only), organisation settings, teach-once
rules (CRUD, history, CSV/Excel import/export, preview) and per-job drawing settings.
"""

from __future__ import annotations

import csv
import io
from copy import deepcopy

import fitz
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.dependencies import require_admin, require_perm
from app.services.general_tolerance import apply as gt
from app.services.general_tolerance import engine as E
from app.services.general_tolerance import standards as S
from app.services.general_tolerance import store
from app.services.general_tolerance.classify import CATEGORIES, CATEGORY_LABELS
from app.services.general_tolerance.detect import PROCESSES

router = APIRouter(prefix="/api/general-tolerances", tags=["general-tolerances"])


def _org(user: dict) -> str:
    return str(user.get("org_id") or "default")


def _who(user: dict) -> str:
    return str(user.get("username") or user.get("id") or "")


# ----------------------------------------------------------------------------- standards / settings

@router.get("/standards")
def get_standards(user: dict = Depends(require_perm("read"))):
    return {"standards": S.standards_summary(), "processes": PROCESSES,
            "categories": [{"id": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES]}


class OrgSettingsIn(BaseModel):
    enabled: bool = True
    default_standard: str = ""
    default_iso_class: str = "m"
    default_process: str = "machining"
    preferred_standard: str = ""
    range_convention: str = "upper_inclusive"
    family_pattern: str = ""


@router.get("/settings")
def get_settings(user: dict = Depends(require_perm("read"))):
    return {"settings": store.get_org_settings(_org(user)), "can_edit": user.get("role") == "super_admin"}


@router.put("/settings")
def put_settings(body: OrgSettingsIn, user: dict = Depends(require_admin)):
    if body.default_standard not in ("", "ISO2768", "TSDS0407") or body.preferred_standard not in ("", "ISO2768", "TSDS0407"):
        raise HTTPException(422, "Standard must be ISO2768 or TSDS0407")
    if body.default_iso_class not in ("f", "m", "c", "v"):
        raise HTTPException(422, "ISO class must be f, m, c or v")
    if body.default_process not in PROCESSES:
        raise HTTPException(422, "Unknown process")
    if body.range_convention not in S.CONVENTIONS:
        raise HTTPException(422, "Unknown range convention")
    try:
        return {"settings": store.put_org_settings(_org(user), body.model_dump(), _who(user)), "can_edit": True}
    except Exception as e:  # bad family pattern
        raise HTTPException(422, f"Part-family pattern: {e}") from e


# ----------------------------------------------------------------------------- rules

class RuleIn(BaseModel):
    scope: str
    scope_key: str = ""
    process: str | None = None
    category: str
    hole_type: str | None = None
    size_min: float | None = None
    size_max: float | None = None
    unit: str = "mm"
    upper: float
    lower: float
    name: str = ""
    active: bool = True


class RulePatch(BaseModel):
    scope: str | None = None
    scope_key: str | None = None
    process: str | None = None
    category: str | None = None
    hole_type: str | None = None
    size_min: float | None = None
    size_max: float | None = None
    unit: str | None = None
    upper: float | None = None
    lower: float | None = None
    name: str | None = None
    active: bool | None = None


def _scope_key(body: dict, user: dict) -> str:
    if body.get("scope") == "customer":
        return _org(user)
    return str(body.get("scope_key") or "")


@router.get("/rules")
def list_rules(user: dict = Depends(require_perm("read"))):
    return {"rules": store.list_rules(_org(user)), "can_edit": True}


@router.post("/rules")
def create_rule(body: RuleIn, user: dict = Depends(require_perm("write"))):
    d = body.model_dump()
    d["scope_key"] = _scope_key(d, user)
    try:
        return store.create_rule(_org(user), d, _who(user))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _own_rule(rule_id: str, user: dict) -> dict:
    r = store.get_rule(rule_id)
    if r is None or r.get("org_id") != _org(user):
        raise HTTPException(404, "Rule not found")
    return r


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: str, body: RulePatch, user: dict = Depends(require_perm("write"))):
    _own_rule(rule_id, user)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("scope") == "customer":
        changes["scope_key"] = _org(user)
    try:
        return store.update_rule(rule_id, changes, _who(user))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, user: dict = Depends(require_perm("write"))):
    _own_rule(rule_id, user)
    store.delete_rule(rule_id, _who(user))
    return {"ok": True}


@router.get("/rules/{rule_id}/history")
def rule_history(rule_id: str, user: dict = Depends(require_perm("read"))):
    _own_rule(rule_id, user)
    return {"history": store.rule_history(rule_id)}


EXPORT_FIELDS = ["name", "scope", "scope_key", "process", "category", "hole_type", "size_min", "size_max", "unit",
                 "upper", "lower", "active"]


@router.get("/rules/export")
def export_rules(fmt: str = Query("csv"), user: dict = Depends(require_perm("read"))):
    rules = store.list_rules(_org(user))
    if fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Rules"
        ws.append(EXPORT_FIELDS)
        for r in rules:
            ws.append([r.get(k) for k in EXPORT_FIELDS])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="general_tolerance_rules.xlsx"'})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
    w.writeheader()
    for r in rules:
        w.writerow({k: r.get(k) for k in EXPORT_FIELDS})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="general_tolerance_rules.csv"'})


def _cell(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


@router.post("/rules/import")
async def import_rules(file: UploadFile = File(...), user: dict = Depends(require_perm("write"))):
    raw = await file.read()
    name = (file.filename or "").lower()
    rows: list[dict] = []
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(raw), data_only=True).active
        it = ws.iter_rows(values_only=True)
        hdr = [str(h or "").strip() for h in next(it)]
        rows = [dict(zip(hdr, r)) for r in it if any(c is not None for c in r)]
    else:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    created, errors = [], []
    for i, r in enumerate(rows, start=2):
        d = {k: _cell(r.get(k)) for k in EXPORT_FIELDS}
        d["active"] = str(d.get("active") or "true").lower() not in ("false", "0", "no")
        d["scope_key"] = _scope_key(d, user)
        try:
            for k in ("size_min", "size_max", "upper", "lower"):
                d[k] = None if d.get(k) is None else float(d[k])
            created.append(store.create_rule(_org(user), d, _who(user)))
        except (ValueError, TypeError) as e:
            errors.append(f"row {i}: {e}")
    return {"created": len(created), "errors": errors}


# ----------------------------------------------------------------------------- jobs

def _job(job_id: str):
    from app.api.routes.autoballoon import _job_dir, _load_meta

    return _load_meta(job_id), _job_dir(job_id)


def _save(job_id: str, meta: dict) -> None:
    from app.api.routes.autoballoon import _save_meta

    _save_meta(job_id, meta, sync_files=False)


def _suggest(row: dict, ctx: E.GTContext) -> dict | None:
    """Default 'teach' range for a row: the band of the active standard containing the value."""
    cat = row.get("tol_category")
    if not cat:
        return None
    try:
        size = float(row["tol_size"]) if row.get("tol_size") is not None else float(str(row.get("nominal") or "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    std, _ = ctx.stated()
    sid = std.id if std else (ctx.org_settings or {}).get("default_standard") or ""
    band = None
    if sid == "ISO2768":
        table = {"radius": "radius_chamfer", "chamfer": "radius_chamfer", "angular": "angular"}.get(cat, "linear")
        band = S.band_limits("ISO2768", table, size)
    elif sid == "TSDS0407":
        proc = ctx.process_for(int(row.get("balloon") or 0))[0]
        table = {"machining": "machining", "casting_hpdc": "casting", "casting_shell": "casting", "casting_sand": "casting",
                 "pressing": "pressing", "forging": "forging", "welding": "welding", "plastic": "plastic",
                 "rubber_moulding": "rubber", "rubber_extrusion": "rubber", "lost_wax": "lost_wax"}.get(proc, "machining")
        if cat == "angular":
            table = "angular_machining"
        band = S.band_limits("TSDS0407", table, size, ctx.convention)
    rule_cat = "chamfer_size" if cat == "chamfer" else ("inside_dia" if cat == "dia" else cat)
    return {"category": rule_cat, "category_label": CATEGORY_LABELS.get(rule_cat, rule_cat), "size": size,
            "size_min": band[0] if band else None, "size_max": band[1] if band else None,
            "band": band[2] if band else ""}


def _job_payload(meta: dict, jd) -> dict:
    ctx = gt.context_for_job(meta, jd)
    rows = []
    for r in meta.get("report_rows") or []:
        if r.get("tol_source") or r.get("tol_category"):
            rows.append({"balloon": r.get("balloon"), "page": r.get("page"), "nominal": r.get("nominal"),
                         "tol_low": r.get("tol_low"), "tol_high": r.get("tol_high"), "tol_source": r.get("tol_source"),
                         "tol_flags": r.get("tol_flags") or [], "tol_auto": r.get("tol_auto"),
                         "tol_angle": r.get("tol_angle", ""), "tol_category": r.get("tol_category", ""),
                         "suggest": _suggest(r, ctx)})
    org = ctx.org_settings or {}
    return {"banner": gt.banner(ctx), "rows": rows, "drawing_settings": ctx.drawing_settings,
            "counts": (meta.get("general_tolerance") or {}).get("counts") or {},
            "scopes": {"drawing": ctx.drawing_key, "family": ctx.family_key or None, "customer": ctx.org_id},
            "family_enabled": bool(org.get("family_pattern") and ctx.family_key),
            "processes": PROCESSES}


@router.get("/jobs/{job_id}")
def job_state(job_id: str, user: dict = Depends(require_perm("read"))):
    meta, jd = _job(job_id)
    if meta.get("report_rows") and not (meta.get("general_tolerance") or {}).get("banner"):
        _save(job_id, meta)
    return _job_payload(meta, jd)


class DrawingSettingsIn(BaseModel):
    standard: dict | None = None
    process: str | None = None
    sheet_thickness: float | None = None
    enabled: bool | None = None
    clear_standard: bool = False
    clear_process: bool = False


@router.put("/jobs/{job_id}/drawing")
def put_drawing(job_id: str, body: DrawingSettingsIn, user: dict = Depends(require_perm("write"))):
    meta, jd = _job(job_id)
    key = gt.drawing_key_for_meta(meta)
    org = gt.org_for_meta(meta)
    upd: dict = {}
    if body.standard is not None:
        if body.standard.get("id") not in ("ISO2768", "TSDS0407"):
            raise HTTPException(422, "Standard must be ISO2768 or TSDS0407")
        upd["standard"] = {"id": body.standard["id"], "cls": body.standard.get("cls")}
    if body.clear_standard:
        upd["standard"] = None
    if body.process is not None:
        if body.process not in PROCESSES:
            raise HTTPException(422, "Unknown process")
        upd["process"] = body.process
    if body.clear_process:
        upd["process"] = None
    if body.sheet_thickness is not None:
        upd["sheet_thickness"] = body.sheet_thickness
    if body.enabled is not None:
        upd["enabled"] = body.enabled
    store.put_drawing_settings(org, key, upd, _who(user))
    _save(job_id, meta)
    return _job_payload(meta, jd)


class FeatureProcessIn(BaseModel):
    process: str | None = None          # None clears the override
    balloons: list[int] = Field(default_factory=list)
    area_page: int | None = None
    area_x: float | None = None
    area_y: float | None = None
    area_w: float | None = None
    area_h: float | None = None


@router.post("/jobs/{job_id}/feature-process")
def feature_process(job_id: str, body: FeatureProcessIn, user: dict = Depends(require_perm("write"))):
    """Per-feature process override (e.g. a machined bore on a die-cast part); balloons or an area."""
    if body.process is not None and body.process not in PROCESSES:
        raise HTTPException(422, "Unknown process")
    meta, jd = _job(job_id)
    nos = set(body.balloons)
    if body.area_page and body.area_w and body.area_h:
        r = fitz.Rect(body.area_x or 0, body.area_y or 0, (body.area_x or 0) + body.area_w, (body.area_y or 0) + body.area_h)
        for b in meta.get("balloons") or []:
            if int(b.get("page") or 1) != body.area_page:
                continue
            c = fitz.Point(float(b.get("x") or 0) + float(b.get("width") or 0) / 2,
                           float(b.get("y") or 0) + float(b.get("height") or 0) / 2)
            if r.contains(c):
                nos.add(int(b.get("balloon") or 0))
    key, org = gt.drawing_key_for_meta(meta), gt.org_for_meta(meta)
    fp = dict(store.get_drawing_settings(org, key).get("feature_process") or {})
    for n in nos:
        if body.process:
            fp[str(n)] = body.process
        else:
            fp.pop(str(n), None)
    store.put_drawing_settings(org, key, {"feature_process": fp}, _who(user))
    _save(job_id, meta)
    out = _job_payload(meta, jd)
    out["updated_balloons"] = sorted(nos)
    return out


class TeachIn(BaseModel):
    balloon: int
    page: int = 1
    scope: str                          # dimension | drawing | family | customer
    upper: float
    lower: float
    unit: str = "mm"
    category: str | None = None
    size_min: float | None = None
    size_max: float | None = None
    process: str | None = None
    name: str = ""


def _changes(before: list[dict], after: list[dict]) -> list[dict]:
    out = []
    old = {(r.get("page"), r.get("balloon")): r for r in before}
    for r in after:
        o = old.get((r.get("page"), r.get("balloon"))) or {}
        if (o.get("tol_low"), o.get("tol_high"), o.get("tol_source")) != (r.get("tol_low"), r.get("tol_high"), r.get("tol_source")):
            out.append({"balloon": r.get("balloon"), "page": r.get("page"), "nominal": r.get("nominal"),
                        "before": {"tol_low": o.get("tol_low", ""), "tol_high": o.get("tol_high", ""), "source": o.get("tol_source", "")},
                        "after": {"tol_low": r.get("tol_low", ""), "tol_high": r.get("tol_high", ""), "source": r.get("tol_source", "")}})
    return out


@router.post("/jobs/{job_id}/teach")
def teach(job_id: str, body: TeachIn, user: dict = Depends(require_perm("write"))):
    """Set one open dimension's tolerance and (optionally) save it as a rule applied to all matching ones."""
    meta, jd = _job(job_id)
    rows = meta.get("report_rows") or []
    row = next((r for r in rows if int(r.get("balloon") or 0) == body.balloon and int(r.get("page") or 1) == body.page), None)
    if row is None:
        raise HTTPException(404, "Report row not found")
    before = deepcopy(rows)
    rule = None
    if body.scope == "dimension":
        row["tol_low"], row["tol_high"] = gt._s(body.lower), gt._s(body.upper)
        row["tol_source"] = "Manual"
    else:
        if body.scope not in ("drawing", "family", "customer"):
            raise HTTPException(422, "scope must be dimension, drawing, family or customer")
        ctx = gt.context_for_job(meta, jd)
        key = {"drawing": ctx.drawing_key, "family": ctx.family_key, "customer": ctx.org_id}[body.scope]
        if not key:
            raise HTTPException(422, "No part family for this drawing (configure the part-family pattern)")
        cat = body.category or ("chamfer_size" if row.get("tol_category") == "chamfer" else row.get("tol_category"))
        if not cat:
            raise HTTPException(422, "Not an open dimension")
        try:
            rule = store.create_rule(gt.org_for_meta(meta), {
                "scope": body.scope, "scope_key": key, "process": body.process, "category": cat,
                "size_min": body.size_min, "size_max": body.size_max, "unit": body.unit,
                "upper": body.upper, "lower": body.lower, "name": body.name}, _who(user))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if row.get("tol_source") == "Manual":
            row["tol_source"] = "Reset"     # the rule now governs this balloon too
    _save(job_id, meta)
    out = _job_payload(meta, jd)
    out["rule"] = rule
    out["changed"] = _changes(before, meta.get("report_rows") or [])
    return out


class PreviewIn(BaseModel):
    rule: RuleIn | None = None
    settings: OrgSettingsIn | None = None
    drawing: DrawingSettingsIn | None = None


@router.post("/jobs/{job_id}/preview")
def preview(job_id: str, body: PreviewIn, user: dict = Depends(require_perm("read"))):
    """Which open dimensions on this drawing a rule / setting would change (nothing is saved)."""
    meta, jd = _job(job_id)
    rows = deepcopy(meta.get("report_rows") or [])
    ctx = gt.context_for_job(meta, jd)
    if body.rule is not None:
        d = body.rule.model_dump()
        d["scope_key"] = {"drawing": ctx.drawing_key, "family": ctx.family_key, "customer": ctx.org_id}.get(d["scope"]) or d["scope_key"]
        errs = store.validate_rule(d)
        if errs:
            raise HTTPException(422, "; ".join(errs))
        d.update({"id": "preview", "active": True, "name": d.get("name") or store.default_rule_name(d)})
        ctx.rules = ctx.rules + [d]
    if body.settings is not None:
        ctx.org_settings = {**(ctx.org_settings or {}), **body.settings.model_dump()}
    if body.drawing is not None:
        ds = dict(ctx.drawing_settings or {})
        dd = body.drawing.model_dump(exclude_unset=True)
        for k in ("standard", "process", "sheet_thickness", "enabled"):
            if dd.get(k) is not None:
                ds[k] = dd[k]
        ctx.drawing_settings = ds
    after = deepcopy(rows)
    gt.apply_rows(after, meta.get("balloons") or [], ctx, (meta.get("general_tolerance") or {}).get("features") or [])
    return {"changes": _changes(rows, after)}
