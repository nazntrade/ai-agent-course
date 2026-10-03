"""ChatService modes, comparison, streaming and run persistence (D22-01..09)."""

from __future__ import annotations

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.prompts import RAG
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.errors import (
    ContextOverflow,
    IndexNotReady,
    InvalidRequest,
    ChatInvalidResponse,
    ChatUnavailable,
)
from tests.helpers import FakeChatModel, FakeKnowledge, fragment


def _service(tmp_path, knowledge, model, **kwargs):
    store = FileChatRunStore(tmp_path / "runs")
    return ChatService(knowledge, model, store, **kwargs), store


def test_without_rag_calls_no_retrieval_even_when_it_would_fail(tmp_path):
    knowledge = FakeKnowledge(search_error=IndexNotReady("no index"))
    model = FakeChatModel(text="plain answer")
    service, _ = _service(tmp_path, knowledge, model)

    record = service.chat({"mode": "without_rag", "question": "Why?"})

    assert knowledge.search_calls == 0
    assert record["mode"] == "without_rag"
    assert record["index"] is None and record["retrieval"] is None
    assert record["answer"]["text"] == "plain answer"
    assert record["prompt"]["template_id"] == "plain-v1"
    assert len(model.calls) == 1
    assert [message.role for message in model.calls[0]] == ["system", "user"]


def test_with_rag_pins_index_and_separates_found_and_passed(tmp_path):
    chunk_id = "a" * 64
    knowledge = FakeKnowledge(
        fragments=[fragment(chunk_id, text="memory text", rank=1)],
        index_version_id="idx-9",
        fingerprint="fp-9",
    )
    model = FakeChatModel(text=f"grounded [{chunk_id}]")
    service, _ = _service(tmp_path, knowledge, model)

    record = service.chat(
        {"mode": "with_rag", "question": "What is memory?", "collection_id": "c1", "strategy": "fixed"}
    )

    assert knowledge.search_calls == 1
    assert record["index"]["index_version_id"] == "idx-9"
    assert record["index"]["fingerprint"] == "fp-9"
    assert record["retrieval"]["found_count"] == 1
    assert record["retrieval"]["passed_count"] == 1
    assert record["retrieval"]["passed"][0]["chunk_id"] == chunk_id
    assert record["answer"]["citations"]["valid"] == [chunk_id]
    assert record["prompt"]["template_id"] == "rag-v1"


def test_with_rag_requires_collection_id(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel())
    with pytest.raises(InvalidRequest):
        service.chat({"mode": "with_rag", "question": "q"})


def test_retrieval_errors_propagate_without_non_rag_fallback(tmp_path):
    knowledge = FakeKnowledge(search_error=IndexNotReady("no ready index"))
    model = FakeChatModel()
    service, _ = _service(tmp_path, knowledge, model)
    with pytest.raises(IndexNotReady):
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert model.calls == []


def test_context_overflow_is_raised_before_calling_the_model(tmp_path):
    chunk_id = "b" * 64
    knowledge = FakeKnowledge(fragments=[fragment(chunk_id, text="x" * 50)])
    model = FakeChatModel()
    service, _ = _service(
        tmp_path,
        knowledge,
        model,
        max_context_tokens=10,
        reserved_output_tokens=0,
        chars_per_token=1,
        safety_margin=0,
    )
    with pytest.raises(ContextOverflow):
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert model.calls == []


def test_tight_budget_drops_chunks_and_records_it(tmp_path):
    first, second = "a" * 64, "b" * 64
    knowledge = FakeKnowledge(
        fragments=[
            fragment(first, text="x" * 400, rank=1),
            fragment(second, text="y" * 400, rank=2),
        ]
    )
    model = FakeChatModel(text="answer")
    # chars_per_token=1 so the mandatory system prompt is measurable in chars.
    # The budget now also counts the rendered <context> wrapper (markup and
    # provenance labels), so it is sized to fit the first rendered chunk and
    # drop the second whole.
    service, _ = _service(
        tmp_path,
        knowledge,
        model,
        max_context_tokens=len(RAG.system) + 700,
        reserved_output_tokens=0,
        chars_per_token=1,
        safety_margin=0,
    )
    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert record["retrieval"]["found_count"] == 2
    assert record["retrieval"]["passed_count"] == 1
    assert record["context"]["dropped_chunks"] == 1
    # The record never leaks the internal chunk text in the passed projection.
    assert "text" not in record["retrieval"]["passed"][0]


def test_model_context_length_caps_the_budget(tmp_path):
    # SPEC 9.2: effective = min(CHAT_CONTEXT_TOKENS, model.context_length).
    model = FakeChatModel(text="answer", context_length=2048)
    service, _ = _service(tmp_path, FakeKnowledge(), model, max_context_tokens=8192)
    record = service.chat({"mode": "without_rag", "question": "q"})
    assert record["context"]["max_context_tokens"] == 2048


def test_model_context_length_drops_chunks_early(tmp_path):
    chunk_id = "e" * 64
    knowledge = FakeKnowledge(fragments=[fragment(chunk_id, text="x" * 400, rank=1)])
    system_chars = len(RAG.system)
    model = FakeChatModel(text="answer", context_length=system_chars + 200)
    service, _ = _service(
        tmp_path,
        knowledge,
        model,
        max_context_tokens=100_000,
        reserved_output_tokens=0,
        chars_per_token=1,
        safety_margin=0,
    )
    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert record["context"]["max_context_tokens"] == system_chars + 200
    assert record["retrieval"]["passed_count"] == 0
    assert record["context"]["dropped_chunks"] == 1


