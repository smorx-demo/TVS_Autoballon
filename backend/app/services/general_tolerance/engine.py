"""
General-tolerance resolution for one open dimension.

PRECEDENCE (highest first) — implemented only in `resolve()`:

  1. Tolerance printed on the dimension          → never touched (the dimension is not "open")
  2. Manual override on that balloon             → "Manual"
  3. Rule scoped to this drawing                 → "Rule: <name> (drawing)"
     (the process / standard the user chose for the drawing applies at level 5)
  4. General-tolerance table printed on the drawing → "General tol – drawing table"
  5. Standard stated on the drawing (TSDS0407 + process, or ISO 2768 / IS 2102 class)
                                                 → "General tol – ISO 2768-m" / "General tol – TSDS0407 machining / outside Ø"
  6. Part-family rule                            → "Rule: <name> (part family)"
  7. Customer/organisation rule, then the organisation's default standard
                                                 → "Rule: <name> (customer)" / "Org default – TSDS0407 machining / …"
  8. None                                        → blank + "None – <reason>"

When the drawing states a standard or table (4/5), levels 6–7 never apply: a matching family or
customer rule is reported as a "standard conflict" warning instead of silently overriding the
drawing. A level that finds its table but no value for the size (out of range, '—', 'NA') ends
the search with a blank and that reason — values are never extrapolated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.services.general_tolerance import standards as S
from app.services.general_tolerance.classify import CATEGORY_LABELS, OpenDim
from app.services.general_tolerance.detect import PROCESSES, DrawingGT, StatedStandard

TSDS = "TSDS0407"
ISO = "ISO2768"


@dataclass
class Part:
    """Tolerance of one looked-up quantity (a size, or a chamfer's angle)."""

    upper: float | None = None
    lower: float | None = None
    unit: str = "mm"
    source: str = ""
    level: int = 8
    flags: list[str] = field(default_factory=list)
    table: str = ""
    column: str = ""
    band: str = ""
    cell: str = ""

    @property
    def ok(self) -> bool:
        return self.upper is not None or self.lower is not None


@dataclass
class GTResult:
    category: str
    size: float
    primary: Part
    angle: Part | None = None          # chamfer angle (chamfers only)
    angle_value: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def flags(self) -> list[str]:
        out = list(self.primary.flags)
        if self.angle:
            out += [f for f in self.angle.flags if f not in out]
        return out


@dataclass
class GTContext:
    drawing: DrawingGT = field(default_factory=DrawingGT)
    org_settings: dict = field(default_factory=dict)
    drawing_settings: dict = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)       # active rules of the organisation
    drawing_key: str = ""
    family_key: str = ""
    org_id: str = "default"

    @property
    def convention(self) -> str:
        c = (self.org_settings or {}).get("range_convention") or "upper_inclusive"
        return c if c in S.CONVENTIONS else "upper_inclusive"

    def stated(self) -> tuple[StatedStandard | None, list[str]]:
        """Standard that governs this drawing (user choice > title block > notes) and flags."""
        flags: list[str] = []
        user = (self.drawing_settings or {}).get("standard")
        if user and user.get("id"):
            return StatedStandard(user["id"], user.get("cls"), user.get("geo"), "", "set by user", ""), flags
        d = self.drawing
        if d.chosen is not None:
            if d.conflict:
                flags.append("standard conflict: " + d.conflict)
            return d.chosen, flags
        if d.standards:  # conflict the title block could not settle
            pref = (self.org_settings or {}).get("preferred_standard") or ""
            pick = next((s for s in d.standards if s.id == pref), None)
            flags.append("standard conflict: " + (d.conflict or "several standards stated")
                         + (f"; using the organisation's preferred {pick.label()}" if pick else "; no preferred standard set"))
            return pick, flags
        return None, flags

    def process_for(self, balloon: int | None) -> tuple[str | None, list[str]]:
        """TSDS0407 process: per-feature override > user's drawing choice > detected > org default (assumed)."""
        ds = self.drawing_settings or {}
        fp = (ds.get("feature_process") or {}).get(str(balloon)) if balloon is not None else None
        if fp:
            return fp, []
        if ds.get("process"):
            return ds["process"], []
        if self.drawing.process:
            return self.drawing.process, []
        dp = (self.org_settings or {}).get("default_process") or "machining"
        return dp, [f"process assumed: {PROCESSES.get(dp, dp)}"]

    @property
    def sheet_thickness(self) -> float | None:
        t = (self.drawing_settings or {}).get("sheet_thickness")
        return float(t) if t not in (None, "") else self.drawing.sheet_thickness


