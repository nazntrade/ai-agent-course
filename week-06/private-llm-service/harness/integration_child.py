"""Center-owned finite LIVE suite with isolated data and authenticated control."""
import asyncio
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import shutil
import stat
import subprocess
import tempfile
import time

from fastapi import FastAPI,Header,Request
from fastapi.responses import JSONResponse
import uvicorn

from gateway.app.upstream import ProviderConfig
from harness.runner import ROOT,save_json
from harness.stub_startup import OwnedJob

SCOPES={"android-emulator","integration-live"}


@contextmanager
def owned_fixture(run_id,state):
    root=Path(tempfile.mkdtemp(prefix="privatechat-integration-")).resolve()
    try:
        yield root
    finally:
        if state.get("cleanup")=="OWNED_SUBTREE_CLOSED":
            env={"APP_RUN_ID":run_id,"D30_FIXTURE_ROOT":str(root),
                 "APP_DATA_DIR":str(root/"data"),"APP_PAIRING_DIR":str(root/"pairing")}
            try:
                fixture_configuration(env)
                shutil.rmtree(root)
                state["fixture_removed"]=True
            except (OSError,ValueError):
                state["cleanup"]="UNCONFIRMED"
        if not state.get("fixture_removed"):
            # This ignored record is private recovery state, never public evidence.
            save_json(ROOT/".runtime/integration-leftovers"/(run_id+".json"),{
                "schema_version":"integration-leftover-v1","run_id":run_id,
                "fixture_path":str(root),"cleanup":"UNCONFIRMED"})
            state["fixture_preserved"]=True


def fixture_configuration(env=None):
    env=os.environ if env is None else env
    run_id=env.get("APP_RUN_ID","")
    original=Path(env.get("D30_FIXTURE_ROOT",""))
    root=original.resolve()
    base=Path(tempfile.gettempdir()).resolve()
    if (not re.fullmatch(r"[a-f0-9]{32}",run_id) or root.parent!=base
            or not root.name.startswith("privatechat-integration-")):
        raise ValueError("owned_fixture_required")
    for candidate in (original,root/"fixture.json"):
        info=candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
            raise ValueError("linked_fixture_context")
    marker=root/"fixture.json"
    if marker.stat().st_size>4096:
        raise ValueError("invalid_fixture_marker")
    value=json.loads(marker.read_text())
    if value!={"schema_version":"privatechat-fixture-v1","run_id":run_id}:
        raise ValueError("fixture_identity_mismatch")
    for key,child in (("APP_DATA_DIR","data"),("APP_PAIRING_DIR","pairing")):
        raw=Path(env.get(key,""))
        info=raw.lstat()
        path=raw.resolve()
        if path!=root/child or stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
            raise ValueError("fixture_path_escape")
    return root


