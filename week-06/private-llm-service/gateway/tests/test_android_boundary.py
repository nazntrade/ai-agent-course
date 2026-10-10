"""P03 initial scope: explicit dispatch, isolation and bounded socket framing."""
import json
import os
from pathlib import Path
import struct
import tempfile

import httpx
import pytest

from gateway.app.upstream import UpstreamError
from harness import center_integration as helper,runner
from harness.integration_child import fixture_configuration,create_control_app
import asyncio
from harness.pairing_bridge import receive_frame,send_frame
from harness import pairing_bridge as bridge
from harness import integration_child,android_build
from harness.runner import save_json

pytestmark=pytest.mark.integration
OWN="b"*32
FOREIGN="c"*32
MODEL="a"*16


@pytest.mark.parametrize("delta",[{"extra":True},{"scenario":"../other"},{"scenario":[]},{"schema_version":"unknown"}])
def test_integration_request_rejects_unknown_fields_types_and_scope(tmp_path,delta):
    value={"schema_version":"center-integration-request-v1","scenario":"android-emulator"}
    value.update(delta)
    save_json(tmp_path/".runtime/center-integration-request.json",value)
    with pytest.raises(UpstreamError,match="invalid_integration_request"):
        helper.requested_scope(tmp_path)


@pytest.mark.parametrize("environment",[{"AI_TEST_LIVE_POLICY":"forbidden"},{"AI_TEST_LIVE_POLICY":"unknown"},
    {"AI_TEST_LIVE_POLICY":"allowed","AI_TEST_MODEL_NAME":"local"}])
def test_live_dispatch_rejects_policy_and_partial_before_request_or_network(monkeypatch,environment):
    monkeypatch.setattr(runner.os,"environ",environment)
    def forbidden(_):
        raise AssertionError("Rejected configuration must not inspect integration request")
    monkeypatch.setattr(helper,"requested_scope",forbidden)
    assert runner.tests("live")==3


def test_fixture_requires_current_run_and_owned_temp_data_and_pairing():
    with tempfile.TemporaryDirectory(prefix="privatechat-integration-") as folder:
        root=Path(folder).resolve()
        (root/"data").mkdir(); (root/"pairing").mkdir()
        (root/"fixture.json").write_text(json.dumps({"schema_version":"privatechat-fixture-v1","run_id":OWN}))
        env={"APP_RUN_ID":OWN,"D30_FIXTURE_ROOT":str(root),"APP_DATA_DIR":str(root/"data"),"APP_PAIRING_DIR":str(root/"pairing")}
        assert fixture_configuration(env)==root
        for delta in ({"APP_RUN_ID":FOREIGN},{"APP_DATA_DIR":str(root.parent/"normal-data")},
                      {"APP_PAIRING_DIR":str(root/"data")}):
            with pytest.raises((ValueError,OSError)):
                fixture_configuration(dict(env,**delta))


class FragmentSocket:
    def __init__(self,payload):
        self.payload=bytearray(payload)
        self.sent=bytearray()
    def recv(self,count):
        result=bytes(self.payload[:min(count,1)])
        del self.payload[:len(result)]
        return result
    def sendall(self,payload):
        self.sent.extend(payload)


def test_socket_frame_actual_split_bytes_and_limits():
    payload=b'{"kind":"nonsecret_probe"}'
    assert receive_frame(FragmentSocket(struct.pack("!I",len(payload))+payload))=={"kind":"nonsecret_probe"}
    for data in (struct.pack("!I",4097),struct.pack("!I",3)+b"{",struct.pack("!I",2)+b"[]"):
        with pytest.raises(ValueError):
            receive_frame(FragmentSocket(data))
    connection=FragmentSocket(b"")
    send_frame(connection,{"kind":"nonsecret_probe"})
    assert receive_frame(FragmentSocket(connection.sent))=={"kind":"nonsecret_probe"}
    with pytest.raises(ValueError):
        send_frame(connection,{"value":"a"*4096})


def test_instrumentation_diagnostic_evidence_only_preserves_whitelist():
    arbitrary=b"private fixture content should never be saved"
    evidence=bridge.instrumentation_evidence(b"INSTRUMENTATION_RESULT: probe_stage=ordinary_uid_accept\n"+arbitrary,
        b"INSTRUMENTATION_FAILED: Process crashed ClassNotFoundException",0)
    assert evidence["instrumentation_failure_stage"]=="ordinary_uid_accept"
    assert evidence["instrumentation_command_failed"] is True
    assert evidence["instrumentation_process_crashed"] is True
    assert evidence["instrumentation_missing_class"] is True
    assert evidence["instrumentation_outcome"]=="UNCONFIRMED"
    assert arbitrary.decode() not in json.dumps(evidence)