# ----------------------------------------------------------------------------- standard lookups

def _part(lk: S.Lookup, source: str, level: int, flags: list[str]) -> Part:
    if not lk.ok:
        return Part(source=f"None – {lk.reason}", level=8, flags=flags + [lk.reason], table=lk.table, column=lk.column, band=lk.band)
    return Part(lk.upper, lk.lower, lk.unit, source, level, flags, lk.table, lk.column, lk.band, lk.cell)


def _leg(od: OpenDim, first_band_max: float) -> tuple[float, list[str]]:
    if od.leg is not None:
        return od.leg, []
    return first_band_max, ["leg length estimated"]


def _iso(cls: str | None, label: str, quantity: str, od: OpenDim, level: int, prefix: str) -> Part:
    if not cls:
        return Part(source=f"None – {label}: tolerance class not stated", flags=[f"{label}: tolerance class not stated"])
    src = f"{prefix} – {label}" if label.startswith(("ISO", "IS ")) else f"{prefix} – {label}"
    if quantity == "angle":
        leg, fl = _leg(od, 10.0)
        return _part(S.lookup(ISO, "angular", cls, leg), src, level, fl)
    if od.category in ("radius", "chamfer"):
        return _part(S.lookup(ISO, "radius_chamfer", cls, od.size), src, level, [])
    if od.category == "angular":
        leg, fl = _leg(od, 10.0)
        return _part(S.lookup(ISO, "angular", cls, leg), src, level, fl)
    return _part(S.lookup(ISO, "linear", cls, od.size), src, level, [])


# (process, category) -> (table, column)
_TSDS_SIZE = {
    "machining": {"linear": ("machining", "length"), "thickness": ("machining", "length"),
                  "outside_dia": ("machining", "outside_dia"), "radius": ("machining", "corner_round"),
                  "chamfer": ("machining", "corner_chamfer")},
    "casting_hpdc": {"linear": ("casting", "hpdc_length"), "thickness": ("casting", "hpdc_length"),
                     "outside_dia": ("casting", "hpdc_od"), "inside_dia": ("casting", "hpdc_id")},
    "casting_shell": {c: ("casting", "shell_casting") for c in ("linear", "thickness", "outside_dia", "inside_dia")},
    "casting_sand": {c: ("casting", "sand_casting") for c in ("linear", "thickness", "outside_dia", "inside_dia")},
    "pressing": {"linear": ("pressing", "length"), "thickness": ("pressing", "length")},
    "forging": {"linear": ("forging", "length"), "thickness": ("forging", "thickness")},
    "welding": {"linear": ("welding", "length"), "thickness": ("welding", "length")},
    "plastic": {"linear": ("plastic", "length"), "thickness": ("plastic", "thickness"),
                "outside_dia": ("plastic", "od"), "inside_dia": ("plastic", "id")},
    "rubber_moulding": {c: ("rubber", "moulding") for c in ("linear", "thickness", "outside_dia", "inside_dia")},
    "rubber_extrusion": {c: ("rubber", "extrusion_and_others") for c in ("linear", "thickness", "outside_dia", "inside_dia")},
    "lost_wax": {**{c: ("lost_wax", "dia_lg") for c in ("linear", "thickness", "outside_dia", "inside_dia")},
                 "radius": ("lost_wax", "rounding")},
}


