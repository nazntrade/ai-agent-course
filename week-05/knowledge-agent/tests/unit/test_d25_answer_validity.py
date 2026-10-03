"""D25 answer validity: empty, truncated and refused answers are not success."""

from __future__ import annotations

import json

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.service.conversation_service import (
    ConversationService,
    is_task_state_summary_request,
)
from knowledge_agent.storage.conversation_store import SqliteConversationStore

from tests.helpers import FakeChatModel, FakeKnowledge, fragment


def _service(tmp_path, *, fragments=None, chat_model=None):
    knowledge = FakeKnowledge(fragments=fragments if fragments is not None else [fragment("a" * 64)])
    chat = ChatService(
        knowledge,
        chat_model or FakeChatModel(text="answer"),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=False,
    )
    store = SqliteConversationStore(tmp_path / "conversations.db")
    return ConversationService(store, chat), store, knowledge, chat


def _ask(service, dialogue, client_id, **overrides):
    payload = {
        "client_turn_id": client_id,
        "question": "вопрос",
        "mode": "with_rag",
        "collection_id": "c1",
        "top_k": 3,
        "grounding": False,
    }
    payload.update(overrides)
    return service.ask(dialogue, payload)


def test_empty_provider_answer_is_stored_as_error_not_ok(tmp_path):
    service, store, _, _ = _service(tmp_path, chat_model=FakeChatModel(text=""))
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1")
    assert turn["status"] == "error"
    assert turn["error"]["code"] == "chat_invalid_response"
    assert turn["answer"]["text"] == ""
    assert store.get_turn(dialogue, turn["turn_id"])["status"] == "error"
    store.close()


def test_truncated_answer_is_incomplete_not_ok(tmp_path):
    service, store, _, _ = _service(tmp_path, chat_model=FakeChatModel(text="cut off", finish_reason="length"))
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1")
    assert turn["status"] == "incomplete"
    assert turn["answer"]["truncated"] is True
    store.close()


def test_non_grounded_inline_citations_are_projected_as_dicts(tmp_path):
    # Regression: the free-text RAG path reports inline citations as chunk-id
    # strings; the dialogue turn must not crash on ``dict(str)``.
    service, store, _, _ = _service(
        tmp_path, chat_model=FakeChatModel(text=f"Answer citing [{'a' * 64}] inline.")
    )
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1")
    assert turn["status"] == "ok"
    assert any(citation.get("chunk_id") == "a" * 64 for citation in turn["citations"])
    store.close()


def test_no_selected_sources_refuses_without_calling_the_model(tmp_path):
    chat_model = FakeChatModel(text="should not run")
    service, store, _, _ = _service(tmp_path, fragments=[], chat_model=chat_model)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1", use_filter=True, min_score=0.9)
    assert turn["status"] == "refused"
    assert turn["answer"]["insufficient_sources"] is True
    assert chat_model.calls == []
    store.close()


def _grounded_service(tmp_path, text: str):
    knowledge = FakeKnowledge(fragments=[fragment("a" * 64, "Memory stores observations.")])
    chat = ChatService(
        knowledge,
        FakeChatModel(text=text),
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=True,
    )
    store = SqliteConversationStore(tmp_path / "conversations.db")
    return ConversationService(store, chat), store


def _grounded_json(answer: str, citations: list[dict], insufficient: bool = False) -> str:
    return json.dumps(
        {"answer": answer, "citations": citations, "insufficient": insufficient, "limitation": None}
    )


def test_bare_insufficient_marker_is_replaced_by_a_user_facing_refusal(tmp_path):
    # Correction defect 5: scenario B turn 9 displayed the literal marker
    # ``insufficient``. The user must get a clear refusal in the dialogue
    # language instead of a service marker.
    text = _grounded_json("insufficient", [], insufficient=True)
    service, store = _grounded_service(tmp_path, text)
    dialogue = service.create_dialogue("B")["dialogue_id"]
    turn = _ask(service, dialogue, "c1", grounding=True, question="Какая сегодня погода в Москве?")

    assert turn["status"] == "refused"
    assert turn["answer"]["insufficient_sources"] is True
    assert turn["answer"]["text"].strip().lower() != "insufficient"
    assert "нет ответа" in turn["answer"]["text"].lower()
    store.close()


def test_refusal_does_not_keep_verified_citations(tmp_path):
    # Consistency (C07): a refusal with verified citations would contradict the
    # displayed text, so the refusal turn must not carry citations.
    text = _grounded_json(
        "insufficient",
        [{"chunk_id": "a" * 64, "quote": "Memory stores observations."}],
        insufficient=True,
    )
    service, store = _grounded_service(tmp_path, text)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1", grounding=True, question="Что описано в разделе?")

    assert turn["status"] == "refused"
    assert turn["citations"] == []
    store.close()


