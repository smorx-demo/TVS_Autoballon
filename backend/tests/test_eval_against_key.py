"""Answer-key gate for the TVS extractor (fails until the demo drawing scores 100%)."""

from __future__ import annotations

from tests.eval_against_key import FIXTURES, evaluate, format_report, run_pipeline


def test_demo_drawing_matches_answer_key():
    report = evaluate(run_pipeline(FIXTURES / "SMX-DEMO-HF-001.pdf"),
                      FIXTURES / "SMX-DEMO-HF-001_expected_extraction.xlsx")
    assert report["passed"], format_report(report)


def test_evaluator_scores_key_against_itself():
    key = FIXTURES / "SMX-DEMO-HF-001_expected_extraction.xlsx"
    report = evaluate(key, key)
    assert report["counts"].get("correct") == report["expected"] == 53
    assert not report["spurious"]
    assert report["notes"]["found"] == 36
