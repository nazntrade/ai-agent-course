"""D23-C05: the context budget records which chunk ids were dropped."""

from __future__ import annotations

from knowledge_agent.chat.context import ContextBudget


def test_dropped_ids_lists_chunks_excluded_by_the_budget():
    budget = ContextBudget(
        max_context_tokens=100,
        reserved_output_tokens=0,
        chars_per_token=1,
        safety_margin=0,
    )
    plan = budget.plan(
        mandatory_texts=["system"],
        candidates=[
            {"rank": 1, "chunk_id": "a", "text": "x" * 40, "metadata": {}},
            {"rank": 2, "chunk_id": "b", "text": "y" * 400, "metadata": {}},
        ],
    )
    assert [item["chunk_id"] for item in plan.passed] == ["a"]
    assert plan.dropped_chunks == 1
    assert plan.dropped_ids == ["b"]
