"""
Ballooned PDF <-> TVS extraction Excel <-> inspection report:
  1. consistency  — each Excel row's Balloon No. is the number of the balloon whose leader ends on
                    that feature's annotation box in the generated PDF (read back from the PDF)
  2. overlap      — no balloon over an annotation box, drawing text or another balloon; no leader
                    ending on a balloon
  3. title block  — full part name / material / mass in the Excel Summary and the inspection report
  4. regression   — Extraction values unchanged (snapshot; row order and the new column ignored)
  5. numbering    — one number per feature, zone order, same field for every output
"""

from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path

import fitz
import openpyxl
import pytest

from tests.eval_against_key import FIXTURES
from tools.balloon_deliverables import build_all, consistency, overlaps

DRAWINGS = ["SMX-DEMO-HF-001", "SMX-DEMO-002"]
SNAPSHOTS = FIXTURES / "snapshots"


@pytest.fixture(scope="module", params=DRAWINGS)
def built(request):
    src = FIXTURES / f"{request.param}.pdf"
    return src, build_all(src)


def _sheet_rows(xlsx: bytes, name: str) -> list[dict]:
    ws = openpyxl.load_workbook(io.BytesIO(xlsx), data_only=True)[name]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    return [dict(zip(hdr, r)) for r in it]


def test_excel_and_pdf_balloon_numbers_match(built):
    src, b = built
    r = consistency(src, b)
    assert r["drawn"] == r["rows"], r
    assert r["ok"] == r["rows"], r["bad"]


def test_no_balloon_overlaps(built):
    src, b = built
    assert overlaps(src, b) == []


def test_excel_rows_listed_by_balloon_number(built):
    _src, b = built
    rows = _sheet_rows(b["xlsx"], "Extraction")
    nos = [r["Balloon No."] for r in rows]
    assert nos == list(range(1, len(rows) + 1))
    hdr = list(rows[0])
    assert hdr[:2] == ["Balloon No.", "Feature ID"]
    # inspection report rows carry the same numbers for the same features
    assert sorted((x["balloon"], x["text"]) for x in b["balloons"]) == \
        sorted((r["Balloon No."], r["Text as shown on drawing"]) for r in rows)
    assert [x["balloon"] for x in b["report_rows"]] == sorted(x["balloon"] for x in b["balloons"])


def test_summary_lists_balloon_numbers(built):
    _src, b = built
    ws = openpyxl.load_workbook(io.BytesIO(b["xlsx"]), data_only=True)["Summary"]
    rows = [r for r in ws.iter_rows(values_only=True)]
    head = next(r for r in rows if r[0] == "Scope item")
    assert head[2] == "Balloon Nos."
    ex = {r["Balloon No."] for r in _sheet_rows(b["xlsx"], "Extraction")}
    i = rows.index(head)
    for r in rows[i + 1:]:
        if r[0] in ("Notes & textual information", "Unaccounted text runs"):
            break
        listed = [int(x) for x in str(r[2] or "").split(",") if x.strip()]
        assert len(listed) == r[1] and set(listed) <= ex, r


def test_summary_lists_balloons_by_zone(built):
    """Summary 'Balloons by zone': every Extraction balloon exactly once, under its own sheet + zone."""
    _src, b = built
    rows = [r for r in openpyxl.load_workbook(io.BytesIO(b["xlsx"]), data_only=True)["Summary"].iter_rows(values_only=True)]
    i = next(k for k, r in enumerate(rows) if r[0] == "Balloons by zone")
    assert rows[i + 1][:3] == ("Sheet / zone", "Count", "Balloon Nos.")
    listed: dict[int, str] = {}
    for r in rows[i + 2:]:
        if r[0] is None:
            break
        nos = [int(x) for x in str(r[2]).split(",")]
        assert len(nos) == r[1]
        for n in nos:
            assert n not in listed
            listed[n] = r[0]
    ex = _sheet_rows(b["xlsx"], "Extraction")
    assert set(listed) == {r["Balloon No."] for r in ex}
    for r in ex:
        assert listed[r["Balloon No."]] == f"Sheet {r['Sheet']} · Zone {r['Zone']}"


def test_title_block_fields_in_summary_and_report(built):
    src, b = built
    from app.services.title_block_extract import title_block_cells

    cells = title_block_cells(fitz.open(str(src))[0])
    title = cells["TITLE"]
    material = cells["MATERIAL"]
    meta = b["meta"]
    assert meta["part_name"] == title and len(title.split()) > 1
    assert meta["material"] == material
    assert meta["mass"] and meta["mass"][0].isdigit() and "kg" in meta["mass"]
    for k in ("part_name", "material", "mass", "customer_name"):
        assert meta.get(k, "") not in ("UNITS / MASS", "P/N", "MASS", "TITLE")
    ws = openpyxl.load_workbook(io.BytesIO(b["xlsx"]), data_only=True)["Summary"]
    head = " ".join(str(ws.cell(row=i, column=1).value or "") for i in (2, 3))
    assert title in head and material in head and meta["mass"] in head
    report_text = " ".join(p.get_text() for p in fitz.open(stream=b["report"], filetype="pdf"))
    report_text = " ".join(report_text.split())
    for v in (title, material, meta["mass"]):
        assert v in report_text, v


