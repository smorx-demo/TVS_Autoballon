"""
Storage for general-tolerance rules, organisation settings and per-drawing settings.

  • rules           → Postgres general_tolerance_rules (+ general_tolerance_rule_audit), else
                      backend/data/general_tolerance_rules.json
  • org settings    → app_settings key 'gt_settings:<org>'   (same store as the instrument rules)
  • drawing settings→ app_settings key 'gt_drawing:<org>:<drawing key>' — the process / standard the
                      user chose, sheet thickness, per-feature process overrides and manual overrides
                      by balloon number. Keyed by the drawing (part number), so they carry over to
                      every revision / re-upload of that drawing.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core import database as dbmod

ROOT = Path(__file__).resolve().parents[3]  # backend/
JSON_PATH = ROOT / "data" / "general_tolerance_rules.json"
SCOPES = ("drawing", "family", "customer")
UNITS = ("mm", "deg", "%")
RULE_FIELDS = ("id", "org_id", "scope", "scope_key", "process", "category", "hole_type", "size_min", "size_max",
               "unit", "upper", "lower", "name", "created_by", "created_at", "updated_at", "active")

DEFAULT_ORG_SETTINGS = {
    "enabled": True,
    "default_standard": "",          # '' | ISO2768 | TSDS0407 — used when a drawing states none
    "default_iso_class": "m",
    "default_process": "machining",  # TSDS0407 process when the drawing does not state one
    "preferred_standard": "",        # on a conflict the title block cannot settle
    "range_convention": "upper_inclusive",
    "family_pattern": "",            # regex on the part number; group 1 (or the match) = family key
}

_lock = threading.Lock()
_tables_ready = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- settings (app_settings)

def _settings_read(key: str) -> dict | None:
    from app.api.routes.app_settings import _read

    return _read(key)


def _settings_write(key: str, value: dict, user: str) -> dict:
    from app.api.routes.app_settings import _write

    return _write(key, value, user)


def get_org_settings(org_id: str) -> dict:
    saved = (_settings_read(f"gt_settings:{org_id or 'default'}") or {}).get("value") or {}
    return {**DEFAULT_ORG_SETTINGS, **saved}


def put_org_settings(org_id: str, value: dict, user: str) -> dict:
    clean = {k: value.get(k, DEFAULT_ORG_SETTINGS[k]) for k in DEFAULT_ORG_SETTINGS}
    if clean["family_pattern"]:
        re.compile(clean["family_pattern"])  # raises on a bad pattern
    _settings_write(f"gt_settings:{org_id or 'default'}", clean, user)
    return get_org_settings(org_id)


DEFAULT_DRAWING_SETTINGS = {
    "standard": None,          # {"id": "ISO2768", "cls": "m"} | {"id": "TSDS0407"} — user choice for this drawing
    "process": None,           # user-chosen TSDS0407 process for this drawing
    "sheet_thickness": None,
    "feature_process": {},     # balloon -> process override
    "manual": {},              # balloon -> {"text", "tol_low", "tol_high", "by", "at"}
    "enabled": True,
}


def get_drawing_settings(org_id: str, drawing_key: str) -> dict:
    saved = (_settings_read(f"gt_drawing:{org_id or 'default'}:{drawing_key}") or {}).get("value") or {}
    out = json.loads(json.dumps(DEFAULT_DRAWING_SETTINGS))
    out.update(saved)
    return out


def put_drawing_settings(org_id: str, drawing_key: str, value: dict, user: str) -> dict:
    cur = get_drawing_settings(org_id, drawing_key)
    for k in DEFAULT_DRAWING_SETTINGS:
        if k in value:
            cur[k] = value[k]
    _settings_write(f"gt_drawing:{org_id or 'default'}:{drawing_key}", cur, user)
    return cur


def family_key(part_number: str, pattern: str) -> str:
    if not pattern or not part_number:
        return ""
    try:
        m = re.search(pattern, part_number)
    except re.error:
        return ""
    if not m:
        return ""
    return (m.group(1) if m.groups() else m.group(0)) or ""


# ----------------------------------------------------------------------------- rules

def _use_pg() -> bool:
    global _tables_ready
    if not dbmod.USE_POSTGRES or dbmod.SessionLocal is None or dbmod.engine is None:
        return False
    if not _tables_ready:
        from app.models.general_tolerance import GeneralToleranceRule, GeneralToleranceRuleAudit

        dbmod.Base.metadata.create_all(bind=dbmod.engine,
                                       tables=[GeneralToleranceRule.__table__, GeneralToleranceRuleAudit.__table__])
        _tables_ready = True
    return True


def _json_load() -> dict:
    if JSON_PATH.exists():
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    return {"rules": [], "audit": []}


def _json_save(db: dict) -> None:
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JSON_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2), encoding="utf-8")
    tmp.replace(JSON_PATH)


def _row_dict(r) -> dict:
    d = {k: getattr(r, k) for k in RULE_FIELDS}
    for k in ("created_at", "updated_at"):
        if isinstance(d[k], datetime):
            d[k] = d[k].isoformat(timespec="seconds")
    return d


def validate_rule(d: dict) -> list[str]:
    from app.services.general_tolerance.classify import CATEGORIES

    errs = []
    if d.get("scope") not in SCOPES:
        errs.append(f"scope must be one of {', '.join(SCOPES)}")
    if not str(d.get("scope_key") or "").strip():
        errs.append("scope_key is required")
    if d.get("category") not in CATEGORIES:
        errs.append(f"category must be one of {', '.join(CATEGORIES)}")
    if d.get("unit", "mm") not in UNITS:
        errs.append(f"unit must be one of {', '.join(UNITS)}")
    try:
        up, lo = float(d.get("upper")), float(d.get("lower"))
        if up < lo:
            errs.append("upper must be ≥ lower")
    except (TypeError, ValueError):
        errs.append("upper and lower must be numbers")
    smin, smax = d.get("size_min"), d.get("size_max")
    if smin is not None and smax is not None and float(smax) <= float(smin):
        errs.append("size_max must be > size_min")
    if d.get("hole_type") not in (None, "", "bored", "drilled"):
        errs.append("hole_type must be bored or drilled")
    return errs


def _normalise(d: dict) -> dict:
    out = {k: d.get(k) for k in RULE_FIELDS if k in d}
    for k in ("size_min", "size_max"):
        out[k] = None if out.get(k) in (None, "") else float(out[k])
    out["upper"], out["lower"] = float(d["upper"]), float(d["lower"])
    out["unit"] = d.get("unit") or "mm"
    out["process"] = d.get("process") or None
    out["hole_type"] = d.get("hole_type") or None
    out["active"] = bool(d.get("active", True))
    out["name"] = str(d.get("name") or "").strip()
    return out


def _audit(rule_id: str, action: str, user: str, before: dict | None, after: dict | None) -> None:
    rec = {"id": secrets.token_hex(8), "rule_id": rule_id, "action": action, "changed_by": user, "changed_at": _now(),
           "before": before, "after": after}
    if _use_pg():
        from app.models.general_tolerance import GeneralToleranceRuleAudit

        with dbmod.SessionLocal() as db:
            db.add(GeneralToleranceRuleAudit(id=rec["id"], rule_id=rule_id, action=action, changed_by=user,
                                             before_json=json.dumps(before), after_json=json.dumps(after)))
            db.commit()
        return
    db = _json_load()
    db.setdefault("audit", []).append(rec)
    _json_save(db)


def list_rules(org_id: str, *, active_only: bool = False) -> list[dict]:
    if _use_pg():
        from app.models.general_tolerance import GeneralToleranceRule

        with dbmod.SessionLocal() as db:
            q = db.query(GeneralToleranceRule).filter(GeneralToleranceRule.org_id == (org_id or "default"))
            if active_only:
                q = q.filter(GeneralToleranceRule.active.is_(True))
            return [_row_dict(r) for r in q.order_by(GeneralToleranceRule.created_at).all()]
    rules = [r for r in _json_load().get("rules", []) if r.get("org_id") == (org_id or "default")]
    return [r for r in rules if r.get("active", True)] if active_only else rules


def get_rule(rule_id: str) -> dict | None:
    if _use_pg():
        from app.models.general_tolerance import GeneralToleranceRule

        with dbmod.SessionLocal() as db:
            r = db.get(GeneralToleranceRule, rule_id)
            return _row_dict(r) if r else None
    return next((r for r in _json_load().get("rules", []) if r["id"] == rule_id), None)


def create_rule(org_id: str, data: dict, user: str) -> dict:
    d = {**data, "org_id": org_id or "default"}
    errs = validate_rule(d)
    if errs:
        raise ValueError("; ".join(errs))
    rec = _normalise(d)
    rec.update({"id": secrets.token_hex(8), "org_id": org_id or "default", "created_by": user,
                "created_at": _now(), "updated_at": _now()})
    if not rec["name"]:
        rec["name"] = default_rule_name(rec)
    with _lock:
        if _use_pg():
            from app.models.general_tolerance import GeneralToleranceRule

            with dbmod.SessionLocal() as db:
                row = GeneralToleranceRule(**{k: v for k, v in rec.items() if k not in ("created_at", "updated_at")})
                db.add(row)
                db.commit()
                rec = _row_dict(row)
        else:
            db = _json_load()
            db.setdefault("rules", []).append(rec)
            _json_save(db)
        _audit(rec["id"], "create", user, None, rec)
    return rec


def update_rule(rule_id: str, changes: dict, user: str) -> dict:
    before = get_rule(rule_id)
    if before is None:
        raise KeyError(rule_id)
    merged = {**before, **{k: v for k, v in changes.items() if k in RULE_FIELDS and k not in ("id", "org_id", "created_by", "created_at")}}
    errs = validate_rule(merged)
    if errs:
        raise ValueError("; ".join(errs))
    after = {**before, **_normalise(merged), "id": rule_id, "org_id": before["org_id"], "created_by": before["created_by"],
             "created_at": before["created_at"], "updated_at": _now()}
    with _lock:
        if _use_pg():
            from app.models.general_tolerance import GeneralToleranceRule

            with dbmod.SessionLocal() as db:
                row = db.get(GeneralToleranceRule, rule_id)
                for k, v in after.items():
                    if k not in ("id", "created_at", "updated_at"):
                        setattr(row, k, v)
                db.commit()
                after = _row_dict(row)
        else:
            db = _json_load()
            db["rules"] = [after if r["id"] == rule_id else r for r in db.get("rules", [])]
            _json_save(db)
        action = "update"
        if before.get("active", True) != after.get("active", True):
            action = "enable" if after.get("active") else "disable"
        _audit(rule_id, action, user, before, after)
    return after


def delete_rule(rule_id: str, user: str) -> None:
    before = get_rule(rule_id)
    if before is None:
        raise KeyError(rule_id)
    with _lock:
        if _use_pg():
            from app.models.general_tolerance import GeneralToleranceRule

            with dbmod.SessionLocal() as db:
                row = db.get(GeneralToleranceRule, rule_id)
                db.delete(row)
                db.commit()
        else:
            db = _json_load()
            db["rules"] = [r for r in db.get("rules", []) if r["id"] != rule_id]
            _json_save(db)
        _audit(rule_id, "delete", user, before, None)


def rule_history(rule_id: str) -> list[dict]:
    if _use_pg():
        from app.models.general_tolerance import GeneralToleranceRuleAudit

        with dbmod.SessionLocal() as db:
            rows = (db.query(GeneralToleranceRuleAudit).filter(GeneralToleranceRuleAudit.rule_id == rule_id)
                    .order_by(GeneralToleranceRuleAudit.changed_at).all())
            return [{"id": r.id, "rule_id": r.rule_id, "action": r.action, "changed_by": r.changed_by,
                     "changed_at": r.changed_at.isoformat(timespec="seconds") if r.changed_at else "",
                     "before": json.loads(r.before_json or "null"), "after": json.loads(r.after_json or "null")}
                    for r in rows]
    return [a for a in _json_load().get("audit", []) if a["rule_id"] == rule_id]


def default_rule_name(r: dict) -> str:
    from app.services.general_tolerance.classify import CATEGORY_LABELS
    from app.services.general_tolerance.standards import fmt_deg

    rng = ""
    if r.get("size_min") is not None or r.get("size_max") is not None:
        lo = "" if r.get("size_min") is None else f">{r['size_min']:g}"
        hi = "" if r.get("size_max") is None else f"≤{r['size_max']:g}"
        rng = " " + (f"{lo}–{hi.lstrip('≤')}" if lo and hi else (lo or hi))
    up, lo = float(r["upper"]), float(r["lower"])
    if r.get("unit") == "deg":
        tol = f"±{fmt_deg(up)}" if abs(up + lo) < 1e-9 else f"+{fmt_deg(up)}/-{fmt_deg(lo)}"
    else:
        suf = "%" if r.get("unit") == "%" else ""
        tol = f"±{up:g}{suf}" if abs(up + lo) < 1e-9 else f"{up:+g}{suf}/{lo:+g}{suf}"
    return f"{CATEGORY_LABELS.get(r['category'], r['category'])}{rng} {tol}".strip()
