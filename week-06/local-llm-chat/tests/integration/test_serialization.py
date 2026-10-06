"""D26-06: a delayed answer after a provider switch is not accepted as current."""

from __future__ import annotations

import threading
import time

from app.config import load_settings
from app.dialogues.store import SqliteDialogueStore
from app.errors import ProviderError
from app.providers.base import AnswerProvider, ChatResult, ChatUsage, ProviderStatus
from app.service import ChatService


class BlockingProvider(AnswerProvider):
    name = "local"

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self._started = started
        self._release = release

    def identity(self):
        return {"provider": self.name, "model": "gemma.gguf"}

    def is_configured(self):
        return True

    def missing_config(self):
        return []

    def status(self):
        return ProviderStatus(provider=self.name, reachable=True, model="gemma.gguf")

    def chat(self, messages, options=None):
        self._started.set()
        self._release.wait(timeout=5)
        return ChatResult(text="late answer", model="gemma.gguf", finish_reason="stop", usage=ChatUsage(1, 1, 2), latency_ms=1.0)


class StubNetwork(AnswerProvider):
    name = "network"

    def identity(self):
        return {"provider": self.name, "model": "deepseek-chat"}

    def is_configured(self):
        return True

    def missing_config(self):
        return []

    def status(self):
        return ProviderStatus(provider=self.name, reachable=True, model="deepseek-chat")

    def chat(self, messages, options=None):
        return ChatResult(text="fast", model="deepseek-chat", finish_reason="stop", usage=None, latency_ms=1.0)


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


def test_stale_generation_after_switch_is_rejected(tmp_path):
    started = threading.Event()
    release = threading.Event()
    settings = load_settings({"DIALOGUE_DB_PATH": str(tmp_path / "conversations.db")})
    store = SqliteDialogueStore(settings.dialogue_db_path)
    service = ChatService(
        settings,
        local_provider=BlockingProvider(started, release),
        network_provider=StubNetwork(),
        gemma_manager=StubGemma(),
        dialogue_store=store,
    )
    dialogue = store.create_dialogue("test")

    error_box = {}

    def run():
        try:
            service.ask(dialogue["dialogue_id"], "question", provider="local")
        except ProviderError as exc:
            error_box["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=5)
    # Switch provider while the local answer is still generating.
    service.select_provider("network", dialogue_id=dialogue["dialogue_id"])
    release.set()
    worker.join(timeout=5)

    assert "error" in error_box, "the stale answer must be rejected"
    messages = store.list_messages(dialogue["dialogue_id"])
    assert not any(m["role"] == "assistant" and m["text"] == "late answer" for m in messages)
