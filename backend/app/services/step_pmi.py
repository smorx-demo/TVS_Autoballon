"""
STEP AP242 semantic PMI -> model-based-definition sheet (vector PDF).

Reads the semantic PMI of a STEP file (ISO 10303-21 text) — dimensional size/location and
angular dimensions with plus/minus tolerances, geometric tolerances (characteristic, value,
diametral zone, MMC/LMC, datum system), datum features, and descriptive notes (BASIC,
REFERENCE, ISO 286 fit, THREAD, CHAMFER) — and draws every characteristic the way a 2D
drawing shows it: values as real text, basic dimensions boxed, references in brackets, GD&T as
feature control frames with vector symbols, datum features as boxed letters with triangles.

The existing drawing extractor, ballooning and TVS report then work on STEP-PMI unchanged.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

# =============================================================================
# ISO 10303-21 parser (enough for AP242 PMI)
# =============================================================================


class Ref(int):
    pass


@dataclass
class Enum:
    name: str


@dataclass
class Typed:
    type: str
    value: object


def decode_step_string(s: str) -> str:
    """
    Decode ISO 10303-21 string escapes: \\X2\\hhhh...\\X0\\ (UTF-16), \\X4\\hhhhhhhh...\\X0\\ (UTF-32),
    \\X\\hh (ISO 8859-1 byte), \\S\\c (upper half: c + 128), \\\\ -> backslash. ('' is already undoubled.)
    """
    if not s or "\\" not in s:
        return s or ""

    def x2(m):
        h = m.group(1)
        return "".join(chr(int(h[k:k + 4], 16)) for k in range(0, len(h) - len(h) % 4, 4))

    def x4(m):
        h = m.group(1)
        return "".join(chr(int(h[k:k + 8], 16)) for k in range(0, len(h) - len(h) % 8, 8))

    s = re.sub(r"\\X2\\([0-9A-Fa-f]*)\\X0\\", x2, s)
    s = re.sub(r"\\X4\\([0-9A-Fa-f]*)\\X0\\", x4, s)
    s = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\S\\(.)", lambda m: chr(ord(m.group(1)) + 128), s)
    return s.replace("\\\\", "\\")


def _tokenize(s: str):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c == "'":
            j, buf = i + 1, []
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            yield ("str", "".join(buf))
            i = j + 1
        elif c == "#":
            j = i + 1
            while j < n and s[j].isdigit():
                j += 1
            yield ("ref", Ref(int(s[i + 1:j])))
            i = j
        elif c == ".":
            j = s.find(".", i + 1)
            word = s[i + 1:j]
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", word):
                yield ("enum", Enum(word))
                i = j + 1
            else:  # number like .5
                m = re.match(r"[-+]?[0-9]*\.?[0-9]*(?:[Ee][-+]?[0-9]+)?", s[i:])
                yield ("num", float(m.group(0)))
                i += len(m.group(0))
        elif c in "()," or c in "$*":
            yield (c, c)
            i += 1
        elif c in "-+" or c.isdigit():
            m = re.match(r"[-+]?[0-9]+\.?[0-9]*(?:[Ee][-+]?[0-9]+)?", s[i:])
            yield ("num", float(m.group(0)))
            i += len(m.group(0))
        else:
            m = re.match(r"[A-Z_][A-Z0-9_]*", s[i:])
            if not m:
                i += 1
                continue
            yield ("kw", m.group(0))
            i += len(m.group(0))


def _parse_value(toks, k):
    kind, val = toks[k]
    if kind == "(":
        out, k = [], k + 1
        while toks[k][0] != ")":
            v, k = _parse_value(toks, k)
            out.append(v)
            if toks[k][0] == ",":
                k += 1
        return out, k + 1
    if kind == "kw":
        if k + 1 < len(toks) and toks[k + 1][0] == "(":
            args, k2 = _parse_value(toks, k + 1)
            return Typed(val, args[0] if len(args) == 1 else args), k2
        return Enum(val), k + 1
    if kind in ("$", "*"):
        return None, k + 1
    return val, k + 1


def _parse_entity(body: str) -> dict[str, list]:
    """'TYPE(args)' or '( A(args) B(args) )' -> {TYPE: [args]}."""
    toks = list(_tokenize(body))
    out: dict[str, list] = {}
    k = 0
    if toks and toks[0][0] == "(":
        k = 1
    while k < len(toks):
        kind, val = toks[k]
        if kind == "kw":
            if k + 1 < len(toks) and toks[k + 1][0] == "(":
                args, k = _parse_value(toks, k + 1)
                out[val] = args
            else:
                out[val] = []
                k += 1
        else:
            k += 1
    return out


def parse_step(path: str | Path) -> dict[int, dict[str, list]]:
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    data = txt[txt.find("DATA;") + 5:]
    ents: dict[int, dict[str, list]] = {}
    for m in re.finditer(r"#(\d+)\s*=\s*(.*?);\s*(?=#\d+\s*=|ENDSEC)", data, re.S):
        try:
            ents[int(m.group(1))] = _parse_entity(m.group(2))
        except Exception:
            continue
    return ents


# =============================================================================
# PMI model
# =============================================================================

GT_TYPES = {
    "POSITION_TOLERANCE": "position", "FLATNESS_TOLERANCE": "flatness", "STRAIGHTNESS_TOLERANCE": "straightness",
    "ROUNDNESS_TOLERANCE": "circularity", "CYLINDRICITY_TOLERANCE": "cylindricity",
    "SURFACE_PROFILE_TOLERANCE": "profile_surface", "LINE_PROFILE_TOLERANCE": "profile_line",
    "PERPENDICULARITY_TOLERANCE": "perpendicularity", "PARALLELISM_TOLERANCE": "parallelism",
    "ANGULARITY_TOLERANCE": "angularity", "CIRCULAR_RUNOUT_TOLERANCE": "circular_runout",
    "TOTAL_RUNOUT_TOLERANCE": "total_runout", "CONCENTRICITY_TOLERANCE": "concentricity",
    "COAXIALITY_TOLERANCE": "concentricity", "SYMMETRY_TOLERANCE": "symmetry",
}


@dataclass
class PmiItem:
    kind: str                       # dim | gdt | datum | thread | chamfer
    text: str = ""                  # annotation as a drawing would show it
    lines: list[str] = field(default_factory=list)
    gdt: str = ""
    tol: str = ""                   # FCF tolerance cell, e.g. 'Ø0.2 M'
    datums: list[str] = field(default_factory=list)
    boxed: bool = False
    note: str = ""
    # STEP identity and 3D context (read_pmi); used for mapping / numbering, never to change values
    eid: int = 0                    # semantic PMI entity #id (dimension, tolerance, datum feature, property)
    aspects: list[int] = field(default_factory=list)   # target shape aspect(s)
    faces: list[int] = field(default_factory=list)     # ADVANCED_FACE #ids
    anchor: tuple | None = None     # 3D anchor point (model units)
    anchor_src: str = ""            # 'annotation plane' | 'face'
    view: str = ""                  # saved view (camera model) presenting the PMI
    view_order: int = 0             # file order of that view (0 = none)
    qty: int = 1                    # identical target features (n×)
    inclined: bool = False          # linear location along a non-orthogonal direction
    pcd: int = 0                    # basic diameter = pitch circle of this many features
    critical: str = ""              # CC marker found in the STEP file
    general: bool = False           # surface finish without target faces (general requirement)
    source: str = ""                # where a surface finish was found
    slot: tuple | None = None       # (page index, fitz.Rect) on the rendered PMI sheet
    axes: list = field(default_factory=list)            # axis-projected centres of its round target features


@dataclass
class PmiModel:
    product: str
    items: list[PmiItem]
    notes: list[tuple[str, str]] = field(default_factory=list)   # (kind, text) text annotations
    views: list[str] = field(default_factory=list)               # saved views in file order


def has_semantic_pmi(path: str | Path) -> bool:
    head = Path(path).read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"DIMENSIONAL_CHARACTERISTIC_REPRESENTATION|GEOMETRIC_TOLERANCE|DATUM_FEATURE", head))


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _dev(v: float) -> str:
    return "0" if abs(v) < 1e-12 else (("+" if v > 0 else "") + _fmt(v))


class _Resolver:
    def __init__(self, ents: dict[int, dict[str, list]]):
        self.e = ents
        self.angle_unit: dict[int, float] = {}   # unit entity -> degrees per unit
        for i, d in ents.items():
            if "PLANE_ANGLE_UNIT" in d or "CONVERSION_BASED_UNIT" in d and "PLANE_ANGLE_UNIT" in d:
                si = d.get("SI_UNIT")
                if si is not None:
                    self.angle_unit[i] = 180.0 / math.pi
                cb = d.get("CONVERSION_BASED_UNIT")
                if cb and isinstance(cb[0], str) and "DEG" in cb[0].upper():
                    self.angle_unit[i] = 1.0

    def get(self, r) -> dict[str, list]:
        return self.e.get(int(r), {}) if isinstance(r, int) else {}

    def measure(self, r) -> tuple[float | None, bool]:
        """(value, is_angle) of a measure entity, angles converted to degrees."""
        d = self.get(r)
        mwu = d.get("MEASURE_WITH_UNIT") or d.get("LENGTH_MEASURE_WITH_UNIT") or d.get("PLANE_ANGLE_MEASURE_WITH_UNIT")
        if not mwu:
            return None, False
        raw, unit = mwu[0], mwu[1] if len(mwu) > 1 else None
        val = raw.value if isinstance(raw, Typed) else raw
        if isinstance(val, list):
            val = val[0]
        if not isinstance(val, (int, float)):
            return None, False
        is_angle = (isinstance(raw, Typed) and "ANGLE" in raw.type) or (isinstance(unit, int) and int(unit) in self.angle_unit)
        if is_angle:
            return float(val) * self.angle_unit.get(int(unit) if isinstance(unit, int) else -1, 180.0 / math.pi), True
        return float(val), False

    def name_of(self, r) -> str:
        d = self.get(r)
        ri = d.get("REPRESENTATION_ITEM")
        if ri and isinstance(ri[0], str):
            return ri[0]
        return ""


# =============================================================================
# shape aspects, faces, presentation (views, annotation planes), CC markers
# =============================================================================

CC_RX = re.compile(r"\b((?:CC|SC)\s*-?\s*\d+)\b|\bcritical\s+characteristic\b", re.I)
SURF_TXT = re.compile(r"^(?:[UL]\s+)?(?:Ra|Rz|Rmax|Rt)\s*\d|^N(?:1[0-2]|[1-9])\b", re.I)


def _refs(v):
    if isinstance(v, Ref):
        yield int(v)
    elif isinstance(v, list):
        for x in v:
            yield from _refs(x)
    elif isinstance(v, Typed):
        yield from _refs(v.value)


def _proj(p, axis):
    """Point projected onto the plane through the origin perpendicular to axis."""
    n = math.sqrt(sum(a * a for a in axis)) or 1.0
    u = [a / n for a in axis]
    t = sum(p[k] * u[k] for k in range(3))
    return tuple(p[k] - t * u[k] for k in range(3))


def _dist(a, b) -> float:
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in range(3)))


def _close(a, b, tol: float = 1e-3) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _orthogonal(v, tol_deg: float = 2.0) -> bool:
    """Direction parallel to a model axis (within tol)."""
    n = math.sqrt(sum(a * a for a in v)) or 1.0
    return max(abs(a) / n for a in v) >= math.cos(math.radians(tol_deg))


class _Graph:
    """Navigation over the parsed file: aspect -> faces, face geometry, presentation (saved views,
    annotation planes) and critical-characteristic markers."""

    def __init__(self, E: dict[int, dict[str, list]]):
        self.E = E
        self.members: dict[int, list[int]] = {}
        self.faces_of: dict[int, list[int]] = {}
        self._users: dict[int, list[int]] | None = None
        for i, d in E.items():
            rel = d.get("SHAPE_ASPECT_RELATIONSHIP")
            if rel and len(rel) >= 4 and isinstance(rel[2], int) and isinstance(rel[3], int):
                self.members.setdefault(int(rel[2]), []).append(int(rel[3]))
            u = d.get("GEOMETRIC_ITEM_SPECIFIC_USAGE")
            if u and len(u) >= 5 and isinstance(u[2], int) and isinstance(u[4], int):
                self.faces_of.setdefault(int(u[2]), []).append(int(u[4]))
        # DMIA: semantic definition -> presented annotation items
        self.presented: dict[int, list[int]] = {}
        for d in E.values():
            dm = d.get("DRAUGHTING_MODEL_ITEM_ASSOCIATION")
            if dm and len(dm) >= 5 and isinstance(dm[2], int) and isinstance(dm[4], int):
                self.presented.setdefault(int(dm[2]), []).append(int(dm[4]))
        # saved views: draughting models carrying a camera model; their items belong to that view
        self.view_of: dict[int, tuple[int, str]] = {}
        self.views: list[str] = []
        for i, d in sorted(E.items()):
            if "DRAUGHTING_MODEL" not in d:
                continue
            rep = d.get("REPRESENTATION") or d.get("DRAUGHTING_MODEL") or []
            items = rep[1] if len(rep) > 1 and isinstance(rep[1], list) else []
            cams = [x for x in items if isinstance(x, int) and any(t.startswith("CAMERA_MODEL") for t in self.get(x))]
            if not cams:
                continue
            cam = self.get(cams[0])
            cargs = next(v for t, v in cam.items() if t.startswith("CAMERA_MODEL"))
            if cargs and isinstance(cargs[0], str) and cargs[0]:
                name = decode_step_string(cargs[0])
            elif rep and isinstance(rep[0], str) and rep[0]:
                name = decode_step_string(rep[0])
            else:
                name = f"View #{i}"
            if name not in self.views:
                self.views.append(name)
            for x in items:
                if isinstance(x, int):
                    self.view_of.setdefault(int(x), (self.views.index(name) + 1, name))
        # annotation planes: plane and its elements -> placement point
        self.plane_pt: dict[int, tuple] = {}
        self.plane_of: dict[int, int] = {}
        for i, d in E.items():
            ap = d.get("ANNOTATION_PLANE")
            if not ap:
                continue
            pt = self.point(ap[2]) if len(ap) > 2 else None
            for x in [i] + (ap[3] if len(ap) > 3 and isinstance(ap[3], list) else []):
                if isinstance(x, int):
                    self.plane_of[int(x)] = i
                    if pt is not None:
                        self.plane_pt[int(x)] = pt

    def get(self, r) -> dict:
        return self.E.get(int(r), {}) if isinstance(r, int) else {}

    def users_of(self, i: int) -> list[int]:
        if self._users is None:
            self._users = {}
            for k, d in self.E.items():
                for args in d.values():
                    for r in _refs(args):
                        self._users.setdefault(r, []).append(k)
        return self._users.get(int(i), [])

    def point(self, r):
        d = self.get(r)
        if "CARTESIAN_POINT" in d:
            return tuple(float(c) for c in d["CARTESIAN_POINT"][1])
        for t in ("AXIS2_PLACEMENT_3D", "AXIS2_PLACEMENT_2D", "PLANE"):
            if t in d:
                return self.point(d[t][1])
        return None

    def direction(self, r):
        d = self.get(r).get("DIRECTION")
        return tuple(float(c) for c in d[1]) if d else None

    def faces(self, aspect, depth: int = 0) -> list[int]:
        if not isinstance(aspect, int):
            return []
        out = list(self.faces_of.get(int(aspect), []))
        if depth < 5:
            for m in self.members.get(int(aspect), []):
                out += self.faces(m, depth + 1)
        return list(dict.fromkeys(out))

    def description(self, aspect) -> str:
        d = self.get(aspect)
        for t in ("COMPOSITE_SHAPE_ASPECT", "SHAPE_ASPECT", "DATUM_FEATURE"):
            if t in d and len(d[t]) > 1 and isinstance(d[t][1], str):
                return decode_step_string(d[t][1])
        return ""

    def surface(self, face):
        """(kind, location, axis, radius) of a face's surface; radius of cylinder / sphere / torus (minor)."""
        af = self.get(face).get("ADVANCED_FACE")
        if not af or len(af) < 3:
            return None
        s = self.get(af[2])
        for t in ("CYLINDRICAL_SURFACE", "TOROIDAL_SURFACE", "SPHERICAL_SURFACE", "PLANE", "CONICAL_SURFACE"):
            if t in s:
                a = s[t]
                ax = self.get(a[1]).get("AXIS2_PLACEMENT_3D")
                loc = self.point(ax[1]) if ax else None
                axis = (self.direction(ax[2]) if ax and len(ax) > 2 else None) or (0.0, 0.0, 1.0)
                r = None
                if t in ("CYLINDRICAL_SURFACE", "SPHERICAL_SURFACE") and len(a) > 2:
                    r = float(a[2])
                elif t == "TOROIDAL_SURFACE" and len(a) > 3:
                    r = float(a[3])
                return t, loc, axis, r
        return None

    def features(self, faces: list[int]) -> list[tuple]:
        """Distinct round features of a face set: (kind, axis-projected centre, axis, radius).
        Patches of one cylinder count once."""
        out = []
        for f in faces:
            s = self.surface(f)
            if not s or s[1] is None:
                continue
            kind, loc, axis, r = s
            q = _proj(loc, axis)
            if not any(o[0] == kind and _dist(o[1], q) < 1e-3 and _close(o[3], r) for o in out):
                out.append((kind, q, axis, r))
        return out

    def presentations(self, ids: list[int]) -> list[int]:
        out = []
        for i in ids:
            out += self.presented.get(int(i), []) if isinstance(i, int) else []
        return out

    def anchor(self, ids: list[int], faces: list[int]) -> tuple[tuple | None, str]:
        for item in self.presentations(ids):
            if item in self.plane_pt:
                return self.plane_pt[item], "annotation plane"
        pts = [s[1] for s in (self.surface(f) for f in faces) if s and s[1] is not None]
        if pts:
            return tuple(round(sum(p[k] for p in pts) / len(pts), 4) for k in range(3)), "face"
        return None, ""

    def view(self, ids: list[int]) -> tuple[int, str]:
        for item in self.presentations(ids):
            for x in (item, self.plane_of.get(item)):
                if x is not None and x in self.view_of:
                    return self.view_of[x]
        return 0, ""

    def critical(self, ids: list[int]) -> str:
        """CC marker attached to a PMI: property definitions on its aspect / entity (name, description,
        values), or a presented (graphical) annotation carrying one."""
        idset = {int(i) for i in ids if isinstance(i, int)}
        hits: list[str] = []

        def take(txt) -> None:
            if isinstance(txt, str):
                m = CC_RX.search(decode_step_string(txt))
                if m:
                    hits.append(re.sub(r"[\s-]", "", m.group(1)).upper() if m.group(1) else "CC")

        for i, d in self.E.items():
            pd = d.get("PROPERTY_DEFINITION")
            if not (pd and len(pd) > 2 and isinstance(pd[2], int) and int(pd[2]) in idset):
                continue
            take(pd[0])
            take(pd[1])
            for u in self.users_of(i):
                pdr = self.get(u).get("PROPERTY_DEFINITION_REPRESENTATION")
                if pdr and len(pdr) > 1 and isinstance(pdr[1], int):
                    rep = self.get(pdr[1])
                    rr = next((rep[t] for t in rep if "REPRESENTATION" in t), None)
                    for it in (rr[1] if rr and len(rr) > 1 and isinstance(rr[1], list) else []):
                        di = self.get(it).get("DESCRIPTIVE_REPRESENTATION_ITEM")
                        if di:
                            take(di[0])
                            take(di[1])
        for item in self.presentations(list(idset)):
            for args in self.get(item).values():
                if args and isinstance(args[0], str):
                    take(args[0])
        numbered = [h for h in hits if any(c.isdigit() for c in h)]
        return (numbered or hits or [""])[0]


