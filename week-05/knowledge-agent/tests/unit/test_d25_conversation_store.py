"""D25 store: dialogue CRUD, turn idempotency, isolation and memory locking."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from knowledge_agent.domain.errors import (
    ConversationStoreSchemaUnsupported,
    MemoryConflict,
)
from knowledge_agent.storage.conversation_store import (
    CONVERSATION_SCHEMA_VERSION,
    SqliteConversationStore,
)


def _store(tmp_path: Path) -> SqliteConversationStore:
    return SqliteConversationStore(tmp_path / "conversations.db")


def _turn(dialogue_id: str, number: int, client_turn_id: str | None = None) -> dict:
    return {
        "schema_version": "dialogue-turn-v1",
        "turn_id": f"turn-{number}",
        "dialogue_id": dialogue_id,
        "ordinal": number,
        "client_turn_id": client_turn_id or f"client-{number}",
        "created_at": "2026-10-03T00:00:00+00:00",
        "user_message": f"question {number}",
        "answer": {"text": f"answer {number}"},
    }


def test_dialogue_crud_and_turn_isolation(tmp_path):
    store = _store(tmp_path)
    first = store.create_dialogue("First")
    second = store.create_dialogue("Second")
    assert first["dialogue_id"] != second["dialogue_id"]
    assert {item["dialogue_id"] for item in store.list_dialogues()} == {
        first["dialogue_id"],
        second["dialogue_id"],
    }

    store.append_turn(_turn(first["dialogue_id"], 1))
    store.append_turn(_turn(first["dialogue_id"], 2))
    assert store.count_turns(first["dialogue_id"]) == 2
    assert store.count_turns(second["dialogue_id"]) == 0
    assert store.list_turns(second["dialogue_id"]) == []

    renamed = store.rename_dialogue(first["dialogue_id"], "Renamed")
    assert renamed["name"] == "Renamed"
    assert store.get_dialogue(first["dialogue_id"])["turn_count"] == 2

    store.delete_dialogue(first["dialogue_id"])
    assert store.get_dialogue(first["dialogue_id"]) is None
    assert store.count_turns(first["dialogue_id"]) == 0
    assert store.get_dialogue(second["dialogue_id"]) is not None
    store.close()


def test_append_turn_is_idempotent_by_client_turn_id(tmp_path):
    store = _store(tmp_path)
    dialogue = store.create_dialogue()["dialogue_id"]
    first = store.append_turn(_turn(dialogue, 1, client_turn_id="repeat"))
    again = store.append_turn(_turn(dialogue, 1, client_turn_id="repeat"))
    assert again == first
    assert store.count_turns(dialogue) == 1
    assert store.get_turn_by_client_id(dialogue, "repeat")["turn_id"] == first["turn_id"]
    store.close()


def test_turn_pagination_is_ordered(tmp_path):
    store = _store(tmp_path)
    dialogue = store.create_dialogue()["dialogue_id"]
    for number in range(1, 8):
        store.append_turn(_turn(dialogue, number))
    ascending = [item["turn_id"] for item in store.list_turns(dialogue, limit=10)]
    assert ascending == [f"turn-{n}" for n in range(1, 8)]
    latest = store.list_turns(dialogue, limit=3, ascending=False)
    assert [item["turn_id"] for item in latest] == ["turn-7", "turn-6", "turn-5"]
    store.close()


def test_stored_ordinal_is_persisted_and_drives_before_pagination(tmp_path):
    # Regression: the service assembles the turn before the store assigns the
    # ordinal, so the payload carries ``ordinal: None``. The stored
    # dialogue-turn-v1 record must expose the real ordinal, otherwise the
    # ``before=`` page cursor is unusable from saved records.
    store = _store(tmp_path)
    dialogue = store.create_dialogue()["dialogue_id"]
    for number in range(1, 8):
        turn = _turn(dialogue, number)
        turn["ordinal"] = None
        assert store.append_turn(turn)["ordinal"] == number

    assert store.get_turn(dialogue, "turn-3")["ordinal"] == 3

    older = store.list_turns(dialogue, limit=3, before=5)
    assert [item["turn_id"] for item in older] == ["turn-1", "turn-2", "turn-3"]
    assert [item["ordinal"] for item in older] == [1, 2, 3]

    latest = store.list_turns(dialogue, limit=3, ascending=False)
    assert [item["ordinal"] for item in latest] == [7, 6, 5]
    store.close()


def test_task_memory_is_a_separate_table_and_versioned(tmp_path):
    store = _store(tmp_path)
    dialogue = store.create_dialogue()["dialogue_id"]
    empty = store.get_memory(dialogue)
    assert empty["schema_version"] == "task-memory-v1"
    assert empty["version"] == 0 and empty["goal"] is None

    updated = {
        "schema_version": "task-memory-v1",
        "dialogue_id": dialogue,
        "version": 1,
        "goal": {"item_id": "goal-1", "text": "learn", "status": "confirmed", "grounds": ["turn-1"]},
        "clarifications": [],
        "constraints": [],
        "terms": [],
    }
    saved = store.save_memory(dialogue, updated, expected_version=0)
    assert saved["version"] == 1
    # The turns table is physically distinct from the memory table.
    with sqlite3.connect(str(tmp_path / "conversations.db")) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"turns", "task_memories", "dialogues"} <= tables
    store.close()


def test_stale_expected_version_raises_memory_conflict(tmp_path):
    store = _store(tmp_path)
    dialogue = store.create_dialogue()["dialogue_id"]
    store.save_memory(
        dialogue, {"dialogue_id": dialogue, "version": 1}, expected_version=0
    )
    with pytest.raises(MemoryConflict) as excinfo:
        store.save_memory(
            dialogue, {"dialogue_id": dialogue, "version": 2}, expected_version=0
        )
    assert excinfo.value.code == "memory_conflict"
    assert store.get_memory(dialogue)["version"] == 1
    store.close()


def test_unknown_schema_version_fails_closed(tmp_path):
    path = tmp_path / "conversations.db"
    store = SqliteConversationStore(path)
    store.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE conversation_meta SET value=? WHERE key='schema_version'",
            (str(CONVERSATION_SCHEMA_VERSION + 1),),
        )
    with pytest.raises(ConversationStoreSchemaUnsupported):
        SqliteConversationStore(path)


def test_migration_is_idempotent_and_does_not_import_legacy_runs(tmp_path):
    path = tmp_path / "conversations.db"
    first = SqliteConversationStore(path)
    first.ensure_schema()
    first.ensure_schema()
    assert first.list_dialogues() == []
    first.close()
    second = SqliteConversationStore(path)
    assert second.list_dialogues() == []
    second.close()
