"""
STEP AP242 PMI report: pairing with the 2D drawing (PDF / DXF), STEP-only mode, and the data the STEP
reader now supplies (quantity, PCD, inclined, surface finishes, CC markers, views, notes).

Fixtures: SMX-DEMO-HF-001.pdf / _all_sheets.dxf (drawing), _PMI_AP242.step (v1: semantic PMI only,
no surface finishes, no saved views) and _PMI_AP242_v3.step (adds presentation: saved view, annotation
planes, surface-finish properties). The demo files tag PMI names with '[PMI-Sn-nnn]'; the matcher never
reads them — the tests below use them only to check the matches.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.step_pmi import parse_step, read_pmi
from app.services.step_report import (
    INF,
    SHEET_3D,
    ZONE_3D,
    build_step_report,
    hungarian,
    match,
    pair_cost,
    step_report_xlsx,
)
from app.services.tvs_extraction_report import _extraction_rows
from tests.eval_against_key import FIXTURES

PDF = FIXTURES / "SMX-DEMO-HF-001.pdf"
DXF = FIXTURES / "SMX-DEMO-HF-001_all_sheets.dxf"
V1 = FIXTURES / "SMX-DEMO-HF-001_PMI_AP242.step"
V3 = FIXTURES / "SMX-DEMO-HF-001_PMI_AP242_v3.step"
FROM_2D = ("Balloon No.", "Feature ID", "Sheet", "Zone", "X (mm)", "Y (mm)", "View", "Critical flag", "Feature type")
VALUES = ("Nominal", "Upper tolerance", "Lower tolerance", "Max limit", "Min limit",
          "Primary datum", "Secondary datum", "Tertiary datum", "Boxed / reference flag")


@pytest.fixture(scope="module")
def pdf_rows():
    return _extraction_rows(PDF)


@pytest.fixture(scope="module")
def reports(pdf_rows):
    return {
        ("v1", "pdf"): build_step_report(V1, PDF, drawing_rows=pdf_rows),
        ("v3", "pdf"): build_step_report(V3, PDF, drawing_rows=pdf_rows),
        ("v1", "dxf"): build_step_report(V1, DXF),
        ("v3", "dxf"): build_step_report(V3, DXF),
        ("v1", None): build_step_report(V1),
        ("v3", None): build_step_report(V3),
    }


def _pmi_tags(step: Path) -> dict[int, set[str]]:
    E = parse_step(step)
    return {i: {t for args in d.values() if args and isinstance(args[0], str)
                for t in re.findall(r"PMI-(S\d-\d{3})", args[0])} for i, d in E.items()}


# ----------------------------------------------------------------------------- 1. paired with the PDF

@pytest.mark.parametrize("ver", ["v1", "v3"])
def test_paired_rows_take_numbering_and_location_from_the_drawing(reports, pdf_rows, ver):
    rep = reports[(ver, "pdf")]
    by_no = {r["Balloon No."]: r for r in pdf_rows[0]}
    assert rep.mode == "paired" and rep.step_only == []
    for f in rep.features:
        d = by_no[f["Balloon No."]]
        for k in FROM_2D:
            assert f[k] == d[k], (k, f["Text as shown on drawing"])
        for k in VALUES:
            assert str(f[k]) == str(d[k]), (k, f["Text as shown on drawing"])
        assert "STEP PMI #" in (f["Notes"] or "")


def test_v3_pairs_53_of_53(reports):
    rep = reports[("v3", "pdf")]
    assert len(rep.features) == 53 and rep.missing == [] and rep.step_only == []


def test_v1_lists_every_missing_drawing_item_with_a_reason(reports):
    rep = reports[("v1", "pdf")]
    assert len(rep.features) == 46
    assert len(rep.features) + len(rep.missing) == 53
    for d, why in rep.missing:
        assert d["Feature type"] == "Surface finish" and "surface-finish" in why


@pytest.mark.parametrize("ver,step", [("v1", V1), ("v3", V3)])
def test_matches_agree_with_demo_pmi_tags(reports, pdf_rows, ver, step):
    """Test-only cross-check: the matched drawing row is the one the demo PMI tag names."""
    import openpyxl

    ws = openpyxl.load_workbook(FIXTURES / "SMX-DEMO-HF-001_expected_extraction.xlsx")
    ws = ws[[n for n in ws.sheetnames if "xtract" in n.lower()][0]]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    key = {r[hdr.index("Feature ID")]: r[hdr.index("Text as shown on drawing")] for r in it}
    tags = _pmi_tags(step)
    norm = lambda t: re.sub(r"[\s()\[\]]", "", str(t))
    checked = 0
    for f in reports[(ver, "pdf")].features:
        it_ = f["_item"]
        ids = set().union(*(tags.get(a, set()) for a in it_.aspects + [it_.eid]))
        if len(ids) != 1:
            continue
        assert norm(key[ids.pop()]) == norm(f["Text as shown on drawing"])
        checked += 1
    assert checked >= 30


# ----------------------------------------------------------------------------- 2. paired with the DXF

@pytest.mark.parametrize("ver", ["v1", "v3"])
def test_dxf_pairing_numbers_like_the_pdf(reports, ver):
    a, b = reports[(ver, "pdf")], reports[(ver, "dxf")]
    key = lambda rep: {f["_item"].eid: tuple(f[k] for k in ("Balloon No.", "Sheet", "Zone", "View", "Critical flag"))
                       for f in rep.features}
    assert key(a) == key(b)
    assert len(b.step_only) == 0 and len(b.missing) == len(a.missing)


# ----------------------------------------------------------------------------- 3. STEP-only

@pytest.mark.parametrize("ver,view", [("v1", "Model"), ("v3", "SMX_ISO_ALL_PMI")])
def test_step_only_has_no_invented_layout(reports, ver, view):
    rep = reports[(ver, None)]
    assert rep.mode == "step-only"
    for f in rep.features:
        assert f["Sheet"] == SHEET_3D and f["Zone"] == ZONE_3D
        assert f["X (mm)"] is None and f["Y (mm)"] is None
        assert f["View"] == view
        assert "3D anchor (" in f["Notes"] and "faces #" in f["Notes"] or f["_item"].general
    assert [f["Balloon No."] for f in rep.features] == list(range(1, len(rep.features) + 1))


@pytest.mark.parametrize("step", [V1, V3])
def test_step_only_numbering_is_deterministic(step):
    a = [(f["Balloon No."], f["_item"].eid) for f in build_step_report(step).features]
    b = [(f["Balloon No."], f["_item"].eid) for f in build_step_report(step).features]
    assert a == b


def test_step_only_types_quantity_finishes_and_notes(reports):
    rep = reports[("v3", None)]
    by_text = {f["Text as shown on drawing"]: f for f in rep.features}
    assert by_text["2× R6"]["Notes"].startswith("Qty 2")
    assert by_text["72 ±0.05"]["Feature type"] == "Inclined linear dimension"
    assert by_text["24 0/-0.1"]["Feature type"] == "Inclined linear dimension"
    assert by_text["Ø95"]["Feature type"] == "Basic dimension (PCD)"
    assert by_text["Ø75"]["Feature type"] == "Basic dimension (PCD)"
    finishes = [f for f in rep.features if f["Feature type"] == "Surface finish"]
    assert sorted(f["Text as shown on drawing"] for f in finishes) == sorted(
        ["Rz 6.3", "Ra 0.8", "N6", "N7", "U Ra 1.6 / L Ra 0.4", "Ra 3.2", "N8"])
    assert len(rep.notes) >= 20


def test_step_report_workbook(reports):
    import io

    import openpyxl

    for k in (("v3", "pdf"), ("v3", None)):
        rep = reports[k]
        wb = openpyxl.load_workbook(io.BytesIO(step_report_xlsx(rep, {}, PDF if k[1] else None)))
        rows = list(wb["Extraction"].iter_rows(values_only=True))
        assert len(rows) - 1 == len(rep.features)
        texts = [r[0] for r in wb["Summary"].iter_rows(values_only=True)]
        if k[1]:
            assert "Missing from STEP (drawing characteristic without PMI)" in texts
            assert wb["Notes"].max_row - 1 == len(rep.notes) and len(rep.notes) > 45
        else:
            assert not any(str(r[3]).startswith(("A", "B", "C", "D", "E", "F")) and str(r[3])[1:].isdigit()
                           for r in rows[1:])


# ----------------------------------------------------------------------------- 4. unit tests

def _row(rows, text):
    return next(r for r in rows if r["Text as shown on drawing"] == text)


def test_matcher_rejects_near_duplicates(pdf_rows):
    rows = pdf_rows[0]
    a, b = _row(rows, "16 0/-0.1"), _row(rows, "16 +0.2/0")
    assert pair_cost(a, b) >= INF and pair_cost(a, a) == 0
    perp = next(r for r in rows if r["Text as shown on drawing"].startswith("⊥"))
    par = next(r for r in rows if r["Text as shown on drawing"].startswith("∥"))
    assert pair_cost(perp, par) >= INF
    # text alone never decides: same text, different tolerance -> no match
    c = dict(a, **{"Upper tolerance": 0.1})
    assert pair_cost(a, c) >= INF
    # a context-only depth symbol (not printed in the text) does not block a match
    d = dict(_row(rows, "10 +1/0"), Symbol=None)
    assert pair_cost(d, _row(rows, "10 +1/0")) < INF


def test_matcher_is_one_to_one(pdf_rows):
    rows = pdf_rows[0]
    pairs = match(rows, list(reversed(rows)))
    assert len(pairs) == len(rows)
    assert len({j for _i, j, _c in pairs}) == len(rows)
    assert hungarian([[4, 1], [2, 3]]) == [1, 0]


def test_quantity_pcd_inclined_from_geometry():
    items = {it.text: it for it in read_pmi(V1).items}
    assert items["2× R6"].qty == 2
    assert items["Ø60 h6 0/-0.019"].qty == 1          # one cylinder modelled as two faces
    assert items["Ø95"].pcd == 4 and items["Ø75"].pcd == 3
    assert items["72 ±0.05"].inclined and items["24 0/-0.1"].inclined
    assert not items["(134.4)"].inclined and not items["52 ±0.1"].inclined


CC_STEP = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('t','',(''),(''),'','','');
FILE_SCHEMA(('AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF'));
ENDSEC;
DATA;
#1=SHAPE_ASPECT('bore','',#9,.T.);
#2=PROPERTY_DEFINITION('critical characteristic','CC 7',#1);
#3=SHAPE_ASPECT('face','',#9,.T.);
#4=PROPERTY_DEFINITION('user defined attributes','',#3);
#5=DESCRIPTIVE_REPRESENTATION_ITEM('classification','SC-2');
#6=REPRESENTATION('',(#5),#9);
#7=PROPERTY_DEFINITION_REPRESENTATION(#4,#6);
#8=SHAPE_ASPECT('plain','',#9,.T.);
ENDSEC;
END-ISO-10303-21;
"""


