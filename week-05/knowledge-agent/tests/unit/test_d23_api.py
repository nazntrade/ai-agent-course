"""D23-C01/C02/C06: HTTP surface for the retrieval filter, rewrite and modes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app
from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.rewrite import ChatQueryRewriter
from knowledge_agent.chat.run_store import FileChatRunStore
from tests.helpers import FakeChatModel, make_service


def _client(tmp_path):
    service, store = make_service(tmp_path)
    source = tmp_path / "corpus.md"
    source.write_text("# Memory\n\nAgents use memory and planning. " * 20, encoding="utf-8")
    collection = service.create_collection("chat")["collection_id"]
    version_id = service.build(collection, [{"path": str(source)}], "fixed", wait=True)["index_version_id"]
    chunk_id = service.list_chunks(version_id)["items"][0]["chunk_id"]
    model = FakeChatModel(text=f"grounded answer [{chunk_id}]")
    chat_service = ChatService(
        service,
        model,
        FileChatRunStore(tmp_path / "runs"),
        query_rewriter=ChatQueryRewriter(FakeChatModel(text="reformed query")),
    )
    app = create_app(service, chat_service, ui_dir=tmp_path / "no-ui")
    return TestClient(app), service, collection, version_id, chunk_id, store


def test_openapi_contains_compare_modes(tmp_path):
    client, *_ = _client(tmp_path)
    schema = client.get("/openapi.json").json()
    assert "/api/chat/compare-modes" in schema["paths"]
    assert "post" in schema["paths"]["/api/chat/compare-modes"]


def test_mode_b_returns_full_trace(tmp_path):
    client, _service, collection, version_id, chunk_id, store = _client(tmp_path)
    response = client.post(
        "/api/chat",
        json={
            "mode": "with_rag",
            "collection_id": collection,
            "question": "Memory?",
            "rag_mode": "B",
            "min_score": 0.0,
            "prefilter_top_k": 5,
            "postfilter_top_k": 2,
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["rag_mode"] == "B"
    retrieval = record["retrieval"]
    assert isinstance(retrieval["candidates"], list) and retrieval["candidates"]
    assert isinstance(retrieval["selected"], list)
    assert isinstance(retrieval["exclusion_reasons"], dict)
    assert set(retrieval["exclusion_reasons"]) == {"threshold", "top_k", "context_budget"}
    assert record["usage"]["input_tokens"] is not None
    store.close()


def test_mode_c_rewrites_only_the_search_query(tmp_path):
    client, *_rest = _client(tmp_path)
    record = client.post(
        "/api/chat",
        json={
            "mode": "with_rag",
            "collection_id": _rest[1],
            "question": "Memory?",
            "rag_mode": "C",
        },
    ).json()
    assert record["original_query"] == "Memory?"
    assert record["search_query"] == "reformed query"
    assert record["rewrite"]["used"] is True


def test_compare_modes_endpoint_returns_four_modes(tmp_path):
    client, _service, collection, version_id, _chunk, store = _client(tmp_path)
    response = client.post(
        "/api/chat/compare-modes",
        json={"collection_id": collection, "question": "Memory?", "min_score": 0.0},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["comparison_kind"] == "four_modes"
    assert [mode["id"] for mode in record["modes"]] == ["A", "B", "C", "D"]
    assert record["comparison"]["index_version_id"] == version_id
    assert record["comparison"]["same_model"] is True
    store.close()


def test_invalid_threshold_has_dedicated_code(tmp_path):
    client, _service, collection, *_rest = _client(tmp_path)
    response = client.post(
        "/api/chat",
        json={"mode": "with_rag", "collection_id": collection, "question": "q", "min_score": 1.5},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_threshold"


def test_top_k_order_and_mode_conflict_are_invalid_request(tmp_path):
    client, _service, collection, *_rest = _client(tmp_path)
    response = client.post(
        "/api/chat",
        json={
            "mode": "with_rag",
            "collection_id": collection,
            "question": "q",
            "prefilter_top_k": 2,
            "postfilter_top_k": 5,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    conflict = client.post(
        "/api/chat",
        json={
            "mode": "with_rag",
            "collection_id": collection,
            "question": "q",
            "rag_mode": "A",
            "use_filter": True,
        },
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "invalid_request"