async def execute(scope,stop,server,run_id):
    report={"schema_version":"integration-progress-v1","run_id":run_id,"scenario":scope,"state":"started"}
    save_json(ROOT/"docs/artifacts/integration-progress.json",report)
    code=1
    cleanup="NOT_ASSESSED"
    fixture_state={"cleanup":cleanup}
    try:
        startup_deadline=time.monotonic()+15
        while not server.started:
            if stop.is_set() or time.monotonic()>=startup_deadline:
                raise ValueError("control_startup_deadline")
            await asyncio.sleep(0.01)
        with owned_fixture(run_id,fixture_state) as root:
            (root/"data").mkdir(); (root/"pairing").mkdir()
            (root/"fixture.json").write_text(json.dumps({"schema_version":"privatechat-fixture-v1","run_id":run_id}))
            env=dict(os.environ)
            env.update(D30_FIXTURE_ROOT=str(root),APP_DATA_DIR=str(root/"data"),APP_PAIRING_DIR=str(root/"pairing"))
            fixture_configuration(env)
            job=OwnedJob()
            process=None
            try:
                process=subprocess.Popen(["cmd.exe","/d","/c",str(ROOT/"test.bat"),"scenario",scope],
                    cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW|4)
                job.attach_and_resume(process)
                deadline=time.monotonic()+1800
                while process.poll() is None and not stop.is_set() and time.monotonic()<deadline:
                    await asyncio.sleep(0.1)
                if process.poll() is None:
                    (root/"CANCEL").write_text("cancel")
                    try:
                        # Reserve ten seconds inside the 330 s cleanup budget
                        # for verified owned-process fallback and final exit.
                        await asyncio.to_thread(process.wait,timeout=320)
                    except subprocess.TimeoutExpired:
                        cleanup="UNCONFIRMED"
                    code=130 if stop.is_set() else 1
                else:
                    code=process.returncode
                if (root/"CLEANUP_UNCONFIRMED").exists():
                    cleanup="UNCONFIRMED"
            finally:
                job.close()
                if process and process.poll() is None:
                    await asyncio.to_thread(process.wait,timeout=10)
                if cleanup!="UNCONFIRMED":
                    cleanup="OWNED_SUBTREE_CLOSED"
                fixture_state["cleanup"]=cleanup
    except Exception:
        code=1
    finally:
        if fixture_state.get("fixture_preserved"):
            cleanup="UNCONFIRMED"
        report.update(state="finished",exit_code=code,cleanup=cleanup,acceptance_status="PARTIAL")
        report.update({key:value for key,value in fixture_state.items() if key!="cleanup"})
        save_json(ROOT/"docs/artifacts/integration-progress.json",report)
        server.should_exit=True
    return code


def create_control_app(run_id,token,stop,completion):
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.get("/healthz")
    async def health():
        return {"status":"ok","run_id":run_id}
    @app.post("/internal/shutdown")
    async def shutdown(request:Request,authorization:str|None=Header(default=None)):
        if request.client.host not in {"127.0.0.1","::1"} or not secrets.compare_digest(authorization or "","Bearer "+token):
            return JSONResponse({"error":{"code":"not_found"}},status_code=404)
        stop.set()
        # Center starts its short final process-exit wait only after this
        # response. Do not acknowledge shutdown while suite cleanup is pending.
        if not await completion():
            return JSONResponse({"error":{"code":"cleanup_unconfirmed"}},status_code=503)
        return {"stopped":True}
    return app


async def run(scope):
    ProviderConfig.from_environment()
    run_id=os.environ.get("APP_RUN_ID","")
    token=os.environ.get("APP_SHUTDOWN_TOKEN","")
    if scope not in SCOPES or not re.fullmatch(r"[a-f0-9]{32}",run_id) or not token:
        raise ValueError("explicit_center_child_required")
    with socket.socket() as reservation:
        reservation.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        reservation.bind(("127.0.0.1",8792))
    stop=asyncio.Event()
    suite=None
    async def completion():
        from harness.center_service import deferred
        from harness.center_preflight import checked_json
        if suite is None:
            return False
        await deferred(suite)
        value=checked_json(ROOT/"docs/artifacts/integration-progress.json")
        return (value.get("run_id")==run_id and value.get("state")=="finished"
                and value.get("cleanup")=="OWNED_SUBTREE_CLOSED")
    app=create_control_app(run_id,token,stop,completion)
    log={"version":1,"handlers":{"null":{"class":"logging.NullHandler"}},
         "loggers":{name:{"handlers":["null"],"propagate":False} for name in ("uvicorn","uvicorn.error","uvicorn.access")}}
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=8792,access_log=False,log_config=log,timeout_graceful_shutdown=330))
    suite=asyncio.create_task(execute(scope,stop,server,run_id))
    try:
        await server.serve()
        return await suite
    finally:
        stop.set()
        if not suite.done():
            await suite


def main(scope):
    try:
        return asyncio.run(run(scope))
    except Exception:
        print("INTEGRATION_CHILD_STATUS: CONFIGURATION_OR_EXECUTION_ERROR")
        return 1
