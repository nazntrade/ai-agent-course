"""C06-C14: durable state, exact budgets and actual HTTP behavior offline."""
import asyncio
from dataclasses import replace
import json
import os
import secrets
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from gateway.app.config import Settings
from gateway.app.context import SYSTEM,prepare_context
from gateway.app.database import DomainError,Store
from gateway.app.main import ProcessLock,create_app
from gateway.app.manage import (confirm_pairing,consume_pairing,expire_payload,provision_device,
                               revoke_device,protect,unprotect,restrict_directory)
from gateway.app.upstream import UpstreamError
from gateway.app.worker import Worker,StubProvider
from gateway.app.models import Snapshot

pytestmark=pytest.mark.integration


@pytest.fixture
def storage(tmp_path):
    now=[1000.0]
    settings=Settings(tmp_path,prompt_cap=128,output_cap=16)
    store=Store(tmp_path/"chat.sqlite3",settings,lambda:now[0])
    device,token=store.provision("owner-a","test",pairing=False)
    identity=store.authenticate(token)
    yield store,identity,now
    store.close()


def admit(store,identity,title="Test",key="key-abcdefghijkl",text="hello"):
    conversation=store.create_conversation(identity["owner_id"],title)
    result,created=store.admit(identity,conversation["id"],key,text,1)
    return conversation,result


def test_concurrent_replay_is_one_job_and_changed_payload_conflicts(storage):
    store,identity,_=storage
    conversation=store.create_conversation(identity["owner_id"],"Race")
    def submit():
        return store.admit(identity,conversation["id"],"key-abcdefghijkl","same",1)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results=list(executor.map(lambda _:submit(),range(4)))
    assert len({value[0]["id"] for value in results})==1
    assert sum(value[1] for value in results)==1
    assert len(store.messages(identity["owner_id"],conversation["id"],100,0)["items"])==1
    with pytest.raises(DomainError,match="idempotency_conflict"):
        store.admit(identity,conversation["id"],"key-abcdefghijkl","changed",1)


def test_rate_burst_retry_after_refill_and_backward_clock(storage):
    store,identity,now=storage
    for index in range(2):
        _,job=admit(store,identity,key=f"key-{index:016d}")
        store.finish(job["id"],"failed","fixture")
    third=store.create_conversation(identity["owner_id"],"Third")
    now[0]-=100
    with pytest.raises(DomainError,match="rate_limited") as failure:
        store.admit(identity,third["id"],"key-third-abcdef","hello",1)
    assert failure.value.status==429 and failure.value.retry_after==12
    now[0]=1012
    assert store.admit(identity,third["id"],"key-third-abcdef","hello",1)[1]


def test_queue_owner_capacity_and_fifo_claim(storage):
    store,identity,_=storage
    first=admit(store,identity,key="key-first-abcdef")[1]
    second=admit(store,identity,key="key-second-abcde")[1]
    third=store.create_conversation(identity["owner_id"],"Third")
    with pytest.raises(DomainError,match="owner_queue_full"):
        store.admit(identity,third["id"],"key-third-abcdef","hello",1)
    assert store.claim()["id"]==first["id"]
    assert store.claim() is None
    store.finish(first["id"],"failed","fixture")
    assert store.claim()["id"]==second["id"]


def test_expired_queue_never_generates(storage):
    store,identity,now=storage
    _,job=admit(store,identity)
    now[0]+=181
    assert store.claim()=={"expired":True}
    assert store.job(identity["owner_id"],job["id"])["error_code"]=="queue_deadline_exceeded"


def test_delete_and_cancel_win_late_completion_and_exclude_partial_context(storage):
    store,identity,_=storage
    conversation,job=admit(store,identity)
    store.claim()
    store.update(job["id"],state="running",partial_content="partial")
    store.cancel(identity["owner_id"],job["id"])
    store.finish(job["id"],"completed",content="late",finish_reason="stop")
    assert store.job(identity["owner_id"],job["id"])["state"]=="cancelled"
    assert store.history(conversation["id"])==[]
    current=store.conversation(identity["owner_id"],conversation["id"])
    store.delete(identity["owner_id"],conversation["id"],current["revision"])
    with pytest.raises(DomainError,match="not_found"):
        store.job(identity["owner_id"],job["id"])
    assert store.conversations(identity["owner_id"],100,0)["items"]==[]


