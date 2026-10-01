"""D22 HTTP surface: chat, stream, compare, runs and evaluation (D22-11/12)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app
from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.errors import ChatModelMissing
from tests.helpers import FakeChatModel, make_service

CHAT_PATHS = {
    "/api/chat",
    "/api/chat/stream",
    "/api/chat/compare",
    "/api/chat-runs",
    "/api/chat-runs/{run_id}",
    "/api/chat-runs/{run_id}/evaluation",
}


def _ready_client(tmp_path):
    service, store = make_service(tmp_path)
    source = tmp_path / "corpus.md"
    source.write_text("# Memory\n\nAgents use memory and planning. " * 20, encoding="utf-8")
    collection = service.create_collection("chat")["collection_id"]
    version_id = service.build(collection, [{"path": str(source)}], "fixed", wait=True)["index_version_id"]
    chunk_id = service.list_chunks(version_id)["items"][0]["chunk_id"]
    model = FakeChatModel(text=f"grounded answer [{chunk_id}]")
    chat_service = ChatService(service, model, FileChatRunStore(tmp_path / "runs"))
    app = create_app(service, chat_service, ui_dir=tmp_path / "no-ui")
    return TestClient(app), service, collection, version_id, chunk_id, store


def test_openapi_contains_all_chat_paths(tmp_path):
    client, *_ = _ready_client(tmp_path)
    schema = client.get("/openapi.json").json()
    for path in CHAT_PATHS:
        assert path in schema["paths"], path
    assert "post" in schema["paths"]["/api/chat"]
    assert "post" in schema["paths"]["/api/chat/stream"]
    assert "get" in schema["paths"]["/api/chat-runs"]
    assert "put" in schema["paths"]["/api/chat-runs/{run_id}/evaluation"]


def test_health_has_chat_block_and_keeps_embedding(tmp_path):
    client, *_ = _ready_client(tmp_path)
    body = client.get("/api/health").json()
    assert body["embedding"]["reachable"] is True
    assert body["chat"]["reachable"] is True
    assert body["chat"]["model"] == "fake-chat"


def test_without_rag_does_not_touch_retrieval(tmp_path):
    client, service, collection, version_id, _cid, store = _ready_client(tmp_path)
    response = client.post("/api/chat", json={"mode": "without_rag", "question": "Hello?"})
    assert response.status_code == 200
    record = response.json()
    assert record["retrieval"] is None and record["index"] is None
    assert record["answer"]["text"]
    store.close()


def test_with_rag_pins_index_and_returns_citations(tmp_path):
    client, _service, collection, version_id, chunk_id, store = _ready_client(tmp_path)
    response = client.post(
        "/api/chat",
        json={"mode": "with_rag", "question": "Memory?", "collection_id": collection},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["index"]["index_version_id"] == version_id
    assert record["retrieval"]["passed_count"] <= record["retrieval"]["found_count"]
    assert record["answer"]["citations"]["valid"] == [chunk_id]
    store.close()


def test_with_rag_empty_collection_returns_409(tmp_path):
    client, service, *_rest = _ready_client(tmp_path)
    empty = service.create_collection("empty")["collection_id"]
    response = client.post(
        "/api/chat",
        json={"mode": "with_rag", "question": "q", "collection_id": empty},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "index_not_ready"


def test_with_rag_without_collection_is_invalid_request(tmp_path):
    client, *_ = _ready_client(tmp_path)
    response = client.post("/api/chat", json={"mode": "with_rag", "question": "q"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_top_k_out_of_range_is_invalid_request(tmp_path):
    client, *_ = _ready_client(tmp_path)
    response = client.post("/api/chat", json={"mode": "without_rag", "question": "q", "top_k": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_stream_endpoint_emits_ordered_sse_events(tmp_path):
    client, *_ = _ready_client(tmp_path)
    response = client.post(
        "/api/chat/stream",
        json={"mode": "without_rag", "question": "Hello?"},
    )
    assert response.status_code == 200
    events = []
    for frame in response.text.split("\n\n"):
        line = next((item for item in frame.splitlines() if item.startswith("data:")), None)
        if line:
            events.append(json.loads(line[5:].strip()))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert events[-1]["answer"]["run_id"] == events[0]["run_id"]


def test_chat_runs_listing_detail_and_404(tmp_path):
    client, *_ = _ready_client(tmp_path)
    created = client.post("/api/chat", json={"mode": "without_rag", "question": "Hello?"}).json()
    listing = client.get("/api/chat-runs?limit=5").json()
    assert listing["total"] == 1 and listing["runs"][0]["run_id"] == created["run_id"]
    detail = client.get(f"/api/chat-runs/{created['run_id']}")
    assert detail.status_code == 200 and detail.json()["run_id"] == created["run_id"]
    assert client.get("/api/chat-runs/missing").status_code == 404
    assert client.get("/api/chat-runs/missing/evaluation").status_code == 404


def test_evaluation_put_and_get(tmp_path):
    client, *_ = _ready_client(tmp_path)
    run_id = client.post("/api/chat", json={"mode": "without_rag", "question": "Hello?"}).json()["run_id"]
    evaluation = {
        "evaluator": "unit",
        "retrieval": {"score": 2},
        "content": {"score": 1},
        "sources": {"score": 2},
        "overall": "pass",
    }
    saved = client.put(f"/api/chat-runs/{run_id}/evaluation", json=evaluation)
    assert saved.status_code == 200
    assert saved.json()["schema_version"] == "chat-eval-v1"
    loaded = client.get(f"/api/chat-runs/{run_id}/evaluation").json()
    assert loaded["overall"] == "pass"


def test_incompatible_kind_mode_is_invalid_request(tmp_path):
    client, *_ = _ready_client(tmp_path)
    response = client.get("/api/chat-runs?kind=single&mode=compare")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_compare_endpoint_returns_two_branches(tmp_path):
    client, _service, collection, *_rest = _ready_client(tmp_path)
    response = client.post(
        "/api/chat/compare",
        json={"collection_id": collection, "question": "Memory?"},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["kind"] == "compare"
    assert record["result_kind"] == "compare"
    assert set(record["branches"]) == {"with_rag", "without_rag"}
    assert record["comparison"]["same_model"] is True
    assert record["comparison"]["index_version_id"] == _rest[0]


def test_missing_chat_model_maps_to_503(tmp_path):
    service, store = make_service(tmp_path)
    source = tmp_path / "corpus.md"
    source.write_text("# Memory\n\nAgents use memory. " * 20, encoding="utf-8")
    collection = service.create_collection("chat")["collection_id"]
    service.build(collection, [{"path": str(source)}], "fixed", wait=True)
    model = FakeChatModel(error=ChatModelMissing("no model", details={"hint": "ollama pull x"}))
    chat_service = ChatService(service, model, FileChatRunStore(tmp_path / "runs"))
    client = TestClient(create_app(service, chat_service, ui_dir=tmp_path / "no-ui"))
    response = client.post("/api/chat", json={"mode": "without_rag", "question": "q"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "chat_model_missing"
    store.close()
