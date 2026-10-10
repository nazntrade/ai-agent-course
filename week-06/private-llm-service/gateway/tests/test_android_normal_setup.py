"""Normal setup explicit selection and no token decryption before proof."""
import json
from types import SimpleNamespace
import pytest
from harness import android_normal_setup as normal,pairing_bridge as bridge
from harness.android_build import TargetScope,PROBE_RUNNER

pytestmark=pytest.mark.integration
DEVICE="a"*32


@pytest.mark.parametrize("delta",[{"extra":"x"},{"target":"other"},{"server_url":"http://example.com"},
    {"server_url":"http://10.0.2.2/path"},{"server_url":"http://secret@10.0.2.2"},
    {"server_url":"https://example.com?x=1"},{"expected_device_id":123}])
def test_normal_setup_rejects_unreviewed_selection(delta):
    request={"schema_version":"android-normal-setup-v1","target":"normal","server_url":"http://10.0.2.2:8791","expected_device_id":DEVICE}
    request.update(delta)
    with pytest.raises(ValueError):
        normal.validate_request(request)


def test_normal_setup_forbidden_inside_fixture_before_network(tmp_path,monkeypatch):
    monkeypatch.setattr(normal,"ROOT",tmp_path)
    monkeypatch.setenv("D30_FIXTURE_ROOT","isolated-fixture")
    monkeypatch.setattr(normal,"manual_service_ready",lambda _:pytest.fail("Fixture must never access normal service"))
    monkeypatch.setattr(normal,"run",lambda *_:pytest.fail("Fixture must never install normal package"))
    assert normal.main()==2
    report=json.loads((tmp_path/"docs/artifacts/android-normal-setup-verification.json").read_text())
    assert normal.ROOT==tmp_path and report["technical_status"]=="FAIL"
    assert report["token_transmitted"] is False


def test_wrong_selected_metadata_never_decrypts_foreign_payload(tmp_path,monkeypatch):
    (tmp_path/"pending.json").write_text(json.dumps({"device_id":"b"*32}))
    calls=[]
    fake=SimpleNamespace(__wrapped__=lambda *_:calls.append(True) or {"device_id":DEVICE,"token":"x"*43})
    monkeypatch.setattr(normal,"consume_pairing",fake)
    with pytest.raises(ValueError,match="selected_pairing_metadata_mismatch"):
        normal.selected_payload(None,tmp_path,DEVICE)
    assert calls==[] and (tmp_path/"pending.json").exists()


def test_failed_actual_peer_validation_never_invokes_token_consumer(tmp_path,monkeypatch):
    monkeypatch.setattr(bridge,"ROOT",tmp_path)
    paths=bridge.apks()
    def identity(path):
        return {"package":bridge.E2E_PACKAGE if path==paths["main"] else bridge.TEST_PACKAGE,
            "certificate_sha256":"c"*64,"sha256":"d"*64,
            "instrumentation":[] if path==paths["main"] else [{"name":PROBE_RUNNER,"targetPackage":bridge.E2E_PACKAGE}]}
    monkeypatch.setattr(bridge,"inspect",identity)
    process=SimpleNamespace(returncode=0,poll=lambda:0,communicate=lambda **_:(b"probe_status=FAIL",b""))
    captured=[]
    def popen(arguments,**_):
        captured.extend(arguments)
        return process
    monkeypatch.setattr(bridge.subprocess,"Popen",popen)
    monkeypatch.setattr(bridge,"toolchain",lambda:{"sdk_path":str(tmp_path)})
    monkeypatch.setattr(bridge,"loopback_forward_verified",lambda _:True)
    def adb(arguments,**_):
        if arguments==["get-state"]: return "device"
        if arguments[:4]==["shell","pm","list","packages"] or arguments[0]=="logcat": return ""
        if arguments[0] in {"install","uninstall"}: return "Success"
        if arguments[:2]==["forward","tcp:0"]: return "3333"
        if arguments==["forward","--list"]:
            index=captured.index("socket")
            return bridge.SERIAL+" tcp:3333 localabstract:"+captured[index+1]
        if arguments[:2]==["forward","--remove"]: return ""
        raise AssertionError("Unexpected operation")
    monkeypatch.setattr(bridge,"adb",adb)
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def settimeout(self,_): pass
        def sendall(self,_): pytest.fail("No payload before peer validation")
    monkeypatch.setattr(bridge,"connect_public_handshake",lambda *_:(Connection(),{
        "kind":"probe_ready","peer_uid":12345,"ordinary_uid":10240,"ordinary_uid_rejected":True},0))
    selection=SimpleNamespace(device_id=DEVICE,server_url="http://10.0.2.2:8791",
        consume=lambda:pytest.fail("No decryption before positive UID proof"),confirm=lambda _:pytest.fail("No unproven confirmation"))
    assert bridge.run(TargetScope.E2E,selection)==1
    report=json.loads((tmp_path/"docs/artifacts/android-socket-probe.json").read_text())
    assert report["token_transmitted"] is False and report["boundary_error"]=="peer_uid_boundary_unconfirmed"