_GT_FIELDS = ("Upper tolerance", "Lower tolerance", "Max limit", "Min limit")


def _strip_general_tolerance(r: dict) -> dict:
    """Snapshot view of a row: general-tolerance fields of open dimensions removed (they are new)."""
    notes = str(r.get("Notes") or "")
    if "Tolerance source:" not in notes:
        return r
    r = {k: v for k, v in r.items() if k not in _GT_FIELDS}
    base = notes.split("Tolerance source:")[0].rstrip("; ").strip()
    r["Notes"] = base or None
    return r


def test_extraction_values_unchanged(built):
    """Everything but the new open-dimension tolerance fields is identical to the snapshot."""
    src, b = built
    snap = json.loads((SNAPSHOTS / f"{src.stem}_extraction.json").read_text(encoding="utf-8"))

    def key(r: dict, drop_gt: bool) -> tuple:
        r = _strip_general_tolerance(r)
        return tuple((k, str(v)) for k, v in sorted(r.items())
                     if k != "Balloon No." and not (drop_gt and k in _GT_FIELDS))

    now = _sheet_rows(b["xlsx"], "Extraction")
    gt_rows = {r["Text as shown on drawing"] for r in now if "Tolerance source:" in str(r.get("Notes") or "")}
    snap_cmp = [r if r["Text as shown on drawing"] not in gt_rows else {k: v for k, v in r.items() if k not in _GT_FIELDS}
                for r in snap]
    assert Counter(key(r, False) for r in now) == Counter(key(r, False) for r in snap_cmp)


def test_extraction_unchanged_with_general_tolerances_off(built):
    src, b = built
    from app.services.tvs_extraction_report import build_tvs_extraction_xlsx

    snap = json.loads((SNAPSHOTS / f"{src.stem}_extraction.json").read_text(encoding="utf-8"))
    off = _sheet_rows(build_tvs_extraction_xlsx(dict(b["meta"]), src, general_tolerances=False), "Extraction")

    def key(r: dict) -> tuple:
        return tuple((k, str(v)) for k, v in sorted(r.items()) if k != "Balloon No.")

    assert Counter(key(r) for r in off) == Counter(key(r) for r in snap)


def test_numbering_follows_sheet_then_zone(built):
    src, _b = built
    from app.services.balloon_numbering import zone_key
    from app.services.tvs_extractor import extract_drawing

    feats = sorted(extract_drawing(src).features, key=lambda f: f.balloon)
    assert [f.balloon for f in feats] == list(range(1, len(feats) + 1))
    keys = [(f.sheet,) + zone_key(f.meta["zone"]) for f in feats]
    assert keys == sorted(keys)


# ----------------------------------------------------------------------------- unit tests

def test_label_text_is_never_a_value():
    from app.services.title_block_extract import _valid_value, fields_from_cells, is_label_text

    assert is_label_text("UNITS / MASS") and is_label_text("P/N") and is_label_text("CUSTOMER P/N")
    assert not is_label_text("STEERING KNUCKLE BRACKET")
    for k in ("mass", "customer_name", "part_name", "material"):
        assert not _valid_value(k, "UNITS / MASS")
    f = fields_from_cells({"TITLE": "STEERING KNUCKLE BRACKET - LH", "MATERIAL": "EN8D", "UNITS / MASS": "mm / 1,25 kg",
                           "CUSTOMER P/N": "X-1"})
    assert f == {"part_name": "STEERING KNUCKLE BRACKET - LH", "material": "EN8D", "mass": "1,25 kg"}
    assert fields_from_cells({"MASS": ""}) == {}


def test_stale_title_meta_is_repaired():
    from app.services.title_block_extract import merge_title_meta

    extracted = {"part_name": "STEERING KNUCKLE BRACKET", "material": "EN8D", "mass": "1.2 kg"}
    meta = {"part_name": "STEERING", "material": "STEERING KNUCKLE BRACKET", "mass": "UNITS / MASS"}
    merge_title_meta(meta, extracted)
    assert meta["part_name"] == "STEERING KNUCKLE BRACKET"
    assert meta["material"] == "EN8D" and meta["mass"] == "1.2 kg"
    user = {"part_name": "My edited name"}
    merge_title_meta(user, extracted)
    assert user["part_name"] == "My edited name"


def test_balloon_order_position_mode(monkeypatch):
    from app.services.tvs_extractor import extract_drawing

    monkeypatch.setenv("TVS_BALLOON_ORDER", "position")
    feats = sorted(extract_drawing(FIXTURES / "SMX-DEMO-HF-001.pdf").features, key=lambda f: f.balloon)
    assert [f.balloon for f in feats] == list(range(1, len(feats) + 1))
    assert [f.sheet for f in feats] == sorted(f.sheet for f in feats)
