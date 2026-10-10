"""Trusted OS startup of an isolated stub, with Windows-owned process cleanup."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import tempfile
import time

import httpx

from gateway.app.config import Settings
from gateway.app.database import Store
from harness.runner import ROOT, clean_environment, save_json


class OwnedJob:
    def __init__(self):
        self.kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        k=self.kernel
        k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        k.CreateJobObjectW.restype=wintypes.HANDLE
        k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
        k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        k.OpenProcess.restype=wintypes.HANDLE
        k.CloseHandle.argtypes=[wintypes.HANDLE]
        k.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        k.OpenThread.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        k.OpenThread.restype=wintypes.HANDLE
        k.ResumeThread.argtypes=[wintypes.HANDLE]
        k.ResumeThread.restype=wintypes.DWORD
        class Basic(ctypes.Structure):
            _fields_=[("process_time",ctypes.c_int64),("job_time",ctypes.c_int64),("flags",wintypes.DWORD),
                      ("minimum",ctypes.c_size_t),("maximum",ctypes.c_size_t),("active",wintypes.DWORD),
                      ("affinity",ctypes.c_size_t),("priority",wintypes.DWORD),("scheduling",wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_=[("basic",Basic),("io",ctypes.c_uint64*6),("process_memory",ctypes.c_size_t),
                      ("job_memory",ctypes.c_size_t),("peak_process",ctypes.c_size_t),("peak_job",ctypes.c_size_t)]
        info=Extended()
        info.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.handle=k.CreateJobObjectW(None,None)
        if not self.handle:
            raise ValueError("owned_job_unavailable")
        if not k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close()
            raise ValueError("owned_job_configuration_failed")

    def attach_and_resume(self,process):
        # The batch process is created suspended: no child can escape assignment.
        if not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            process.terminate()
            process.wait(timeout=5)
            raise ValueError("owned_job_assignment_failed")
        class ThreadEntry(ctypes.Structure):
            _fields_=[("size",wintypes.DWORD),("usage",wintypes.DWORD),("id",wintypes.DWORD),
                      ("owner",wintypes.DWORD),("priority",wintypes.LONG),("delta",wintypes.LONG),("flags",wintypes.DWORD)]
        k=self.kernel
        k.Thread32First.argtypes=[wintypes.HANDLE,ctypes.POINTER(ThreadEntry)]
        k.Thread32Next.argtypes=[wintypes.HANDLE,ctypes.POINTER(ThreadEntry)]
        snapshot=k.CreateToolhelp32Snapshot(4,0)
        if snapshot==ctypes.c_void_p(-1).value:
            raise ValueError("owned_thread_inventory_failed")
        resumed=False
        try:
            entry=ThreadEntry(size=ctypes.sizeof(ThreadEntry))
            found=k.Thread32First(snapshot,ctypes.byref(entry))
            while found:
                if entry.owner==process.pid:
                    thread=k.OpenThread(2,False,entry.id)
                    if not thread:
                        raise ValueError("owned_thread_open_failed")
                    try:
                        if k.ResumeThread(thread)==0xFFFFFFFF:
                            raise ValueError("owned_thread_resume_failed")
                        resumed=True
                    finally:
                        k.CloseHandle(thread)
                found=k.Thread32Next(snapshot,ctypes.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        if not resumed:
            raise ValueError("owned_thread_missing")

    def contains(self,pid):
        process=self.kernel.OpenProcess(0x1000,False,pid)
        if not process:
            return False
        try:
            present=wintypes.BOOL()
            return bool(self.kernel.IsProcessInJob(process,self.handle,ctypes.byref(present)) and present.value)
        finally:
            self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle=None


def listening_pid(port):
    api=ctypes.WinDLL("iphlpapi",use_last_error=True).GetExtendedTcpTable
    api.argtypes=[ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD),wintypes.BOOL,wintypes.DWORD,ctypes.c_int,wintypes.DWORD]
    api.restype=wintypes.DWORD
    size=wintypes.DWORD(65536)
    buffer=ctypes.create_string_buffer(size.value)
    result=api(buffer,ctypes.byref(size),False,socket.AF_INET,3,0)
    if result or size.value>65536:
        raise ValueError("bounded_port_inventory_failed")
    count=struct.unpack_from("<I",buffer.raw)[0]
    if count>2000 or 4+count*24>len(buffer):
        raise ValueError("invalid_port_inventory")
    owners=[]
    for index in range(count):
        state,address,local_port,_,_,pid=struct.unpack_from("<IIIIII",buffer.raw,4+index*24)
        if state==2 and socket.ntohs(local_port&0xFFFF)==port:
            if socket.inet_ntoa(struct.pack("<I",address))!="127.0.0.1":
                raise ValueError("unexpected_listener_binding")
            owners.append(pid)
    if len(owners)>1:
        raise ValueError("ambiguous_listener_identity")
    return owners[0] if owners else None


def checked_response(client,method,url,**kwargs):
    with client.stream(method,url,**kwargs) as response:
        body=bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body)>4096:
                raise ValueError("bounded_http_response_exceeded")
        if response.status_code!=200:
            raise ValueError("unexpected_stub_http_status")
        value=json.loads(body)
        if not isinstance(value,dict):
            raise ValueError("invalid_stub_http_response")
        return value


def main():
    report={"schema_version":"trusted-stub-startup-v1","technical_status":"FAIL","model_calls":"NOT_RUN",
            "acceptance_status":"PARTIAL","process_tree_cleanup":"NOT_ASSESSED"}
    if os.name!="nt":
        report["error_code"]="windows_owned_process_boundary_required"
        save_json(ROOT/"docs/artifacts/trusted-stub-startup-verification.json",report)
        return 2
    process=None
    job=None
    try:
        with tempfile.TemporaryDirectory(prefix="private-chat-startup-") as directory:
            data=Path(directory)
            settings=Settings(data)
            store=Store(data/"chat.sqlite3",settings)
            try:
                device,token=store.provision("isolated-startup","test",pairing=False)
            finally:
                store.close()
            with socket.socket() as reservation:
                reservation.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                reservation.bind(("127.0.0.1",0))
                port=reservation.getsockname()[1]
            if listening_pid(port) is not None:
                raise ValueError("port_became_occupied")
            secret=secrets.token_urlsafe(32)
            env=clean_environment()
            env.update(APP_DATA_DIR=str(data),APP_BIND_HOST="127.0.0.1",APP_PORT=str(port),APP_SHUTDOWN_TOKEN=secret)
            job=OwnedJob()
            process=subprocess.Popen(["cmd.exe","/d","/c",str(ROOT/"run_app.bat"),"stub"],cwd=ROOT,env=env,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW|4)
            job.attach_and_resume(process)
            origin="http://127.0.0.1:"+str(port)
            attributed=False
            try:
                with httpx.Client(trust_env=False,follow_redirects=False,timeout=2) as client:
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        if process.poll() is not None:
                            raise ValueError("trusted_child_exited_before_ready")
                        pid=listening_pid(port)
                        if pid:
                            if not job.contains(pid):
                                raise ValueError("foreign_listener_preserved")
                            attributed=True
                            break
                        time.sleep(0.05)
                    if not attributed:
                        raise ValueError("owned_startup_deadline")
                    health=checked_response(client,"GET",origin+"/healthz")
                    identity=checked_response(client,"GET",origin+"/v1/me",headers={"Authorization":"Bearer "+token})
                    if health.get("status")!="ok" or identity.get("device_id")!=device:
                        raise ValueError("owned_health_or_database_mismatch")
                    report.update(health_ready=True,listener_owned_by_process_job=True,own_temporary_device_verified=True,
                                  live_policy="forbidden",provider_fields_present=False,loopback_only=True)
                    stopped=checked_response(client,"POST",origin+"/internal/shutdown",headers={"Authorization":"Bearer "+secret})
                    if stopped.get("stopped") is not True:
                        raise ValueError("owned_graceful_stop_unconfirmed")
                    process.wait(timeout=10)
                    if process.returncode!=0 or listening_pid(port) is not None:
                        raise ValueError("owned_process_exit_or_port_cleanup_failed")
                    report.update(technical_status="PASS",graceful_shutdown_verified=True,trusted_exit_code=0)
            finally:
                job.close()
                job=None
                if process.poll() is None:
                    process.wait(timeout=10)
                report["process_tree_cleanup"]="OWNED_JOB_CLOSED"
    except Exception:
        report["error_code"]="isolated_trusted_startup_or_cleanup_failed"
    finally:
        if job:
            job.close()
            if process:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    report["process_tree_cleanup"]="UNCONFIRMED"
    save_json(ROOT/"docs/artifacts/trusted-stub-startup-verification.json",report)
    print("TRUSTED_STUB_STARTUP_STATUS: "+report["technical_status"])
    return 0 if report["technical_status"]=="PASS" else 1
