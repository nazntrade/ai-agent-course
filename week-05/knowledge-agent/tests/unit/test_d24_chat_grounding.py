"""D24-01..D24-13: ChatService grounded layer, refusal and error separation."""

from __future__ import annotations

import json

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.errors import ChatInvalidResponse, ChatUnavailable
from tests.helpers import FakeChatModel, FakeKnowledge, fragment

PASSED = "a" * 64
CHUNK_TEXT = "Memory stores observations. Planning decomposes goals into steps."


def _grounded(answer: str, citations, *, insufficient=False, limitation=None) -> str:
    return json.dumps(
        {
            "answer": answer,
            "citations": citations,
            "insufficient": insufficient,
            "limitation": limitation,
        }
    )


def _service(tmp_path, *, model, fragments=None, **kwargs):
    knowledge = FakeKnowledge(
        fragments=fragments
        if fragments is not None
        else [fragment(PASSED, text=CHUNK_TEXT, rank=1, score=0.7)]
    )
    store = FileChatRunStore(tmp_path / "runs")
    grounding_enabled = kwargs.pop("grounding_enabled", True)
    return (
        ChatService(knowledge, model, store, grounding_enabled=grounding_enabled, **kwargs),
        knowledge,
        store,
    )


def _chat(service, **overrides):
    payload = {"mode": "with_rag", "question": "q", "collection_id": "c1"}
    payload.update(overrides)
    return service.chat(payload)


def test_grounded_verified_answer_reports_sources_and_citations(tmp_path):
    text = _grounded(
        f"Answer [{PASSED}].",
        [{"chunk_id": PASSED, "quote": "Memory stores observations."}],
    )
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    record = _chat(service)
    grounding = record["answer"]["grounding"]
    assert record["prompt"]["template_id"] == "grounded-rag-v1"
    assert grounding["status"] == "verified"
    assert grounding["meaning_check"] == "not_performed"
    assert grounding["threshold"] == 0.0
    citation = grounding["citations"][0]
    assert citation["chunk_id"] == PASSED
    assert citation["source"] == "sample.txt"
    assert citation["section"] == "Section"
    assert citation["source_exists"] is True and citation["quote_verbatim"] is True
    assert citation["meaning_supported"] is None
    # Sources are exactly the passed chunks, never candidates.
    assert [item["chunk_id"] for item in record["retrieval"]["passed"]] == [PASSED]
    assert record["answer"]["insufficient_sources"] is False


def test_unknown_chunk_id_is_not_confirmed(tmp_path):
    text = _grounded("Answer.", [{"chunk_id": "b" * 64, "quote": CHUNK_TEXT}])
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    grounding = _chat(service)["answer"]["grounding"]
    assert grounding["status"] == "failed"
    assert grounding["citations"][0]["status"] == "unknown_chunk_id"
    assert grounding["citations"][0]["source_exists"] is False


def test_fabricated_quote_at_real_id_is_not_verified(tmp_path):
    text = _grounded("Answer.", [{"chunk_id": PASSED, "quote": "absent text"}])
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    grounding = _chat(service)["answer"]["grounding"]
    assert grounding["status"] == "failed"
    assert grounding["reason"] == "quote_mismatch"
    assert grounding["citations"][0]["quote_verbatim"] is False


def test_model_insufficient_is_an_honest_refusal(tmp_path):
    text = _grounded("Not available.", [], insufficient=True)
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    answer = _chat(service)["answer"]
    assert answer["insufficient_sources"] is True
    assert answer["grounding"]["status"] == "refused"
    assert answer["grounding"]["reason"] == "model_insufficient"
    assert answer["grounding"]["refusal"]["reason"] == "model_insufficient"


def test_limitation_is_partial_and_never_verified(tmp_path):
    text = _grounded(
        "Partial.",
        [{"chunk_id": PASSED, "quote": "Memory stores observations."}],
        limitation="partial answer: documents do not say everything.",
    )
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    grounding = _chat(service)["answer"]["grounding"]
    assert grounding["status"] == "partial"
    assert grounding["status"] != "verified"
    assert grounding["limitation"]


def test_below_threshold_refusal_calls_no_model(tmp_path):
    service, knowledge, _ = _service(
        tmp_path, model=FakeChatModel(text=_grounded("x", [])), fragments=[]
    )
    model = service.chat_model
    record = _chat(service, use_filter=True, min_score=0.9)
    assert model.calls == []
    assert knowledge.search_calls == 1
    assert record["answer"]["insufficient_sources"] is True
    grounding = record["answer"]["grounding"]
    assert grounding["status"] == "refused"
    assert grounding["reason"] == "below_threshold"
    assert grounding["threshold"] == 0.9
    assert record["retrieval"]["passed"] == []
    assert grounding["citations"] == []