def test_rename_active_does_not_cancel_and_only_completed_pairs_enter_context(storage):
    store,identity,_=storage
    conversation,job=admit(store,identity)
    store.rename(identity["owner_id"],conversation["id"],"Renamed",2)
    store.finish(job["id"],"completed",content="answer",finish_reason="length")
    assert store.history(conversation["id"])==[({"role":"user","content":"hello"},{"role":"assistant","content":"answer"})]
    assert store.job(identity["owner_id"],job["id"])["finish_reason"]=="length"
    completed=store.job(identity["owner_id"],job["id"])
    assert store.cancel(identity["owner_id"],job["id"],partial_content="late local buffer")==completed
    assert len(store.history(conversation["id"]))==1


def test_restart_terminalizes_and_replay_keeps_original_job(tmp_path):
    settings=Settings(tmp_path)
    store=Store(tmp_path/"chat.sqlite3",settings)
    _,token=store.provision("owner-a","test",pairing=False)
    identity=store.authenticate(token)
    conversation,job=admit(store,identity)
    store.close()
    reopened=Store(tmp_path/"chat.sqlite3",settings)
    try:
        reopened.recover()
        replay,created=reopened.admit(identity,conversation["id"],"key-abcdefghijkl","hello",1)
        assert not created and replay["id"]==job["id"] and replay["error_code"]=="service_restarted"
    finally:
        reopened.close()


def test_revocation_pairing_expiry_and_owner_isolation(storage):
    store,identity,now=storage
    conversation,job=admit(store,identity)
    other,token=store.provision("owner-b","other",pairing=True)
    second=store.authenticate(token)
    with pytest.raises(DomainError,match="not_found"):
        store.job(second["owner_id"],job["id"])
    now[0]+=901
    store.expire_pairings()
    with pytest.raises(DomainError,match="unauthorized"):
        store.authenticate(token)
    store.revoke(identity["device_id"])
    with pytest.raises(DomainError,match="unauthorized"):
        store.admit(identity,conversation["id"],"key-abcdefghijkl","hello",1)


def test_events_reset_retention_versions_and_terminal(storage):
    store,identity,_=storage
    _,job=admit(store,identity)
    for index in range(140):
        store.update(job["id"],partial_content=str(index))
    assert len(store.events(job["id"],None))==1
    seq,event,snapshot=store.events(job["id"],0)[0]
    assert event=="reset" and snapshot["partial_content"]=="139"
    store.finish(job["id"],"failed","fixture")
    terminal=store.events(job["id"],seq)
    assert terminal[-1][1]=="terminal" and terminal[-1][2]["state_version"]>snapshot["state_version"]


class ExactFake:
    async def count(self,body):
        return {"input_tokens":sum(len(m["content"]) for m in body["messages"]),"context_capacity":8192}


async def test_exact_context_boundary_and_one_over(tmp_path):
    settings=Settings(tmp_path,prompt_cap=128,output_cap=16)
    allowed=128-len(SYSTEM)
    body,meta=await prepare_context(ExactFake(),settings,"a"*allowed,[])
    assert meta["prompt_tokens"]==128 and body["messages"][-1]["content"]=="a"*allowed
    with pytest.raises(UpstreamError,match="context_too_long"):
        await prepare_context(ExactFake(),settings,"a"*(allowed+1),[])


async def test_history_trim_keeps_current_system_and_newest_complete_pair(tmp_path):
    settings=Settings(tmp_path,prompt_cap=128,output_cap=16)
    history=[({"role":"user","content":"old"*20},{"role":"assistant","content":"old answer"}),
             ({"role":"user","content":"new"},{"role":"assistant","content":"answer"})]
    body,meta=await prepare_context(ExactFake(),settings,"now",history)
    assert meta["history_truncated"] and [m["content"] for m in body["messages"]]==[SYSTEM,"new","answer","now"]


def wait_job(client,headers,identifier,terminal=True):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        job=client.get("/v1/requests/"+identifier,headers=headers).json()
        if (job["state"] in {"completed","cancelled","failed"})==terminal:
            return job
        time.sleep(0.01)
    raise AssertionError("Job state deadline")


@pytest.fixture
def api(tmp_path):
    settings=Settings(tmp_path)
    store=Store(tmp_path/"chat.sqlite3",settings)
    _,token=store.provision("owner-a","test",pairing=False)
    other,other_token=store.provision("owner-b","other",pairing=False)
    app=create_app(settings,store=store)
    with TestClient(app) as client:
        yield client,{"Authorization":"Bearer "+token},store,other_token