def size_quantity(G: _Graph, aspect, nominal: float, sub: str) -> int:
    """n× of a size dimension: the target is several distinct round features, all of that size."""
    feats = G.features(G.faces(aspect))
    want = nominal / 2 if "diameter" in sub else nominal
    same = [f for f in feats if f[3] is not None and _close(f[3], want)]
    return len(same) if len(same) >= 2 and len(same) == len(feats) else 1


def pitch_circle(G: _Graph, aspect, nominal: float) -> int:
    """Number of features when a basic diameter is the pitch circle of a pattern (their centres lie on
    a circle of that diameter and none of them has that size); else 0."""
    feats = G.features(G.faces(aspect))
    if len(feats) < 2 or any(f[3] is not None and _close(f[3], nominal / 2) for f in feats):
        return 0
    c = tuple(sum(f[1][k] for f in feats) / len(feats) for k in range(3))
    return len(feats) if all(_close(_dist(f[1], c), nominal / 2, 5e-3) for f in feats) else 0


def location_inclined(G: _Graph, a, b, nominal: float) -> bool:
    """Linear location measured along a direction that is not a model axis: the normal of a planar
    target, or the centre-to-centre direction of two axes when the value is that distance."""
    sa = [x for x in (G.surface(f) for f in G.faces(a)) if x]
    sb = [x for x in (G.surface(f) for f in G.faces(b)) if x]
    if not sa or not sb:
        return False
    planes = [x for x in sa + sb if x[0] == "PLANE"]
    if planes:
        return not _orthogonal(planes[0][2])
    fa, fb = G.features(G.faces(a)), G.features(G.faces(b))
    if len(fa) != 1 or len(fb) != 1:
        return False
    v = tuple(fb[0][1][k] - fa[0][1][k] for k in range(3))
    if not _close(math.sqrt(sum(x * x for x in v)), nominal, 2e-3):
        return False
    return not _orthogonal(v)


