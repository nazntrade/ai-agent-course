"""D25 HTTP API: /api/dialogues CRUD, turns and task-memory contracts."""

from __future__ import annotations

from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app
from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.service.conversation_service import ConversationService
from knowledge_agent.storage.conversation_store import SqliteConversationStore

from tests.helpers import FakeChatModel, FakeKnowledge, fragment, make_service


def _client(tmp_path):
    service, index_store = make_service(tmp_path)
    chat = ChatService(
        FakeKnowledge(fragments=[fragment("a" * 64)]),
        FakeChatModel(text="the answer"),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=False,
    )
    conv_store = SqliteConversationStore(tmp_path / "conversations.db")
    conversation = ConversationService(conv_store, chat)
    app = create_app(
        service,
        chat_service=chat,
        conversation_service=conversation,
        ui_dir=tmp_path / "no-ui",
    )
    return TestClient(app), index_store, conv_store


def _turn(client, dialogue_id, client_turn_id, question):
    return client.post(
        f"/api/dialogues/{dialogue_id}/turns",
        json={
            "client_turn_id": client_turn_id,
            "question": question,
            "mode": "with_rag",
            "collection_id": "c1",
            "top_k": 3,
            "grounding": False,
        },
    )


def test_dialogue_crud_and_delete_confirmation(tmp_path):
    client, index_store, conv_store = _client(tmp_path)
    created = client.post("/api/dialogues", json={"name": "Session"})
    assert created.status_code == 200
    dialogue_id = created.json()["dialogue_id"]

    listing = client.get("/api/dialogues").json()
    assert listing["dialogues"][0]["dialogue_id"] == dialogue_id

    renamed = client.patch(f"/api/dialogues/{dialogue_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"

    refused = client.delete(f"/api/dialogues/{dialogue_id}")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "deletion_requires_confirmation"
    assert client.get(f"/api/dialogues/{dialogue_id}").status_code == 200

    deleted = client.delete(f"/api/dialogues/{dialogue_id}?confirm=true")
    assert deleted.status_code == 200
    assert client.get(f"/api/dialogues/{dialogue_id}").status_code == 404
    index_store.close()
    conv_store.close()


def test_turn_idempotency_and_memory_patch(tmp_path):
    client, index_store, conv_store = _client(tmp_path)
    dialogue_id = client.post("/api/dialogues", json={}).json()["dialogue_id"]

    first = _turn(client, dialogue_id, "same", "Цель: изучить RAG")
    assert first.status_code == 200
    turn = first.json()
    assert turn["status"] == "ok"
    assert turn["memory_after"]["goal"]["text"] == "изучить RAG"

    again = _turn(client, dialogue_id, "same", "Цель: изучить RAG")
    assert again.json()["turn_id"] == turn["turn_id"]
    assert client.get(f"/api/dialogues/{dialogue_id}/turns").json()["total"] == 1

    memory = client.get(f"/api/dialogues/{dialogue_id}/memory").json()
    conflict = client.patch(
        f"/api/dialogues/{dialogue_id}/memory",
        json={"expected_version": 999, "operations": [{"op": "set_goal", "text": "x", "grounds": [turn["turn_id"]]}]},
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "memory_conflict"

    invalid = client.patch(
        f"/api/dialogues/{dialogue_id}/memory",
        json={"expected_version": memory["version"], "operations": [{"op": "set_goal", "text": "x", "grounds": ["assistant-1"]}]},
    )
    assert invalid.status_code == 422

    updated = client.patch(
        f"/api/dialogues/{dialogue_id}/memory",
        json={"expected_version": memory["version"], "operations": [{"op": "set_goal", "text": "новая цель", "grounds": [turn["turn_id"]]}]},
    )
    assert updated.status_code == 200 and updated.json()["goal"]["text"] == "новая цель"
    index_store.close()
    conv_store.close()


def test_clarification_exposes_the_question(tmp_path):
    client, index_store, conv_store = _client(tmp_path)
    dialogue_id = client.post("/api/dialogues", json={}).json()["dialogue_id"]
    response = _turn(client, dialogue_id, "c1", "а второй вариант?")
    assert response.status_code == 200
    turn = response.json()
    assert turn["status"] == "clarification"
    assert turn["reference_resolution"]["clarification_question"] == turn["answer"]["text"]
    assert turn["reference_resolution"]["clarification_question"]
    index_store.close()
    conv_store.close()


def test_context_overflow_returns_422_and_stores_no_turn(tmp_path):
    client, index_store, conv_store = _client(tmp_path)
    dialogue_id = client.post("/api/dialogues", json={}).json()["dialogue_id"]
    response = client.post(
        f"/api/dialogues/{dialogue_id}/turns",
        json={
            "client_turn_id": "c1",
            "question": "q",
            "mode": "with_rag",
            "collection_id": "c1",
            "max_context_tokens": 1,
            "grounding": False,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "context_overflow"
    assert client.get(f"/api/dialogues/{dialogue_id}/turns").json()["total"] == 0
    index_store.close()
    conv_store.close()


def test_invalid_and_missing_requests(tmp_path):
    client, index_store, conv_store = _client(tmp_path)
    assert client.post("/api/dialogues/does-not-exist/turns", json={"client_turn_id": "c", "question": "q"}).status_code == 404
    dialogue_id = client.post("/api/dialogues", json={}).json()["dialogue_id"]
    missing = client.post(f"/api/dialogues/{dialogue_id}/turns", json={"question": "q"})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_request"
    store = client.get(f"/api/dialogues/{dialogue_id}/memory").json()
    assert store["version"] == 0
    index_store.close()
    conv_store.close()