def _tsds(process: str, quantity: str, od: OpenDim, level: int, prefix: str, ctx: GTContext, pflags: list[str]) -> Part:
    pname = PROCESSES.get(process, process).replace("Casting – ", "casting ").replace("Rubber – ", "rubber ").lower()
    pname = {"casting hpdc": "HPDC casting"}.get(pname, pname)
    conv = ctx.convention
    flags = list(pflags)
    cat = od.category

    def src(column_label: str) -> str:
        return f"{prefix} – TSDS0407 {pname} / {column_label}"

    if quantity == "angle" or cat == "angular":
        if process == "machining":
            leg, fl = _leg(od, 4.0)
            return _part(S.lookup(TSDS, "angular_machining", "degrees", leg, convention=conv), src("angular"), level, flags + fl)
        if process == "lost_wax":
            return _part(S.lookup(TSDS, "lost_wax_angular_roughness", "angles", None), src("angles"), level, flags)
        reason = f"TSDS0407: angles not specified for {pname}"
        return Part(source=f"None – {reason}", flags=flags + [reason])
    if cat == "dia":
        reason = "TSDS0407: Ø inside/outside not determined (needed for the column)"
        return Part(source=f"None – {reason}", flags=flags + [reason])
    if process == "machining" and cat == "inside_dia":
        ht = od.hole_type
        if ht not in ("bored", "drilled"):
            ht = "bored"
            flags.append("hole type assumed: boring")
        col = "inside_dia_drilling" if ht == "drilled" else "inside_dia_boring"
        label = "inside Ø drilling" if ht == "drilled" else "inside Ø boring"
        return _part(S.lookup(TSDS, "machining", col, od.size, convention=conv), src(label), level, flags)
    if process == "pressing" and cat == "inside_dia":
        t = ctx.sheet_thickness
        if t is None:
            reason = "TSDS0407 pressing: sheet thickness unknown (set it for this drawing)"
            return Part(source=f"None – {reason}", flags=flags + [reason])
        col = "inside_dia_t_le_2_3" if t <= 2.3 else "inside_dia_t_gt_2_3"
        return _part(S.lookup(TSDS, "pressing", col, od.size, convention=conv),
                     src(f"inside Ø t{'≤' if t <= 2.3 else '>'}2.3"), level, flags)
    hit = _TSDS_SIZE.get(process, {}).get(cat)
    if hit is None:
        reason = f"TSDS0407: not specified for {CATEGORY_LABELS.get(cat, cat)} under {pname}"
        return Part(source=f"None – {reason}", flags=flags + [reason])
    table, col = hit
    label = S.load(TSDS)["tables"][table]["columns"][col]["label"]
    col_label = {"length": "length", "outside_dia": "outside Ø", "corner_round": "corner round",
                 "corner_chamfer": "corner chamfer", "thickness": "thickness"}.get(col, label.lower())
    return _part(S.lookup(TSDS, table, col, od.size, convention=conv, nominal=od.size), src(col_label), level, flags)


def _apply_standard(std: StatedStandard, quantity: str, od: OpenDim, level: int, prefix: str,
                    ctx: GTContext, balloon: int | None) -> Part:
    if std.id == ISO:
        label = f"{std.via or 'ISO 2768'}-{std.cls}" if std.cls else (std.via or "ISO 2768")
        if std.via:
            label += f" (= ISO 2768-{std.cls})" if std.cls else ""
        return _iso(std.cls, label, quantity, od, level, prefix)
    if std.id == TSDS:
        proc, pflags = ctx.process_for(balloon)
        return _tsds(proc, quantity, od, level, prefix, ctx, pflags)
    return Part(source=f"None – unknown standard {std.id}", flags=[f"unknown standard {std.id}"])