def read_pmi(path: str | Path) -> PmiModel:
    ents = parse_step(path)
    R = _Resolver(ents)
    G = _Graph(ents)
    product = next((d["PRODUCT"][0] for d in ents.values() if "PRODUCT" in d and d["PRODUCT"]), Path(path).stem)
    items: list[PmiItem] = []

    def add(it: PmiItem, ids: list, aspects: list) -> None:
        """Append with STEP context: target faces, 3D anchor, saved view, CC marker."""
        it.aspects = [int(a) for a in aspects if isinstance(a, int)]
        faces: list[int] = []
        for a in it.aspects:
            faces += G.faces(a)
        it.faces = list(dict.fromkeys(faces))
        it.axes = [tuple(round(c, 3) for c in f[1]) for f in G.features(it.faces)]
        keys = [int(x) for x in ids if isinstance(x, int)] + it.aspects
        it.anchor, it.anchor_src = G.anchor(keys, it.faces)
        it.view_order, it.view = G.view(keys)
        it.critical = G.critical(keys)
        items.append(it)

    tol_by_dim: dict[int, tuple[float, float]] = {}
    for d in ents.values():
        pmt = d.get("PLUS_MINUS_TOLERANCE")
        if pmt:
            rng = R.get(pmt[0])
            tv = rng.get("TOLERANCE_VALUE")
            if tv:
                lo, _ = R.measure(tv[0])
                hi, _ = R.measure(tv[1])
                if lo is not None and hi is not None:
                    tol_by_dim[int(pmt[1])] = (lo, hi)

    # ---- dimensions
    for i, d in sorted(ents.items()):
        dcr = d.get("DIMENSIONAL_CHARACTERISTIC_REPRESENTATION")
        if not dcr:
            continue
        dim_id, rep = int(dcr[0]), R.get(dcr[1])
        dim = R.get(dim_id)
        sdr = rep.get("SHAPE_DIMENSION_REPRESENTATION")
        nominal = is_angle = None
        notes: list[str] = []
        for it in (sdr[1] if sdr and isinstance(sdr[1], list) else []):
            ed = R.get(it)
            if "DESCRIPTIVE_REPRESENTATION_ITEM" in ed:
                notes.append(str(ed["DESCRIPTIVE_REPRESENTATION_ITEM"][1]))
                continue
            v, ang = R.measure(it)
            if v is not None and (nominal is None or R.name_of(it).lower().startswith("nominal")):
                nominal, is_angle = v, ang
        if nominal is None:
            continue
        dtype = ""
        aspects: list = []
        for t in ("DIMENSIONAL_SIZE", "ANGULAR_SIZE", "DIMENSIONAL_LOCATION", "ANGULAR_LOCATION"):
            if t in dim:
                dtype = t
                args = dim[t]
                sub = (args[1] if t in ("DIMENSIONAL_SIZE", "ANGULAR_SIZE") else args[0]) if args else ""
                aspects = ([args[0]] if t in ("DIMENSIONAL_SIZE", "ANGULAR_SIZE") else list(args[2:4])) if args else []
                break
        # a size dimension's target may carry its note as the shape aspect description (THREAD / FIT / CHAMFER)
        if dtype in ("DIMENSIONAL_SIZE", "ANGULAR_SIZE") and aspects:
            desc = G.description(aspects[0])
            if desc and desc not in notes:
                notes.append(desc)
        note = " ".join(notes)
        ids = [dim_id, i]
        sub = (sub or "").lower() if isinstance(sub, str) else ""
        angle = is_angle or dtype.startswith("ANGULAR")
        tol = tol_by_dim.get(dim_id)

        # a THREAD note names a designation: 'THREAD [n x] M...' ('full-thread depth' is not one)
        th = re.search(r"\bTHREAD\s+((?:\d+\s*x\s*)?M\d[^;]*)", note)
        if th:
            callout = _thread_text(th.group(1))
            add(PmiItem("thread", text=" / ".join(callout), lines=callout, note=_rest(note, th), eid=dim_id), ids, aspects)
            continue
        ch = re.search(r"\bCHAMFER\s+(?:(\d+)\s*x\s+)?(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*deg", note, re.I)
        if ch:
            qty = f"{ch.group(1)}× " if ch.group(1) else ""
            add(PmiItem("chamfer", text=f"{qty}{ch.group(2)} × {ch.group(3)}°", note=note, eid=dim_id), ids, aspects)
            continue
        pre = "Ø" if "diameter" in sub else ("R" if "radius" in sub else "")
        if "spherical" in sub:
            pre = "S" + pre
        val = f"{pre}{_fmt(nominal)}" + ("°" if angle else "")
        fit = re.search(r"\bFIT\s+([A-Za-z]{1,2}\d{1,2})\b", note)
        if fit:
            val += f" {fit.group(1)}"
        # AP242 dimensional notes: 'theoretical' = basic, 'auxiliary' = reference
        basic = bool(re.search(r"\bBASIC\b|THEORETICALLY EXACT|\btheoretical\b", note, re.I))
        ref = bool(re.search(r"\bREFERENCE\b|\bauxiliary\b", note, re.I))
        if ref:
            text = f"({_fmt(nominal)}{'°' if angle else ''})"
        elif basic:
            text = val
        elif tol is not None:
            lo, hi = tol
            if abs(hi + lo) < 1e-12 and hi > 0:
                text = f"{val} ±{_fmt(hi)}{'°' if angle else ''}"
            else:
                text = f"{val} {_dev(hi)}/{_dev(lo)}"
        else:
            text = val
        it = PmiItem("dim", text=text, boxed=basic and not ref, note=note, eid=dim_id)
        if dtype == "DIMENSIONAL_SIZE" and aspects and not basic and not ref and not angle and pre in ("Ø", "R"):
            it.qty = size_quantity(G, aspects[0], nominal, sub)
            if it.qty > 1:
                it.text = f"{it.qty}× {text}"
        if dtype == "DIMENSIONAL_SIZE" and aspects and basic and pre == "Ø":
            it.pcd = pitch_circle(G, aspects[0], nominal)
        if dtype == "DIMENSIONAL_LOCATION" and len(aspects) == 2 and not basic and not ref:
            it.inclined = location_inclined(G, aspects[0], aspects[1], nominal)
        add(it, ids, aspects)

    # ---- datum letters
    datum_letter = {i: d["DATUM"][4] for i, d in ents.items() if "DATUM" in d and len(d["DATUM"]) > 4}
    feature_letter: dict[int, str] = {}
    for d in ents.values():
        rel = d.get("SHAPE_ASPECT_RELATIONSHIP")
        if rel and len(rel) >= 4:
            a, b = int(rel[2]) if isinstance(rel[2], int) else -1, int(rel[3]) if isinstance(rel[3], int) else -1
            if a in datum_letter and "DATUM_FEATURE" in R.get(b):
                feature_letter[b] = datum_letter[a]
            elif b in datum_letter and "DATUM_FEATURE" in R.get(a):
                feature_letter[a] = datum_letter[b]
    df_ids = [i for i, d in sorted(ents.items()) if "DATUM_FEATURE" in d]
    letters_left = [l for l in sorted(datum_letter.values()) if l not in feature_letter.values()]
    for i in df_ids:
        letter = feature_letter.get(i) or (letters_left.pop(0) if letters_left else "")
        if letter:
            add(PmiItem("datum", text=letter, eid=i), [i], [i])

    # ---- geometric tolerances
    zone_form: dict[int, str] = {}
    for d in ents.values():
        tz = d.get("TOLERANCE_ZONE")
        if tz and len(tz) >= 6:
            form = R.get(tz[5]).get("TOLERANCE_ZONE_FORM")
            for gt in (tz[4] if isinstance(tz[4], list) else []):
                if form:
                    zone_form[int(gt)] = str(form[0])
    for i, d in sorted(ents.items()):
        kind = next((GT_TYPES[t] for t in d if t in GT_TYPES), None)
        if kind is None:
            continue
        g = d.get("GEOMETRIC_TOLERANCE") or next((d[t] for t in d if t in GT_TYPES and d[t]), None)
        if not g or len(g) < 3:
            continue
        mag, _ = R.measure(g[2])
        if mag is None:
            continue
        diam = "circular" in zone_form.get(i, "") or "cylindrical" in zone_form.get(i, "")
        mods = d.get("GEOMETRIC_TOLERANCE_WITH_MODIFIERS")
        mod = ""
        if mods:
            names = {m.name for m in (mods[0] if isinstance(mods[0], list) else []) if isinstance(m, Enum)}
            mod = " M" if "MAXIMUM_MATERIAL_REQUIREMENT" in names else (" L" if "LEAST_MATERIAL_REQUIREMENT" in names else "")
        datums: list[str] = []
        wdr = d.get("GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE")
        if wdr:
            for ds in (wdr[0] if isinstance(wdr[0], list) else []):
                sysd = R.get(ds).get("DATUM_SYSTEM")
                for comp in (sysd[4] if sysd and len(sysd) > 4 and isinstance(sysd[4], list) else []):
                    c = R.get(comp).get("DATUM_REFERENCE_COMPARTMENT")
                    base = c[4] if c and len(c) > 4 else None
                    if isinstance(base, int) and int(base) in datum_letter:
                        datums.append(datum_letter[int(base)])
                    elif isinstance(base, list):
                        datums.append("-".join(datum_letter.get(int(x), "?") for x in base if isinstance(x, int)))
        tol = f"{'Ø' if diam else ''}{_fmt(mag)}{mod}"
        add(PmiItem("gdt", gdt=kind, tol=tol, datums=datums, eid=i), [i], [g[3]] if len(g) > 3 else [])

    _surface_finishes(ents, G, add)
    return PmiModel(str(product), items, _text_notes(ents, G, items), list(G.views))