def test_crash_diagnostics_only_fresh_owned_class_and_source_line():
    old="Process: com.example.privatechat.e2e, PID: 123\njava.io.IOException: arbitrary-private-content\n"
    new="Process: com.example.privatechat.e2e, PID: 124\njava.lang.IllegalStateException: arbitrary-private-content\n at com.example.privatechat.ProbeInstrumentation.onStart(ProbeInstrumentation.kt:55)\n"
    evidence=bridge.owned_crash_evidence(new,old)
    assert evidence=={"owned_crash_diagnostic":"FRESH_OWNED_JAVA_CRASH",
        "owned_crash_exception_class":"java.lang.IllegalStateException","owned_crash_probe_source_line":55}
    assert "arbitrary-private-content" not in json.dumps(evidence)
    assert bridge.owned_crash_evidence(old,old)=={"owned_crash_diagnostic":"NO_FRESH_OWNED_JAVA_CRASH"}
    assert bridge.owned_crash_evidence("Process: com.example.foreign, PID: 99\njava.io.IOException\n",old)=={
        "owned_crash_diagnostic":"NO_FRESH_OWNED_JAVA_CRASH"}


def test_public_handshake_retries_only_empty_frame_before_payload(monkeypatch):
    class PublicSocket(FragmentSocket):
        def settimeout(self,_):
            pass
        def close(self):
            self.closed=True
    from types import SimpleNamespace
    message=b'{"kind":"probe_ready","peer_uid":2000}'
    empty=PublicSocket(b"")
    valid=PublicSocket(struct.pack("!I",len(message))+message)
    sockets=iter([empty,valid])
    monkeypatch.setattr(bridge.socket,"create_connection",lambda *_,**__:next(sockets))
    monkeypatch.setattr(bridge.time,"sleep",lambda _:None)
    connection,ready,retries=bridge.connect_public_handshake(10000,SimpleNamespace(poll=lambda:None))
    assert connection is valid and ready=={"kind":"probe_ready","peer_uid":2000} and retries==1
    assert empty.closed and not empty.sent and not valid.sent
    partial=PublicSocket(b"\x00")
    calls=[]
    monkeypatch.setattr(bridge.socket,"create_connection",lambda *_,**__:calls.append(True) or partial)
    with pytest.raises(ValueError,match="socket_frame_incomplete"):
        bridge.connect_public_handshake(10000,SimpleNamespace(poll=lambda:None))
    assert partial.closed and len(calls)==1 and not partial.sent
    class BodyInterrupted(PublicSocket):
        def recv(self,count):
            if self.payload:
                result=bytes(self.payload); self.payload.clear(); return result
            raise TimeoutError("bounded fixture timeout")
    body=BodyInterrupted(struct.pack("!I",4))
    calls.clear()
    monkeypatch.setattr(bridge.socket,"create_connection",lambda *_,**__:calls.append(True) or body)
    with pytest.raises(ValueError,match="socket_frame_interrupted"):
        bridge.connect_public_handshake(10000,SimpleNamespace(poll=lambda:None))
    assert body.closed and len(calls)==1 and not body.sent


class SnapshotAPI:
    def __init__(self,root,*,foreign=False,replace=False,stale=False):
        self.root=root
        self.active=FOREIGN if foreign else None
        self.replace,self.stale=replace,stale
        self.launch="/fixed-launch"
        self.stop="/fixed-stop/"
        self.calls=[]
    async def request(self,method,path,body=None,**_):
        self.calls.append((method,path))
        if path==helper.CATALOG:
            return {"models":[{"id":MODEL,"status":"stopped"}]}
        if method=="POST" and path==self.launch:
            save_json(self.root/"docs/artifacts/integration-progress.json",{
                "run_id":FOREIGN if self.stale else OWN,"scenario":"android-emulator","state":"finished",
                "exit_code":0,"cleanup":"OWNED_SUBTREE_CLOSED"})
            save_json(self.root/"docs/artifacts/android-socket-probe.json",{
                "run_id":OWN,"technical_status":"PASS","scope":"NONSECRET_SOCKET_PROBE_ONLY",
                "token_transmitted":False,"peer_uid":2000,"ordinary_uid_rejected_before_payload":True,
                "loopback_forward_verified":True,"own_forward_removed":True,"own_test_apk_removed":True,
                "own_main_apk_removed":True,"same_certificate":True})
            self.active=FOREIGN if self.replace else None
            return {"running":False,"runId":OWN}
        return {"configured":True,"running":bool(self.active),"runId":self.active}