def test_citation_failure_is_not_displayed_as_a_refusal(tmp_path):
    # Correction defect 4: documents were passed but the citation is unknown;
    # this is a citation failure with its own clear reason, not a refusal and
    # not a successful substantive answer.
    text = _grounded_json("Plan [bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb].",
                          [{"chunk_id": "b" * 64, "quote": "not in any passed chunk"}])
    service, store = _grounded_service(tmp_path, text)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1", grounding=True, question="Дай итоговый план с источниками")

    assert turn["status"] == "citation_failed"
    assert turn["answer"]["insufficient_sources"] is False
    assert "подтвердить" in turn["answer"]["text"].lower()
    store.close()


# -- Correction A12: task-state summary must not degrade into a refusal -------

def _seed_task_state(service, dialogue):
    _ask(service, dialogue, "c1", question="Цель: научиться строить RAG-ассистентов")
    _ask(service, dialogue, "c2", question="условие: код можно, только Python")


def test_mandatory_task_state_summary_is_not_suppressed_by_insufficient(tmp_path):
    # Correction A12: the grounded model legitimately (but wrongly) flagged
    # ``insufficient=true`` for a summary of the user's own conditions. The turn
    # is task-state, so it must become a substantive summary from task memory,
    # not a documentary refusal, and must stay internally consistent.
    text = _grounded_json("insufficient", [], insufficient=True)
    service, store = _grounded_service(tmp_path, text)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _seed_task_state(service, dialogue)

    turn = _ask(service, dialogue, "c3", grounding=True, question="Подведи итог с учётом всех условий")

    assert turn["status"] == "ok"
    assert turn["answer"]["insufficient_sources"] is False
    assert turn["answer"]["grounding_status"] is None
    assert turn["answer"]["task_state_summary"] is True
    assert turn["answer"]["generation_performed"] is False
    assert turn["retrieval_performed"] is True
    assert turn["usage"] is None
    assert turn["citations"] == []
    body = turn["answer"]["text"]
    assert "научиться строить RAG-ассистентов" in body
    assert "код можно, только Python" in body
    assert "нет ответа" not in body.lower()
    store.close()


def test_task_state_summary_is_substantive_not_a_bare_marker(tmp_path):
    # Regression: the resulting text is a real user-facing summary (goal +
    # active condition), never a bare service marker or refusal sentence.
    text = _grounded_json("insufficient", [], insufficient=True)
    service, store = _grounded_service(tmp_path, text)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _seed_task_state(service, dialogue)

    turn = _ask(service, dialogue, "c3", grounding=True, question="Перечисли сохранённые условия")

    assert turn["status"] == "ok"
    body = turn["answer"]["text"].strip().lower()
    assert body != "insufficient"
    assert len(body.split()) >= 4
    assert "код можно, только python" in body
    store.close()


def test_task_state_summary_detector_excludes_documentary_questions():
    # The detector must not reroute genuine documentary turns: a plan with
    # sources, a weak-context question or an out-of-corpus question keep the
    # documentary grounded outcome.
    memory = {
        "goal": {"text": "научиться строить RAG-ассистентов", "status": "confirmed"},
        "constraints": [{"text": "код можно, только Python", "status": "active"}],
        "clarifications": [],
        "terms": [],
    }
    assert is_task_state_summary_request("Подведи итог с учётом всех условий", memory) is True
    assert is_task_state_summary_request("Перечисли сохранённые условия", memory) is True
    assert is_task_state_summary_request("Дай итоговый план с источниками", memory) is False
    assert is_task_state_summary_request("Какая сегодня погода в Москве?", memory) is False
    assert is_task_state_summary_request("Какие компоненты нужны для RAG-системы?", memory) is False


def test_documentary_refusal_is_still_a_refusal_when_task_state_is_empty(tmp_path):
    # Consistency (C07): the task-state override only applies to turns that ask
    # about the user's own conditions; without task state the same grounded
    # insufficient answer stays a clear refusal.
    text = _grounded_json("insufficient", [], insufficient=True)
    service, store = _grounded_service(tmp_path, text)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "c1", grounding=True, question="Что описано в разделе про память?")

    assert turn["status"] == "refused"
    assert turn["answer"]["insufficient_sources"] is True
    assert turn["citations"] == []
    store.close()