_FINISH_PROP = re.compile(r"surface\s*(?:finish|texture|roughness)|roughness", re.I)


def _finish_text(raw: str) -> tuple[str, bool]:
    """Finish value as a drawing shows it, and whether it is the general ( √ ) requirement."""
    t = re.sub(r"\s+", " ", decode_step_string(raw)).strip()
    general = bool(re.search(r"\(\s*√\s*\)", t))
    t = re.sub(r"\(\s*√\s*\)", "", t).strip()
    return t, general


def _surface_finishes(E: dict, G: _Graph, add) -> None:
    """Surface finishes, by tier: (a) property definitions / attribute values on a shape aspect,
    (b) text literals of annotations presented for an aspect, (c) the name of a stroked (graphical)
    annotation presented for an aspect. One finish per target aspect; OCR is not attempted."""
    covered: set[int] = set()
    presented_by: dict[int, int] = {}
    for d, items in G.presented.items():
        for x in items:
            presented_by[x] = d

    def emit(text: str, eid: int, aspect, source: str) -> None:
        val, general = _finish_text(text)
        if not SURF_TXT.match(val):
            return
        a = int(aspect) if isinstance(aspect, int) else None
        if a is not None:
            if a in covered:
                return
            covered.add(a)
        it = PmiItem("surface", text=val, eid=eid, source=source)
        add(it, [eid] + ([a] if a is not None else []), [a] if a is not None else [])
        it.general = general or not it.faces

    for i, d in sorted(E.items()):
        pd = d.get("PROPERTY_DEFINITION")
        if not pd or not any(isinstance(x, str) and _FINISH_PROP.search(x) for x in pd[:2]):
            continue
        vals = []
        for u in G.users_of(i):
            pdr = G.get(u).get("PROPERTY_DEFINITION_REPRESENTATION")
            if pdr and len(pdr) > 1 and isinstance(pdr[1], int):
                rep_ = G.get(pdr[1])
                rr = next((rep_[t] for t in rep_ if "REPRESENTATION" in t), None)
                for it in (rr[1] if rr and len(rr) > 1 and isinstance(rr[1], list) else []):
                    di = G.get(it).get("DESCRIPTIVE_REPRESENTATION_ITEM")
                    if di and isinstance(di[1], str):
                        vals.append(di[1])
        if isinstance(pd[1], str):
            vals.append(pd[1])
        for v in vals:
            if SURF_TXT.match(_finish_text(v)[0]):
                emit(v, i, pd[2] if len(pd) > 2 else None, "property")
                break

    def callout_of(i: int, depth: int = 0):
        if i in presented_by:
            return i
        if depth > 3:
            return None
        for u in G.users_of(i):
            if any(t in G.get(u) for t in ("ANNOTATION_TEXT_OCCURRENCE", "DRAUGHTING_CALLOUT", "ANNOTATION_OCCURRENCE",
                                           "TESSELLATED_ANNOTATION_OCCURRENCE")):
                c = callout_of(u, depth + 1)
                if c is not None:
                    return c
        return None

    for i, d in sorted(E.items()):
        tl = d.get("TEXT_LITERAL") or d.get("TEXT_LITERAL_WITH_EXTENT")
        if not tl or not isinstance(tl[1], str) or not SURF_TXT.match(_finish_text(tl[1])[0]):
            continue
        c = callout_of(i)
        emit(tl[1], i, presented_by.get(c) if c is not None else None, "text annotation")

    for i, d in sorted(E.items()):
        for t in ("TESSELLATED_ANNOTATION_OCCURRENCE", "ANNOTATION_OCCURRENCE", "DRAUGHTING_CALLOUT"):
            a = d.get(t)
            if not a or not isinstance(a[0], str):
                continue
            name = decode_step_string(a[0])
            m = re.search(r"(?:surface\s*(?:finish|texture)|roughness)[^:]*:\s*(.+)$", name, re.I)
            c = callout_of(i)
            if m and c is not None:
                emit(m.group(1), i, presented_by.get(c), "annotation name")