async def test_finite_probe_fast_completion_requires_fresh_owned_results(tmp_path):
    result=await helper.observe(SnapshotAPI(tmp_path),tmp_path,MODEL,"android-emulator",poll_seconds=0)
    assert result["technical_status"]=="PASS" and result["scope"]=="NONSECRET_SOCKET_PROBE_ONLY"
    assert result["full_android_e2e"]=="NOT_ASSESSED" and result["token_transmitted"] is False
    result=await helper.observe(SnapshotAPI(tmp_path,stale=True),tmp_path,MODEL,"android-emulator",poll_seconds=0)
    assert result["technical_status"]=="FAIL" and result["error_code"]=="integration_result_not_current_success"


@pytest.mark.parametrize("options",[{"foreign":True},{"replace":True}])
async def test_foreign_integration_run_is_never_stopped(tmp_path,options):
    api=SnapshotAPI(tmp_path,**options)
    result=await helper.observe(api,tmp_path,MODEL,"android-emulator",poll_seconds=0)
    assert result["technical_status"]=="FAIL"
    assert not any(path.startswith(api.stop) for _,path in api.calls)


@pytest.mark.parametrize("cleanup_confirmed",[True,False])
async def test_control_http_stop_waits_for_actual_delayed_cleanup(cleanup_confirmed):
    stopped=asyncio.Event()
    released=asyncio.Event()
    async def completion():
        await released.wait()
        return cleanup_confirmed
    app=create_control_app(OWN,"isolated-control-fixture",stopped,completion)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=("127.0.0.1",10000)),base_url="http://test") as client:
        request=asyncio.create_task(client.post("/internal/shutdown",headers={"Authorization":"Bearer isolated-control-fixture"}))
        await asyncio.wait_for(stopped.wait(),1)
        await asyncio.sleep(0.02)
        assert not request.done()
        released.set()
        response=await asyncio.wait_for(request,1)
        assert response.status_code==(200 if cleanup_confirmed else 503)
        if cleanup_confirmed:
            assert response.json()=={"stopped":True}


def test_nonsecret_probe_preserves_preexisting_emulator_packages(tmp_path,monkeypatch):
    monkeypatch.setattr(bridge,"ROOT",tmp_path)
    artifacts=bridge.apks()
    def identity(path):
        return {"package":bridge.E2E_PACKAGE if path==artifacts["main"] else bridge.TEST_PACKAGE,
                "instrumentation":[] if path==artifacts["main"] else [{"name":android_build.PROBE_RUNNER,"targetPackage":bridge.E2E_PACKAGE}],
                "certificate_sha256":"a"*64,"sha256":"b"*64}
    monkeypatch.setattr(bridge,"inspect",identity)
    calls=[]
    def adb(arguments,**_):
        calls.append(arguments)
        if arguments==["get-state"]:
            return "device"
        if arguments[:4]==["shell","pm","list","packages"]:
            return "package:"+bridge.E2E_PACKAGE
        raise AssertionError("Existing namespace must never be installed over or removed")
    monkeypatch.setattr(bridge,"adb",adb)
    assert bridge.main()==1
    assert len(calls)==2
    assert not any(arguments[0] in {"install","uninstall"} for arguments in calls)


