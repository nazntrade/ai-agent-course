"""Offline D23 projections: trace sample and comparison answers (SPEC D23 13).

These helpers only reshape chat-run records; they never call a model. The LIVE
runner uses them to persist artifacts, and offline tests use them to prove the
projection without network access.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

TRACE_SCHEMA_VERSION = "d23-trace-v1"
COMPARISON_SCHEMA_VERSION = "d23-comparison-v1"
CALIBRATION_SCHEMA_VERSION = "d23-calibration-v1"

_EXCLUSION_KEYS = ("threshold", "top_k", "context_budget")


def _retrieval(record: Mapping[str, Any]) -> Mapping[str, Any]:
    retrieval = record.get("retrieval")
    return retrieval if isinstance(retrieval, Mapping) else {}


def _answer(record: Mapping[str, Any]) -> Mapping[str, Any]:
    answer = record.get("answer")
    return answer if isinstance(answer, Mapping) else {}


def _exclusion_reasons(record: Mapping[str, Any]) -> dict[str, list[str]]:
    reasons = _retrieval(record).get("exclusion_reasons") or {}
    if not isinstance(reasons, Mapping):
        reasons = {}
    return {key: list(reasons.get(key) or []) for key in _EXCLUSION_KEYS}


def trace_sample(record: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one with_rag run record into the frozen trace-sample shape."""

    retrieval = _retrieval(record)
    candidates = retrieval.get("candidates") or retrieval.get("found") or []
    selected = retrieval.get("selected") or []
    passed = retrieval.get("passed") or []
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": record.get("run_id"),
        "original_query": record.get("original_query", record.get("question")),
        "search_query": record.get("search_query", record.get("question")),
        "rewrite": record.get("rewrite"),
        "candidates": list(candidates),
        "selected": list(selected),
        "passed": list(passed),
        "counts": {
            "found_count": retrieval.get("found_count", len(candidates)),
            "selected_count": retrieval.get("selected_count", len(selected)),
            "passed_count": retrieval.get("passed_count", len(passed)),
        },
        "exclusion_reasons": _exclusion_reasons(record),
    }


def passed_count_table(answers: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int | None]]:
    """Derive ``{question_id: {mode: passed_count}}`` from saved answers.

    This is the single deterministic source for the retrieval table in
    ``quality-assessment.md``: it reads only ``retrieval.passed_count`` from
    each answer and never inspects prose.
    """

    table: dict[str, dict[str, int | None]] = {}
    for answer in answers:
        question = str(answer.get("question_id"))
        mode = str(answer.get("mode"))
        table.setdefault(question, {})[mode] = _retrieval(answer).get("passed_count")
    return table


def assert_passed_count_table(
    answers: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int | None]]:
    """Raise ``AssertionError`` when the saved answers disagree with the protocol."""

    table = passed_count_table(answers)
    for question, modes in expected.items():
        if question not in table:
            raise AssertionError(f"missing question {question}")
        for mode, value in modes.items():
            actual = table[question].get(mode)
            if actual != value:
                raise AssertionError(
                    f"passed_count mismatch for {question} mode {mode}: "
                    f"expected {value}, found {actual}"
                )
    return table


def comparison_answer(
    record: Mapping[str, Any], *, question_id: str, mode: str
) -> dict[str, Any]:
    """Project one mode branch into the comparison ``answers[]`` record."""

    retrieval = _retrieval(record)
    answer = _answer(record)
    latency = record.get("latency_ms") or {}
    selected_ids = [item.get("chunk_id") for item in (retrieval.get("selected") or [])]
    passed_ids = [item.get("chunk_id") for item in (retrieval.get("passed") or [])]
    return {
        "question_id": question_id,
        "mode": mode,
        "run_id": record.get("run_id"),
        "rag_mode": record.get("rag_mode"),
        "original_query": record.get("original_query", record.get("question")),
        "search_query": record.get("search_query", record.get("question")),
        "rewrite": record.get("rewrite"),
        "answer_text": answer.get("text"),
        "finish_reason": answer.get("finish_reason"),
        "truncated": bool(answer.get("truncated")),
        "insufficient_sources": bool(answer.get("insufficient_sources")),
        "usage": record.get("usage"),
        "latency_ms": {
            "retrieval": latency.get("retrieval"),
            "context": latency.get("context"),
            "chat": latency.get("chat"),
            "total": latency.get("total"),
        },
        "retrieval": {
            "found_count": retrieval.get("found_count", 0),
            "selected_count": retrieval.get("selected_count", 0),
            "passed_count": retrieval.get("passed_count", 0),
            "selected_chunk_ids": selected_ids,
            "passed_chunk_ids": passed_ids,
            "exclusion_reasons": _exclusion_reasons(record),
        },
        "citations": answer.get("citations") or {"valid": [], "unsupported": []},
        "errors": list(record.get("errors") or []),
    }
