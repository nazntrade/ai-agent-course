"""D25 task memory: grounds, pinpoint constraint change, no silent guesses."""

from __future__ import annotations

from knowledge_agent.chat.memory import extract, validate_patch
from knowledge_agent.domain.contracts import MemoryOperation, TaskMemory

NOW = "2026-10-03T00:00:00+00:00"


def _memory(**kwargs) -> TaskMemory:
    return TaskMemory(dialogue_id="d1", **kwargs)


def test_goal_and_constraint_require_and_accept_user_grounds():
    memory = _memory()
    known = {"turn-1"}
    proposal = extract(memory, "Цель: изучить RAG", ground_turn_id="turn-1", known_user_turn_ids=known, now=NOW)
    updated, report = validate_patch(memory, proposal["operations"], known_user_turn_ids=known, now=NOW)
    assert report["rejected"] == []
    assert updated.version == 1
    assert updated.goal["text"] == "изучить RAG"
    assert updated.goal["grounds"] == ["turn-1"]

    proposal = extract(memory, "условие: без кода", ground_turn_id="turn-1", known_user_turn_ids=known, now=NOW)
    updated, report = validate_patch(updated, proposal["operations"], known_user_turn_ids=known, now=NOW)
    assert updated.constraints[0]["text"] == "без кода"
    assert updated.constraints[0]["status"] == "active"


def test_operation_without_grounds_is_rejected_not_silently_dropped():
    memory = _memory()
    operation = MemoryOperation(op="set_goal", text="guess", grounds=[])
    updated, report = validate_patch(memory, [operation], known_user_turn_ids={"turn-1"}, now=NOW)
    assert updated.version == 0 and updated.goal is None
    assert report["rejected"] == [{"op": "set_goal", "reason": "missing_grounds"}]


def test_assistant_or_document_ground_is_not_a_user_turn():
    memory = _memory()
    # An assistant answer / document / model guess has no user turn id.
    operation = MemoryOperation(op="set_goal", text="invented", grounds=["assistant-answer-1"])
    updated, report = validate_patch(memory, [operation], known_user_turn_ids={"turn-1"}, now=NOW)
    assert updated.goal is None
    assert report["rejected"][0]["reason"] == "unknown_ground"


