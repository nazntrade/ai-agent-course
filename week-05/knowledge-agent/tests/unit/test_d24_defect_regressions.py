"""D24 defect regressions: context-prompt budget, inline refs and limitation.

Every test here is written so the *old* implementation fails: the assertions
contradict the pre-fix behaviour of ``ChatService._plan`` (budget ignored
``GROUNDED_RAG.system``), of ``GroundingVerifier.verify`` (inline ``[chunk_id]``
references were never checked) and of its status ordering (a ``limitation`` made
an answer ``partial`` even without a single verified citation).
"""

from __future__ import annotations

import json

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.citations import GroundingVerifier
from knowledge_agent.chat.context import ContextBudget
from knowledge_agent.chat.prompts import GROUNDED_RAG, RAG
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.contracts import GroundedAnswer
from knowledge_agent.domain.errors import ContextOverflow
from tests.helpers import FakeChatModel, FakeKnowledge, fragment

PASSED = "a" * 64
OTHER = "b" * 64
UNKNOWN = "c" * 64
PASSED_TEXT = "Memory stores observations. Planning decomposes goals into steps."
OTHER_TEXT = "Tools extend agent abilities. Planning sequences actions."
PASSED_QUOTE = "Memory stores observations."
OTHER_QUOTE = "Tools extend agent abilities."


def _passed():
    return [
        {
            "chunk_id": PASSED,
            "text": PASSED_TEXT,
            "metadata": {
                "source_label": "agents.md",
                "section_path": "2.1 Memory",
                "page_start": 5,
                "page_end": 6,
            },
        },
        {
            "chunk_id": OTHER,
            "text": OTHER_TEXT,
            "metadata": {
                "source_label": "agents.md",
                "section_path": "2.2 Tools",
                "page_start": 7,
                "page_end": 8,
            },
        },
    ]


def _verify(answer: str, citations, **kwargs):
    grounded = GroundedAnswer(answer=answer, citations=citations, **kwargs)
    return GroundingVerifier(_passed()).verify(grounded, threshold=0.45)


def _grounded_text(answer: str, citations, *, limitation=None) -> str:
    return json.dumps(
        {
            "answer": answer,
            "citations": citations,
            "insufficient": False,
            "limitation": limitation,
        }
    )


def _grounded_service(tmp_path, text: str, *, finish_reason: str = "stop") -> ChatService:
    knowledge = FakeKnowledge(
        fragments=[fragment(PASSED, text=PASSED_TEXT, rank=1, score=0.7)]
    )
    return ChatService(
        knowledge,
        FakeChatModel(text=text, finish_reason=finish_reason),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=True,
    )


# -- F01: the budget counts the actually sent system prompt ------------------


def _budget_service(tmp_path, max_context_tokens: int, *, grounding: bool) -> ChatService:
    knowledge = FakeKnowledge(
        fragments=[fragment(PASSED, text="", rank=1, score=0.7)]
    )
    return ChatService(
        knowledge,
        FakeChatModel(text="free"),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=grounding,
        max_context_tokens=max_context_tokens,
        reserved_output_tokens=0,
        safety_margin=0,
        chars_per_token=3,
    )


def test_grounded_system_prompt_overflows_budget_that_fits_plain_rag(tmp_path):
    budget = ContextBudget(chars_per_token=3)
    rag_tokens = budget.estimate(RAG.system)
    grounded_tokens = budget.estimate(GROUNDED_RAG.system)
    question = "q"
    question_tokens = budget.estimate(question)
    assert grounded_tokens > rag_tokens
    tight = grounded_tokens + question_tokens - 1
    assert tight >= rag_tokens + question_tokens

    # Old code budgeted RAG.system here, so the grounded system prompt silently
    # overflowed an already-exhausted budget and no error was raised.
    with pytest.raises(ContextOverflow):
        _budget_service(tmp_path, tight, grounding=True).prepare(
            {"mode": "with_rag", "question": question, "collection_id": "c1"}
        )

    # Without grounding the same budget still fits RAG.system: unchanged.
    plan = _budget_service(tmp_path, tight, grounding=False).prepare(
        {"mode": "with_rag", "question": question, "collection_id": "c1"}
    )
    assert plan["template"].template_id == RAG.template_id
    assert plan["context"]["prompt_tokens_estimated"] == rag_tokens + question_tokens


