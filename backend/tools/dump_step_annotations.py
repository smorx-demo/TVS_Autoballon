"""
Dump every non-geometric annotation / PMI-presentation entity of STEP files (exploration aid).

    python -m tools.dump_step_annotations FILE.step [FILE2.step ...] [--full]

Per file: carrier type x count, then every surface-finish candidate with its chain
(text literal -> annotation text occurrence -> callout -> DMIA -> shape_aspect -> faces,
property definitions and their attribute values, annotation plane placement, tessellated
stroke counts). --full also lists every annotation entity.
Reuses the existing Part 21 parser (app.services.step_pmi.parse_step).
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from app.services.step_pmi import Enum, Ref, Typed, decode_step_string, parse_step

ANNOT_TYPES = (
    "ANNOTATION_PLANE", "ANNOTATION_OCCURRENCE", "ANNOTATION_TEXT_OCCURRENCE", "ANNOTATION_FILL_AREA_OCCURRENCE",
    "ANNOTATION_CURVE_OCCURRENCE", "DRAUGHTING_CALLOUT", "TESSELLATED_ANNOTATION_OCCURRENCE", "TEXT_LITERAL",
    "TEXT_LITERAL_WITH_EXTENT", "DESCRIPTIVE_REPRESENTATION_ITEM", "PROPERTY_DEFINITION",
    "PROPERTY_DEFINITION_REPRESENTATION", "DRAUGHTING_MODEL_ITEM_ASSOCIATION", "DRAUGHTING_MODEL",
    "DRAUGHTING_RELATIONSHIP", "PRESENTATION_MAP", "TESSELLATED_CURVE_SET", "TESSELLATED_GEOMETRIC_SET",
    "GEOMETRIC_CURVE_SET", "POLYLINE", "STYLED_ITEM",
)
FINISH_RX = re.compile(r"\b(Ra|Rz|Rmax|Rt)\s*[=:]?\s*\d|\bN(1[0-2]|[1-9])\b|√|\bsurface\b|\btexture\b|\broughness\b", re.I)


def _s(v) -> str:
    return decode_step_string(v) if isinstance(v, str) else ""


def _point(E, ref) -> tuple | None:
    d = E.get(int(ref), {}) if isinstance(ref, int) else {}
    if "CARTESIAN_POINT" in d:
        return tuple(round(float(c), 3) for c in d["CARTESIAN_POINT"][1])
    for t in ("AXIS2_PLACEMENT_3D", "AXIS2_PLACEMENT_2D"):
        if t in d:
            return _point(E, d[t][1])
    if "PLANE" in d:
        return _point(E, d["PLANE"][1])
    return None


def _main_type(d: dict) -> str:
    for t in d:
        if t in ANNOT_TYPES:
            return t
    return next(iter(d), "?")


def dump(path: Path, full: bool = False) -> None:
    E = parse_step(path)
    print(f"\n######## {path.name}  ({len(E)} entities)")
    counts = Counter()
    for d in E.values():
        for t in d:
            if t in ANNOT_TYPES:
                counts[t] += 1
    print("carrier type x count:")
    for t, n in counts.most_common():
        print(f"   {n:4d}  {t}")

    # reverse links
    users: dict[int, list[int]] = {}
    for i, d in E.items():
        for args in d.values():
            for r in _refs(args):
                users.setdefault(r, []).append(i)

    faces_of_aspect: dict[int, list[int]] = {}
    for i, d in E.items():
        g = d.get("GEOMETRIC_ITEM_SPECIFIC_USAGE")
        if g and len(g) >= 5 and isinstance(g[2], int) and isinstance(g[4], int):
            if "ADVANCED_FACE" in E.get(int(g[4]), {}):
                faces_of_aspect.setdefault(int(g[2]), []).append(int(g[4]))

    def aspect_faces(ref, depth=0) -> list[int]:
        if not isinstance(ref, int) or depth > 4:
            return []
        out = list(faces_of_aspect.get(int(ref), []))
        d = E.get(int(ref), {})
        if "ADVANCED_FACE" in d:
            out.append(int(ref))
        for t in ("SHAPE_ASPECT_RELATIONSHIP",):
            pass
        for u in users.get(int(ref), []):
            ud = E.get(u, {})
            rel = ud.get("SHAPE_ASPECT_RELATIONSHIP")
            if rel and len(rel) >= 4 and rel[2] == ref:
                out += aspect_faces(rel[3], depth + 1)
        return sorted(set(out))

    # text literals -> chain
    print("\nTEXT_LITERAL strings and their chain:")
    for i, d in sorted(E.items()):
        tl = d.get("TEXT_LITERAL") or d.get("TEXT_LITERAL_WITH_EXTENT")
        if not tl:
            continue
        lit = _s(tl[1])
        place = _point(E, tl[2]) if len(tl) > 2 else None
        align = _s(tl[3]) if len(tl) > 3 else ""
        font = _main_type(E.get(int(tl[5]), {})) if len(tl) > 5 and isinstance(tl[5], int) else ""
        print(f"  #{i} TEXT_LITERAL name={_s(tl[0])!r} literal={lit!r} at={place} align={align!r} font={font}"
              f"{'  <-- finish candidate' if FINISH_RX.search(lit) else ''}")
        for u in users.get(i, []):
            _chain(E, users, u, aspect_faces, indent="      ")

    # tessellated annotation occurrences
    print("\nTESSELLATED_ANNOTATION_OCCURRENCE (stroked graphics, no string):")
    shown = 0
    cand = 0
    for i, d in sorted(E.items()):
        ta = d.get("TESSELLATED_ANNOTATION_OCCURRENCE")
        if not ta:
            continue
        name = _s(ta[0])
        strips = _strip_count(E, ta[2])
        is_c = bool(FINISH_RX.search(name))
        cand += is_c
        if full or is_c:
            print(f"  #{i} name={name!r} strokes={strips}{'  <-- finish candidate (by name)' if is_c else ''}")
            for u in users.get(i, []):
                _chain(E, users, u, aspect_faces, indent="      ")
            shown += 1
    print(f"  ({shown} shown; {cand} with finish-like names)")

    # property definitions
    print("\nPROPERTY_DEFINITION / attribute values:")
    for i, d in sorted(E.items()):
        pd = d.get("PROPERTY_DEFINITION")
        if not pd:
            continue
        target = pd[2] if len(pd) > 2 else None
        tt = _main_type(E.get(int(target), {})) if isinstance(target, int) else ""
        vals = []
        for u in users.get(i, []):
            pdr = E.get(u, {}).get("PROPERTY_DEFINITION_REPRESENTATION")
            if pdr and isinstance(pdr[1], int):
                rep = E.get(int(pdr[1]), {})
                rr = next((rep[t] for t in rep if "REPRESENTATION" in t), None)
                for it in (rr[1] if rr and isinstance(rr[1], list) else []):
                    di = E.get(int(it), {}).get("DESCRIPTIVE_REPRESENTATION_ITEM")
                    if di:
                        vals.append(f"{_s(di[0])}={_s(di[1])!r}")
        faces = aspect_faces(target)
        print(f"  #{i} name={_s(pd[0])!r} desc={_s(pd[1])!r} -> #{target} {tt} faces={faces} values={vals}")

    # descriptive items not reached via property definitions
    print("\nDESCRIPTIVE_REPRESENTATION_ITEM (all):")
    for i, d in sorted(E.items()):
        di = d.get("DESCRIPTIVE_REPRESENTATION_ITEM")
        if di:
            txt = _s(di[1])
            print(f"  #{i} {_s(di[0])!r}: {txt[:110]!r}{'  <-- finish candidate' if FINISH_RX.search(txt) else ''}")

    if full:
        print("\nALL annotation entities:")
        for i, d in sorted(E.items()):
            t = _main_type(d)
            if t in ANNOT_TYPES and t not in ("STYLED_ITEM",):
                args = d[t]
                print(f"  #{i} {t} {_brief(args)}")


def _refs(v):
    if isinstance(v, Ref):
        yield int(v)
    elif isinstance(v, list):
        for x in v:
            yield from _refs(x)
    elif isinstance(v, Typed):
        yield from _refs(v.value)


def _strip_count(E, ref) -> str:
    d = E.get(int(ref), {}) if isinstance(ref, int) else {}
    tg = d.get("TESSELLATED_GEOMETRIC_SET")
    if tg:
        n = 0
        pts = 0
        for it in (tg[1] if isinstance(tg[1], list) else []):
            cs = E.get(int(it), {}).get("TESSELLATED_CURVE_SET")
            if cs:
                n += len(cs[2]) if isinstance(cs[2], list) else 0
                cl = E.get(int(cs[1]), {}).get("COORDINATES_LIST") if isinstance(cs[1], int) else None
                pts += int(cl[1]) if cl else 0
        return f"{n} polylines / {pts} points"
    return _main_type(d)


def _brief(args) -> str:
    out = []
    for a in args[:5]:
        if isinstance(a, str):
            out.append(repr(decode_step_string(a))[:60])
        elif isinstance(a, int):
            out.append(f"#{a}")
        elif isinstance(a, list):
            out.append(f"[{len(a)}]")
        elif isinstance(a, Enum):
            out.append(f".{a.name}.")
        else:
            out.append(str(a)[:30])
    return "(" + ", ".join(out) + ")"


def _chain(E, users, i, aspect_faces, indent="", depth=0):
    d = E.get(i, {})
    t = _main_type(d)
    args = d.get(t, [])
    if t in ("ANNOTATION_TEXT_OCCURRENCE", "TESSELLATED_ANNOTATION_OCCURRENCE", "ANNOTATION_OCCURRENCE"):
        print(f"{indent}<- #{i} {t} name={_s(args[0])!r}")
    elif t == "DRAUGHTING_CALLOUT":
        print(f"{indent}<- #{i} DRAUGHTING_CALLOUT name={_s(args[0])!r} contents={len(args[1]) if isinstance(args[1], list) else '?'}")
    elif t == "ANNOTATION_PLANE":
        plane = args[2] if len(args) > 2 else None
        print(f"{indent}<- #{i} ANNOTATION_PLANE name={_s(args[0])!r} placement={_point(E, plane)}")
        return
    elif t == "DRAUGHTING_MODEL_ITEM_ASSOCIATION":
        defn = args[2] if len(args) > 2 else None
        dt = _main_type(E.get(int(defn), {})) if isinstance(defn, int) else ""
        print(f"{indent}<- #{i} DMIA name={_s(args[0])!r} desc={_s(args[1])!r} definition=#{defn} {dt} faces={aspect_faces(defn)}")
        return
    else:
        return
    if depth < 4:
        for u in users.get(i, []):
            _chain(E, users, u, aspect_faces, indent + "   ", depth + 1)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for a in args:
        dump(Path(a), full="--full" in sys.argv)