def _apply_table(rows: list[dict], quantity: str, od: OpenDim) -> Part | None:
    if quantity == "angle" or od.category == "angular":
        ang = next((r for r in rows if r["kind"] == "angle"), None)
        if ang is None:
            return None
        return Part(ang["pm"], -ang["pm"], "deg", "General tol – drawing table", 4, [], "drawing table", "angles", "", ang["text"])
    for r in rows:
        if r["kind"] != "size":
            continue
        lo, hi = r["min"], r["max"]
        if (lo is None or od.size > lo or (lo == 0 and od.size >= 0)) and (hi is None or od.size <= hi):
            return Part(r["pm"], -r["pm"], "mm", "General tol – drawing table", 4, [], "drawing table", "", r["text"], f"±{r['pm']:g}")
    reason = f"drawing table: {od.size:g} mm outside its rows"
    return Part(source=f"None – {reason}", flags=[reason])


# ----------------------------------------------------------------------------- rules

def _rule_category(od: OpenDim, quantity: str) -> str:
    if od.category == "chamfer":
        return "chamfer_angle" if quantity == "angle" else "chamfer_size"
    return od.category


def _rule_size(od: OpenDim, quantity: str) -> float | None:
    if quantity == "angle" or od.category == "angular":
        return od.leg
    return od.size


def matching_rule(rules: list[dict], scope: str, key: str, od: OpenDim, quantity: str, process: str | None) -> dict | None:
    cat = _rule_category(od, quantity)
    size = _rule_size(od, quantity)
    best = None
    for r in rules:
        if not r.get("active", True) or r.get("scope") != scope or (r.get("scope_key") or "") != key:
            continue
        if r.get("category") != cat:
            continue
        if r.get("process") and r["process"] != process:
            continue
        if r.get("hole_type") and r["hole_type"] != od.hole_type:
            continue
        lo, hi = r.get("size_min"), r.get("size_max")
        if (lo is not None or hi is not None) and size is None:
            continue
        if lo is not None and not size > lo:
            continue
        if hi is not None and not size <= hi:
            continue
        width = (hi if hi is not None else 1e9) - (lo if lo is not None else 0)
        rank = (bool(r.get("process")), bool(r.get("hole_type")), -width, str(r.get("updated_at") or ""))
        if best is None or rank > best[0]:
            best = (rank, r)
    return best[1] if best else None


def _rule_part(r: dict, od: OpenDim, scope_label: str, level: int) -> Part:
    up, lo, unit = float(r["upper"]), float(r["lower"]), r.get("unit") or "mm"
    if unit == "%":
        up, lo, unit = round(od.size * up / 100, 6), round(od.size * lo / 100, 6), "mm"
    return Part(up, lo, unit, f"Rule: {r.get('name') or 'unnamed'} ({scope_label})", level, [], "rule", r.get("id", ""))


# ----------------------------------------------------------------------------- precedence

def _resolve_quantity(od: OpenDim, quantity: str, ctx: GTContext, balloon: int | None,
                      manual: dict | None) -> Part:
    # 2. manual override on this balloon
    if manual is not None and quantity == "size":
        return Part(manual.get("upper"), manual.get("lower"), manual.get("unit") or "mm", "Manual", 2, [])
    std, sflags = ctx.stated()
    proc = ctx.process_for(balloon)[0] if (std is not None and std.id == TSDS) or not std else None
    # 3. rule scoped to this drawing
    if ctx.drawing_key:
        r = matching_rule(ctx.rules, "drawing", ctx.drawing_key, od, quantity, proc)
        if r is not None:
            return _rule_part(r, od, "drawing", 3)
    drawing_states = bool(ctx.drawing.table) or std is not None
    # 4. general-tolerance table printed on this drawing
    if ctx.drawing.table:
        p = _apply_table(ctx.drawing.table, quantity, od)
        if p is not None:
            p.flags = sflags + p.flags
            return _with_conflicts(p, od, quantity, ctx, proc)
    # 5. standard stated on the drawing
    if std is not None:
        p = _apply_standard(std, quantity, od, 5, "General tol", ctx, balloon)
        p.flags = sflags + [f for f in p.flags if f not in sflags]
        return _with_conflicts(p, od, quantity, ctx, proc)
    if drawing_states:
        reason = "drawing table has no row for this feature"
        return Part(source=f"None – {reason}", flags=sflags + [reason])
    # 6. part-family rule
    if ctx.family_key:
        r = matching_rule(ctx.rules, "family", ctx.family_key, od, quantity, proc)
        if r is not None:
            return _rule_part(r, od, "part family", 6)
    # 7. customer / organisation rule, then the organisation's default standard
    r = matching_rule(ctx.rules, "customer", ctx.org_id, od, quantity, proc)
    if r is not None:
        return _rule_part(r, od, "customer", 7)
    os_ = ctx.org_settings or {}
    if os_.get("default_standard"):
        dstd = StatedStandard(os_["default_standard"], os_.get("default_iso_class") or None, None, "", "org default", "")
        p = _apply_standard(dstd, quantity, od, 7, "Org default", ctx, balloon)
        return p
    # 8. none
    flags = list(sflags)
    if ctx.drawing.text_source.startswith("none"):
        reason = "drawing text unreadable (no text layer, OCR unavailable)"
    else:
        reason = "No general tolerance stated"
    return Part(source=f"None – {reason}", flags=flags + [reason])