def _text_notes(E: dict, G: _Graph, items: list[PmiItem]) -> list[tuple[str, str]]:
    """Every text annotation in the file: descriptive notes, shape-aspect descriptions, text literals
    (finishes already reported as features are left out)."""
    used = {it.text for it in items if it.kind == "surface"}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def put(kind: str, txt) -> None:
        t = re.sub(r"\s+", " ", decode_step_string(txt)).strip() if isinstance(txt, str) else ""
        if t and t not in seen and _finish_text(t)[0] not in used:
            seen.add(t)
            out.append((kind, t))

    for i, d in sorted(E.items()):
        di = d.get("DESCRIPTIVE_REPRESENTATION_ITEM")
        if di and len(di) > 1:
            put("PMI note", di[1])
        tl = d.get("TEXT_LITERAL") or d.get("TEXT_LITERAL_WITH_EXTENT")
        if tl and len(tl) > 1:
            put("Text annotation", tl[1])
        for t in ("SHAPE_ASPECT", "COMPOSITE_SHAPE_ASPECT"):
            if t in d and len(d[t]) > 1:
                put("Feature description", d[t][1])
    return out


def _thread_text(callout: str) -> list[str]:
    t = callout.strip()
    t = re.sub(r"\bDIA\s*", "Ø", t)
    t = re.sub(r"^(\d+)\s*x\s*(?=M)", r"\1× ", t)   # quantity prefix: '4x M8' -> '4× M8'
    t = re.sub(r"(\d)\s*x\s*(?=\d)", r"\1×", t)      # size x pitch, CSK dia x angle
    t = re.sub(r"(\d)deg\b", r"\1°", t)
    t = re.sub(r"\bDEPTH\s*(\d+(?:\.\d+)?)", r"↧\1", t)
    return [p.strip() for p in t.split("/") if p.strip()]


