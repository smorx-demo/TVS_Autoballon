"""Ballooning uses the drawing extractor: one balloon per TVS-report feature."""

from __future__ import annotations

from tests.eval_against_key import FIXTURES
from app.services.autoballoon_engine import balloon_pdf
from app.services.tvs_extractor import extract_drawing


def test_balloons_match_extracted_features():
    pdf = FIXTURES / "SMX-DEMO-HF-001.pdf"
    _out, chars, log, _ext = balloon_pdf(pdf.read_bytes())
    feats = extract_drawing(pdf).features
    assert log[0]["mode"] == "tvs_extractor"
    assert len(chars) == len(feats)
    assert sorted((c.page, c.text) for c in chars) == sorted((f.sheet, f.text) for f in feats)
    assert [c.balloon for c in chars] == list(range(1, len(chars) + 1))


def test_legacy_engine_still_available(monkeypatch):
    monkeypatch.setenv("SMORX_BALLOON_ENGINE", "legacy")
    _out, chars, log, _ext = balloon_pdf((FIXTURES / "SMX-DEMO-HF-001.pdf").read_bytes())
    assert chars and log[0]["mode"] != "tvs_extractor"
