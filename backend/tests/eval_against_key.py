"""
Score a TVS extraction report (xlsx) against an answer key (xlsx) with the same layout.

    python -m tests.eval_against_key [drawing.pdf] [key.xlsx]      (run from backend/)

Matching: expected ↔ output features one-to-one by Hungarian assignment on centre distance,
restricted to the same sheet, centre within MATCH_MM, and a compatible feature family.
Every field of a matched feature is checked; a feature is "correct" only if all fields pass.
"""

from __future__ import annotations

import io
import math
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

import openpyxl

MATCH_MM = 5.0
XY_MM = 3.0
NUM_TOL = 1e-6

FIELDS = [
    "Feature type", "Symbol", "Nominal", "Upper tolerance", "Lower tolerance", "Max limit", "Min limit",
    "Primary datum", "Secondary datum", "Tertiary datum", "Boxed / reference flag", "Critical flag",
    "Sheet", "Zone", "View", "X/Y", "Details tokens",
]
NUMERIC = {"Nominal", "Upper tolerance", "Lower tolerance", "Max limit", "Min limit"}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _rows(ws) -> list[dict]:
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    out = []
    for r in it:
        if not any(v is not None and str(v).strip() for v in r):
            continue
        out.append({h: v for h, v in zip(header, r)})
    return out


def _feature_sheet(wb):
    for name in ("Expected Extraction", "Extraction"):
        if name in wb.sheetnames:
            return wb[name]
    raise KeyError("no extraction sheet")


def load_workbook(src) -> dict:
    wb = openpyxl.load_workbook(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src, data_only=True)
    features = _rows(_feature_sheet(wb))
    notes = _rows(wb["Notes"]) if "Notes" in wb.sheetnames else []
    summary = _summary(wb["Summary"]) if "Summary" in wb.sheetnames else {}
    return {"features": features, "notes": notes, "summary": summary}


