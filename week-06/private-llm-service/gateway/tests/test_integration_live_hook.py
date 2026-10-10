"""Orchestration boundaries for the fixed LIVE hook; real model is never contacted."""
import threading
import pytest
from harness.integration_live import stability,LiveClient,fixture_operation
import asyncio
from contextlib import contextmanager
import json
from types import SimpleNamespace
from harness import integration_live as hook

pytestmark=pytest.mark.integration


def public_records_fixture():
    items=[]
    for index in range(30):
        conversation="conversation-"+str(index)
        items.append({"conversation":conversation,"stored_assistant_content":"Forest "*25+"FULL_OUTPUT_END",
            "latency_seconds":1.5,"key":"SECRET_KEY_SENTINEL",
            "payload":{"text":"PRIVATE_PAYLOAD_SENTINEL"},"token":"SECRET_TOKEN_SENTINEL",
            "job":{"id":"job-"+str(index),"conversation_id":conversation,"state":"completed",
                "finish_reason":"stop","partial_content":"Forest "*25+"FULL_OUTPUT_END", "prompt_tokens":38,
                "prompt_budget":6144,"created_at":1.0,"updated_at":2.0,
                "private_path":"PRIVATE_PATH_SENTINEL","history":"PRIVATE_HISTORY_SENTINEL"}})
    return items,[(items[20+index*2],items[21+index*2]) for index in range(5)]


def test_stability_report_preserves_full_answers_and_allowlisted_provenance():
    items,pairs=public_records_fixture()
    records=hook.stability_records(items,pairs)
    assert len(records)==30 and all(record["answer"].endswith("FULL_OUTPUT_END") and len(record["answer"])>100 for record in records)
    assert all(record["answer"]==items[index]["job"]["partial_content"] for index,record in enumerate(records))
    assert all(record["stored_assistant_content"]==record["answer"] for record in records)
    assert len({record["job_id"] for record in records})==30 and len({record["conversation_id"] for record in records})==30
    assert all(record["client_label"]=="first" and record["sequence"]==index+1 for index,record in enumerate(records[:20]))
    assert [record["client_label"] for record in records[20:]]==["first","second"]*5
    assert [record["pair_index"] for record in records[20:]]==[1,1,2,2,3,3,4,4,5,5]
    serialized=json.dumps(records)
    assert not any(marker in serialized for marker in ("SECRET_","PRIVATE_"))
    assert all(record["native_prompt_tokens"]==38 and record["prompt_budget"]==6144 for record in records)


@pytest.mark.parametrize("failure",["missing_answer","wrong_conversation","not_terminal","missing_count","wrong_pair","only_29","wrong_stored_answer","duplicate_conversation"])
def test_incomplete_or_misattributed_stability_report_fails(failure):
    items,pairs=public_records_fixture()
    if failure=="missing_answer": items[0]["job"]["partial_content"]=""
    elif failure=="wrong_conversation": items[0]["job"]["conversation_id"]="foreign-conversation"
    elif failure=="not_terminal": items[0]["job"]["state"]="running"
    elif failure=="missing_count": items[0]["job"]["prompt_tokens"]=None
    elif failure=="wrong_pair": pairs[0]=(items[21],items[20])
    elif failure=="only_29": items.pop()
    elif failure=="wrong_stored_answer": items[0]["stored_assistant_content"]="Wrong stored output"
    elif failure=="duplicate_conversation":
        items[0]["conversation"]=items[1]["conversation"]
        items[0]["job"]["conversation_id"]=items[1]["conversation"]
    with pytest.raises(ValueError): hook.stability_records(items,pairs)


def test_completed_live_job_must_belong_to_selected_conversation_before_history():
    client=object.__new__(LiveClient)
    client.conversation=lambda _: {"id":"expected-conversation"}
    client.submit=lambda *_: ({"id":"job"},"public-key",{})
    client.wait=lambda _: {"conversation_id":"foreign-conversation"}
    client.request=lambda *_: pytest.fail("Mismatched job must fail before reading history")
    with pytest.raises(ValueError,match="conversation_attribution"):
        client.completed()


