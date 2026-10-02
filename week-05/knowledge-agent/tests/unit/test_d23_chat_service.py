"""D23-C01..C09: modes, trace, rewrite semantics, metrics and D22 merge rule."""

from __future__ import annotations

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.rewrite import ChatQueryRewriter
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.errors import InvalidRequest, InvalidThreshold
from tests.helpers import FakeChatModel, FakeKnowledge, fragment


class CapturingKnowledge(FakeKnowledge):
    """FakeKnowledge that also records the top_k passed to ``search``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.top_k_seen: list[int] = []

    def search(self, collection_id, query, top_k=5, index_version_id=None, strategy=None):
        self.top_k_seen.append(top_k)
        return super().search(collection_id, query, top_k=top_k,
                              index_version_id=index_version_id, strategy=strategy)


def _service(tmp_path, knowledge, model, rewriter_model=None, **kwargs):
    store = FileChatRunStore(tmp_path / "runs")
    rewriter = ChatQueryRewriter(rewriter_model) if rewriter_model is not None else None
    return ChatService(knowledge, model, store, query_rewriter=rewriter, **kwargs), store


def _fragments(count: int, score: float = 0.9):
    return [fragment(f"{index:064d}", text=f"chunk {index}", rank=index, score=score)
            for index in range(1, count + 1)]


def test_modes_a_b_c_d_produce_distinct_traces(tmp_path):
    knowledge = FakeKnowledge(fragments=_fragments(4, 0.6))
    model = FakeChatModel(text="grounded")
    service, _ = _service(tmp_path, knowledge, model, FakeChatModel(text="reformed query"))

    plain = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                          "rag_mode": "A"})
    filtered = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                             "rag_mode": "B", "min_score": 0.5, "postfilter_top_k": 2})
    rewritten = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                              "rag_mode": "C"})
    both = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                         "rag_mode": "D", "min_score": 0.5, "postfilter_top_k": 2})

    assert plain["rag_mode"] == "A" and plain["use_filter"] is False
    assert filtered["rag_mode"] == "B" and filtered["retrieval"]["selected_count"] == 2
    assert rewritten["search_query"] == "reformed query"
    assert rewritten["original_query"] == "q"
    assert both["use_filter"] is True and both["use_rewrite"] is True
    assert filtered["retrieval"]["selected_count"] != plain["retrieval"]["selected_count"]


def test_rewrite_changes_only_the_search_query(tmp_path):
    knowledge = FakeKnowledge(fragments=_fragments(1))
    generation = FakeChatModel(text="answer")
    service, _ = _service(tmp_path, knowledge, generation, FakeChatModel(text="reformed query"))

    record = service.chat({"mode": "with_rag", "question": "original question",
                           "collection_id": "c1", "use_rewrite": True})

    assert record["original_query"] == "original question"
    assert record["search_query"] == "reformed query"
    assert record["rewrite"]["used"] is True
    user_messages = [message.content for message in generation.calls[0] if message.role == "user"]
    assert "original question" in user_messages


def test_rewrite_fallback_keeps_the_original_query(tmp_path):
    from knowledge_agent.domain.errors import ChatTimeout

    knowledge = FakeKnowledge(fragments=_fragments(1))
    service, _ = _service(
        tmp_path, knowledge, FakeChatModel(text="answer"),
        FakeChatModel(error=ChatTimeout("slow")),
    )
    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                           "use_rewrite": True})
    assert record["rewrite"]["fallback"] is True
    assert record["rewrite"]["reason"] == "chat_timeout"
    assert record["search_query"] == "q"


def test_empty_filter_is_deterministic_and_calls_no_model(tmp_path):
    chunk_id = "a" * 64
    knowledge = FakeKnowledge(fragments=[fragment(chunk_id, text="low score", score=0.2)])
    model = FakeChatModel(text="should not be called")
    service, _ = _service(tmp_path, knowledge, model)

    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                           "use_filter": True, "min_score": 0.9})

    assert model.calls == []
    assert record["retrieval"]["passed"] == []
    assert record["retrieval"]["exclusion_reasons"]["threshold"] == [chunk_id]
    assert record["usage"] is None
    assert record["latency_ms"]["chat"] is None
    assert record["answer"]["insufficient_sources"] is True
    assert record["answer"]["finish_reason"] is None
    assert "[stub]" not in record["answer"]["text"]


def test_flat_usage_and_separate_rewrite_metrics(tmp_path):
    knowledge = FakeKnowledge(fragments=_fragments(1))
    generation = FakeChatModel(text="answer")
    service, _ = _service(tmp_path, knowledge, generation, FakeChatModel(text="reformed query"))
    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                           "use_rewrite": True})
    assert record["usage"]["input_tokens"] == 10  # flat generation usage (D22 shape)
    assert record["latency_ms"]["chat"] == 12.5
    assert record["rewrite"]["usage"]["input_tokens"] == 10
    assert record["latency_ms"]["total"] >= record["latency_ms"]["chat"]


def test_legacy_top_k_is_preserved_without_d23_fields(tmp_path):
    knowledge = CapturingKnowledge(fragments=_fragments(2))
    model = FakeChatModel(text="answer")
    service, _ = _service(tmp_path, knowledge, model)

    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                           "top_k": 2})

    assert knowledge.top_k_seen == [2]
    assert record["retrieval"]["found_count"] == 2
    assert record["retrieval"]["selected_count"] == 2
    assert record["prefilter_top_k"] == 2 and record["postfilter_top_k"] == 2
    assert record["rag_mode"] == "A"


def test_d23_request_uses_config_prefilter_default(tmp_path):
    knowledge = CapturingKnowledge(fragments=_fragments(3))
    model = FakeChatModel(text="answer")
    service, _ = _service(tmp_path, knowledge, model, rag_prefilter_top_k=7, rag_filter_top_k=2)

    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                           "use_filter": True, "min_score": 0.1})

    assert knowledge.top_k_seen == [7]
    # Without an explicit postfilter, /api/chat falls back to the resolved prefilter.
    assert record["prefilter_top_k"] == 7 and record["postfilter_top_k"] == 7


def test_config_enabled_flags_are_used_when_request_omits_them(tmp_path):
    knowledge = FakeKnowledge(fragments=_fragments(2))
    generation = FakeChatModel(text="answer")
    service, _ = _service(
        tmp_path, knowledge, generation, FakeChatModel(text="reformed query"),
        rag_filter_enabled=True, rag_rewrite_enabled=True,
    )
    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert record["use_filter"] is True and record["use_rewrite"] is True
    assert record["rag_mode"] == "D"
    assert record["search_query"] == "reformed query"

    explicit = service.chat({
        "mode": "with_rag", "question": "q", "collection_id": "c1",
        "use_filter": False, "use_rewrite": False,
    })
    assert explicit["use_filter"] is False and explicit["use_rewrite"] is False
    assert explicit["rag_mode"] == "A"


def test_without_rag_ignores_d23_fields(tmp_path):
    knowledge = FakeKnowledge(search_error=AssertionError("search must not run"))
    model = FakeChatModel(text="plain")
    service, _ = _service(tmp_path, knowledge, model)

    record = service.chat({"mode": "without_rag", "question": "q", "use_filter": True,
                           "use_rewrite": True, "min_score": 0.9})

    assert knowledge.search_calls == 0
    assert len(model.calls) == 1
    assert [message.role for message in model.calls[0]] == ["system", "user"]
    assert record["retrieval"] is None and record["index"] is None
    assert record["rag_mode"] is None


def test_compare_modes_runs_four_branches_on_one_index(tmp_path):
    knowledge = FakeKnowledge(fragments=_fragments(4, 0.6))
    generation = FakeChatModel(text="answer")
    service, _ = _service(tmp_path, knowledge, generation, FakeChatModel(text="reformed query"))

    record = service.compare_modes({
        "collection_id": "c1",
        "question": "q",
        "min_score": 0.5,
        "prefilter_top_k": 4,
        "postfilter_top_k": 2,
    })

    assert record["comparison_kind"] == "four_modes"
    assert [mode["id"] for mode in record["modes"]] == ["A", "B", "C", "D"]
    assert knowledge.resolve_calls == 1
    assert record["comparison"]["same_model"] is True
    assert record["comparison"]["threshold"] == 0.5
    assert record["comparison"]["prefilter_top_k"] == 4
    for mode in record["modes"]:
        assert mode["branch"]["rag_mode"] == mode["id"]


def test_validation_errors(tmp_path):
    service, _ = _service(tmp_path, FakeKnowledge(), FakeChatModel())

    with pytest.raises(InvalidThreshold):
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                      "min_score": 1.5})
    with pytest.raises(InvalidRequest):
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                      "prefilter_top_k": 2, "postfilter_top_k": 5})
    with pytest.raises(InvalidRequest):
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1",
                      "rag_mode": "A", "use_filter": True})
