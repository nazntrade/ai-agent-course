"""D23-C05/C12: offline trace/comparison projections and protocol consistency."""

from __future__ import annotations

import pytest

from harness import d23_eval


def _record() -> dict:
    return {
        "run_id": "run-1",
        "original_query": "original",
        "search_query": "rewritten",
        "rewrite": {"used": True, "latency_ms": 12.0, "usage": None},
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        "latency_ms": {"retrieval": 1.0, "context": 0.5, "chat": 9.0, "total": 22.5},
        "answer": {
            "text": "answer text",
            "finish_reason": "stop",
            "truncated": False,
            "insufficient_sources": False,
            "citations": {"valid": ["a"], "unsupported": []},
        },
        "retrieval": {
            "candidates": [{"rank": 1, "score": 0.8, "chunk_id": "a", "metadata": {}}],
            "selected": [{"rank": 1, "score": 0.8, "chunk_id": "a", "metadata": {}}],
            "passed": [{"rank": 1, "chunk_id": "a", "estimated_tokens": 5, "metadata": {}}],
            "found_count": 1,
            "selected_count": 1,
            "passed_count": 1,
            "exclusion_reasons": {"threshold": [], "top_k": [], "context_budget": []},
        },
        "errors": [],
    }


def test_trace_sample_shape():
    trace = d23_eval.trace_sample(_record())
    assert trace["original_query"] == "original"
    assert trace["search_query"] == "rewritten"
    assert isinstance(trace["candidates"], list) and trace["candidates"]
    assert isinstance(trace["exclusion_reasons"], dict)
    assert trace["counts"]["found_count"] == 1
    assert trace["schema_version"] == "d23-trace-v1"


def test_comparison_answer_projection_is_flat():
    answer = d23_eval.comparison_answer(_record(), question_id="D22-Q01", mode="D")
    assert answer["question_id"] == "D22-Q01" and answer["mode"] == "D"
    assert answer["usage"]["input_tokens"] == 3
    assert answer["latency_ms"]["chat"] == 9.0
    assert answer["retrieval"]["selected_chunk_ids"] == ["a"]
    assert answer["retrieval"]["passed_chunk_ids"] == ["a"]


def _answer(question: str, mode: str, passed: int) -> dict:
    return {"question_id": question, "mode": mode, "retrieval": {"passed_count": passed}}


def test_passed_count_table_is_derived_from_saved_answers():
    answers = [
        _answer("D22-Q01", "A", 5),
        _answer("D22-Q01", "B", 5),
        _answer("D22-Q01", "C", 5),
        _answer("D22-Q01", "D", 2),
    ]
    table = d23_eval.passed_count_table(answers)
    assert table == {"D22-Q01": {"A": 5, "B": 5, "C": 5, "D": 2}}
    # The same deterministic projection must satisfy a declared protocol table.
    d23_eval.assert_passed_count_table(answers, {"D22-Q01": {"A": 5, "B": 5, "C": 5, "D": 2}})


def test_passed_count_table_detects_a_protocol_mismatch():
    answers = [_answer("D22-Q07", "D", 1)]
    with pytest.raises(AssertionError, match="D22-Q07 mode D"):
        d23_eval.assert_passed_count_table(answers, {"D22-Q07": {"D": 5}})