def _with_conflicts(p: Part, od: OpenDim, quantity: str, ctx: GTContext, proc: str | None) -> Part:
    """A family / customer rule that would match is never applied over the drawing's own standard."""
    for scope, key, label in (("family", ctx.family_key, "part family"), ("customer", ctx.org_id, "customer")):
        if not key:
            continue
        r = matching_rule(ctx.rules, scope, key, od, quantity, proc)
        if r is not None:
            msg = (f"standard conflict: {label} rule '{r.get('name')}' not applied — the drawing states "
                   f"{p.source.replace('General tol – ', '') if p.ok else 'its own general tolerance'}")
            if msg not in p.flags:
                p.flags.append(msg)
    return p


def resolve(od: OpenDim, ctx: GTContext, *, balloon: int | None = None, manual: dict | None = None) -> GTResult:
    """The single precedence function (see module docstring). `od` is an eligible open dimension —
    printed tolerances (level 1) never reach here."""
    primary = _resolve_quantity(od, "size", ctx, balloon, manual)
    for f in od.flags:
        if f not in primary.flags:
            primary.flags.insert(0, f)
    angle = None
    if od.category == "chamfer":
        angle = _resolve_quantity(od, "angle", ctx, balloon, None)
    return GTResult(od.category, od.size, primary, angle, od.angle)


# ----------------------------------------------------------------------------- formatting

def tol_strings(p: Part) -> tuple[str, str]:
    """(tol_low, tol_high) strings for the inspection report ('' when none)."""
    if not p.ok:
        return "", ""

    def f(v: float | None) -> str:
        if v is None:
            return ""
        if abs(v) < 1e-12:
            return "0"
        s = f"{v:+.4f}".rstrip("0").rstrip(".")
        return s
    return f(p.lower), f(p.upper)


def describe(p: Part) -> str:
    if not p.ok:
        return ""
    if p.unit == "deg":
        if p.upper is not None and p.lower is not None and abs(p.upper + p.lower) < 1e-9:
            return f"±{S.fmt_deg(p.upper)}"
        return f"+{S.fmt_deg(p.upper or 0)}/-{S.fmt_deg(p.lower or 0)}"
    up, lo = p.upper or 0.0, p.lower or 0.0
    if abs(up + lo) < 1e-9:
        return f"±{up:g}"
    return f"{'+' if up > 0 else ''}{up:g}/{'+' if lo > 0 else ''}{lo:g}"


def summary_note(res: GTResult) -> str:
    """Text appended to the TVS 'Notes' cell: source, angle tolerance and flags."""
    parts = [f"Tolerance source: {res.primary.source}"]
    if res.angle is not None:
        if res.angle.ok:
            parts.append(f"chamfer angle {res.angle_value:g}° {describe(res.angle)} ({res.angle.source})")
        else:
            parts.append(f"chamfer angle: {res.angle.source}")
    fl = [f for f in res.flags if not res.primary.source.endswith(f)]
    if fl:
        parts.append("flags: " + "; ".join(dict.fromkeys(fl)))
    return "; ".join(parts)
