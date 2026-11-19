"""STEP AP242 semantic PMI -> extracted characteristics."""

from __future__ import annotations

import collections

import pytest

from app.services.step_pmi import _parse_entity, _thread_text, has_semantic_pmi, read_pmi, step_pmi_to_pdf
from app.services.tvs_extraction_report import build_tvs_extraction_xlsx
from app.services.tvs_extractor import extract_drawing
from tests.eval_against_key import FIXTURES, _num, _s, load_workbook


def test_part21_complex_entity_parse():
    d = _parse_entity("( GEOMETRIC_TOLERANCE('','',#3545,#3548) GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE((#3544)) "
                      "GEOMETRIC_TOLERANCE_WITH_MODIFIERS((.MAXIMUM_MATERIAL_REQUIREMENT.)) POSITION_TOLERANCE() )")
    assert set(d) == {"GEOMETRIC_TOLERANCE", "GEOMETRIC_TOLERANCE_WITH_DATUM_REFERENCE",
                      "GEOMETRIC_TOLERANCE_WITH_MODIFIERS", "POSITION_TOLERANCE"}
    assert d["GEOMETRIC_TOLERANCE"][2] == 3545 and d["GEOMETRIC_TOLERANCE_WITH_MODIFIERS"][0][0].name == "MAXIMUM_MATERIAL_REQUIREMENT"


def test_thread_note_to_callout():
    assert _thread_text("4x M8x1.25-6H DEPTH 10 / DIA6.8 DEPTH 13, CSK DIA9x90deg") == [
        "4× M8×1.25-6H ↧10", "Ø6.8 ↧13, CSK Ø9×90°"]


@pytest.mark.parametrize("name, n_items", [("SMX-DEMO-HF-001", 46), ("SMX-DEMO-002", 50), ("SMX-DEMO-003", 62)])
def test_every_pmi_item_becomes_one_feature(name, n_items, tmp_path):
    step = FIXTURES / f"{name}_PMI_AP242.step"
    assert has_semantic_pmi(step)
    model = read_pmi(step)
    assert len(model.items) == n_items
    step_pmi_to_pdf(step, tmp_path / "pmi.pdf")
    ext = extract_drawing(tmp_path / "pmi.pdf")
    assert len(ext.features) == n_items and ext.orphans == []


def _sig(f):
    t = _s(f["Feature type"])
    t = "Basic dimension" if t.startswith("Basic dimension") else ("Linear dimension" if t == "Inclined linear dimension" else t)
    return (t, _num(f.get("Nominal")), _num(f.get("Upper tolerance")), _num(f.get("Lower tolerance")),
            _s(f.get("Primary datum")).upper(), _s(f.get("Secondary datum")).upper(), _s(f.get("Tertiary datum")).upper(),
            _s(f.get("Boxed / reference flag")).upper())


@pytest.mark.parametrize("name", ["SMX-DEMO-HF-001", "SMX-DEMO-002"])
def test_pmi_values_match_the_drawing_answer_key(name, tmp_path):
    """Everything the 3D model carries matches the 2D drawing's key (surface finishes are not in these models)."""
    step_pmi_to_pdf(FIXTURES / f"{name}_PMI_AP242.step", tmp_path / "pmi.pdf")
    out = load_workbook(build_tvs_extraction_xlsx({}, tmp_path / "pmi.pdf"))
    key = load_workbook(FIXTURES / f"{name}_expected_extraction.xlsx")
    pool = collections.Counter(_sig(f) for f in out["features"])
    missing = []
    for f in key["features"]:
        s = _sig(f)
        if pool[s] > 0:
            pool[s] -= 1
        else:
            missing.append(s[0])
    assert set(missing) == {"Surface finish"}
    assert +pool == collections.Counter()  # no extra characteristics