def _summary(ws) -> dict:
    """Section → {label: count} from a Summary sheet laid out as titled 2/3-column blocks."""
    out: dict[str, dict[str, int]] = {}
    section = ""
    for r in ws.iter_rows(values_only=True):
        a = r[0] if r else None
        b = r[1] if len(r) > 1 else None
        if a is None:
            continue
        a = str(a).strip()
        if b is None and not re.match(r"^\d+$", a):
            section = a
            continue
        if isinstance(b, (int, float)):
            out.setdefault(section, {})[a] = int(b)
        elif re.match(r"^\d+$", a) and isinstance(b, (int, float)):
            out.setdefault(section, {})[a] = int(b)
    return out


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def _s(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _num(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(",", ".").replace("+", "").strip()
    try:
        return float(t)
    except ValueError:
        return t.upper()


def _datum(v) -> str:
    return _s(v).upper()


def _flag(v) -> str:
    t = _s(v)
    return "" if t in {"", "-"} else t.lower()


def family(ftype: str) -> str:
    t = _s(ftype).lower()
    if t.startswith("gd&t"):
        return "gdt"
    if t.startswith("surface"):
        return "surface"
    if t.startswith("thread"):
        return "thread"
    if t.startswith("datum"):
        return "datum"
    return "dimension"


DETAIL_TOKEN_RES = [
    re.compile(r"\bM\d+(?:[.,]\d+)?(?:\s*[×x]\s*\d+(?:[.,]\d+)?)?"),  # thread designation
    re.compile(r"(?<![\w.])\d[GHgh](?![\w])"),                         # thread tolerance class
    re.compile(r"\bdepth\s+\d+(?:[.,]\d+)?", re.I),                    # depths
    re.compile(r"\b(?:MMC|LMC|RFS)\b"),                                 # material condition
    re.compile(r"(?<=ISO 286 )[A-Za-z]{1,2}\d{1,2}\b"),                # fit designation
    re.compile(r"\b(?:Ra|Rz)\s*\d+(?:[.,]\d+)?"),                      # roughness values
    re.compile(r"\bN\d{1,2}\b"),                                        # roughness grade
    re.compile(r"\bCSK\b|\bTHRU\b|\bthrough\b", re.I),
]


def detail_tokens(text: str) -> set[str]:
    t = _s(text)
    toks: set[str] = set()
    for rx in DETAIL_TOKEN_RES:
        for m in rx.finditer(t):
            tok = re.sub(r"\s+", " ", m.group(0)).replace("x", "×") if rx is DETAIL_TOKEN_RES[0] else m.group(0)
            tok = tok.lower().replace(" ", "")
            if tok == "through":
                tok = "thru"
            toks.add(tok)
    return toks


def _norm_note(t: str) -> str:
    t = _s(t).lower()
    t = re.sub(r"[|/,;:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def hungarian(cost: list[list[float]]) -> list[int]:
    """Min-cost assignment for an n×m matrix (n ≤ m). Returns column index per row (or -1)."""
    n = len(cost)
    m = len(cost[0]) if n else 0
    if n == 0 or m == 0:
        return [-1] * n
    transpose = n > m
    if transpose:
        cost = [list(col) for col in zip(*cost)]
        n, m = m, n
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    rows = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            rows[p[j] - 1] = j - 1
    if transpose:
        cols = [-1] * m
        for i, j in enumerate(rows):
            if j >= 0:
                cols[j] = i
        return cols
    return rows


def _xy(f) -> tuple[float, float]:
    return float(_num(f.get("X (mm)")) or 0.0), float(_num(f.get("Y (mm)")) or 0.0)


def match(expected: list[dict], output: list[dict]) -> dict[int, int]:
    BIG = 1e9
    cost = []
    for e in expected:
        row = []
        ex, ey = _xy(e)
        for o in output:
            ox, oy = _xy(o)
            d = math.hypot(ex - ox, ey - oy)
            ok = (
                _num(e.get("Sheet")) == _num(o.get("Sheet"))
                and d <= MATCH_MM
                and family(e.get("Feature type")) == family(o.get("Feature type"))
            )
            row.append(d if ok else BIG)
        cost.append(row)
    if not expected or not output:
        return {}
    assign = hungarian(cost)
    return {i: j for i, j in enumerate(assign) if j >= 0 and cost[i][j] < BIG}


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def check_fields(e: dict, o: dict) -> dict[str, bool]:
    res: dict[str, bool] = {}
    res["Feature type"] = _s(e.get("Feature type")).lower() == _s(o.get("Feature type")).lower()
    res["Symbol"] = _s(e.get("Symbol")) == _s(o.get("Symbol"))
    for k in NUMERIC:
        a, b = _num(e.get(k)), _num(o.get(k))
        if isinstance(a, float) and isinstance(b, float):
            res[k] = abs(a - b) <= NUM_TOL
        else:
            res[k] = a == b
    for k in ("Primary datum", "Secondary datum", "Tertiary datum"):
        res[k] = _datum(e.get(k)) == _datum(o.get(k))
    res["Boxed / reference flag"] = _flag(e.get("Boxed / reference flag")) == _flag(o.get("Boxed / reference flag"))
    res["Critical flag"] = _flag(e.get("Critical flag")) == _flag(o.get("Critical flag"))
    res["Sheet"] = _num(e.get("Sheet")) == _num(o.get("Sheet"))
    res["Zone"] = _s(e.get("Zone")).upper() == _s(o.get("Zone")).upper()
    res["View"] = _s(e.get("View")).lower() == _s(o.get("View")).lower()
    ex, ey = _xy(e)
    ox, oy = _xy(o)
    res["X/Y"] = abs(ex - ox) <= XY_MM and abs(ey - oy) <= XY_MM
    key = "Fit / thread / finish / GD&T details (incl. datums)"
    need = detail_tokens(e.get(key))
    have = detail_tokens(o.get(key)) | detail_tokens(o.get("Text as shown on drawing"))
    res["Details tokens"] = need <= have
    return res


def evaluate(output_src, key_src) -> dict:
    key = load_workbook(key_src)
    out = load_workbook(output_src)
    exp_f, out_f = key["features"], out["features"]
    pairs = match(exp_f, out_f)

    per_feature = OrderedDict()
    field_ok = Counter()
    for i, e in enumerate(exp_f):
        fid = _s(e.get("Feature ID")) or f"#{i + 1}"
        if i not in pairs:
            per_feature[fid] = {"status": "missed", "failed": [], "output": None}
            continue
        o = out_f[pairs[i]]
        res = check_fields(e, o)
        for k, ok in res.items():
            field_ok[k] += ok
        failed = [k for k, ok in res.items() if not ok]
        if not failed:
            status = "correct"
        elif res["Feature type"] and (res["Nominal"] or res["Symbol"]):
            status = "partial"
        else:
            status = "wrong"
        per_feature[fid] = {
            "status": status,
            "failed": failed,
            "output": _s(o.get("Feature ID")),
            "diff": {k: (e.get(k) if k != "X/Y" else _xy(e), o.get(k) if k != "X/Y" else _xy(o)) for k in failed
                     if k != "Details tokens"},
        }
        if "Details tokens" in failed:
            k = "Fit / thread / finish / GD&T details (incl. datums)"
            per_feature[fid]["diff"]["Details tokens"] = (
                sorted(detail_tokens(e.get(k)) - detail_tokens(o.get(k)) - detail_tokens(o.get("Text as shown on drawing"))),
                _s(o.get(k)),
            )
    matched_out = set(pairs.values())
    spurious = [o for j, o in enumerate(out_f) if j not in matched_out]
    counts = Counter(v["status"] for v in per_feature.values())

    # notes
    out_notes = [(int(_num(n.get("Sheet")) or 0), _norm_note(n.get("Text (verbatim)")), _s(n.get("Item type")))
                 for n in out["notes"]]
    remaining = list(out_notes)
    notes_missing = []
    notes_found = 0
    for n in key["notes"]:
        k = (int(_num(n.get("Sheet")) or 0), _norm_note(n.get("Text (verbatim)")))
        hit = next((x for x in remaining if (x[0], x[1]) == k), None)
        if hit:
            remaining.remove(hit)
            notes_found += 1
        else:
            notes_missing.append((k[0], _s(n.get("Item type")), _s(n.get("Text (verbatim)"))))
    notes_extra = [x for x in remaining if x[2].lower() != "heading"]

    # summary
    summary_diff = []
    ks, os_ = key["summary"], out["summary"]
    def _sec_key(s: str) -> str:
        # Section titles may carry a parenthetical source note; compare the title words only.
        return re.sub(r"\s*\(.*\)\s*$", "", _norm_note(s))

    for section, items in ks.items():
        osec = next((v for s, v in os_.items() if _sec_key(s) == _sec_key(section)), {})
        for label, n in items.items():
            got = osec.get(label)
            if got != n:
                summary_diff.append((section, label, n, got))

    passed = (
        counts.get("correct", 0) == len(exp_f)
        and not spurious
        and notes_found == len(key["notes"])
        and not notes_extra
        and not summary_diff
    )
    return {
        "expected": len(exp_f),
        "output": len(out_f),
        "counts": dict(counts),
        "per_feature": per_feature,
        "spurious": [(_s(o.get("Feature ID")), _s(o.get("Feature type")), _s(o.get("Text as shown on drawing")))
                     for o in spurious],
        "field_accuracy": {k: (field_ok[k], len(pairs)) for k in FIELDS},
        "notes": {"expected": len(key["notes"]), "found": notes_found, "missing": notes_missing,
                  "extra": notes_extra},
        "summary_diff": summary_diff,
        "passed": passed,
    }


def format_report(r: dict, verbose: bool = True) -> str:
    L = []
    c = r["counts"]
    L.append(f"Features: expected {r['expected']}, output {r['output']} | correct {c.get('correct', 0)}, "
             f"partial {c.get('partial', 0)}, wrong {c.get('wrong', 0)}, missed {c.get('missed', 0)}, "
             f"spurious {len(r['spurious'])}")
    n = r["notes"]
    L.append(f"Notes: {n['found']}/{n['expected']} found, {len(n['extra'])} extra non-heading rows")
    L.append(f"Summary differences: {len(r['summary_diff'])}")
    L.append("PASS" if r["passed"] else "FAIL")
    if verbose:
        L.append("\nPer-field accuracy (over matched features):")
        for k, (ok, tot) in r["field_accuracy"].items():
            L.append(f"  {k:<24} {ok}/{tot}")
        L.append("\nPer feature:")
        for fid, v in r["per_feature"].items():
            if v["status"] == "correct":
                L.append(f"  {fid}: correct ({v['output']})")
            else:
                L.append(f"  {fid}: {v['status']} ({v.get('output')}) {v.get('diff', '')}")
        if r["spurious"]:
            L.append("\nSpurious output rows:")
            L.extend(f"  {s}" for s in r["spurious"])
        if n["missing"]:
            L.append("\nMissing notes:")
            L.extend(f"  {m}" for m in n["missing"])
        if n["extra"]:
            L.append("\nExtra (non-heading) notes:")
            L.extend(f"  {m}" for m in n["extra"])
        if r["summary_diff"]:
            L.append("\nSummary differences (section, label, key, output):")
            L.extend(f"  {d}" for d in r["summary_diff"])
    return "\n".join(L)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run_pipeline(pdf: Path) -> bytes:
    from app.services.tvs_extraction_report import build_tvs_extraction_from_pdf

    return build_tvs_extraction_from_pdf(pdf)


if __name__ == "__main__":
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURES / "SMX-DEMO-HF-001.pdf"
    key = Path(sys.argv[2]) if len(sys.argv) > 2 else FIXTURES / "SMX-DEMO-HF-001_expected_extraction.xlsx"
    report = evaluate(run_pipeline(pdf), key)
    print(format_report(report))
    sys.exit(0 if report["passed"] else 1)