def test_grounded_plan_counts_the_grounded_system_prompt(tmp_path):
    budget = ContextBudget(chars_per_token=3)
    rag_tokens = budget.estimate(RAG.system)
    grounded_tokens = budget.estimate(GROUNDED_RAG.system)
    question = "q"
    question_tokens = budget.estimate(question)
    exact = grounded_tokens + question_tokens
    assert exact > rag_tokens + question_tokens

    plan = _budget_service(tmp_path, exact, grounding=True).prepare(
        {"mode": "with_rag", "question": question, "collection_id": "c1"}
    )
    assert plan["template"].template_id == GROUNDED_RAG.template_id
    # Old code reported rag_tokens + question_tokens; the estimate must match the
    # grounded system prompt that is really sent.
    assert plan["context"]["prompt_tokens_estimated"] == exact


# -- F02: inline [chunk_id] must be passed AND have a verified citation ------


def test_consistent_inline_ref_with_verified_citation_is_verified():
    result = _verify(
        f"Answer [{PASSED}].",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
    )
    assert result.status == "verified"
    assert result.inline_unsupported == []
    assert result.inline_missing_quote == []


def test_unknown_inline_ref_with_valid_citation_is_not_verified():
    # Old code returned "verified": the verifier only looked at the structured
    # citation and ignored the unknown inline reference entirely.
    result = _verify(
        f"Answer [{PASSED}] and [{UNKNOWN}].",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
    )
    assert result.status != "verified"
    assert result.status == "partial"
    assert result.reason == "unsupported_citation"
    assert UNKNOWN in result.inline_unsupported


def test_inline_ref_to_passed_id_without_citation_is_not_verified():
    # OTHER was passed to the model but has no structured citation; a bare inline
    # reference is not proof. Old code returned "verified".
    result = _verify(
        f"Answer [{PASSED}] and [{OTHER}].",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
    )
    assert result.status != "verified"
    assert result.status == "partial"
    assert result.reason == "missing_quote"
    assert OTHER in result.inline_missing_quote


def test_unknown_inline_ref_without_any_valid_citation_is_failed():
    result = _verify(
        f"Answer [{UNKNOWN}].",
        [{"chunk_id": PASSED, "quote": "some sentence that is absent"}],
    )
    assert result.status == "failed"
    assert result.status != "verified"
    assert UNKNOWN in result.inline_unsupported


def test_inline_ref_backed_only_by_a_mismatched_citation_is_failed():
    result = _verify(
        f"Answer [{PASSED}].",
        [{"chunk_id": PASSED, "quote": "This sentence is not in the fragment."}],
    )
    assert result.status == "failed"
    assert result.reason == "missing_quote"
    assert PASSED in result.inline_missing_quote


# -- F03: no citations never becomes partial because of a limitation ---------


def test_no_citations_with_null_limitation_is_failed_no_citations():
    result = _verify("Answer.", [], limitation=None)
    assert result.status == "failed"
    assert result.reason == "no_citations"


def test_no_citations_with_empty_limitation_is_failed_no_citations():
    # Old code: limitation "" is not None -> "partial". Regression.
    result = _verify("Answer.", [], limitation="")
    assert result.status == "failed"
    assert result.reason == "no_citations"


def test_no_citations_with_nonempty_limitation_is_failed_no_citations():
    result = _verify("Answer.", [], limitation="partial: documents lack the fact")
    assert result.status == "failed"
    assert result.reason == "no_citations"


def test_whitespace_limitation_does_not_make_a_partial():
    result = _verify(
        "Answer.",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
        limitation="   ",
    )
    assert result.status == "verified"


def test_valid_citation_with_limitation_is_partial():
    result = _verify(
        "Answer.",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
        limitation="partial: documents lack the rest",
    )
    assert result.status == "partial"