def test_actual_http_chat_stream_snapshot_replay_and_openapi(api,monkeypatch):
    client,headers,store,_=api
    generations=[]
    original=StubProvider.stream
    async def counted(provider,body,on_content=None):
        generations.append(True)
        return await original(provider,body,on_content)
    monkeypatch.setattr(StubProvider,"stream",counted)
    conversation=client.post("/v1/conversations",json={"title":"First"},headers=headers).json()
    h=dict(headers,**{"Idempotency-Key":"key-abcdefghijkl"})
    body={"text":"hello","expected_revision":1}
    admitted=client.post("/v1/conversations/"+conversation["id"]+"/requests",json=body,headers=h)
    assert admitted.status_code==202
    identifier=admitted.json()["id"]
    done=wait_job(client,headers,identifier)
    assert done["state"]=="completed" and done["partial_content"]=="Demo reply: hello"
    replay=client.post("/v1/conversations/"+conversation["id"]+"/requests",json=body,headers=h)
    assert replay.status_code==200 and replay.json()["id"]==identifier
    assert Snapshot.model_validate(admitted.json()).id==Snapshot.model_validate(replay.json()).id
    assert len(generations)==1
    stream=client.get("/v1/requests/"+identifier+"/events",headers=headers)
    assert "event: reset" in stream.text and "Demo reply: hello" in stream.text
    ahead=client.get("/v1/requests/"+identifier+"/events",headers=dict(headers,**{"Last-Event-ID":"99999999999999999999"}))
    assert ahead.status_code==200 and "event: reset" in ahead.text and '"state":"completed"' in ahead.text
    invalid=client.get("/v1/requests/"+identifier+"/events",headers=dict(headers,**{"Last-Event-ID":"9"*21}))
    assert invalid.status_code==422
    schema=client.app.openapi()
    assert schema["openapi"]=="3.1.0" and "Snapshot" in schema["components"]["schemas"]
    responses=schema["paths"]["/v1/conversations/{identifier}/requests"]["post"]["responses"]
    for code in ("200","202"):
        assert responses[code]["content"]["application/json"]["schema"]=={"$ref":"#/components/schemas/Snapshot"}
    assert "owner_id" not in schema["components"]["schemas"]["SubmitRequest"]["properties"]


@pytest.mark.parametrize("operation",["cancel","shutdown"])
def test_coalesced_unpublished_tail_is_preserved_atomically_at_terminal(tmp_path,operation):
    ready=threading.Event()
    class TailProvider(StubProvider):
        async def stream(self,body,on_content=None):
            await on_content("published ")
            await on_content("unpublished tail")
            ready.set()
            await asyncio.Event().wait()
            raise AssertionError("Cancelled generation must not complete")
    class TailPool:
        manager=None
        async def get(self):
            return TailProvider()
        async def close(self):
            pass
    settings=Settings(tmp_path,shutdown_token="isolated-stop-fixture")
    store=Store(tmp_path/"chat.sqlite3",settings)
    _,token=store.provision("owner-a","test",pairing=False)
    headers={"Authorization":"Bearer "+token}
    with TestClient(create_app(settings,store=store,pool=TailPool()),client=("127.0.0.1",50000)) as client:
        conversation=client.post("/v1/conversations",json={"title":"Tail"},headers=headers).json()
        admitted=client.post("/v1/conversations/"+conversation["id"]+"/requests",
            json={"text":"hello","expected_revision":1},headers=dict(headers,**{"Idempotency-Key":"key-abcdefghijkl"})).json()
        assert ready.wait(3)
        identifier=admitted["id"]
        before=store.job("owner-a",identifier)
        assert before["state"]=="running" and before["partial_content"]=="published "
        if operation=="cancel":
            response=client.post("/v1/requests/"+identifier+"/cancel",headers=headers)
            assert response.status_code==200
            terminal=response.json()
        else:
            response=client.post("/internal/shutdown",headers={"Authorization":"Bearer isolated-stop-fixture"})
            assert response.status_code==200
            terminal=store.job("owner-a",identifier)
        assert terminal["state"]=="cancelled" and terminal["partial_content"]=="published unpublished tail"
        events=store.events(identifier,None)
        assert events[-1][2]["partial_content"]==terminal["partial_content"]
        assert events[-1][2]["state_version"]==terminal["state_version"]
        assert terminal["state_version"]==before["state_version"]+1
        assert store.history(conversation["id"])==[]
        assert not store.update(identifier,partial_content="late chunk")
        store.finish(identifier,"completed",content="late answer",finish_reason="stop")
        assert store.job("owner-a",identifier)==terminal
        assert store.history(conversation["id"])==[]


def test_actual_http_privacy_bytes_auth_and_validation(api):
    client,headers,store,other_token=api
    assert client.get("/healthz").status_code==200
    assert client.get("/v1/conversations").status_code==401
    large=client.post("/v1/conversations",content=b"x"*131073,headers=dict(headers,**{"Content-Length":"1"}))
    assert large.status_code==413
    bad=client.post("/v1/conversations",json={"title":"Secret input","model":"private"},headers=headers)
    assert bad.status_code==422 and "Secret input" not in bad.text and "private" not in bad.text
    conversation=client.post("/v1/conversations",json={"title":"Private"},headers=headers).json()
    foreign=client.get("/v1/conversations/"+conversation["id"],headers={"Authorization":"Bearer "+other_token})
    assert foreign.status_code==404
    assert client.post("/internal/shutdown",headers=headers).status_code==404


