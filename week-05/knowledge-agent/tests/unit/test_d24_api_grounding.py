"""D24-01/D24-14: API ``grounding`` field and the optional ``chunk_id`` filter."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app
from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from tests.helpers import FakeChatModel, make_service


def _client(tmp_path):
    service, store = make_service(tmp_path)
    source = tmp_path / "corpus.md"
    source.write_text("# Memory\n\nAgents use memory and planning. " * 20, encoding="utf-8")
    collection = service.create_collection("chat")["collection_id"]
    version_id = service.build(collection, [{"path": str(source)}], "fixed", wait=True)["index_version_id"]
    chunk_id = service.list_chunks(version_id)["items"][0]["chunk_id"]
    text = json.dumps(
        {
            "answer": f"grounded answer [{chunk_id}]",
            "citations": [{"chunk_id": chunk_id, "quote": "Agents use memory and planning."}],
            "insufficient": False,
            "limitation": None,
        }
    )
    chat_service = ChatService(
        service,
        FakeChatModel(text=text),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=True,
    )
    app = create_app(service, chat_service, ui_dir=tmp_path / "no-ui")
    return TestClient(app), service, collection, version_id, chunk_id, store


def test_chunks_chunk_id_filter_returns_the_match(tmp_path):
    client, _service, _collection, version_id, chunk_id, store = _client(tmp_path)
    response = client.get(f"/api/index-versions/{version_id}/chunks?chunk_id={chunk_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["chunk_id"] for item in body["items"]] == [chunk_id]
    assert body["filters"]["chunk_id"] == chunk_id
    store.close()


def test_unknown_chunk_id_is_not_an_error(tmp_path):
    client, _service, _collection, version_id, _chunk_id, store = _client(tmp_path)
    response = client.get(f"/api/index-versions/{version_id}/chunks?chunk_id={'f' * 64}")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0 and body["items"] == []
    store.close()


def test_chunk_id_filter_totals_after_offset(tmp_path):
    client, _service, _collection, version_id, chunk_id, store = _client(tmp_path)
    response = client.get(
        f"/api/index-versions/{version_id}/chunks?chunk_id={chunk_id}&offset=1"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1 and body["items"] == []
    store.close()


def test_chat_grounding_field_toggles_the_layer(tmp_path):
    client, _service, collection, _version, _chunk_id, store = _client(tmp_path)
    grounded = client.post(
        "/api/chat",
        json={"mode": "with_rag", "collection_id": collection, "question": "Memory?", "grounding": True},
    )
    assert grounded.status_code == 200, grounded.text
    record = grounded.json()
    assert record["prompt"]["template_id"] == "grounded-rag-v1"
    assert record["answer"]["grounding"]["status"] == "verified"

    legacy = client.post(
        "/api/chat",
        json={"mode": "with_rag", "collection_id": collection, "question": "Memory?", "grounding": False},
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["prompt"]["template_id"] == "rag-v1"
    assert "grounding" not in legacy.json()["answer"]
    store.close()


def test_compare_and_compare_modes_ignore_the_request_grounding_field(tmp_path):
    client, _service, collection, _version, _chunk_id, store = _client(tmp_path)
    compare = client.post(
        "/api/chat/compare",
        json={"collection_id": collection, "question": "Memory?", "grounding": True},
    )
    assert compare.status_code == 200, compare.text
    assert compare.json()["comparison"]["prompt_templates"]["with_rag"] == "rag-v1"
    assert "grounding" not in compare.json()["branches"]["with_rag"]["answer"]

    modes = client.post(
        "/api/chat/compare-modes",
        json={"collection_id": collection, "question": "Memory?", "grounding": False},
    )
    assert modes.status_code == 200, modes.text
    assert modes.json()["comparison"]["prompt_templates"]["generation"] == "grounded-rag-v1"
    for mode in modes.json()["modes"]:
        assert "grounding" in mode["branch"]["answer"]
    store.close()
