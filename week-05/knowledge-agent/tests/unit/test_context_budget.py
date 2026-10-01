"""Context budget ``heuristic-v1``: selection, dropping and overflow (D22-04/07)."""

from __future__ import annotations

import math

import pytest

from knowledge_agent.chat.context import BUDGET_METHOD, ContextBudget
from knowledge_agent.domain.errors import ContextOverflow


def _candidate(chunk_id: str, text: str, rank: int = 1) -> dict:
    return {"rank": rank, "chunk_id": chunk_id, "text": text, "metadata": {"section_path": "S"}}


def test_estimate_is_utf8_ceil_over_chars_per_token():
    budget = ContextBudget(chars_per_token=3)
    assert budget.estimate("") == 0
    assert budget.estimate("abc") == 1
    assert budget.estimate("abcd") == 2
    assert budget.estimate("привет") == math.ceil(len("привет".encode("utf-8")) / 3)


def test_passed_is_a_subset_and_whole_chunks_are_kept_or_dropped():
    budget = ContextBudget(
        max_context_tokens=60, reserved_output_tokens=0, chars_per_token=1, safety_margin=0
    )
    candidates = [
        _candidate("a" * 64, "x" * 30, rank=1),
        _candidate("b" * 64, "y" * 20, rank=2),
        _candidate("c" * 64, "z" * 30, rank=3),
    ]
    plan = budget.plan(mandatory_texts=["q"], candidates=candidates)
    assert plan.budget_method == BUDGET_METHOD
    passed_ids = [item["chunk_id"] for item in plan.passed]
    assert set(passed_ids) <= {candidate["chunk_id"] for candidate in candidates}
    # The first chunk (30) plus mandatory (1) fits; the second (20) fits; the
    # third would exceed and is dropped whole.
    assert passed_ids == ["a" * 64, "b" * 64]
    assert plan.dropped_chunks == 1
    assert all(item["estimated_tokens"] == 30 or item["estimated_tokens"] == 20 for item in plan.passed)


def test_overflow_raises_before_any_selection():
    budget = ContextBudget(max_context_tokens=10, reserved_output_tokens=0, chars_per_token=1)
    with pytest.raises(ContextOverflow) as excinfo:
        budget.plan(mandatory_texts=["instruction that is far too long"], candidates=[])
    assert excinfo.value.code == "context_overflow"


def test_request_budget_can_only_shrink_and_model_length_is_a_cap():
    budget = ContextBudget(max_context_tokens=1000, reserved_output_tokens=0, chars_per_token=1)
    assert budget.effective_context_tokens(None) == 1000
    assert budget.effective_context_tokens(500) == 500
    # A larger request value never increases the configured budget.
    assert budget.effective_context_tokens(5000) == 1000
    capped = ContextBudget(
        max_context_tokens=1000, reserved_output_tokens=0, chars_per_token=1, model_context_length=300
    )
    assert capped.effective_context_tokens(800) == 300
    assert capped.effective_context_tokens(100) == 100


def test_zero_candidates_produce_empty_plan():
    budget = ContextBudget(max_context_tokens=100, reserved_output_tokens=10, chars_per_token=1)
    plan = budget.plan(mandatory_texts=["q"], candidates=[])
    assert plan.passed == []
    assert plan.dropped_chunks == 0