def test_failed_fresh_test_install_cleans_only_owned_main_package(tmp_path,monkeypatch):
    monkeypatch.setattr(bridge,"ROOT",tmp_path)
    artifacts=bridge.apks()
    monkeypatch.setattr(bridge,"inspect",lambda path:{"package":bridge.E2E_PACKAGE if path==artifacts["main"] else bridge.TEST_PACKAGE,
        "instrumentation":[] if path==artifacts["main"] else [{"name":android_build.PROBE_RUNNER,"targetPackage":bridge.E2E_PACKAGE}],
        "certificate_sha256":"a"*64,"sha256":"b"*64})
    calls=[]
    def adb(arguments,**_):
        calls.append(arguments)
        if arguments==["get-state"]:
            return "device"
        if arguments[:4]==["shell","pm","list","packages"]:
            return ""
        if arguments==["install",str(artifacts["main"])]:
            return "Success"
        if arguments==["install",str(artifacts["test"])]:
            raise ValueError("fresh_test_install_failed")
        if arguments==["uninstall",bridge.E2E_PACKAGE]:
            return "Success"
        raise AssertionError("Foreign package must never be removed")
    monkeypatch.setattr(bridge,"adb",adb)
    assert bridge.main()==1
    assert [args for args in calls if args[0]=="uninstall"]==[["uninstall",bridge.E2E_PACKAGE]]
    report=json.loads((tmp_path/"docs/artifacts/android-socket-probe.json").read_text())
    assert report["failure_stage"]=="fresh_test_install"
    assert report["failure_category"]=="validation"
    assert "fresh_test_install_failed" not in json.dumps(report)


@pytest.mark.parametrize("cleanup",["NOT_ASSESSED","UNCONFIRMED","OWNED_SUBTREE_CLOSED"])
def test_owned_fixture_deletes_only_confirmed_cleanup(tmp_path,monkeypatch,cleanup):
    monkeypatch.setattr(integration_child,"ROOT",tmp_path)
    state={"cleanup":cleanup}
    with integration_child.owned_fixture(OWN,state) as root:
        (root/"data").mkdir(); (root/"pairing").mkdir()
        (root/"fixture.json").write_text(json.dumps({"schema_version":"privatechat-fixture-v1","run_id":OWN}))
    if cleanup=="OWNED_SUBTREE_CLOSED":
        assert not root.exists() and state["fixture_removed"] is True
        assert not (tmp_path/".runtime/integration-leftovers").exists()
    else:
        try:
            assert root.exists() and state["fixture_preserved"] is True
            private=json.loads((tmp_path/".runtime/integration-leftovers"/(OWN+".json")).read_text())
            assert private["fixture_path"]==str(root) and private["cleanup"]=="UNCONFIRMED"
        finally:
            import shutil
            shutil.rmtree(root)


def test_actual_compiled_manifest_target_and_no_main_instrumentation():
    payload=b'''N: android=http://schemas.android.com/apk/res/android
  E: manifest (line=1)
    E: instrumentation (line=2)
      A: android:name(0x01010003)="com.example.privatechat.ProbeInstrumentation" (Raw: "com.example.privatechat.ProbeInstrumentation")
      A: android:targetPackage(0x01010021)="com.example.privatechat.e2e" (Raw: "com.example.privatechat.e2e")
    E: application (line=3)
      A: android:name(0x01010003)="com.example.other.Application"
'''
    manifest=android_build.compiled_instrumentation(payload)
    assert manifest==[{"name":android_build.PROBE_RUNNER,"targetPackage":bridge.E2E_PACKAGE}]
    main={"package":bridge.E2E_PACKAGE,"certificate_sha256":"a"*64,"instrumentation":[]}
    test={"package":bridge.TEST_PACKAGE,"certificate_sha256":"a"*64,"instrumentation":manifest}
    android_build.validate_probe_identity(main,test)
    for invalid in ([{"name":android_build.PROBE_RUNNER,"targetPackage":android_build.BASE_PACKAGE}],
                    [{"name":"com.example.StaleRunner","targetPackage":bridge.E2E_PACKAGE}],[],manifest+manifest):
        with pytest.raises(ValueError,match="compiled_probe_identity_required"):
            android_build.validate_probe_identity(main,{**test,"instrumentation":invalid})
    with pytest.raises(ValueError,match="compiled_probe_identity_required"):
        android_build.validate_probe_identity({**main,"instrumentation":manifest},test)
    normal_main={**main,"package":android_build.BASE_PACKAGE}
    normal_test={**test,"package":android_build.BASE_PACKAGE+".test",
        "instrumentation":[{"name":android_build.PROBE_RUNNER,"targetPackage":android_build.BASE_PACKAGE}]}
    android_build.validate_probe_identity(normal_main,normal_test,android_build.TargetScope.NORMAL)
    with pytest.raises(ValueError,match="compiled_probe_identity_required"):
        android_build.validate_probe_identity(normal_main,normal_test)
    with pytest.raises(ValueError,match="fixed_typed_target_required"):
        android_build.apks("normal")
