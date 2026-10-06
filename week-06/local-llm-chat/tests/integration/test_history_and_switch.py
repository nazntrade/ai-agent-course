"""D26-05/D26-06: history across switches, factual model stored, isolation."""

from __future__ import annotations

from app.config import load_settings
from app.dialogues.store import SqliteDialogueStore
from app.local_process.gemma_manager import GemmaProcessManager
from app.providers.base import AnswerProvider, ChatResult, ChatUsage, ProviderStatus
from app.service import ChatService


class StubProvider(AnswerProvider):
    def __init__(self, name: str, model: str, text: str = "answer") -> None:
        self.name = name
        self._model = model
        self._text = text
        self.calls = 0

    def identity(self):
        return {"provider": self.name, "model": self._model}

    def is_configured(self) -> bool:
        return True

    def missing_config(self):
        return []

    def status(self):
        return ProviderStatus(provider=self.name, reachable=True, model=self._model)

    def chat(self, messages, options=None):
        self.calls += 1
        return ChatResult(
            text=self._text,
            model=self._model,
            finish_reason="stop",
            usage=ChatUsage(1, 2, 3),
            latency_ms=1.0,
            parameters={"temperature": 0.0},
        )


class StubGemma:
    def __init__(self) -> None:
        self.state = "unloaded"
        self.starts = 0
        self.stops = 0

    def ensure_started(self):
        self.starts += 1
        self.state = "ready"
        return self.state_snapshot()

    def stop(self):
        self.stops += 1
        self.state = "unloaded"
        return self.state_snapshot()

    def unload(self):
        return self.stop()

    def shutdown(self):
        self.stop()

    def set_generating(self, value):
        self.state = "generating" if value else "ready"

    def state_snapshot(self):
        return {"state": self.state, "pid": 1 if self.state in ("ready", "generating") else None, "port": 8791, "model": "gemma.gguf", "error": None}


def build(tmp_path, local_text="local answer", network_text="network answer"):
    settings = load_settings({"DIALOGUE_DB_PATH": str(tmp_path / "conversations.db")})
    local = StubProvider("local", "gemma-4-12b.gguf", local_text)
    network = StubProvider("network", "deepseek-chat", network_text)
    gemma = StubGemma()
    store = SqliteDialogueStore(settings.dialogue_db_path)
    service = ChatService(
        settings,
        local_provider=local,
        network_provider=network,
        gemma_manager=gemma,
        dialogue_store=store,
    )
    return service, store, local, network, gemma


def test_history_survives_provider_switch_and_stores_factual_model(tmp_path):
    service, store, local, network, gemma = build(tmp_path)
    dialogue = store.create_dialogue("test")

    service.select_provider("local", dialogue_id=dialogue["dialogue_id"])
    first = service.ask(dialogue["dialogue_id"], "hello", provider="local")
    assert first["answer"]["model"] == "gemma-4-12b.gguf"

    service.select_provider("network", dialogue_id=dialogue["dialogue_id"])
    second = service.ask(dialogue["dialogue_id"], "again", provider="network")
    assert second["answer"]["model"] == "deepseek-chat"

    messages = store.list_messages(dialogue["dialogue_id"])
    # user, assistant, user, assistant — history is preserved across the switch.
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["model"] == "gemma-4-12b.gguf"
    assert messages[3]["model"] == "deepseek-chat"


def test_switching_to_network_stops_only_own_process(tmp_path):
    service, store, local, network, gemma = build(tmp_path)
    dialogue = store.create_dialogue("test")
    service.select_provider("local", dialogue_id=dialogue["dialogue_id"])
    service.ensure_local_ready()
    assert gemma.starts == 1
    service.select_provider("network", dialogue_id=dialogue["dialogue_id"])
    assert gemma.stops == 1
    # Switching back to local restarts properly.
    service.select_provider("local", dialogue_id=dialogue["dialogue_id"])
    service.ensure_local_ready()
    assert gemma.starts == 2


def test_dialogue_storage_is_isolated_between_databases(tmp_path):
    service_a, store_a, *_ = build(tmp_path)
    dialogue_a = store_a.create_dialogue("only-a")
    service_a.ask(dialogue_a["dialogue_id"], "hi", provider="local")

    other = SqliteDialogueStore(str(tmp_path / "other.db"))
    assert other.list_dialogues() == []
    other.close()


def test_saved_task_memory_reaches_model_after_old_history_is_trimmed(tmp_path):
    service, store, local, _, _ = build(tmp_path)
    dialogue = store.create_dialogue("memory")
    did = dialogue["dialogue_id"]
    store.save_memory(did, {"goal": "Explain ORION", "constraints": ["Answer in Russian"]}, expected_version=0)
    for _ in range(8):
        store.append_message({"dialogue_id": did, "role": "user", "text": "old " * 800})
    captured = []
    original = local.chat
    def observe(messages, options=None):
        captured.extend(messages)
        return original(messages, options)
    local.chat = observe
    service.ask(did, "What is my goal?", provider="local")
    assert "Explain ORION" in captured[0].content
    assert "Answer in Russian" in captured[0].content
    assert sum(len(m.content) for m in captured) <= 12000
    service.close()