def test_truncated_generation_is_failed_not_a_refusal(tmp_path):
    text = _grounded(f"Partial [{PASSED}]", [{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    service, _knowledge, _ = _service(
        tmp_path, model=FakeChatModel(text=text, finish_reason="length")
    )
    answer = _chat(service)["answer"]
    assert answer["truncated"] is True
    assert answer["insufficient_sources"] is False
    assert answer["grounding"]["status"] == "failed"
    assert answer["grounding"]["reason"] == "truncated_generation"


def test_format_error_has_priority_over_truncated(tmp_path):
    service, _knowledge, _ = _service(
        tmp_path, model=FakeChatModel(text='{"answer": "x", "citations": [', finish_reason="length")
    )
    with pytest.raises(ChatInvalidResponse) as excinfo:
        _chat(service)
    assert excinfo.value.details.get("format") == "grounded_json"
    # Safe provider metadata for diagnosis, never the answer text.
    assert excinfo.value.details.get("finish_reason") == "length"
    assert "answer_chars" in excinfo.value.details


def test_empty_provider_answer_is_an_error_not_a_refusal(tmp_path):
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text="   "))
    with pytest.raises(ChatInvalidResponse):
        _chat(service)


def test_provider_error_is_not_masked_as_refusal(tmp_path):
    service, _knowledge, _ = _service(
        tmp_path, model=FakeChatModel(error=ChatUnavailable("down"))
    )
    with pytest.raises(ChatUnavailable):
        _chat(service)


def test_grounding_disabled_restores_the_d22_rag_path(tmp_path):
    service, _knowledge, _ = _service(
        tmp_path, model=FakeChatModel(text=f"free text [{PASSED}]"), grounding_enabled=False
    )
    record = _chat(service)
    assert record["prompt"]["template_id"] == "rag-v1"
    assert "grounding" not in record["answer"]
    assert record["answer"]["citations"]["valid"] == [PASSED]


def test_request_grounding_false_overrides_the_service_setting(tmp_path):
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text="free text"))
    record = _chat(service, grounding=False)
    assert record["prompt"]["template_id"] == "rag-v1"
    assert "grounding" not in record["answer"]


def test_without_rag_never_carries_grounding(tmp_path):
    text = _grounded("plain", [])
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text="plain answer"))
    record = service.chat({"mode": "without_rag", "question": "q"})
    assert record["prompt"]["template_id"] == "plain-v1"
    assert "grounding" not in record["answer"]


def test_compare_is_never_grounded_even_when_enabled(tmp_path):
    text = _grounded(f"answer [{PASSED}]", [{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    record = service.compare({"collection_id": "c1", "question": "q"})
    assert record["comparison"]["prompt_templates"]["with_rag"] == "rag-v1"
    assert "grounding" not in record["branches"]["with_rag"]["answer"]
    assert "grounding" not in record["branches"]["without_rag"]["answer"]


def test_compare_modes_carries_grounding_in_each_branch(tmp_path):
    text = _grounded(f"answer [{PASSED}]", [{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    record = service.compare_modes({"collection_id": "c1", "question": "q"})
    assert record["comparison"]["prompt_templates"]["generation"] == "grounded-rag-v1"
    for mode in record["modes"]:
        assert mode["branch"]["answer"]["grounding"]["status"] == "verified"


def test_grounded_stream_done_carries_grounding(tmp_path):
    text = _grounded(f"answer [{PASSED}]", [{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    service, _knowledge, _ = _service(tmp_path, model=FakeChatModel(text=text))
    plan = service.prepare({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    events = list(service.stream_events(plan, False))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "start" and kinds[1] == "sources" and kinds[-1] == "done"
    done = events[-1]["answer"]
    assert done["answer"]["grounding"]["status"] == "verified"


def test_grounded_sources_are_only_passed_not_candidates(tmp_path):
    fragments = [
        fragment(PASSED, text=CHUNK_TEXT, rank=1, score=0.9),
        fragment("b" * 64, text="other", rank=2, score=0.8),
    ]
    text = _grounded(f"answer [{PASSED}]", [{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    service, _knowledge, _ = _service(
        tmp_path, model=FakeChatModel(text=text), fragments=fragments, top_k=1
    )
    record = _chat(service, top_k=1)
    passed_ids = [item["chunk_id"] for item in record["retrieval"]["passed"]]
    assert passed_ids == [PASSED]
    assert record["retrieval"]["found_count"] == 2