def _rest(note: str, m) -> str:
    return note[m.end():].lstrip("; ").strip()


# =============================================================================
# sheet renderer
# =============================================================================

W, H = 1190.55, 841.89
FRAME = fitz.Rect(56.7, 28.35, 1162.2, 813.5)


def _draw_symbol(page: fitz.Page, cell: fitz.Rect, kind: str) -> None:
    c = fitz.Point((cell.x0 + cell.x1) / 2, (cell.y0 + cell.y1) / 2)
    h = cell.height * 0.26

    def line(a, b):
        sh = page.new_shape()
        sh.draw_line(a, b)
        sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
        sh.commit()

    def circle(r):
        sh = page.new_shape()
        sh.draw_circle(c, r)
        sh.finish(width=0.7, color=(0, 0, 0))
        sh.commit()

    def poly(pts, fill=False):
        sh = page.new_shape()
        sh.draw_polyline(pts + [pts[0]])
        sh.finish(width=0.7, color=None if fill else (0, 0, 0), fill=(0, 0, 0) if fill else None, closePath=False)
        sh.commit()

    def arrow(tip, frm, size=4):
        ang = math.atan2(tip.y - frm.y, tip.x - frm.x)
        poly([tip, fitz.Point(tip.x - size * math.cos(ang - 0.3), tip.y - size * math.sin(ang - 0.3)),
              fitz.Point(tip.x - size * math.cos(ang + 0.3), tip.y - size * math.sin(ang + 0.3))], fill=True)

    P = fitz.Point
    if kind == "position":
        circle(h * 0.75); line(P(c.x - h * 1.6, c.y), P(c.x + h * 1.6, c.y)); line(P(c.x, c.y - h * 1.6), P(c.x, c.y + h * 1.6))
    elif kind == "perpendicularity":
        line(P(c.x - h * 1.3, c.y + h), P(c.x + h * 1.3, c.y + h)); line(P(c.x, c.y + h), P(c.x, c.y - h * 1.2))
    elif kind == "parallelism":
        line(P(c.x - h, c.y + h), P(c.x - h * 0.2, c.y - h)); line(P(c.x + h * 0.2, c.y + h), P(c.x + h, c.y - h))
    elif kind == "flatness":
        poly([P(c.x - h * 1.3, c.y + h * 0.6), P(c.x + h * 0.7, c.y + h * 0.6), P(c.x + h * 1.3, c.y - h * 0.6), P(c.x - h * 0.7, c.y - h * 0.6)])
    elif kind == "circular_runout":
        a, b = P(c.x - h, c.y + h), P(c.x + h, c.y - h)
        line(a, b); arrow(P(b.x + 1, b.y - 1), a)
    elif kind == "total_runout":
        for dx in (-h * 0.8, h * 0.6):
            a, b = P(c.x + dx - h * 0.5, c.y + h), P(c.x + dx + h * 0.5, c.y - h)
            line(a, b); arrow(P(b.x + 0.5, b.y - 1), a)
    elif kind in ("profile_surface", "profile_line"):
        sh = page.new_shape()
        sh.draw_curve(P(c.x - h * 1.2, c.y + h * 0.6), P(c.x, c.y - h * 1.4), P(c.x + h * 1.2, c.y + h * 0.6))
        sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
        sh.commit()
        if kind == "profile_surface":
            line(P(c.x - h * 1.2, c.y + h * 0.6), P(c.x + h * 1.2, c.y + h * 0.6))
    elif kind == "straightness":
        line(P(c.x - h * 1.3, c.y), P(c.x + h * 1.3, c.y))
    elif kind == "cylindricity":
        circle(h * 0.7); line(P(c.x - h * 1.4, c.y + h), P(c.x - h * 0.2, c.y - h)); line(P(c.x + h * 0.2, c.y + h), P(c.x + h * 1.4, c.y - h))
    elif kind == "angularity":
        line(P(c.x - h * 1.2, c.y + h), P(c.x + h * 1.2, c.y + h)); line(P(c.x - h * 1.2, c.y + h), P(c.x + h * 1.0, c.y - h))
    elif kind == "concentricity":
        circle(h * 0.5); circle(h * 1.0)
    elif kind == "symmetry":
        line(P(c.x - h * 0.8, c.y - h * 0.7), P(c.x + h * 0.8, c.y - h * 0.7))
        line(P(c.x - h * 1.3, c.y), P(c.x + h * 1.3, c.y))
        line(P(c.x - h * 0.8, c.y + h * 0.7), P(c.x + h * 0.8, c.y + h * 0.7))
    elif kind == "circularity":
        circle(h * 0.9)


