"""Fixed selected-model integration suite; never executed by offline checks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import time
import threading
import math
from dataclasses import replace

import httpx

from gateway.app.config import Settings
from gateway.app.database import Store
from gateway.app.manage import revoke_device
from gateway.app.context import prepare_context
from gateway.app.upstream import ProviderConfig,LeaseManager,LocalProvider
from harness.android_gateway import gateway_fixture
from harness.integration_child import fixture_configuration
from harness.runner import ROOT,save_json

TERMINAL={"completed","failed","cancelled","interrupted"}


class LiveClient:
    def __init__(self,origin,token,fixture,deadline):
        self.origin,self.token,self.fixture,self.deadline=origin,token,fixture,deadline
        self.client=httpx.Client(trust_env=False,follow_redirects=False,timeout=5)

    def close(self): self.client.close()

    def pause(self,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if (self.fixture/"CANCEL").exists() or time.monotonic()>self.deadline:
                raise ValueError("integration_cancelled_or_deadline")
            time.sleep(min(.1,max(0,end-time.monotonic())))

    def request(self,method,path,body=None,key=None):
        if (self.fixture/"CANCEL").exists() or time.monotonic()>self.deadline:
            raise ValueError("integration_cancelled_or_deadline")
        if method not in {"GET","POST","PATCH","DELETE"} or not path.startswith("/v1/") or len(path)>256:
            raise ValueError("fixed_live_route_required")
        headers={"Authorization":"Bearer "+self.token}
        if key: headers["Idempotency-Key"]=key
        with self.client.stream(method,self.origin+path,json=body,headers=headers) as response:
            data=bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data)>4194304: raise ValueError("integration_response_limit")
            if not data: value={}
            else:
                value=json.loads(data)
                if not isinstance(value,dict): raise ValueError("integration_response_invalid")
            retry=response.headers.get("Retry-After","")
            return response.status_code,value,int(retry) if retry.isdigit() and len(retry)<5 else None

    def conversation(self,title):
        status,value,_=self.request("POST","/v1/conversations",{"title":title})
        if status!=201: raise ValueError("live_conversation_create_failed")
        return value

    def submit(self,identifier,text,*,key=None,allow_rate_wait=True):
        status,conversation,_=self.request("GET","/v1/conversations/"+identifier)
        if status!=200: raise ValueError("live_conversation_read_failed")
        key=key or secrets.token_hex(16)
        payload={"text":text,"expected_revision":conversation["revision"]}
        while True:
            status,value,retry=self.request("POST","/v1/conversations/"+identifier+"/requests",payload,key)
            if status in {200,202}: return value,key,payload
            if status==429 and value.get("error",{}).get("code")=="rate_limited" and retry and allow_rate_wait:
                self.pause(retry); continue
            raise ValueError("live_job_admission_failed")

    def wait(self,job,*,running=False):
        limit=min(self.deadline,time.monotonic()+330)
        while time.monotonic()<limit:
            status,value,_=self.request("GET","/v1/requests/"+job)
            if status!=200: raise ValueError("live_snapshot_failed")
            if value["state"] in TERMINAL or (running and value["state"]=="running" and value["partial_content"]): return value
            self.pause(.1)
        raise ValueError("live_generation_deadline")

    def completed(self,text="Reply with exactly OK.",*,admission_barrier=None):
        conversation=self.conversation("Stability fixture")
        if admission_barrier is not None: admission_barrier.wait(timeout=5)
        started=time.monotonic()
        snapshot,key,payload=self.submit(conversation["id"],text)
        result=self.wait(snapshot["id"])
        if result["conversation_id"]!=conversation["id"]:
            raise ValueError("live_job_conversation_attribution_failed")
        if result["state"]!="completed" or not result["partial_content"] or result["finish_reason"] not in {"stop","length"}:
            raise ValueError("live_complete_answer_missing")
        status,messages,_=self.request("GET","/v1/conversations/"+conversation["id"]+"/messages")
        if (status!=200 or len(messages["items"])!=2 or messages["items"][1]["text"]!=result["partial_content"]
            or messages["items"][0]["role"]!="user" or messages["items"][1]["role"]!="assistant"
            or any(message["job_id"]!=result["id"] for message in messages["items"])):
            raise ValueError("live_history_output_mismatch")
        return {"job":result,"conversation":conversation["id"],"key":key,"payload":payload,
                "stored_assistant_content":messages["items"][1]["text"],
                "started":started,"ended":time.monotonic(),"latency_seconds":round(time.monotonic()-started,3)}


def stability(first,second,progress):
    completed=[]
    for index in range(20):
        completed.append(first.completed())
        progress("sequential",index+1)
    pairs=[]
    with ThreadPoolExecutor(max_workers=2) as executor:
        for index in range(5):
            # Wait for a normal refill before beginning BOTH clients. Waiting
            # Python functions alone does not count as simultaneous model jobs.
            first.pause(12)
            barrier=threading.Barrier(2)
            text="Write one short sentence of twenty words about a forest. Do not add anything else."
            a=executor.submit(first.completed,text,admission_barrier=barrier)
            b=executor.submit(second.completed,text,admission_barrier=barrier)
            left,right=a.result(),b.result()
            if max(left["job"]["created_at"],right["job"]["created_at"])>=min(left["job"]["updated_at"],right["job"]["updated_at"]):
                raise ValueError("live_pair_did_not_overlap")
            if left["job"]["id"]==right["job"]["id"]: raise ValueError("live_pair_job_identity_mixed")
            pairs.append((left,right)); completed.extend((left,right))
            progress("pairs",index+1)
    if len(completed)!=30 or len({item["job"]["id"] for item in completed})!=30:
        raise ValueError("thirty_distinct_completed_required")
    return completed,pairs


def stability_records(completed,pairs):
    """Allowlisted full public-fixture outputs, with exact client provenance."""
    if (len(completed)!=30 or len(pairs)!=5 or len({item["job"]["id"] for item in completed})!=30
        or len({item["conversation"] for item in completed})!=30):
        raise ValueError("complete_stability_report_required")
    records=[]
    for index,item in enumerate(completed):
        job=item["job"]
        if (job["conversation_id"]!=item["conversation"] or job["state"]!="completed"
            or job["finish_reason"] not in {"stop","length"} or not isinstance(job["partial_content"],str)
            or not job["partial_content"] or len(job["partial_content"])>131072
            or item["stored_assistant_content"]!=job["partial_content"]
            or type(job["prompt_tokens"]) is not int or type(job["prompt_budget"]) is not int
            or not 0<job["prompt_tokens"]<=job["prompt_budget"]
            or not all(type(job[key]) in {int,float} and math.isfinite(job[key]) for key in ("created_at","updated_at"))
            or job["updated_at"]<job["created_at"]
            or type(item["latency_seconds"]) not in {int,float} or not math.isfinite(item["latency_seconds"])
            or item["latency_seconds"]<0):
            raise ValueError("invalid_stability_record")
        pair_index=None if index<20 else (index-20)//2
        side=0 if index<20 else (index-20)%2
        if pair_index is not None and pairs[pair_index][side]["job"]["id"]!=job["id"]:
            raise ValueError("stability_pair_provenance_mismatch")
        records.append({"job_id":job["id"],"conversation_id":item["conversation"],
            "client_label":"first" if side==0 else "second","phase":"sequential" if index<20 else "pair",
            "sequence":index+1 if index<20 else None,"pair_index":None if pair_index is None else pair_index+1,
            "terminal_state":job["state"],"finish_reason":job["finish_reason"],
            "answer":job["partial_content"],"stored_assistant_content":item["stored_assistant_content"],
            "native_prompt_tokens":job["prompt_tokens"],"prompt_budget":job["prompt_budget"],
            "created_at":job["created_at"],"terminal_at":job["updated_at"],"latency_seconds":item["latency_seconds"]})
    return records


async def native_budget_checks(settings):
    config=ProviderConfig.from_environment()
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as client:
        manager=LeaseManager(config,client)
        try:
            await manager.ensure()
            provider=LocalProvider(config,client,manager)
            # Public fixture history is used to exercise the REAL native template
            # counter through the exact production context planner, without RAG.
            history=[[{"role":"user","content":"fixture "+("planet "*1500)},
                      {"role":"assistant","content":"fixture "+("ocean "*1500)}] for _ in range(5)]
            body,meta=await prepare_context(provider,settings,"Reply with OK.",history)
            actual=await provider.count(body)
            if not meta["history_truncated"] or actual["input_tokens"]!=meta["prompt_tokens"] or actual["input_tokens"]>meta["prompt_budget"] or actual["context_capacity"]<8192:
                raise ValueError("native_context_trim_failed")
            return {"native_fixture_history_trim":True,"native_template_count":actual["input_tokens"],
                    "prompt_budget":meta["prompt_budget"],"actual_slot_capacity":actual["context_capacity"]}
        finally:
            await manager.close()


def checks(first,second,store,settings,completed):
    sample=completed[0]
    status,replayed,_=first.request("POST","/v1/conversations/"+sample["conversation"]+"/requests",sample["payload"],sample["key"])
    if status!=200 or replayed["id"]!=sample["job"]["id"]: raise ValueError("live_idempotent_replay_failed")
    changed=dict(sample["payload"],text="Changed fixture")
    if first.request("POST","/v1/conversations/"+sample["conversation"]+"/requests",changed,sample["key"])[0]!=409:
        raise ValueError("live_idempotency_conflict_missing")
    if second.request("GET","/v1/conversations/"+sample["conversation"])[0]!=404 or second.request("GET","/v1/requests/"+sample["job"]["id"])[0]!=404:
        raise ValueError("live_owner_isolation_failed")
    context=first.conversation("Context fixture")
    oversized=("qzxv9876 "*3550).strip()
    snapshot,_,_=first.submit(context["id"],oversized)
    rejected=first.wait(snapshot["id"])
    if rejected["state"]!="failed" or rejected["error_code"]!="context_too_long" or rejected["partial_content"]:
        raise ValueError("live_oversized_context_not_rejected")
    valid,_,_=first.submit(context["id"],"Reply with exactly VALID.")
    if first.wait(valid["id"])["state"]!="completed": raise ValueError("live_valid_after_context_reject_failed")
    native=asyncio.run(fixture_operation(native_budget_checks(settings),first.fixture))
    # A fresh owner with two sequential completions proves the REAL token bucket
    # while replay continues to bypass an exhausted admission quota.
    _,token=store.provision("rate-fixture","integration",pairing=False)
    rate=LiveClient(first.origin,token,first.fixture,first.deadline)
    try:
        one=rate.completed(); rate.completed()
        candidate=rate.conversation("Rate fixture")
        status,error,retry=rate.request("POST","/v1/conversations/"+candidate["id"]+"/requests",
            {"text":"Reply OK.","expected_revision":candidate["revision"]},secrets.token_hex(16))
        if status!=429 or error.get("error",{}).get("code")!="rate_limited" or not retry:
            raise ValueError("live_rate_boundary_not_observed")
        replay_status,replay,_=rate.request("POST","/v1/conversations/"+one["conversation"]+"/requests",one["payload"],one["key"])
        if replay_status!=200 or replay["id"]!=one["job"]["id"]: raise ValueError("live_quota_exhausted_replay_failed")
        rate.pause(retry)
        after,_,_=rate.submit(candidate["id"],"Reply with exactly REFILL.",allow_rate_wait=False)
        if rate.wait(after["id"])["state"]!="completed": raise ValueError("live_rate_refill_failed")
    finally: rate.close()
    if first.request("POST","/v1/conversations",{"title":"x","extra":"x"})[0]!=422:
        raise ValueError("live_unknown_field_not_rejected")
    # Actual bytes limit, bounded in-memory public fixture body.
    with first.client.stream("POST",first.origin+"/v1/conversations",content=b"x"*131073,
            headers={"Authorization":"Bearer "+first.token,"Content-Type":"application/json"}) as response:
        if response.status_code!=413: raise ValueError("live_http_byte_limit_failed")
    return {"replay_same_job":True,"changed_key_payload_conflict":True,"owner_isolation":True,
            "actual_context_rejection_before_content":True,"valid_after_context_rejection":True,
            "actual_rate_429_retry_after":True,"actual_rate_refill":True,"quota_exhausted_replay":True,
            "actual_http_byte_cap":True,"unknown_fields_rejected":True,**native}


async def fixture_operation(operation,fixture):
    from harness.center_service import deferred
    task=asyncio.create_task(operation)
    try:
        while not task.done():
            if (fixture/"CANCEL").exists():
                task.cancel()
                break
            await asyncio.sleep(.05)
        try: return await task
        except asyncio.CancelledError: raise ValueError("fixture_operation_cancelled") from None
    finally:
        if not task.done():
            task.cancel()
            try: await deferred(task)
            except asyncio.CancelledError: pass


def borrowed_preflight(fixture):
    from harness.model_preflight import main as selected_boundary
    # This library executes inside the already validated fixed LIVE scenario;
    # no separate unowned Python/cmd process can outlive its cancellation.
    result=asyncio.run(fixture_operation(selected_boundary(operation_deadline=240),fixture))
    report=json.loads((ROOT/"docs/artifacts/model-verification.json").read_text())
    if result or report.get("run_id")!=os.environ.get("APP_RUN_ID") or report.get("technical_status")!="PASS":
        raise ValueError("borrowed_selected_preflight_failed")
    lifecycle=json.loads((ROOT/"docs/artifacts/lifecycle-verification.json").read_text())
    if (lifecycle.get("run_id")!=os.environ.get("APP_RUN_ID") or lifecycle.get("renew",{}).get("renewed") is not True
        or lifecycle.get("release",{}).get("released") is not True
        or lifecycle.get("release",{}).get("disposition") not in {"shared","retained"}):
        raise ValueError("borrowed_preflight_did_not_retain_shared_runtime")
    return {"borrowed_selected_preflight":True,"borrowed_release_disposition":lifecycle["release"]["disposition"],
            "details_source":"lifecycle-verification.json"}


def queue_and_revoke_checks(first,store,settings):
    clients=[]
    jobs=[]
    def owner(index):
        device,token=store.provision("boundary-"+str(index),"integration",pairing=False)
        client=LiveClient(first.origin,token,first.fixture,first.deadline)
        clients.append(client)
        return device,client
    try:
        _,active=owner(0)
        conversation=active.conversation("Active queue fixture")
        job,_,_=active.submit(conversation["id"],"Write a very long detailed numbered list of 250 animals with explanations. Continue until the output limit.")
        jobs.append((active,job["id"]))
        running=active.wait(job["id"],running=True)
        if running["state"]!="running" or not running["partial_content"]: raise ValueError("live_active_partial_missing")
        # Keep exactly seven waiting jobs, then race the final available slot.
        for index in range(7):
            _,client=owner(index+1)
            queued=client.conversation("Waiting queue fixture")
            value,_,_=client.submit(queued["id"],"Reply with OK.",allow_rate_wait=False)
            if value["state"]!="queued": raise ValueError("live_queue_was_not_waiting")
            jobs.append((client,value["id"]))
        racers=[]
        for index in (20,21):
            _,client=owner(index)
            queued=client.conversation("Last slot fixture")
            racers.append((client,queued))
        barrier=threading.Barrier(2)
        def race(entry):
            client,queued=entry; barrier.wait(timeout=5)
            return client,queued,client.request("POST","/v1/conversations/"+queued["id"]+"/requests",
                {"text":"Reply OK.","expected_revision":queued["revision"]},secrets.token_hex(16))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(race,racers))
        accepted=[item for item in results if item[2][0]==202]
        rejected=[item for item in results if item[2][0]==429 and item[2][1].get("error",{}).get("code")=="queue_full"]
        if len(accepted)!=1 or len(rejected)!=1: raise ValueError("live_last_queue_slot_not_atomic")
        jobs.append((accepted[0][0],accepted[0][2][1]["id"]))
        status,snapshot,_=jobs[1][0].request("POST","/v1/requests/"+jobs[1][1]+"/cancel")
        if status!=200 or snapshot["state"]!="cancelled": raise ValueError("live_queued_cancel_failed")
        loser,queued,_=rejected[0]
        value,_,_=loser.submit(queued["id"],"Reply OK.",allow_rate_wait=False)
        if value["state"]!="queued": raise ValueError("live_cancel_did_not_free_slot")
        jobs.append((loser,value["id"]))
        # Stop queued jobs before releasing the active generation, so they do
        # not create incidental extra inference while checking admission limits.
        for client,identifier in jobs[1:]: client.request("POST","/v1/requests/"+identifier+"/cancel")
        status,cancelled,_=active.request("POST","/v1/requests/"+job["id"]+"/cancel")
        if status!=200 or cancelled["state"]!="cancelled" or not cancelled["partial_content"] or store.history(conversation["id"]):
            raise ValueError("live_cancel_partial_or_context_exclusion_failed")
        # Open a real SSE response and revoke precisely its temporary device.
        device,viewer=owner(30)
        _,controller_token=store.provision("boundary-30","integration-controller",pairing=False)
        controller=LiveClient(first.origin,controller_token,first.fixture,first.deadline); clients.append(controller)
        conversation=viewer.conversation("Revocation fixture")
        current,_,_=viewer.submit(conversation["id"],"Write a very long numbered list of 250 stars and explain every item.")
        jobs.append((controller,current["id"]))
        if viewer.wait(current["id"],running=True)["state"]!="running": raise ValueError("live_revocation_job_not_active")
        opened=threading.Event(); closed=threading.Event(); observed={}
        def stream():
            try:
                with httpx.Client(trust_env=False,follow_redirects=False,timeout=5) as client:
                    with client.stream("GET",viewer.origin+"/v1/requests/"+current["id"]+"/events",
                            headers={"Authorization":"Bearer "+viewer.token}) as response:
                        if response.status_code!=200: raise ValueError("live_sse_open_failed")
                        for line in response.iter_lines():
                            if line.startswith("data: "): opened.set()
                observed["natural_closed"]=True
            except Exception:
                observed["natural_closed"]=False
            finally: closed.set()
        thread=threading.Thread(target=stream,daemon=True); thread.start()
        if not opened.wait(5): raise ValueError("live_sse_not_open")
        revoke_device(store,settings.pairing_dir,device)
        if not closed.wait(5) or observed.get("natural_closed") is not True or viewer.request("GET","/v1/me")[0]!=401:
            raise ValueError("live_revocation_did_not_close_sse")
        controller.request("POST","/v1/requests/"+current["id"]+"/cancel")
        status,latest,_=controller.request("GET","/v1/conversations/"+conversation["id"])
        if status!=200 or controller.request("DELETE","/v1/conversations/"+conversation["id"]+"?expected_revision="+str(latest["revision"]))[0]!=204:
            raise ValueError("live_delete_failed")
        if controller.request("GET","/v1/conversations/"+conversation["id"])[0]!=404:
            raise ValueError("live_deleted_conversation_revived")
        return {"actual_eight_waiting_queue":True,"actual_last_slot_race":True,"actual_cancel_frees_slot":True,
            "actual_cancel_preserves_partial":True,"cancelled_not_future_context":True,"actual_open_sse_revocation_closes":True,
            "revoked_device_unauthorized":True,"delete_no_revive":True,"deleted_conversation":conversation["id"]}
    finally:
        for client,identifier in reversed(jobs):
            try: client.request("POST","/v1/requests/"+identifier+"/cancel")
            except Exception: pass
        for client in clients: client.close()


def main():
    fixture=fixture_configuration()
    run_id=os.environ["APP_RUN_ID"]
    report={"schema_version":"integration-live-verification-v1","run_id":run_id,"technical_status":"FAIL","phase":"setup",
            "acceptance_status":"PARTIAL","scope":"actual selected local model","stability_status":"NOT_RUN"}
    runtime={}
    restarted={}
    clients=[]
    try:
        with gateway_fixture(relay_enabled=False) as (selection,_,runtime):
            payload=selection.consume()
            origin=selection.server_url.replace("10.0.2.2","127.0.0.1")
            deadline=time.monotonic()+1500
            first=LiveClient(origin,payload["token"],fixture,deadline); clients.append(first)
            status,identity,_=first.request("GET","/v1/me")
            if status!=200 or identity["device_id"]!=selection.device_id: raise ValueError("fixture_device_identity_failed")
            selection.confirm(selection.device_id); del payload
            settings=replace(Settings.from_environment("live"),queue_capacity=8,owner_waiting=2,prompt_cap=6144,output_cap=1024)
            store=Store(settings.data_dir/"chat.sqlite3",settings)
            try:
                _,token=store.provision("concurrent-fixture","integration",pairing=False)
                second=LiveClient(origin,token,fixture,deadline); clients.append(second); del token
                def progress(phase,count):
                    save_json(ROOT/"docs/artifacts/stability-progress.json",{
                        "schema_version":"stability-progress-v1","run_id":run_id,"phase":phase,"completed_in_phase":count,
                        "stability_required_completed":30,"acceptance_status":"PARTIAL"})
                report["phase"]="stability"
                completed,pairs=stability(first,second,progress)
                report.update(stability_status="PASS",sequential_completed=20,overlapping_pairs_completed=5,total_stability_completed=30,
                    stable_jobs_distinct=True,complete_outputs_inspected=True,latencies_seconds=[item["latency_seconds"] for item in completed],
                    stability_records=stability_records(completed,pairs))
                report["phase"]="basic_checks"
                report["checks"]=checks(first,second,store,settings,completed)
                report["phase"]="queue_revoke"
                report["checks"].update(queue_and_revoke_checks(first,store,settings))
                report["phase"]="borrowed"
                report["checks"].update(borrowed_preflight(fixture))
            finally: store.close()
        for client in clients: client.close()
        clients.clear()
        # A new explicitly owned process uses the same TEMP SQLite database.
        # Only GET checks run after restart; no stability job is replayed.
        report["phase"]="restart"
        with gateway_fixture(relay_enabled=False) as (selection,_,restarted):
            payload=selection.consume()
            origin=selection.server_url.replace("10.0.2.2","127.0.0.1")
            client=LiveClient(origin,payload["token"],fixture,time.monotonic()+30)
            try:
                if client.request("GET","/v1/me")[0]!=200: raise ValueError("restarted_fixture_device_failed")
                selection.confirm(selection.device_id); del payload
                if client.request("GET","/v1/conversations/"+completed[0]["conversation"]+"/messages")[1]["items"][1]["text"]!=completed[0]["job"]["partial_content"]:
                    raise ValueError("live_restart_history_lost")
                report["checks"]["sqlite_history_after_gateway_restart"]=True
                inspection=Store(Settings.from_environment("live").data_dir/"chat.sqlite3",Settings.from_environment("live"))
                try:
                    tombstone=inspection.db.execute("SELECT deleted_at FROM conversations WHERE id=?",
                        (report["checks"]["deleted_conversation"],)).fetchone()
                    if not tombstone or tombstone[0] is None: raise ValueError("restart_delete_tombstone_lost")
                    report["checks"]["delete_tombstone_after_gateway_restart"]=True
                finally: inspection.close()
            finally: client.close()
        report["restart_gateway_fixture"]=restarted
        if restarted.get("gateway_cleanup")!="CONFIRMED" or restarted.get("relay_cleanup")!="CONFIRMED":
            raise ValueError("restart_cleanup_unconfirmed")
        if restarted.get("admitted_jobs")!=runtime.get("admitted_jobs") or restarted.get("jobs_with_native_token_count")!=runtime.get("jobs_with_native_token_count"):
            raise ValueError("restart_created_new_model_job")
        report["checks"]["restart_get_checks_no_new_job"]=True
        report["actual_pair_intervals"]=[{"left_created_at":a["job"]["created_at"],"left_terminal_at":a["job"]["updated_at"],
            "right_created_at":b["job"]["created_at"],"right_terminal_at":b["job"]["updated_at"]} for a,b in pairs]
        if runtime.get("gateway_cleanup")!="CONFIRMED" or runtime.get("relay_cleanup")!="CONFIRMED":
            raise ValueError("first_gateway_cleanup_unconfirmed")
        report["phase"]="completed"
        report["technical_status"]="PASS"
    except Exception:
        report["technical_status"]="FAIL"
        report["error_code"]="selected_live_integration_failed"
    finally:
        for client in clients: client.close()
        report["gateway_fixture"]=runtime
        report["restart_gateway_fixture"]=restarted
        if (runtime.get("gateway_cleanup")!="CONFIRMED" or runtime.get("relay_cleanup")!="CONFIRMED"
            or restarted.get("gateway_cleanup")!="CONFIRMED" or restarted.get("relay_cleanup")!="CONFIRMED"):
            report["technical_status"]="FAIL"
        save_json(ROOT/"docs/artifacts/integration-live-verification.json",report)
    print("INTEGRATION_LIVE_STATUS: "+report["technical_status"])
    return 0 if report["technical_status"]=="PASS" else 1
