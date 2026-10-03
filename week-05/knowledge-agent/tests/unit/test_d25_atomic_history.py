"""Atomic memory grounds, concurrent duplicates and recent history pages."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from knowledge_agent.domain.errors import MemoryConflict
from knowledge_agent.storage.conversation_store import SqliteConversationStore
from tests.unit.test_d25_conversation_store import _turn
from tests.unit.test_d25_conversation_service import _service, _ask

def memory(dialogue, turn):
    return {"schema_version":"task-memory-v1", "dialogue_id":dialogue,"version":1,
            "goal":{"text":"learn","status":"confirmed","grounds":[turn]}}

def test_turn_insert_failure_rolls_confirmed_memory_back(tmp_path):
    store=SqliteConversationStore(tmp_path/'dialogues.db')
    dialogue=store.create_dialogue()["dialogue_id"]
    # Simulate a real storage write rejection, after the memory SQL has run.
    with store._conn:
        store._conn.execute("CREATE TRIGGER reject_turn BEFORE INSERT ON turns BEGIN SELECT RAISE(ABORT,'write rejected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_turn(_turn(dialogue,1),memory(dialogue,"turn-1"),expected_version=0)
    assert store.count_turns(dialogue)==0
    assert store.get_memory(dialogue)["version"]==0
    assert store.get_memory(dialogue)["goal"] is None
    store.close()

def test_concurrent_duplicate_returns_one_turn_and_one_memory_change(tmp_path):
    store=SqliteConversationStore(tmp_path/'dialogues.db')
    dialogue=store.create_dialogue()["dialogue_id"]
    def submit(number):
        turn=_turn(dialogue,number,client_turn_id="same")
        return store.commit_turn(turn,memory(dialogue,turn["turn_id"]),expected_version=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=list(pool.map(submit,[1,2]))
    assert first==second
    assert store.count_turns(dialogue)==1
    saved=store.get_memory(dialogue)
    assert saved["version"]==1
    assert saved["goal"]["grounds"]==[first["turn_id"]]
    store.close()

def test_new_turn_conflicts_with_a_concurrent_memory_edit(tmp_path):
    store=SqliteConversationStore(tmp_path/'dialogues.db')
    dialogue=store.create_dialogue()["dialogue_id"]
    store.commit_turn(_turn(dialogue,1),memory(dialogue,"turn-1"),expected_version=0)
    with pytest.raises(MemoryConflict):
        store.commit_turn(_turn(dialogue,2),None,expected_version=0)
    assert store.count_turns(dialogue)==1
    store.close()

def test_atomically_saved_memory_and_turn_survive_reopen(tmp_path):
    path=tmp_path/'dialogues.db'
    store=SqliteConversationStore(path)
    dialogue=store.create_dialogue()["dialogue_id"]
    store.commit_turn(_turn(dialogue,1),memory(dialogue,"turn-1"),expected_version=0)
    store.close()
    reopened=SqliteConversationStore(path)
    saved=reopened.get_memory(dialogue)
    assert reopened.get_turn(dialogue,saved["goal"]["grounds"][0])
    reopened.close()

def test_service_turn_pages_are_recent_and_older_pages_stay_chronological(tmp_path):
    service,store,_,_=_service(tmp_path)
    dialogue=service.create_dialogue()["dialogue_id"]
    for number in range(1,9):
        store.append_turn(_turn(dialogue,number))
    latest=service.list_turns(dialogue,limit=3)
    assert [t["ordinal"] for t in latest["turns"]]==[6,7,8]
    older=service.list_turns(dialogue,limit=3,before=str(latest["turns"][0]["ordinal"]))
    assert [t["ordinal"] for t in older["turns"]]==[3,4,5]
    assert latest["total"]==older["total"]==8
    store.close()

def test_generation_uses_recent_history_beyond_500_turns(tmp_path):
    service,store,knowledge,model=_service(tmp_path,history_max_turns=2)
    dialogue=service.create_dialogue()["dialogue_id"]
    for number in range(1,503):
        store.append_turn(_turn(dialogue,number))
    turn=_ask(service,dialogue,"Расскажи про память","new")
    contents=[m.content for m in model.calls[-1]]
    assert "question 502" in contents
    assert "question 500" not in contents
    assert turn["context"]["history_turns_used"]==2
    assert store.count_turns(dialogue)==503
    # Grounds from both old and new saved USER turns remain checkable without
    # sending their entire history to the model.
    updated=service.patch_memory(dialogue,expected_version=0,
        operations=[{"op":"set_goal","text":"next","grounds":["turn-502"]}])
    assert updated["goal"]["grounds"]==["turn-502"]
    store.close()