def test_unreadable_normal_configuration_preserves_pending_without_consumption(tmp_path,monkeypatch):
    monkeypatch.setattr(bridge,"ROOT",tmp_path)
    paths=bridge.apks(TargetScope.NORMAL)
    def identity(path):
        return {"package":bridge.BASE_PACKAGE if path==paths["main"] else bridge.BASE_PACKAGE+".test",
            "certificate_sha256":"c"*64,"sha256":"d"*64,
            "instrumentation":[] if path==paths["main"] else [{"name":PROBE_RUNNER,"targetPackage":bridge.BASE_PACKAGE}]}
    monkeypatch.setattr(bridge,"inspect",identity)
    process=SimpleNamespace(returncode=0,poll=lambda:0,communicate=lambda **_:(b"probe_status=FAIL",b""))
    captured=[]; operations=[]
    monkeypatch.setattr(bridge.subprocess,"Popen",lambda arguments,**_:(captured.extend(arguments) or process))
    monkeypatch.setattr(bridge,"toolchain",lambda:{"sdk_path":str(tmp_path)})
    monkeypatch.setattr(bridge,"loopback_forward_verified",lambda _:True)
    def adb(arguments,**_):
        operations.append(arguments)
        if arguments==["get-state"]: return "device"
        if arguments[:4]==["shell","pm","list","packages"] or arguments[0]=="logcat": return ""
        if arguments[0] in {"install","uninstall"}: return "Success"
        if arguments[:2]==["forward","tcp:0"]: return "3333"
        if arguments==["forward","--list"]:
            return bridge.SERIAL+" tcp:3333 localabstract:"+captured[captured.index("socket")+1]
        if arguments[:2]==["forward","--remove"]: return ""
        raise AssertionError("Unexpected operation")
    monkeypatch.setattr(bridge,"adb",adb)
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def settimeout(self,_): pass
        def sendall(self,_): pass
    monkeypatch.setattr(bridge,"connect_public_handshake",lambda *_:(Connection(),{
        "kind":"probe_ready","peer_uid":2000,"ordinary_uid":10240,"ordinary_uid_rejected":True},0))
    frames=[{"kind":"probe_pass","peer_uid":2000,"ordinary_uid_rejected":True},
            {"kind":"configuration_state","state":"UNREADABLE"}]
    monkeypatch.setattr(bridge,"receive_frame",lambda _:frames.pop(0))
    pending=tmp_path/"pending.dpapi"; pending.write_bytes(b"public encrypted fixture")
    calls=[]
    selection=SimpleNamespace(device_id=DEVICE,server_url="http://10.0.2.2:8791",
        consume=lambda:calls.append("consume"),confirm=lambda _:calls.append("confirm"))
    assert bridge.run(TargetScope.NORMAL,selection)==1 and calls==[]
    report=json.loads((tmp_path/"docs/artifacts/android-normal-setup-verification.json").read_text())
    assert report["unreadable_configuration_preserved"] is True and report["token_transmitted"] is False
    assert pending.read_bytes()==b"public encrypted fixture" and ["uninstall",bridge.BASE_PACKAGE] not in operations