def render_pmi_sheet(model: PmiModel, dest: Path) -> int:
    from app.services.dxf_to_pdf import _font_file

    fontfile = _font_file()
    doc = fitz.open()

    def text(page, p, s, size=9.5, bold=False):
        kw = {"fontsize": size}
        if fontfile:
            kw.update(fontname="pmi0", fontfile=fontfile)
        else:
            kw.update(fontname="hebo" if bold else "helv")
            s = s.replace("↧", "DEPTH ")
        page.insert_text(p, s, **kw)

    def rect(page, r, width=0.7):
        sh = page.new_shape()
        sh.draw_rect(r)
        sh.finish(width=width, color=(0, 0, 0))
        sh.commit()

    def width_of(s, size=9.5):
        f = fitz.Font(fontfile=fontfile) if fontfile else fitz.Font("helv")
        return f.text_length(s, fontsize=size)

    def new_page(n):
        page = doc.new_page(width=W, height=H)
        rect(page, FRAME, 1.4)
        for c in range(8):
            x = FRAME.x0 + (c + 0.5) * FRAME.width / 8
            for y in (18, H - 10):
                text(page, fitz.Point(x - 3, y), str(c + 1))
        for r in range(6):
            y = FRAME.y0 + (r + 0.5) * FRAME.height / 6 + 3
            for x in (30, W - 22):
                text(page, fitz.Point(x, y), "ABCDEF"[r])
        text(page, fitz.Point(90, 75), f"MODEL-BASED DEFINITION (STEP AP242 PMI): {model.product}", 9.5)
        text(page, fitz.Point(470, 770), "MODEL VIEW (SEMANTIC PMI)", 8.5)
        return page

    cols, x0s, row_h = 4, [100, 340, 580, 820], 42.0
    y_start, y_end = 120.0, 720.0
    rows = int((y_end - y_start) // row_h)
    page = new_page(1)
    slot = 0
    for it in model.items:
        if slot >= rows * cols:
            page = new_page(doc.page_count + 1)
            slot = 0
        x = x0s[slot % cols]
        y = y_start + (slot // cols) * row_h
        slot += 1
        it.slot = (doc.page_count - 1, fitz.Rect(x - 8, y - 30, x + 236, y + 22))
        if it.kind == "gdt":
            hgt, sw = 17.0, 20.0
            tw = width_of(it.tol, 9) + 10
            cells = [fitz.Rect(x, y - 12, x + sw, y + 5), fitz.Rect(x + sw, y - 12, x + sw + tw, y + 5)]
            cx = x + sw + tw
            for _ in it.datums:
                cells.append(fitz.Rect(cx, y - 12, cx + 17, y + 5))
                cx += 17
            frame = fitz.Rect(cells[0].x0, cells[0].y0, cells[-1].x1, cells[-1].y1)
            rect(page, frame)
            for c in cells[1:]:
                sh = page.new_shape()
                sh.draw_line(fitz.Point(c.x0, c.y0), fitz.Point(c.x0, c.y1))
                sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
                sh.commit()
            _draw_symbol(page, cells[0], it.gdt)
            text(page, fitz.Point(cells[1].x0 + 5, y + 1), it.tol, 9)
            for c, dl in zip(cells[2:], it.datums):
                text(page, fitz.Point(c.x0 + 5, y + 1), dl, 9)
        elif it.kind == "datum":
            box = fitz.Rect(x, y - 13, x + 17, y + 4)
            rect(page, box)
            text(page, fitz.Point(box.x0 + 5, box.y1 - 5), it.text)
            apex = fitz.Point(box.x1 + 10, (box.y0 + box.y1) / 2)
            sh = page.new_shape()
            sh.draw_line(fitz.Point(box.x1, apex.y), apex)
            sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
            sh.commit()
            sh = page.new_shape()
            pts = [apex, fitz.Point(apex.x + 8, apex.y - 4.5), fitz.Point(apex.x + 8, apex.y + 4.5)]
            sh.draw_polyline(pts + [pts[0]])
            sh.finish(fill=(0, 0, 0), color=None, closePath=False)
            sh.commit()
        elif it.kind == "thread":
            for k, ln in enumerate(it.lines):
                text(page, fitz.Point(x, y + k * 11), ln, 8.5)
        elif it.kind == "surface":
            # surface texture symbol (open check mark) with the value written on its tail
            parts = [q.strip() for q in it.text.split("/")] if re.match(r"^U\s", it.text) else [it.text]
            tail = x + 16 + max(width_of(q, 7.5) for q in parts) + 3
            sh = page.new_shape()
            sh.draw_polyline([fitz.Point(x, y - 6), fitz.Point(x + 5, y + 2), fitz.Point(x + 13, y - 14),
                              fitz.Point(tail, y - 14)])
            sh.finish(width=0.7, color=(0, 0, 0), closePath=False)
            sh.commit()
            for k, q in enumerate(parts):
                text(page, fitz.Point(x + 16, y - 16 + k * 10), q, 7.5)
            if it.general:
                text(page, fitz.Point(x + 70, y - 6), "= GENERAL SURFACE TEXTURE, ALL OVER UNLESS OTHERWISE STATED", 6.5)
        else:
            text(page, fitz.Point(x, y), it.text)
            if it.boxed:
                w = width_of(it.text)
                rect(page, fitz.Rect(x - 3, y - 9.5 * 0.95, x + w + 3, y + 9.5 * 0.3))
    n = doc.page_count
    doc.save(str(dest), garbage=3, deflate=True)
    doc.close()
    return n


def step_pmi_to_pdf(src: Path, dest: Path) -> dict:
    model = read_pmi(src)
    if not model.items:
        raise ValueError("STEP file has no semantic PMI")
    pages = render_pmi_sheet(model, dest)
    counts: dict[str, int] = {}
    for it in model.items:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    return {"converter": "step_ap242_pmi", "pages": pages, "vector_hint": True, "product": model.product,
            "pmi_counts": counts}
