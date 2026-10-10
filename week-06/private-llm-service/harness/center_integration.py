"""Explicit fixed integration dispatch; only the Center child receives credentials."""
import asyncio
import os
from pathlib import Path
import time

import httpx

from gateway.app.upstream import UpstreamError,bounded_json
from harness.center_preflight import CATALOG,ORIGIN,RUN_ID,checked_json,model_state,selected_configuration
from harness.runner import ROOT,save_json

PROFILES={"android-emulator":"private-llm-android-tests","integration-live":"private-llm-integration-tests"}


def requested_scope(root):
    path=root/".runtime/center-integration-request.json"
    if not path.exists():
        return None
    value=checked_json(path,4096)
    if (set(value)!={"schema_version","scenario"} or value["schema_version"]!="center-integration-request-v1"
            or not isinstance(value["scenario"],str) or value["scenario"] not in PROFILES):
        raise UpstreamError("invalid_integration_request")
    return value["scenario"]


class IntegrationAPI:
    def __init__(self,client,scope):
        self.client=client
        profile=PROFILES[scope]
        self.launch="/api/projects/ai-agent-course/launch?profile="+profile
        self.stop="/api/projects/ai-agent-course/launch/stop?profile="+profile+"&expectedRunId="

    async def request(self,method,path,body=None,*,timeout=15):
        allowed=((method=="GET" and path in {CATALOG,self.launch}) or (method=="POST" and path==self.launch)
                 or (method=="POST" and path.startswith(self.stop) and RUN_ID.fullmatch(path[len(self.stop):])))
        if not allowed:
            raise UpstreamError("unsupported_integration_operation")
        try:
            async with asyncio.timeout(timeout):
                async with self.client.stream(method,ORIGIN+path,json=body if method=="POST" else None,
                    headers={"x-ai-server-request":"1"},timeout=httpx.Timeout(timeout,connect=5)) as response:
                    return await bounded_json(response,cap=262144)
        except (httpx.HTTPError,TimeoutError):
            raise UpstreamError("center_integration_transport_error") from None


def current_results(root,run_id,scope):
    progress=checked_json(root/"docs/artifacts/integration-progress.json")
    if (progress.get("run_id")!=run_id or progress.get("scenario")!=scope or progress.get("state")!="finished"
            or progress.get("exit_code")!=0 or progress.get("cleanup")!="OWNED_SUBTREE_CLOSED"):
        raise UpstreamError("integration_result_not_current_success")
    if scope=="android-emulator":
        report=checked_json(root/"docs/artifacts/android-socket-probe.json")
        full=report.get("scope")=="ISOLATED_ANDROID_UI_E2E"
        probe=report.get("scope")=="NONSECRET_SOCKET_PROBE_ONLY"
        if (report.get("run_id")!=run_id or report.get("technical_status")!="PASS"
                or not (full or probe) or report.get("token_transmitted") is not full
                or report.get("peer_uid") not in {0,2000} or report.get("ordinary_uid_rejected_before_payload") is not True
                or report.get("loopback_forward_verified") is not True or report.get("own_forward_removed") is not True
                or report.get("own_test_apk_removed") is not True or report.get("own_main_apk_removed") is not True
                or report.get("same_certificate") is not True):
            raise UpstreamError("android_nonsecret_boundary_incomplete")
        if full and (report.get("full_android_e2e")!="PASS"
            or report.get("gateway_fixture",{}).get("gateway_cleanup")!="CONFIRMED"
            or report.get("gateway_fixture",{}).get("relay_cleanup")!="CONFIRMED"
            or report.get("relaunch_new_submission_count")!=0
            or report.get("process_restart_checks")!={"keystore_after_process_restart":True,
                "sqlite_draft_after_process_restart":True,"authoritative_history_after_process_restart":True}):
            raise UpstreamError("android_ui_or_gateway_cleanup_incomplete")
        return {"scope":report["scope"],"peer_uid":report["peer_uid"],"token_transmitted":full,
                "full_android_e2e":report.get("full_android_e2e","NOT_ASSESSED"),"model_calls":report.get("model_calls","NOT_RUN")}
    report=checked_json(root/"docs/artifacts/integration-live-verification.json")
    if (report.get("run_id")!=run_id or report.get("technical_status")!="PASS" or report.get("stability_status")!="PASS"
        or report.get("sequential_completed")!=20 or report.get("overlapping_pairs_completed")!=5
        or report.get("total_stability_completed")!=30 or report.get("stable_jobs_distinct") is not True
        or report.get("complete_outputs_inspected") is not True
        or report.get("gateway_fixture",{}).get("gateway_cleanup")!="CONFIRMED"
        or report.get("restart_gateway_fixture",{}).get("gateway_cleanup")!="CONFIRMED"):
        raise UpstreamError("selected_live_integration_incomplete")
    return {"scope":"actual selected local model","stability_completed":30,"technical_status":"PASS","model_calls":"REAL_SELECTED_MODEL"}