def test_actual_http_revision_rejects_boolean_instead_of_coercing_to_one(api):
    client,headers,_,_=api
    conversation=client.post("/v1/conversations",json={"title":"Strict"},headers=headers).json()
    response=client.post("/v1/conversations/"+conversation["id"]+"/requests",
        json={"text":"hello","expected_revision":True},headers=dict(headers,**{"Idempotency-Key":"key-abcdefghijkl"}))
    assert response.status_code==422


async def test_failed_cold_acquire_retires_broken_pool_before_next_request(storage):
    store,identity,_=storage
    _,snapshot=admit(store,identity)
    job=store.claim()
    class FailedPool:
        def __init__(self):
            self.manager=type("FailedManager",(),{"renew_failed":False})()
            self.closed=0
        async def get(self):
            raise UpstreamError("model_unavailable")
        async def close(self):
            self.closed+=1
            self.manager=None
    pool=FailedPool()
    await Worker(store,store.settings,pool).execute(job)
    assert store.job(identity["owner_id"],snapshot["id"])["error_code"]=="model_unavailable"
    assert pool.closed==1 and pool.manager is None


def test_actual_http_cancel_delete_and_persistence_revisions(api):
    client,headers,store,_=api
    conversation=client.post("/v1/conversations",json={"title":"Cancel"},headers=headers).json()
    job=client.post("/v1/conversations/"+conversation["id"]+"/requests",json={"text":"a"*100,"expected_revision":1},
                    headers=dict(headers,**{"Idempotency-Key":"key-abcdefghijkl"})).json()
    assert client.post("/v1/requests/"+job["id"]+"/cancel",headers=headers).json()["state"]=="cancelled"
    assert wait_job(client,headers,job["id"])["state"]=="cancelled"
    assert store.history(conversation["id"])==[]
    current=client.get("/v1/conversations/"+conversation["id"],headers=headers).json()
    renamed=client.patch("/v1/conversations/"+conversation["id"],json={"title":"Renamed","expected_revision":current["revision"]},headers=headers)
    assert renamed.status_code==200
    deleted=client.delete("/v1/conversations/"+conversation["id"]+"?expected_revision="+str(renamed.json()["revision"]),headers=headers)
    assert deleted.status_code==204 and client.get("/v1/conversations/"+conversation["id"],headers=headers).status_code==404


def test_single_worker_lock_rejects_second_process_lock(tmp_path):
    first,second=ProcessLock(tmp_path/"owned.lock"),ProcessLock(tmp_path/"owned.lock")
    first.acquire()
    try:
        with pytest.raises((OSError,ValueError)):
            second.acquire()
    finally:
        first.release()


def test_pairing_ttl_confirmation_and_revoke_do_not_replace_pending(storage,tmp_path):
    store,identity,now=storage
    directory=tmp_path/"pairing"
    restrict=lambda folder:folder.mkdir(exist_ok=True)
    first=provision_device(store,directory,"owner-a","phone",protector=lambda b:b,restrict=restrict)
    with pytest.raises(ValueError,match="pending_pairing_exists"):
        provision_device(store,directory,"owner-a","phone2",protector=lambda b:b,restrict=restrict)
    payload=consume_pairing(store,directory,decryptor=lambda b:b)
    assert payload["device_id"]==first["device_id"]
    confirm_pairing(store,directory,first["device_id"])
    assert not (directory/"initial-device.dpapi").exists()
    assert store.authenticate(payload["token"])["device_id"]==first["device_id"]
    revoke_device(store,directory,first["device_id"])
    with pytest.raises(DomainError,match="unauthorized"):
        store.authenticate(payload["token"])
    second=provision_device(store,directory,"owner-a","phone3",protector=lambda b:b,restrict=restrict)
    now[0]+=901
    expire_payload(store,directory)
    assert not (directory/"initial-device.dpapi").exists()


@pytest.mark.skipif(os.name!="nt",reason="Windows security boundary")
def test_real_windows_dpapi_and_owner_acl_on_isolated_pairing_directory(tmp_path):
    directory=tmp_path/"pairing"
    restrict_directory(directory)
    plaintext=b"isolated non-secret security fixture"
    encrypted=protect(plaintext)
    assert not secrets.compare_digest(encrypted,plaintext)
    assert secrets.compare_digest(unprotect(encrypted),plaintext)
