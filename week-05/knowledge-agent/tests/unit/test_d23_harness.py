"""D23 harness modules compile and LIVE stays opt-in (never fake inference)."""

from __future__ import annotations

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import d23_eval  # noqa: E402
import d23_live  # noqa: E402


def test_d23_harness_modules_import():
    assert d23_eval.trace_sample is not None
    assert d23_eval.comparison_answer is not None
    assert d23_live.main is not None


def test_trace_priority_prefers_filtered_branches():
    with_threshold = {"retrieval": {"candidates": [1], "exclusion_reasons": {"threshold": ["a"]}}}
    filtered = {"retrieval": {"candidates": [1], "exclusion_reasons": {"threshold": []}}}
    plain = {"retrieval": {"candidates": [1], "exclusion_reasons": {"threshold": []}}}
    assert d23_live.trace_priority_for(with_threshold, "B", [1]) == 0
    assert d23_live.trace_priority_for(filtered, "D", [1]) == 1
    assert d23_live.trace_priority_for(plain, "A", [1]) == 2
    assert d23_live.trace_priority_for(plain, "A", []) is None


def _answer(passed_count: int) -> dict:
    return {
        "retrieval": {"passed_count": passed_count},
        "citations": {"valid": [], "unsupported": []},
        "truncated": False,
        "insufficient_sources": passed_count == 0,
    }


def test_summarize_retrieval_passed_rate_counts_empty_context_as_miss():
    summary = d23_live._summarize([_answer(5), _answer(5), _answer(0), _answer(1)])
    assert summary["retrieval_passed_rate"] == 0.75
    assert summary["retrieval_passed_rate"] != 1.0
    # Real artifact shape: 38 of 40 answers received context.
    answers = [_answer(5)] * 38 + [_answer(0)] * 2
    assert d23_live._summarize(answers)["retrieval_passed_rate"] == 0.95
    assert d23_live._summarize([])["retrieval_passed_rate"] is None


def test_d23_live_requires_a_profile(monkeypatch, capsys):
    for key in list(__import__("os").environ):
        if key.startswith("AI_TEST_MODEL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("AI_TEST_LIVE_POLICY", raising=False)
    assert d23_live.main(argv=[]) == 3
    assert "BLOCKED" in capsys.readouterr().out
