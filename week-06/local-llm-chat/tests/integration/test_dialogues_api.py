"""D26-16: API paths for dialogue rename and delete (used by the UI)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import load_settings
from app.dialogues.store import SqliteDialogueStore
from app.providers.base import AnswerProvider, ChatResult, ProviderStatus
from app.service import ChatService


class StubProvider(AnswerProvider):
    name = "local"

    def identity(self):
        return {"provider": self.name, "model": "gemma.gguf"}

    def is_configured(self):
        return True

    def missing_config(self):
        return []

    def status(self):
        return ProviderStatus(provider=self.name, reachable=True, model="gemma.gguf")

    def chat(self, messages, options=None):
        return ChatResult(text="ok", model="gemma.gguf", finish_reason="stop", usage=None, latency_ms=1.0)


class StubGemma:
    state = "unloaded"

    def ensure_started(self):
        self.state = "ready"
        return self.state_snapshot()

    def stop(self):
        self.state = "unloaded"
        return self.state_snapshot()

    unload = stop

    def shutdown(self):
        pass

    def set_generating(self, value):
        pass

    def state_snapshot(self):
        return {"state": self.state, "pid": None, "port": 8791, "model": "gemma.gguf", "error": None}


def make_client(tmp_path):
    settings = load_settings({"DIALOGUE_DB_PATH": str(tmp_path / "conversations.db")})
    service = ChatService(
        settings,
        local_provider=StubProvider(),
        network_provider=StubProvider(),
        gemma_manager=StubGemma(),
        dialogue_store=SqliteDialogueStore(settings.dialogue_db_path),
    )
    return TestClient(create_app(service))


def test_rename_dialogue_via_api(tmp_path):
    client = make_client(tmp_path)
    created = client.post("/api/dialogues", json={}).json()
    renamed = client.patch(
        "/api/dialogues/" + created["dialogue_id"], json={"name": "Renamed"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    listed = client.get("/api/dialogues").json()["dialogues"]
    assert any(d["dialogue_id"] == created["dialogue_id"] and d["name"] == "Renamed" for d in listed)


def test_delete_dialogue_via_api(tmp_path):
    client = make_client(tmp_path)
    created = client.post("/api/dialogues", json={}).json()
    deleted = client.delete("/api/dialogues/" + created["dialogue_id"])
    assert deleted.status_code == 200
    listed = client.get("/api/dialogues").json()["dialogues"]
    assert all(d["dialogue_id"] != created["dialogue_id"] for d in listed)


def test_rename_missing_dialogue_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.patch("/api/dialogues/does-not-exist", json={"name": "x"})
    assert response.status_code == 404