# -- F05: boundary and contradictory provider answers ------------------------


def test_structured_unknown_chunk_id_is_not_confirmed():
    result = _verify(
        "Answer.",
        [{"chunk_id": UNKNOWN, "quote": PASSED_QUOTE}],
    )
    assert result.status == "failed"
    assert result.citations[0].status == "unknown_chunk_id"
    assert result.citations[0].source_exists is False


def test_structured_quote_mismatch_is_not_verified():
    result = _verify(
        "Answer.",
        [{"chunk_id": PASSED, "quote": "This sentence is not in the fragment."}],
    )
    assert result.status == "failed"
    assert result.reason == "quote_mismatch"
    assert result.citations[0].quote_verbatim is False


def test_mixed_verified_and_unknown_citations_are_partial():
    result = _verify(
        f"Answer [{PASSED}].",
        [
            {"chunk_id": PASSED, "quote": PASSED_QUOTE},
            {"chunk_id": UNKNOWN, "quote": "fabricated"},
        ],
    )
    assert result.status == "partial"
    assert result.reason == "unknown_chunk_id"
    statuses = {c.chunk_id: c.status for c in result.citations}
    assert statuses[PASSED] == "verified"
    assert statuses[UNKNOWN] == "unknown_chunk_id"


def test_translated_citation_keeps_original_and_is_verified():
    result = _verify(
        f"Answer [{PASSED}].",
        [
            {
                "chunk_id": PASSED,
                "quote": PASSED_QUOTE,
                "translation": "Память хранит наблюдения.",
            }
        ],
    )
    assert result.status == "verified"
    citation = result.citations[0]
    assert citation.is_translation is True
    assert citation.quote == PASSED_QUOTE
    assert citation.meaning_supported is None
    assert result.meaning_check == "not_performed"


def test_truncated_generation_stays_failed_even_with_consistent_inline_refs(tmp_path):
    text = _grounded_text(
        f"Partial [{PASSED}]",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
    )
    service = _grounded_service(tmp_path, text, finish_reason="length")
    answer = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})[
        "answer"
    ]
    assert answer["truncated"] is True
    assert answer["grounding"]["status"] == "failed"
    assert answer["grounding"]["reason"] == "truncated_generation"
    assert answer["grounding"]["refusal"] is None


def test_chat_service_flags_unknown_inline_reference(tmp_path):
    text = _grounded_text(
        f"Answer [{PASSED}] and [{UNKNOWN}].",
        [{"chunk_id": PASSED, "quote": PASSED_QUOTE}],
    )
    record = _grounded_service(tmp_path, text).chat(
        {"mode": "with_rag", "question": "q", "collection_id": "c1"}
    )
    grounding = record["answer"]["grounding"]
    assert grounding["status"] == "partial"
    assert grounding["status"] != "verified"
    assert UNKNOWN in grounding["inline_unsupported"]
    assert UNKNOWN in record["answer"]["citations"]["unsupported"]


def test_chat_service_empty_limitation_without_citations_is_failed(tmp_path):
    text = _grounded_text("Answer.", [], limitation="")
    record = _grounded_service(tmp_path, text).chat(
        {"mode": "with_rag", "question": "q", "collection_id": "c1"}
    )
    grounding = record["answer"]["grounding"]
    assert grounding["status"] == "failed"
    assert grounding["reason"] == "no_citations"


def test_truncated_insufficient_json_is_generation_failure_not_refusal(tmp_path):
    text = json.dumps(
        {
            "answer": "There is not enough evidence.",
            "citations": [],
            "insufficient": True,
            "limitation": "The requested fact is absent from the supplied context.",
        }
    )
    service = _grounded_service(tmp_path, text, finish_reason="length")
    answer = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})[
        "answer"
    ]
    assert answer["truncated"] is True
    assert answer["insufficient_sources"] is False
    assert answer["grounding"]["status"] == "failed"
    assert answer["grounding"]["reason"] == "truncated_generation"
    assert answer["grounding"]["refusal"] is None
