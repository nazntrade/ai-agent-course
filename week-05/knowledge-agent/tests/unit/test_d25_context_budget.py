"""D25 context budget: memory and bounded history enter the actual request."""

from __future__ import annotations

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.context import ContextBudget
from knowledge_agent.chat.prompts import build_context_block
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.contracts import ChatMessage
from knowledge_agent.domain.errors import ContextOverflow

from tests.helpers import FakeChatModel, FakeKnowledge, fragment


def _service(tmp_path, knowledge=None, chat=None, **kwargs):
    return ChatService(
        knowledge or FakeKnowledge(fragments=[fragment("a" * 64, "memory chunk text")]),
        chat or FakeChatModel(text="answer"),
        FileChatRunStore(tmp_path / "runs"),
        **kwargs,
    )


def _request(**overrides):
    payload = {
        "mode": "with_rag",
        "collection_id": "c1",
        "question": "search query",
        "original_query": "original question",
        "top_k": 3,
        "grounding": False,
        "max_context_tokens": 4096,
    }
    payload.update(overrides)
    return payload


def test_mandatory_parts_count_memory_history_question_and_fragments(tmp_path):
    chat = FakeChatModel(text="answer")
    service = _service(tmp_path, chat=chat)
    record = service.conversation_turn(
        _request(
            memory_text="<task_memory>\ngoal: learn\n</task_memory>",
            history_messages=[
                ChatMessage("user", "previous question"),
                ChatMessage("assistant", "previous answer"),
            ],
        )
    )
    parts = record["context"]["mandatory_parts"]
    assert parts["task_memory_tokens"] > 0
    assert parts["selected_history_tokens"] > 0
    assert parts["question_tokens"] > 0
    assert parts["passed_fragments_tokens"] > 0
    assert record["context"]["history_turns_used"] == 2

    messages = chat.calls[0]
    contents = [message.content for message in messages]
    assert any("goal: learn" in content for content in contents)
    assert "previous question" in contents
    assert "previous answer" in contents
    assert contents[-1] == "original question"


def test_query_is_retrieval_query_but_generation_uses_original(tmp_path):
    knowledge = FakeKnowledge(fragments=[fragment("a" * 64)])
    service = _service(tmp_path, knowledge=knowledge)
    record = service.conversation_turn(
        _request(memory_text=None, question="resolved search query", original_query="а второй вариант?")
    )
    assert knowledge.search_calls == 1
    assert record["search_query"] == "resolved search query"
    assert record["original_query"] == "а второй вариант?"


def test_memory_that_cannot_fit_overflows_before_generation(tmp_path):
    chat = FakeChatModel(text="answer")
    service = _service(tmp_path, chat=chat)
    with pytest.raises(ContextOverflow):
        service.conversation_turn(
            _request(memory_text="goal: " + "x" * 100000, max_context_tokens=200)
        )
    assert chat.calls == []


# -- C17 regression: the rendered <context> block is part of the real request --


def _chunk_candidate(text: str = "x" * 20) -> dict:
    return {
        "rank": 1,
        "chunk_id": "a" * 64,
        "text": text,
        "metadata": {
            "section_path": "Section",
            "page_start": 1,
            "page_end": 1,
            "source_label": "sample.txt",
        },
    }


def test_prompt_tokens_estimated_includes_the_rendered_context_wrapper():
    # Old (buggy) code budgeted only the chunk text and left the wrapper out of
    # ``prompt_tokens_estimated``. The rendered block (markup + per-chunk labels)
    # is what the model actually receives and must be included.
    budget = ContextBudget(
        max_context_tokens=10000, reserved_output_tokens=0, chars_per_token=1, safety_margin=0
    )
    candidate = _chunk_candidate()
    plan = budget.plan(
        mandatory_texts=["system", "question"],
        candidates=[candidate],
        context_renderer=build_context_block,
    )
    other = budget.estimate("system") + budget.estimate("question")
    rendered = budget.estimate(build_context_block(plan.passed))
    assert plan.prompt_tokens_estimated == other + rendered
    # Discriminator: text-only accounting is strictly smaller because the
    # ``<context>``/``</context>`` markup and the provenance labels are real.
    text_only = other + budget.estimate(candidate["text"])
    assert plan.prompt_tokens_estimated > text_only
    assert rendered > budget.estimate(candidate["text"])


def test_prompt_estimate_matches_mandatory_parts_including_wrappers(tmp_path):
    # The saved turn projection must be self-consistent: the reported estimate
    # of the outgoing request equals the sum of its mandatory parts, wrappers
    # included (the original defect left ``wrappers_tokens`` out).
    service = _service(tmp_path)
    record = service.conversation_turn(
        _request(
            memory_text="<task_memory>\ngoal: learn\n</task_memory>",
            history_messages=[ChatMessage("user", "previous question")],
        )
    )
    context = record["context"]
    parts = context["mandatory_parts"]
    assert parts["wrappers_tokens"] > 0
    assert context["prompt_tokens_estimated"] == sum(parts.values())


def test_budget_that_fits_chunk_text_but_not_the_wrapper_overflows():
    # A budget that fits the instruction plus the raw chunk text (bytes=1 token)
    # is still too small for the wrapper block that is actually sent, so the
    # budget must reject it before any model call instead of under-counting.
    candidate = _chunk_candidate("x" * 5)
    other = 11  # len("instruction") with chars_per_token=1
    text_tokens = len(candidate["text"])
    budget = ContextBudget(
        max_context_tokens=other + text_tokens,
        reserved_output_tokens=0,
        chars_per_token=1,
        safety_margin=0,
    )

    # Historical contract (no renderer) still fits exactly: this is the pre-fix
    # behaviour the regression discriminates against.
    legacy = budget.plan(mandatory_texts=["instruction"], candidates=[candidate])
    assert legacy.prompt_tokens_estimated == other + text_tokens
    assert [item["chunk_id"] for item in legacy.passed] == [candidate["chunk_id"]]

    # The real request includes the wrapper, which alone exceeds this budget.
    with pytest.raises(ContextOverflow):
        budget.plan(
            mandatory_texts=["instruction"],
            candidates=[candidate],
            context_renderer=build_context_block,
        )
