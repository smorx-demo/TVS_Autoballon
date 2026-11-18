"""Generalisation check: synthetic drawings (own answer keys) must score 100%."""

from __future__ import annotations

import pytest

from tests.eval_against_key import evaluate, format_report, run_pipeline
from tests.synth_drawings import make_drawing


@pytest.mark.parametrize("seed", [1, 2, 3, 21, 22, 23, 24, 25, 26])
def test_synthetic_drawing_matches_its_key(seed, tmp_path):
    pdf, key = tmp_path / f"synth_{seed}.pdf", tmp_path / f"synth_{seed}_key.xlsx"
    make_drawing(seed, pdf, key)
    report = evaluate(run_pipeline(pdf), key)
    assert report["passed"], format_report(report)