def test_live_stability_requires_twenty_plus_five_overlapping_pairs():
    class Fixture:
        def __init__(self,name): self.name=name; self.count=0; self.lock=threading.Lock()
        def pause(self,_): pass
        def completed(self,*_,**__):
            with self.lock:
                self.count+=1
                return {"job":{"id":self.name+str(self.count),"created_at":1,"updated_at":2},"started":1,"ended":2}
    a,b=Fixture("a"),Fixture("b")
    progress=[]
    completed,pairs=stability(a,b,lambda phase,count:progress.append((phase,count)))
    assert len(completed)==30 and len(pairs)==5 and a.count==25 and b.count==5
    assert progress[19]==("sequential",20) and progress[-1]==("pairs",5)


def test_live_pair_without_temporal_overlap_fails():
    class Fixture:
        def __init__(self,name,start,end): self.name=name; self.start=start; self.end=end; self.count=0
        def pause(self,_): pass
        def completed(self,*_,**__):
            self.count+=1
            return {"job":{"id":self.name+str(self.count),"created_at":self.start,"updated_at":self.end},"started":1,"ended":10}
    with pytest.raises(ValueError,match="did_not_overlap"):
        stability(Fixture("a",1,2),Fixture("b",3,4),lambda *_:None)


@pytest.mark.parametrize("failed_stage",["restart","basic_checks"])
def test_second_gateway_history_failure_cannot_return_success(tmp_path,monkeypatch,failed_stage):
    monkeypatch.setattr(hook,"ROOT",tmp_path)
    monkeypatch.setattr(hook,"fixture_configuration",lambda:tmp_path)
    monkeypatch.setenv("APP_RUN_ID","a"*32)
    calls=[]
    @contextmanager
    def gateway(**_):
        calls.append(True)
        selection=SimpleNamespace(device_id="d"*32,server_url="http://10.0.2.2:1",
            consume=lambda:{"token":"public-fixture"},confirm=lambda _:None)
        yield selection,None,{"gateway_cleanup":"CONFIRMED","relay_cleanup":"CONFIRMED","admitted_jobs":1,"jobs_with_native_token_count":1}
    class Client:
        def __init__(self,*_): pass
        def close(self): pass
        def request(self,method,path,*_):
            if path=="/v1/me": return 200,{"device_id":"d"*32},None
            raise ValueError("controlled_restart_history_failure")
    class StoreFixture:
        def __init__(self,*_): pass
        def close(self): pass
        def provision(self,*_,**__): return "d"*32,"public-fixture"
    monkeypatch.setattr(hook,"gateway_fixture",gateway)
    monkeypatch.setattr(hook,"LiveClient",Client)
    monkeypatch.setattr(hook,"Store",StoreFixture)
    monkeypatch.setattr(hook,"stability",lambda *_:public_records_fixture())
    def basic_checks(*_):
        if failed_stage=="basic_checks": raise ValueError("Controlled public fixture basic check failure")
        return {"deleted_conversation":"deleted-fixture"}
    monkeypatch.setattr(hook,"checks",basic_checks)
    monkeypatch.setattr(hook,"queue_and_revoke_checks",lambda *_:{})
    monkeypatch.setattr(hook,"borrowed_preflight",lambda *_:{})
    assert hook.main()==1 and len(calls)==(2 if failed_stage=="restart" else 1)
    report=json.loads((tmp_path/"docs/artifacts/integration-live-verification.json").read_text())
    assert report["technical_status"]=="FAIL" and report["error_code"]=="selected_live_integration_failed"
    assert report["phase"]==failed_stage and report["stability_status"]=="PASS"
    assert len(report["stability_records"])==30 and all(record["answer"].endswith("FULL_OUTPUT_END") for record in report["stability_records"])
    if failed_stage=="restart": assert report["restart_gateway_fixture"]["gateway_cleanup"]=="CONFIRMED"


def test_live_client_cancel_marker_prevents_any_http(tmp_path):
    (tmp_path/"CANCEL").write_text("cancel")
    client=LiveClient("http://127.0.0.1:1","public-fixture-token",tmp_path,float("inf"))
    try:
        with pytest.raises(ValueError,match="cancelled"):
            client.request("GET","/v1/me")
    finally: client.close()


@pytest.mark.asyncio
async def test_live_fixture_cancellation_waits_for_operation_finally(tmp_path):
    cleaned=asyncio.Event()
    started=asyncio.Event()
    async def pending():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.03)
            cleaned.set()
    running=asyncio.create_task(fixture_operation(pending(),tmp_path))
    await started.wait()
    (tmp_path/"CANCEL").write_text("cancel")
    with pytest.raises(ValueError,match="cancelled"):
        await running
    assert cleaned.is_set()
