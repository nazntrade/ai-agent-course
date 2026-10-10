"""Owned TEMP gateway and fixed test-only transport faults for Android UI checks."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import secrets
import socket
import subprocess
import threading
import time

import httpx
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,StreamingResponse
import uvicorn

from gateway.app.config import Settings
from gateway.app.database import Store
from gateway.app.manage import provision_device,confirm_pairing
from harness.android_normal_setup import PairingSelection,selected_payload
from harness.integration_child import fixture_configuration
from harness.runner import ROOT
from harness.stub_startup import OwnedJob,listening_pid,checked_response

COMMANDS={"arm_drop","arm_rate","arm_history_hold","history_held","release_history","job_count"}


class FaultRelay:
    """Faults affect only this freshly owned E2E fixture, never normal service."""
    def __init__(self,origin,fixture):
        self.origin=origin
        self.fixture=fixture
        self.lock=threading.Lock()
        self.drop=False
        self.rate=False
        self.hold=False
        self.held=threading.Event()
        self.release=threading.Event()
        self.posts=0
        self.dropped=0
        self.rate_responses=0
        self.app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        self.app.api_route("/{path:path}",methods=["GET","POST","PATCH","DELETE"])(self.forward)

    def control(self,action):
        if action not in COMMANDS:
            raise ValueError("fixed_e2e_control_required")
        with self.lock:
            if action=="arm_drop": self.drop=True
            elif action=="arm_rate": self.rate=True
            elif action=="arm_history_hold":
                self.hold=True; self.held.clear(); self.release.clear()
            elif action=="release_history": self.release.set(); self.hold=False
            return {"kind":"control_result","action":action,"held":self.held.is_set(),
                    "posts":self.posts,"dropped":self.dropped,"controlled_rate_responses":self.rate_responses}

    async def forward(self,request:Request,path:str):
        # A fixed origin, no redirect/proxy/endpoint selected by the client.
        if (not path.startswith("v1/") or ".." in path.split("/") or len(path)>256
                or (self.fixture/"CANCEL").exists()):
            return JSONResponse({"error":{"code":"not_found"}},status_code=404)
        body=bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body)>131072:
                return JSONResponse({"error":{"code":"request_too_large"}},status_code=413)
        submission=request.method=="POST" and path.startswith("v1/conversations/") and path.endswith("/requests")
        with self.lock:
            rate=submission and self.rate
            drop=submission and self.drop and not rate
            held=request.method=="GET" and path.endswith("/messages") and self.hold
            if held: self.hold=False
            if rate: self.rate=False; self.rate_responses+=1
            if drop: self.drop=False
            if submission and not rate: self.posts+=1
        if rate:
            return JSONResponse({"error":{"code":"rate_limited"}},status_code=429,headers={"Retry-After":"2"})
        if held:
            self.held.set()
            deadline=time.monotonic()+30
            while not self.release.is_set():
                if time.monotonic()>deadline or (self.fixture/"CANCEL").exists():
                    return JSONResponse({"error":{"code":"service_stopping"}},status_code=503)
                await asyncio.sleep(.05)
        headers={key:value for key,value in request.headers.items()
                 if key.lower() in {"authorization","content-type","idempotency-key","last-event-id"}}
        url=self.origin+"/"+path+("?"+request.url.query if request.url.query else "")
        client=httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=httpx.Timeout(330,connect=3))
        try:
            response=await client.send(client.build_request(request.method,url,headers=headers,content=bytes(body)),stream=True)
        except BaseException:
            await client.aclose()
            raise
        result_headers={key:value for key,value in response.headers.items()
                        if key.lower() in {"content-type","retry-after","cache-control"}}
        async def contents():
            try:
                if drop and response.status_code in {200,202}:
                    # Backend admitted the request; fail the transport before any
                    # response body reaches the device. Android must reuse its key.
                    with self.lock: self.dropped+=1
                    await response.aread()
                    raise RuntimeError("controlled_e2e_response_disconnect")
                total=0
                async for chunk in response.aiter_bytes():
                    total+=len(chunk)
                    if total>8388608 or (self.fixture/"CANCEL").exists():
                        return
                    yield chunk
            finally:
                await response.aclose(); await client.aclose()
        return StreamingResponse(contents(),status_code=response.status_code,headers=result_headers)


@contextmanager
def gateway_fixture(*,relay_enabled=True):
    fixture=fixture_configuration()
    if (fixture/"CANCEL").exists(): raise ValueError("fixture_cancelled")
    settings=replace(Settings.from_environment("live"),queue_capacity=8,owner_waiting=2,prompt_cap=6144,output_cap=1024)
    store=Store(settings.data_dir/"chat.sqlite3",settings)
    process=job=server=thread=relay=None
    cancellation_watch=None
    watch_done=threading.Event()
    shutdown_lock=threading.Lock()
    shutdown_requested=False
    report={"gateway_cleanup":"NOT_ASSESSED","controlled_faults":"UI_FIXTURE_ONLY" if relay_enabled else "NONE"}
    try:
        provisioned=provision_device(store,settings.pairing_dir,"android-fixture","isolated-emulator")
        with socket.socket() as reservation:
            reservation.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            reservation.bind(("127.0.0.1",0)); port=reservation.getsockname()[1]
        if listening_pid(port) is not None: raise ValueError("foreign_gateway_listener_preserved")
        secret=secrets.token_urlsafe(32)
        env=dict(os.environ)
        env.update(APP_BIND_HOST="127.0.0.1",APP_PORT=str(port),APP_SHUTDOWN_TOKEN=secret,
                   APP_QUEUE_CAPACITY="8",APP_OWNER_WAITING="2",APP_PROMPT_CAP="6144",APP_OUTPUT_CAP="1024")
        job=OwnedJob()
        process=subprocess.Popen(["cmd.exe","/d","/c",str(ROOT/"run_app.bat"),"live"],cwd=ROOT,env=env,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW|4)
        job.attach_and_resume(process)
        origin="http://127.0.0.1:"+str(port)
        deadline=time.monotonic()+20
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=3) as client:
            while True:
                if process.poll() is not None: raise ValueError("owned_gateway_early_exit")
                pid=listening_pid(port)
                if pid:
                    if not job.contains(pid): raise ValueError("foreign_gateway_listener_preserved")
                    if checked_response(client,"GET",origin+"/healthz")!={"status":"ok"}:
                        raise ValueError("owned_gateway_not_ready")
                    break
                if time.monotonic()>deadline: raise ValueError("owned_gateway_deadline")
                time.sleep(.05)
        report.update(trusted_gateway_ready=True,listener_owned=True,lazy_start=True)
        def stop_gateway(deadline):
            nonlocal shutdown_requested
            with shutdown_lock:
                if report["gateway_cleanup"]=="CONFIRMED": return
                try:
                    if process.poll() is not None: raise ValueError("gateway_unexpected_exit")
                    pid=listening_pid(port)
                    if not pid or not job.contains(pid): raise ValueError("shutdown_owner_unconfirmed")
                    shutdown_requested=True
                    with httpx.Client(trust_env=False,follow_redirects=False,timeout=max(1,deadline-time.monotonic()-10)) as client:
                        stopped=checked_response(client,"POST",origin+"/internal/shutdown",headers={"Authorization":"Bearer "+secret})
                        if stopped!={"stopped":True}: raise ValueError("gateway_shutdown_unconfirmed")
                    process.wait(timeout=max(1,deadline-time.monotonic()))
                    if process.returncode or listening_pid(port) is not None: raise ValueError("gateway_exit_unconfirmed")
                    report["gateway_cleanup"]="CONFIRMED"
                except Exception:
                    report["gateway_cleanup"]="UNCONFIRMED"
        def cancel_watch():
            while not watch_done.wait(.05):
                if (fixture/"CANCEL").exists():
                    # Gateway and an auxiliary borrowed lease drain concurrently,
                    # rather than adding two late-acquire finally timeouts.
                    stop_gateway(time.monotonic()+320)
                    return
        cancellation_watch=threading.Thread(target=cancel_watch,daemon=True); cancellation_watch.start()
        relay=FaultRelay(origin,fixture)
        listener=socket.socket()
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        listener.bind(("127.0.0.1",0)); listener.listen(32)
        relay_port=listener.getsockname()[1]
        log={"version":1,"handlers":{"null":{"class":"logging.NullHandler"}},
             "loggers":{name:{"handlers":["null"],"propagate":False} for name in ("uvicorn","uvicorn.error","uvicorn.access","httpx")}}
        server=uvicorn.Server(uvicorn.Config(relay.app,access_log=False,log_config=log,timeout_graceful_shutdown=10))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True); thread.start()
        deadline=time.monotonic()+10
        while not server.started:
            if not thread.is_alive() or time.monotonic()>deadline: raise ValueError("relay_start_failed")
            time.sleep(.02)
        selection=PairingSelection("http://10.0.2.2:"+str(relay_port if relay_enabled else port),provisioned["device_id"],
            lambda:selected_payload(store,settings.pairing_dir,provisioned["device_id"]),
            lambda device:confirm_pairing(store,settings.pairing_dir,device))
        yield selection,relay.control,report
    finally:
        cleanup_deadline=time.monotonic()+320
        if relay: relay.release.set()
        if server:
            server.should_exit=True
            thread.join(timeout=5)
            if thread.is_alive(): report["relay_cleanup"]="UNCONFIRMED"
            else: report["relay_cleanup"]="CONFIRMED"
        if process and process.poll() is None:
            if cancellation_watch is not None:
                stop_gateway(cleanup_deadline)
        watch_done.set()
        if cancellation_watch is not None:
            cancellation_watch.join(timeout=max(1,cleanup_deadline-time.monotonic()))
            if cancellation_watch.is_alive(): report["gateway_cleanup"]="UNCONFIRMED"
        if job:
            job.close()
            if process and process.poll() is None: process.wait(timeout=5)
        if report["gateway_cleanup"]!="CONFIRMED":
            (fixture/"CLEANUP_UNCONFIRMED").write_text("owned gateway cleanup unconfirmed")
        report["admitted_jobs"]=store.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        report["jobs_with_native_token_count"]=store.db.execute("SELECT count(*) FROM jobs WHERE prompt_tokens IS NOT NULL").fetchone()[0]
        report["terminal_job_counts"]={row[0]:row[1] for row in store.db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
        store.close()
