"""D25 ConversationService: fresh retrieval, memory, idempotency, isolation."""

from __future__ import annotations

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.errors import (
    ContextOverflow,
    DeletionRequiresConfirmation,
    InvalidMemoryOperation,
    MemoryConflict,
)
from knowledge_agent.service.conversation_service import ConversationService
from knowledge_agent.storage.conversation_store import SqliteConversationStore

from tests.helpers import FakeChatModel, FakeKnowledge, fragment


def _service(tmp_path, fragments=None, **kwargs):
    knowledge = FakeKnowledge(fragments=fragments if fragments is not None else [fragment("a" * 64)])
    chat_model = FakeChatModel(text="the answer")
    chat = ChatService(
        knowledge,
        chat_model,
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=False,
    )
    store = SqliteConversationStore(tmp_path / "conversations.db")
    service = ConversationService(
        store,
        chat,
        history_max_turns=kwargs.get("history_max_turns", 2),
        history_max_tokens=kwargs.get("history_max_tokens", 100000),
    )
    return service, store, knowledge, chat_model


def _ask(service, dialogue_id, question, client_id, **overrides):
    payload = {
        "client_turn_id": client_id,
        "question": question,
        "mode": "with_rag",
        "collection_id": "c1",
        "top_k": 3,
        "grounding": False,
    }
    payload.update(overrides)
    return service.ask(dialogue_id, payload)


def test_every_question_runs_a_fresh_retrieval_and_dialogues_are_isolated(tmp_path):
    service, store, knowledge, _ = _service(tmp_path)
    first = service.create_dialogue("A")["dialogue_id"]
    second = service.create_dialogue("B")["dialogue_id"]

    _ask(service, first, "Первый вопрос про память", "c1")
    _ask(service, first, "Второй вопрос про планирование", "c2")
    assert knowledge.search_calls == 2

    assert store.count_turns(first) == 2
    assert store.count_turns(second) == 0
    store.close()


def test_repeated_client_turn_id_does_not_create_a_second_turn(tmp_path):
    service, store, knowledge, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    first = _ask(service, dialogue, "Один вопрос", "same")
    again = _ask(service, dialogue, "Один вопрос", "same")
    assert again["turn_id"] == first["turn_id"]
    assert store.count_turns(dialogue) == 1
    assert knowledge.search_calls == 1
    store.close()


def test_goal_and_pinpoint_constraint_update_preserve_other_conditions(tmp_path):
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _ask(service, dialogue, "Цель: изучить RAG", "c1")
    _ask(service, dialogue, "условие: без кода", "c2")
    _ask(service, dialogue, "условие: коротко", "c3")
    memory = service.get_memory(dialogue)
    assert memory["goal"]["text"] == "изучить RAG"
    assert len(memory["constraints"]) == 2

    _ask(service, dialogue, 'измени условие "без кода" на "код можно, только Python"', "c4")
    memory = service.get_memory(dialogue)
    active = [item for item in memory["constraints"] if item["status"] == "active"]
    cancelled = [item for item in memory["constraints"] if item["status"] == "cancelled"]
    assert memory["goal"]["text"] == "изучить RAG"
    assert len(active) == 2 and len(cancelled) == 1
    assert any("Python" in item["text"] for item in active)
    assert any(item["text"] == "коротко" and item["status"] == "active" for item in memory["constraints"])
    store.close()


def test_now_update_pinpoints_the_relevant_condition(tmp_path):
    # Correction defect 5: with several active conditions, "Теперь код можно…"
    # changes only "без кода" and keeps the goal and the remaining conditions.
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _ask(service, dialogue, "Цель: изучить агентов", "c0")
    _ask(service, dialogue, "условие: без кода", "c1")
    _ask(service, dialogue, "условие: коротко", "c2")
    turn = _ask(service, dialogue, "Теперь код можно, но только Python", "c3")
    assert turn["status"] == "ok"
    memory = service.get_memory(dialogue)
    active = {item["text"] for item in memory["constraints"] if item["status"] == "active"}
    assert "код можно, но только Python" in active and "коротко" in active and "без кода" not in active
    assert memory["goal"]["text"] == "изучить агентов"
    store.close()


def test_ambiguous_now_update_with_two_matching_conditions_asks_clarification(tmp_path):
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _ask(service, dialogue, "условие: без кода", "c1")
    _ask(service, dialogue, "условие: без кода в примерах", "c2")
    before = service.get_memory(dialogue)["version"]
    turn = _ask(service, dialogue, "Теперь код можно, но только Python", "c3")
    assert turn["status"] == "clarification"
    assert service.get_memory(dialogue)["version"] == before
    store.close()


def test_ordinary_formulation_persists_goal_and_conditions_then_pinpoint_change(tmp_path):
    # Correction defect 5, service level: the ordinary formulation creates the
    # goal and all three conditions; the follow-up changes only "без кода".
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(
        service,
        dialogue,
        "Хочу разобраться в архитектуре агента. Объясняй кратко, по-русски и без кода.",
        "c1",
    )
    assert turn["status"] == "ok"
    memory = service.get_memory(dialogue)
    assert memory["goal"]["text"] == "разобраться в архитектуре агента"
    active = {item["text"] for item in memory["constraints"] if item["status"] == "active"}
    assert {"кратко", "по-русски", "без кода"} <= active
    _ask(service, dialogue, "Теперь код можно, но только Python", "c2")
    memory = service.get_memory(dialogue)
    active = {item["text"] for item in memory["constraints"] if item["status"] == "active"}
    assert {"кратко", "по-русски", "код можно, но только Python"} == active
    assert memory["goal"]["text"] == "разобраться в архитектуре агента"
    store.close()