def test_cc_markers_attach_to_their_pmi(tmp_path):
    from app.services.step_pmi import _Graph

    p = tmp_path / "cc.step"
    p.write_text(CC_STEP, encoding="utf-8")
    G = _Graph(parse_step(p))
    assert G.critical([1]) == "CC7"
    assert G.critical([3]) == "SC2"
    assert G.critical([8]) == ""


def test_cc_conflict_is_reported(pdf_rows):
    model = read_pmi(V1)
    target = next(it for it in model.items if it.text == "Ø35 H7 +0.025/0")
    target.critical = "CC9"
    rep = build_step_report(V1, PDF, model=model, drawing_rows=pdf_rows)
    row = next(f for f in rep.features if f["_item"] is target)
    assert row["Critical flag"] == "CC1"
    assert "CC conflict: STEP CC9 vs drawing CC1" in row["Notes"]


def test_no_cc_markers_in_demo_step_files(reports):
    """The demo STEP files carry no CC markers: STEP-only reports leave the flag empty, paired ones
    take it from the drawing."""
    assert all(f["Critical flag"] == "-" for f in reports[("v3", None)].features)
    assert sum(f["Critical flag"] != "-" for f in reports[("v3", "pdf")].features) == 10


# ----------------------------------------------------------------------------- ballooned PDF of a STEP job