def test_compare_uses_one_model_separate_histories_and_no_leak(tmp_path):
    chunk_id = "c" * 64
    knowledge = FakeKnowledge(fragments=[fragment(chunk_id, text="secret chunk", rank=1)])
    model = FakeChatModel(text="answer")
    service, store = _service(tmp_path, knowledge, model)

    record = service.compare({"collection_id": "c1", "question": "q", "strategy": "fixed"})

    assert knowledge.resolve_calls == 1
    assert knowledge.search_calls == 1  # only the with_rag branch retrieves
    assert record["result_kind"] == "compare" and record["mode"] == "compare"
    assert record["kind"] == "compare"
    assert set(record["branches"]) == {"with_rag", "without_rag"}
    assert record["comparison"]["same_model"] is True
    assert record["comparison"]["same_settings"] is True
    assert record["comparison"]["prompt_templates"] == {"with_rag": "rag-v1", "without_rag": "plain-v1"}
    assert record["comparison"]["policy_differences"]
    assert len(model.calls) == 2
    rag_messages, plain_messages = model.calls
    plain_text = " ".join(message.content for message in plain_messages)
    assert chunk_id not in plain_text and "secret chunk" not in plain_text
    rag_text = " ".join(message.content for message in rag_messages)
    assert "secret chunk" in rag_text
    saved = store.get_run(record["run_id"])
    assert saved["result_kind"] == "compare"


def test_compare_without_ready_index_makes_no_run(tmp_path):
    knowledge = FakeKnowledge(resolve_error=IndexNotReady("no index"))
    model = FakeChatModel()
    service, store = _service(tmp_path, knowledge, model)
    with pytest.raises(IndexNotReady):
        service.compare({"collection_id": "c1", "question": "q"})
    assert model.calls == []
    assert store.list_runs(limit=10) == []


def test_compare_requires_collection_id(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel())
    with pytest.raises(InvalidRequest):
        service.compare({"question": "q"})


def test_runs_are_saved_listed_and_missing_raises_keyerror(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel(text="hi"))
    record = service.chat({"mode": "without_rag", "question": "q"})
    listed = service.list_runs(limit=10)
    assert listed["total"] == 1 and listed["runs"][0]["run_id"] == record["run_id"]
    assert service.get_run(record["run_id"])["run_id"] == record["run_id"]
    with pytest.raises(KeyError):
        service.get_run("missing")


def test_list_runs_rejects_incompatible_kind_mode_combinations(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel())
    with pytest.raises(InvalidRequest):
        service.list_runs(kind="single", mode="compare")
    with pytest.raises(InvalidRequest):
        service.list_runs(kind="compare", mode="with_rag")
    with pytest.raises(InvalidRequest):
        service.list_runs(mode="bogus")


def test_stream_emits_start_sources_tokens_done_and_saves(tmp_path):
    chunk_id = "d" * 64
    knowledge = FakeKnowledge(fragments=[fragment(chunk_id, text="chunk", rank=1)])
    model = FakeChatModel(text="abcdef")
    service, store = _service(tmp_path, knowledge, model)
    plan = service.prepare({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    events = list(service.stream_events(plan, True))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "start"
    assert kinds[1] == "sources"
    assert "token" in kinds
    assert kinds[-1] == "done"
    start, done = events[0], events[-1]
    assert done["answer"]["run_id"] == start["run_id"]
    assert store.get_run(start["run_id"]) is not None


def test_stream_without_sources_in_plain_mode(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel(text="abc"))
    plan = service.prepare({"mode": "without_rag", "question": "q"})
    kinds = [event["type"] for event in service.stream_events(plan, False)]
    assert "sources" not in kinds
    assert kinds[0] == "start" and kinds[-1] == "done"


def test_stream_error_yields_error_event_and_partial_run(tmp_path):
    model = FakeChatModel(error=ChatUnavailable("provider down"))
    service, store = _service(tmp_path, FakeKnowledge(), model)
    plan = service.prepare({"mode": "without_rag", "question": "q"})
    events = list(service.stream_events(plan, True))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "error"
    assert "done" not in kinds
    error = events[-1]["error"]
    assert error["code"] == "chat_unavailable"
    assert not any(event.get("type") == "done" for event in events)
    partial = store.get_run(plan["run_id"])
    assert partial is not None and partial["errors"]


def test_health_reports_chat_status(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel())
    health = service.health()
    assert health["reachable"] is True
    assert health["model_present"] is True
    assert health["model"] == "fake-chat"
    assert health["hint"] is None


def test_empty_question_is_rejected(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel())
    with pytest.raises(InvalidRequest):
        service.chat({"mode": "without_rag", "question": "   "})


def test_empty_model_answer_raises_and_is_never_saved(tmp_path):
    model = FakeChatModel(text="")
    service, store = _service(tmp_path, FakeKnowledge(), model)
    with pytest.raises(ChatInvalidResponse):
        service.chat({"mode": "without_rag", "question": "q"})
    assert store.list_runs(limit=10) == []


def test_blank_model_answer_raises_and_is_never_saved(tmp_path):
    model = FakeChatModel(text="  \n")
    service, store = _service(tmp_path, FakeKnowledge(), model)
    with pytest.raises(ChatInvalidResponse):
        service.chat({"mode": "without_rag", "question": "q"})
    assert store.list_runs(limit=10) == []
