"""D26-18: secrets never leak into API responses; missing config names params."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import load_settings
from app.dialogues.store import SqliteDialogueStore
from app.providers.base import AnswerProvider, ChatResult, ChatUsage, ProviderStatus
from app.service import ChatService

SECRET = "sk-super-secret-value"


class StubProvider(AnswerProvider):
    def __init__(self, name, model, configured=True):
        self.name = name
        self._model = model
        self._configured = configured

    def identity(self):
        return {"provider": self.name, "model": self._model}

    def is_configured(self):
        return self._configured

    def missing_config(self):
        return [] if self._configured else ["DEEPSEEK_API_KEY"]

    def status(self):
        return ProviderStatus(provider=self.name, reachable=self._configured, model=self._model)

    def chat(self, messages, options=None):
        return ChatResult(text="ok", model=self._model, finish_reason="stop", usage=ChatUsage(1, 1, 2), latency_ms=1.0)


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


def make_client(tmp_path, configured=True):
    settings = load_settings({
        "DIALOGUE_DB_PATH": str(tmp_path / "conversations.db"),
        "DEEPSEEK_API_KEY": SECRET,
    })
    store = SqliteDialogueStore(settings.dialogue_db_path)
    service = ChatService(
        settings,
        local_provider=StubProvider("local", "gemma.gguf"),
        network_provider=StubProvider("network", "deepseek-chat", configured=configured),
        gemma_manager=StubGemma(),
        dialogue_store=store,
    )
    return TestClient(create_app(service)), service


def test_provider_state_does_not_expose_key(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/provider", json={"provider": "network"})
    response = client.get("/api/provider")
    assert response.status_code == 200
    body = response.text
    assert SECRET not in body


def test_health_and_dialogues_are_secret_free(tmp_path):
    client, _ = make_client(tmp_path)
    for path in ("/api/health", "/api/dialogues", "/api/mcp/connection-point"):
        response = client.get(path)
        assert response.status_code == 200
        assert SECRET not in response.text


def test_missing_network_config_names_parameters_without_values(tmp_path):
    client, service = make_client(tmp_path, configured=False)
    client.post("/api/provider", json={"provider": "network"})
    state = client.get("/api/provider").json()
    assert state["missing_config"] == ["DEEPSEEK_API_KEY"]
    assert SECRET not in client.get("/api/provider").text


def test_invalid_payload_does_not_echo_input(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post("/api/provider", json={"provider": "bogus"})
    assert response.status_code == 422
    assert SECRET not in response.text