def test_assistant_answer_alone_does_not_change_memory(tmp_path):
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    _ask(service, dialogue, "Цель: изучить RAG", "c1")
    before = service.get_memory(dialogue)["version"]
    _ask(service, dialogue, "Расскажи про планирование", "c2")
    assert service.get_memory(dialogue)["version"] == before
    store.close()


def test_ambiguous_reference_requests_clarification_without_retrieval(tmp_path):
    service, store, knowledge, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "а второй вариант?", "c1")
    assert turn["status"] == "clarification"
    assert turn["retrieval_performed"] is False
    assert turn["reference_resolution"]["ambiguous"] is True
    # SPEC D25 11.2: the clarification exposes the question, not only a flag.
    question = turn["reference_resolution"]["clarification_question"]
    assert question
    assert question == turn["answer"]["text"]
    assert knowledge.search_calls == 0
    store.close()


def test_sources_carry_the_real_fragment_score(tmp_path):
    # Regression: ``sources[].score`` was always null because it was read from
    # ``metadata`` while the score is a top-level fragment/candidate field.
    fragments = [
        fragment("a" * 64, rank=1, score=0.61),
        fragment("b" * 64, rank=2, score=0.42),
    ]
    service, store, _, _ = _service(tmp_path, fragments=fragments)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "вопрос", "c1")
    assert turn["sources"], "passed chunks must produce sources"
    expected = {item["chunk_id"]: item["score"] for item in fragments}
    for source in turn["sources"]:
        assert source["score"] is not None
        assert source["score"] == expected[source["chunk_id"]]
    store.close()


def test_context_overflow_propagates_and_stores_no_turn(tmp_path):
    # SPEC D25 6.5: overflow is detected before the model call and surfaces as a
    # typed 422; a failed turn must not be persisted as a stored error turn.
    service, store, _, chat_model = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    with pytest.raises(ContextOverflow):
        _ask(service, dialogue, "вопрос", "c1", max_context_tokens=1)
    assert store.count_turns(dialogue) == 0
    assert chat_model.calls == []
    store.close()


def test_sources_are_a_subset_of_passed_chunks(tmp_path):
    fragments = [fragment("a" * 64), fragment("b" * 64), fragment("c" * 64)]
    service, store, _, _ = _service(tmp_path, fragments=fragments)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "вопрос", "c1")
    assert turn["retrieval_performed"] is True
    assert set(turn["retrieval"]["passed_chunk_ids"]) <= {item["chunk_id"] for item in fragments}
    assert {item["chunk_id"] for item in turn["sources"]} <= set(turn["retrieval"]["passed_chunk_ids"])
    store.close()


def test_history_window_is_bounded_but_history_is_fully_saved(tmp_path):
    service, store, _, _ = _service(tmp_path, history_max_turns=2)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    for number in range(1, 5):
        turn = _ask(service, dialogue, f"вопрос номер {number}", f"c{number}")
    assert store.count_turns(dialogue) == 4
    assert turn["context"]["history_turns_used"] <= 2
    store.close()


def test_patch_memory_requires_expected_version_and_grounds(tmp_path):
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    turn = _ask(service, dialogue, "Цель: начальная", "c1")
    current = service.get_memory(dialogue)
    with pytest.raises(MemoryConflict):
        service.patch_memory(
            dialogue,
            expected_version=999,
            operations=[{"op": "set_goal", "text": "next", "grounds": [turn["turn_id"]]}],
        )
    with pytest.raises(InvalidMemoryOperation):
        service.patch_memory(
            dialogue,
            expected_version=current["version"],
            operations=[{"op": "set_goal", "text": "guessed", "grounds": ["assistant-1"]}],
        )
    updated = service.patch_memory(
        dialogue,
        expected_version=current["version"],
        operations=[{"op": "set_goal", "text": "обновлённая цель", "grounds": [turn["turn_id"]]}],
    )
    assert updated["goal"]["text"] == "обновлённая цель"
    store.close()


def test_delete_requires_confirmation(tmp_path):
    service, store, _, _ = _service(tmp_path)
    dialogue = service.create_dialogue("A")["dialogue_id"]
    with pytest.raises(DeletionRequiresConfirmation):
        service.delete_dialogue(dialogue, confirm=False)
    assert store.get_dialogue(dialogue) is not None
    service.delete_dialogue(dialogue, confirm=True)
    assert store.get_dialogue(dialogue) is None
    store.close()


@pytest.mark.parametrize("specified,expected", [(None,10),(5,5)])
def test_conversation_breadth_is_explicit_and_never_ignores_requested_limit(tmp_path,specified,expected):
    service,store,knowledge,_=_service(tmp_path,fragments=[fragment(hex(i)[2:].zfill(64),"A fragment") for i in range(15)])
    dialogue=service.create_dialogue("A")["dialogue_id"]
    seen=[]
    original=knowledge.search
    def search(*args,**kwargs):
        seen.append(kwargs["top_k"])
        result=original(*args,**kwargs)
        result["fragments"]=result["fragments"][:kwargs["top_k"]]
        return result
    knowledge.search=search
    result=_ask(service,dialogue,"What is documented?","c1",top_k=specified,use_filter=False)
    assert seen==[expected]
    assert result["settings"]["top_k"]==expected
    assert result["settings"]["prefilter_top_k"]==expected
    assert result["settings"]["postfilter_top_k"]==expected
    assert result["retrieval"]["passed_count"]==expected
    store.close()