async def observe(api,root,model_id,scope,*,poll_seconds=0.2):
    result={"schema_version":"center-integration-observation-v1","technical_status":"FAIL","acceptance_status":"PARTIAL"}
    own_id=None
    pending=None
    try:
        model_state(await api.request("GET",CATALOG),model_id)
        status=await api.request("GET",api.launch)
        if status.get("configured") is not True or status.get("running") is not False or status.get("runId"):
            raise UpstreamError("foreign_integration_profile_active")
        pending=asyncio.create_task(api.request("POST",api.launch,
            {"model":"ai-server-local-"+model_id+"/local","effort":"","externalSearch":False},timeout=410))
        launched=await asyncio.shield(pending)
        candidate=launched.get("runId")
        if not isinstance(candidate,str) or not RUN_ID.fullmatch(candidate):
            raise UpstreamError("integration_child_identity_missing")
        own_id=candidate
        result["run_id"]=own_id
        deadline=time.monotonic()+2130
        while True:
            active=await api.request("GET",api.launch)
            if active.get("running") is False and not active.get("runId"):
                result.update(current_results(root,own_id,scope))
                result["technical_status"]="PASS"
                break
            if active.get("running") is not True or active.get("runId")!=own_id:
                raise UpstreamError("integration_child_identity_changed")
            if time.monotonic()>=deadline:
                raise UpstreamError("integration_completion_deadline")
            await asyncio.sleep(poll_seconds)
    except UpstreamError as error:
        result["error_code"]=error.code
    except asyncio.CancelledError:
        result["error_code"]="integration_observer_cancelled"
    except Exception:
        result["error_code"]="integration_execution_failed"
    finally:
        from harness.center_service import deferred
        if pending and not own_id:
            try:
                launched=await deferred(pending)
                candidate=launched.get("runId")
                if isinstance(candidate,str) and RUN_ID.fullmatch(candidate):
                    own_id=candidate
            except Exception:
                result["launch_ownership"]="UNCONFIRMED"
        if own_id:
            async def cleanup():
                active=await api.request("GET",api.launch)
                if active.get("running") is True and active.get("runId")==own_id:
                    stopped=await api.request("POST",api.stop+own_id,{},timeout=345)
                    if stopped.get("running") is not False or stopped.get("runId"):
                        raise UpstreamError("integration_cleanup_unconfirmed")
                elif active.get("running") is True or active.get("runId"):
                    result["foreign_launch_preserved"]=True
            try:
                await deferred(asyncio.create_task(cleanup()))
            except Exception:
                result.update(technical_status="FAIL",cleanup="UNCONFIRMED")
    return result


async def main(scope,*,root=ROOT,env=None,client=None):
    try:
        selection=selected_configuration(root,os.environ if env is None else env)
        if scope not in PROFILES:
            raise UpstreamError("invalid_integration_scope")
    except UpstreamError:
        print("INTEGRATION_STATUS: BLOCKED (explicit allowed selected configuration required).")
        return 3
    if client:
        value=await observe(IntegrationAPI(client,scope),root,selection["model_id"],scope)
    else:
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as actual:
            value=await observe(IntegrationAPI(actual,scope),root,selection["model_id"],scope)
    save_json(root/"docs/artifacts/center-integration-observation.json",value)
    print("INTEGRATION_STATUS: "+value["technical_status"])
    return 0 if value["technical_status"]=="PASS" else 1