def test_update_constraint_changes_only_target_and_preserves_others():
    memory = _memory(
        goal={"item_id": "g", "text": "keep goal", "status": "confirmed", "grounds": ["turn-1"], "updated_at": NOW},
        constraints=[
            {"item_id": "c1", "text": "без кода", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
            {"item_id": "c2", "text": "без внешних сервисов", "status": "active", "grounds": ["turn-2"], "updated_at": NOW},
        ],
        version=2,
    )
    operation = MemoryOperation(
        op="update_constraint", target_item_id="c1", text="код можно, только Python", grounds=["turn-3"]
    )
    updated, report = validate_patch(memory, [operation], known_user_turn_ids={"turn-1", "turn-2", "turn-3"}, now=NOW)
    assert report["rejected"] == []
    by_id = {item["item_id"]: item for item in updated.constraints}
    assert by_id["c1"]["status"] == "cancelled"
    assert by_id["c1"]["superseded_by"] == "c2" or by_id["c1"]["superseded_by"] in by_id
    replacement = next(item for item in updated.constraints if item["status"] == "active" and item["text"] == "код можно, только Python")
    assert replacement is not None
    assert by_id["c2"]["status"] == "active" and by_id["c2"]["text"] == "без внешних сервисов"
    assert updated.goal["text"] == "keep goal"
    assert updated.version == 3


def test_explicit_cancel_keeps_goal_and_other_constraints():
    memory = _memory(
        goal={"item_id": "g", "text": "keep", "status": "confirmed", "grounds": ["turn-1"], "updated_at": NOW},
        constraints=[
            {"item_id": "c1", "text": "без кода", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
            {"item_id": "c2", "text": "коротко", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
        ],
        version=1,
    )
    operation = MemoryOperation(op="cancel_constraint", target_item_id="c1", grounds=["turn-2"])
    updated, report = validate_patch(memory, [operation], known_user_turn_ids={"turn-1", "turn-2"}, now=NOW)
    assert report["applied"]
    assert next(item for item in updated.constraints if item["item_id"] == "c1")["status"] == "cancelled"
    assert next(item for item in updated.constraints if item["item_id"] == "c2")["status"] == "active"
    assert updated.goal["text"] == "keep"


def test_ambiguous_cancel_with_multiple_active_constraints_asks_for_clarification():
    memory = _memory(
        constraints=[
            {"item_id": "c1", "text": "без кода", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
            {"item_id": "c2", "text": "коротко", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
        ],
        version=1,
    )
    proposal = extract(memory, "отмени условие", ground_turn_id="turn-2", known_user_turn_ids={"turn-1", "turn-2"}, now=NOW)
    assert proposal["clarification"] is not None
    assert not proposal["operations"]


def test_now_phrase_updates_the_only_active_constraint():
    memory = _memory(
        constraints=[{"item_id": "c1", "text": "без кода", "status": "active", "grounds": ["turn-1"], "updated_at": NOW}],
        version=1,
    )
    proposal = extract(
        memory, "Теперь код можно, но только Python", ground_turn_id="turn-2",
        known_user_turn_ids={"turn-1", "turn-2"}, now=NOW,
    )
    updated, report = validate_patch(
        memory, proposal["operations"], known_user_turn_ids={"turn-1", "turn-2"}, now=NOW
    )
    assert report["applied"]
    assert updated.constraints[0]["status"] == "cancelled"
    assert any(item["status"] == "active" and "Python" in item["text"] for item in updated.constraints)


def test_unchanged_memory_keeps_version_when_nothing_confirms():
    memory = _memory(version=4)
    updated, report = validate_patch(memory, [], known_user_turn_ids=set(), now=NOW)
    assert updated.version == 4 and report["applied"] == []


# -- correction defect 5: ordinary explicit formulations -----------------------


def _apply(memory, message, ground="turn-1", known=("turn-1",)):
    proposal = extract(
        memory, message, ground_turn_id=ground, known_user_turn_ids=set(known), now=NOW
    )
    return validate_patch(memory, proposal["operations"], known_user_turn_ids=set(known), now=NOW)


def test_ordinary_formulation_extracts_goal_and_multiple_conditions():
    # Regression: "Хочу разобраться… . Объясняй кратко, по-русски и без кода"
    # previously produced no memory because only the "Цель:/условие:" markers
    # were recognised.
    memory = _memory()
    updated, report = _apply(
        memory,
        "Хочу разобраться в архитектуре агента. Объясняй кратко, по-русски и без кода.",
    )
    assert report["rejected"] == []
    assert updated.goal["text"] == "разобраться в архитектуре агента"
    texts = {item["text"] for item in updated.constraints}
    assert {"кратко", "по-русски", "без кода"} <= texts
    assert all(item["grounds"] == ["turn-1"] for item in updated.constraints)
    assert updated.goal["grounds"] == ["turn-1"]


def test_plain_request_without_conditions_does_not_become_memory():
    memory = _memory()
    updated, report = _apply(memory, "Расскажи про планирование")
    assert report["applied"] == [] and report["rejected"] == []
    assert updated.goal is None and updated.constraints == []
    assert updated.version == 0


def test_now_update_pinpoints_the_relevant_condition_and_preserves_others():
    # Regression: with several active conditions, "Теперь код можно…" must
    # change only "без кода" (shared root "код"), keeping the goal and the
    # unrelated conditions intact.
    memory = _memory(
        goal={"item_id": "g", "text": "изучить агентов", "status": "confirmed", "grounds": ["turn-1"], "updated_at": NOW},
        constraints=[
            {"item_id": "c1", "text": "кратко", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
            {"item_id": "c2", "text": "по-русски", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
            {"item_id": "c3", "text": "без кода", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
        ],
        version=3,
    )
    updated, report = _apply(memory, "Теперь код можно, но только Python", ground="turn-2", known=("turn-1", "turn-2"))
    assert report["rejected"] == []
    by_id = {item["item_id"]: item for item in updated.constraints}
    assert by_id["c3"]["status"] == "cancelled"
    assert by_id["c1"]["status"] == "active" and by_id["c2"]["status"] == "active"
    active_texts = [item["text"] for item in updated.active_constraints()]
    assert active_texts == ["кратко", "по-русски", "код можно, но только Python"]
    assert updated.goal["text"] == "изучить агентов"


def test_now_update_is_ambiguous_when_two_conditions_share_the_target_root():
    memory = _memory(
        constraints=[
            {"item_id": "c1", "text": "без кода", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
            {"item_id": "c2", "text": "без кода в примерах", "status": "active", "grounds": ["turn-1"], "updated_at": NOW},
        ],
        version=2,
    )
    proposal = extract(
        memory, "Теперь код можно, но только Python", ground_turn_id="turn-2",
        known_user_turn_ids={"turn-1", "turn-2"}, now=NOW,
    )
    assert proposal["clarification"] is not None
    assert not proposal["operations"]
