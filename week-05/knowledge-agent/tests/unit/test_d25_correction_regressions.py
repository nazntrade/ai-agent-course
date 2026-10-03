"""Correction regressions for memory/answer consistency and goal use (D25 3, 6).

These tests assert on the actual outgoing generation prompt and the produced
answer, not only on the stored task-memory record.
"""

from __future__ import annotations

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.contracts import ChatResult
from knowledge_agent.service.conversation_service import ConversationService
from knowledge_agent.storage.conversation_store import SqliteConversationStore

from tests.helpers import FakeChatModel, FakeKnowledge, fragment

TASK_STATE_MARKER = "not from <context>"


class EchoPromptChatModel(FakeChatModel):
    """Deterministic model whose answer is its own outgoing prompt.

    This lets a unit test verify that the goal and the active conditions are
    present in the actual generation call and can therefore reach the answer
    (a DB-only check would not prove this).
    """

    def chat(self, messages, options=None):
        self.calls.append(list(messages))
        self.options_seen.append(dict(options) if options else None)
        text = " ".join(message.content for message in messages)
        return ChatResult(
            text=text,
            finish_reason="stop",
            usage=None,
            model=self.model,
            created_at="2026-10-01T00:00:00+00:00",
            latency_ms=1.0,
            output_tokens_per_second=None,
        )


def _service(tmp_path, chat_model=None):
    knowledge = FakeKnowledge(fragments=[fragment("a" * 64, "memory chunk")])
    chat = ChatService(
        knowledge,
        chat_model or EchoPromptChatModel(text="unused"),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=False,
    )
    store = SqliteConversationStore(tmp_path / "conversations.db")
    service = ConversationService(store, chat)
    return service, store, chat.chat_model


def _ask(service, dialogue, question, client_id):
    return service.ask(
        dialogue,
        {
            "client_turn_id": client_id,
            "question": question,
            "mode": "with_rag",
            "collection_id": "c1",
            "top_k": 3,
            "grounding": False,
        },
    )


def test_condition_change_prompt_uses_task_state_not_documents(tmp_path):
    # Correction defect 3: after «коротко» -> «подробно» the stored memory and
    # the answer prompt agree, and the user's condition comes from
    # <task_memory>, not from the retrieved PDF context.
    service, store, chat_model = _service(tmp_path)
    dialogue = service.create_dialogue("B")["dialogue_id"]
    _ask(service, dialogue, "Цель: составить план внедрения RAG-ассистента", "c1")
    _ask(service, dialogue, "условие: без облачных сервисов", "c2")
    _ask(service, dialogue, "условие: только локальные модели", "c3")
    _ask(service, dialogue, "условие: коротко", "c4")
    turn = _ask(service, dialogue, 'измени условие "коротко" на "подробно"', "c5")

    memory = service.get_memory(dialogue)
    active = {item["text"] for item in memory["constraints"] if item["status"] == "active"}
    assert "подробно" in active and "коротко" not in active
    assert {"без облачных сервисов", "только локальные модели"} <= active
    assert memory["goal"]["text"] == "составить план внедрения RAG-ассистента"

    assert turn["answer"]["generation_performed"] is False
    assert "подробно" in turn["answer"]["text"]
    # The next documentary answer must use the changed condition in its actual
    # generation prompt. A pure directive itself needs no model call.
    _ask(service, dialogue, "Расскажи о модуле памяти", "c6")
    prompt = " ".join(message.content for message in chat_model.calls[-1])
    assert TASK_STATE_MARKER in prompt
    assert "<task_memory>" in prompt and "goal: составить план внедрения RAG-ассистента" in prompt
    assert "constraint: подробно" in prompt
    assert turn["context"]["mandatory_parts"]["task_memory_tokens"] > 0


def test_returning_to_the_goal_after_a_distraction_uses_goal_and_conditions(tmp_path):
    # Correction defect 6: a conversational reference after a distraction must
    # reach the actual answer through history/task memory, not only the DB.
    service, store, chat_model = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _ask(service, dialogue, "Цель: изучить память агентов", "c1")
    _ask(service, dialogue, "условие: без кода", "c2")
    _ask(service, dialogue, "Сколько будет два плюс два?", "c3")

    turn = _ask(service, dialogue, "как это связано с памятью?", "c4")
    assert turn["reference_resolution"]["used_memory"] is True
    assert "память агентов" in turn["search_query"]

    prompt = " ".join(message.content for message in chat_model.calls[-1])
    assert "goal: изучить память агентов" in prompt
    assert "constraint: без кода" in prompt
    assert TASK_STATE_MARKER in prompt

    # The answer itself is generated from that prompt, so the goal and the
    # active condition are honoured in the answer, not only in the record.
    assert "изучить память агентов" in turn["answer"]["text"]
    assert "без кода" in turn["answer"]["text"]
    assert turn["status"] == "ok"