def test_step_sheet_balloons_carry_report_numbers(tmp_path, reports):
    """The ballooned PMI sheet of a STEP job shows the report's numbers (paired: the drawing's)."""
    from app.services.autoballoon_engine import balloon_pdf
    from app.services.step_pmi import render_pmi_sheet
    from app.services.step_report import balloon_number_lookup
    from tools.balloon_deliverables import read_balloons

    rep = build_step_report(V3, PDF)
    sheet = tmp_path / "pmi.pdf"
    render_pmi_sheet(rep.model, sheet)
    out, chars, _log, _x = balloon_pdf(sheet.read_bytes(), balloon_numbers=balloon_number_lookup(rep))
    want = sorted(int(f["Balloon No."]) for f in rep.features)
    assert sorted(c.balloon for c in chars) == want
    drawn = read_balloons(out)
    slot_of = {int(f["Balloon No."]): f["_item"].slot for f in rep.features}
    for b in drawn:
        pg, rect = slot_of[b.number]
        assert pg == b.sheet - 1 and rect.contains(b.tip), b.number


# ----------------------------------------------------------------------------- app: upload -> balloon -> report

def test_app_pairs_step_job_with_drawing_job(tmp_path, monkeypatch):
    """Upload the drawing and the STEP as separate jobs (route functions called directly; jobs in a
    temp folder): the STEP job is auto-paired by part number, its balloons carry the drawing's numbers
    and its TVS report the drawing's sheet / zone / view; pair_job='' gives the STEP-only report."""
    import asyncio
    import io

    import openpyxl
    from fastapi import UploadFile

    import app.api.routes.autoballoon as ab

    monkeypatch.setattr(ab, "JOBS", tmp_path)
    import app.job_store as job_store

    monkeypatch.setattr(job_store, "sync_job_files_to_storage", lambda *a, **k: {})  # nothing copied to app storage
    monkeypatch.setattr(job_store, "upsert_job_from_meta", lambda *a, **k: None)
    user = {"id": "test", "username": "test", "role": "super_admin", "active": True}

    def upload(path: Path) -> str:
        f = UploadFile(file=io.BytesIO(path.read_bytes()), filename=path.name)
        return asyncio.run(ab.upload_pdfs(files=[f], user=user))["job_id"]

    def report(job: str, pair_job=None) -> list[dict]:
        resp = ab.download_tvs_extraction(job, page=None, area_page=None, area_x=None, area_y=None, area_w=None,
                                          area_h=None, pair_job=pair_job, user=user)
        ws = openpyxl.load_workbook(resp.path)["Extraction"]
        it = ws.iter_rows(values_only=True)
        hdr = list(next(it))
        return [dict(zip(hdr, r)) for r in it]

    pdf_job = upload(PDF)
    step_job = upload(V3)
    res = ab.run_balloon(step_job, file_index=0, passes=1, use_zones=True, use_tiles=True,
                         numbering_mode="continuous", user=user)
    assert sorted(b["balloon"] for b in res["balloons"]) == list(range(1, 54))
    assert ab._load_meta(step_job).get("step_pair_job") == pdf_job
    ref = {r["Balloon No."]: r for r in _extraction_rows(PDF)[0]}
    rows = report(step_job)
    assert len(rows) == 53
    for d in rows:
        r = ref[d["Balloon No."]]
        assert (d["Sheet"], d["Zone"], d["View"], d["Critical flag"]) == (r["Sheet"], r["Zone"], r["View"], r["Critical flag"])
    alone = report(step_job, pair_job="")
    assert {d["Sheet"] for d in alone} == {SHEET_3D} and {d["Zone"] for d in alone} == {ZONE_3D}
