"""Nonsecret proof of the test-only socket boundary before any device token."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import tempfile
import time

from harness.android_build import E2E_PACKAGE,TEST_PACKAGE,BASE_PACKAGE,TargetScope,target_identity,apks,inspect,validate_probe_identity
from harness.android_setup import toolchain
from harness.runner import ROOT,clean_environment,save_json

SERIAL="emulator-5554"


def adb(arguments,*,timeout=20):
    config=toolchain()
    executable=Path(config["sdk_path"])/"platform-tools/adb.exe"
    env=clean_environment()
    env.pop("APP_SHUTDOWN_TOKEN",None)
    result=subprocess.run([str(executable),"-s",SERIAL,*arguments],capture_output=True,env=env,timeout=timeout)
    if result.returncode or len(result.stdout)>262144 or len(result.stderr)>262144:
        raise ValueError("bounded_adb_operation_failed")
    return result.stdout.decode("utf-8",errors="strict").strip()


def loopback_forward_verified(port):
    api=ctypes.WinDLL("iphlpapi",use_last_error=True).GetExtendedTcpTable
    api.argtypes=[ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD),wintypes.BOOL,wintypes.DWORD,ctypes.c_int,wintypes.DWORD]
    api.restype=wintypes.DWORD
    listeners=0
    for family in (socket.AF_INET,socket.AF_INET6):
        size=wintypes.DWORD(65536)
        buffer=ctypes.create_string_buffer(size.value)
        if api(buffer,ctypes.byref(size),False,family,3,0) or size.value>65536:
            raise ValueError("forward_inventory_failed")
        count=struct.unpack_from("<I",buffer.raw)[0]
        row_size=24 if family==socket.AF_INET else 56
        if count>1000 or 4+count*row_size>len(buffer):
            raise ValueError("forward_inventory_invalid")
        for index in range(count):
            offset=4+index*row_size
            if family==socket.AF_INET:
                state,address,local_port,_,_,_=struct.unpack_from("<IIIIII",buffer.raw,offset)
                local=socket.inet_ntop(family,struct.pack("<I",address))
            else:
                address,_,local_port,_,_,_,state,_=struct.unpack_from("<16sII16sIIII",buffer.raw,offset)
                local=socket.inet_ntop(family,address)
            if state==2 and socket.ntohs(local_port&65535)==port:
                if local not in {"127.0.0.1","::1"}:
                    raise ValueError("forward_is_not_loopback_only")
                listeners+=1
    if listeners==0:
        raise ValueError("forward_listener_missing")
    return True


class SocketNotReady(ValueError):
    pass


def receive_frame(connection):
    def exact(count,initial=False):
        result=bytearray()
        while len(result)<count:
            try:
                chunk=connection.recv(count-len(result))
            except OSError:
                if initial and not result:
                    raise SocketNotReady("socket_frame_incomplete") from None
                raise ValueError("socket_frame_interrupted") from None
            if not chunk:
                if initial and not result:
                    raise SocketNotReady("socket_frame_incomplete")
                raise ValueError("socket_frame_incomplete")
            result.extend(chunk)
        return bytes(result)
    count=struct.unpack("!I",exact(4,initial=True))[0]
    if not 1<=count<=4096:
        raise ValueError("socket_frame_limit")
    value=json.loads(exact(count))
    if not isinstance(value,dict):
        raise ValueError("socket_frame_invalid")
    return value


def receive_ui_frame(connection,cancelled,*,seconds=1200):
    """After pairing, timeout polling keeps owned shutdown interruptible.

    Partial frames are retained, never retried; no authenticated payload replay.
    """
    deadline=time.monotonic()+seconds
    data=bytearray()
    wanted=4
    connection.settimeout(.5)
    while len(data)<wanted:
        if cancelled() or time.monotonic()>deadline:
            raise ValueError("e2e_cancelled_or_deadline")
        try: part=connection.recv(wanted-len(data))
        except socket.timeout: continue
        if not part: raise ValueError("e2e_frame_incomplete")
        data.extend(part)
        if len(data)==4 and wanted==4:
            count=struct.unpack("!I",data)[0]
            if not 1<=count<=4096: raise ValueError("e2e_frame_limit")
            wanted=4+count
    value=json.loads(data[4:])
    if not isinstance(value,dict): raise ValueError("e2e_frame_invalid")
    return value


def connect_public_handshake(port,instrument,*,seconds=15):
    deadline=time.monotonic()+seconds
    retries=0
    while True:
        connection=None
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise ValueError("probe_connect_deadline")
            connection=socket.create_connection(("127.0.0.1",port),timeout=min(2,remaining))
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise ValueError("probe_connect_deadline")
            connection.settimeout(min(5,remaining))
            return connection,receive_frame(connection),retries
        except (OSError,SocketNotReady):
            if connection:
                connection.close()
            if time.monotonic()>=deadline or instrument.poll() is not None:
                raise ValueError("probe_connect_deadline") from None
            retries+=1
            time.sleep(0.1)
        except BaseException:
            if connection:
                connection.close()
            raise


def send_frame(connection,value):
    payload=json.dumps(value,separators=(",",":")).encode()
    if not 1<=len(payload)<=4096:
        raise ValueError("socket_payload_limit")
    connection.sendall(struct.pack("!I",len(payload))+payload)


def instrumentation_evidence(output,error,returncode):
    """Classify public probe results without persisting arbitrary subprocess text."""
    merged=output+error
    result={"instrumentation_exit_code":returncode,
        "instrumentation_output_bytes":len(merged),
        "android_domain_checks":("PASS" if b"domain_checks=PASS" in output else "NOT_CONFIRMED"),
        "instrumentation_outcome":("PASS" if b"probe_status=PASS" in output
            else "FAIL" if b"probe_status=FAIL" in output else "UNCONFIRMED")}
    for name,marker in (("command_failed",b"INSTRUMENTATION_FAILED"),
            ("process_crashed",b"Process crashed"),("missing_class",b"ClassNotFoundException"),
            ("permission_denied",b"Permission Denial"),("missing_runner",b"Unable to find instrumentation")):
        result["instrumentation_"+name]=marker in merged
    phase=re.search(rb"probe_stage=(socket_create|ordinary_uid_accept|ordinary_uid_confirmation|host_uid_accept|public_handshake|public_payload|settings_pairing)(?:\s|$)",output)
    if phase:
        result["instrumentation_failure_stage"]=phase.group(1).decode("ascii")
    return result


def owned_crash_evidence(current,baseline,package=E2E_PACKAGE):
    if package not in {BASE_PACKAGE,E2E_PACKAGE}:
        raise ValueError("fixed_crash_target_required")
    def owned_block(text):
        lines=text.splitlines()
        indexes=[index for index,line in enumerate(lines)
            if re.search(r"Process: "+re.escape(package)+r", PID: [0-9]+(?:\s|$)",line)]
        if not indexes:
            return ""
        start=indexes[-1]
        end=next((index for index in range(start+1,len(lines)) if "Process: " in lines[index]),len(lines))
        return "\n".join(lines[start:end])
    block=owned_block(current)
    if not block or block==owned_block(baseline):
        return {"owned_crash_diagnostic":"NO_FRESH_OWNED_JAVA_CRASH"}
    result={"owned_crash_diagnostic":"FRESH_OWNED_JAVA_CRASH"}
    exception=re.search(r"\b((?:java|android|kotlin)\.[a-zA-Z0-9_.]+(?:Exception|Error))\b",block)
    frame=re.search(r"ProbeInstrumentation\.kt:([0-9]{1,5})",block)
    if exception:
        result["owned_crash_exception_class"]=exception.group(1)
    if frame:
        result["owned_crash_probe_source_line"]=int(frame.group(1))
    return result


def installed_main_identity(package):
    if package!=BASE_PACKAGE:
        raise ValueError("normal_installed_identity_required")
    value=adb(["shell","pm","path",package])
    match=re.fullmatch(r"package:(/data/app/[A-Za-z0-9_./=+~\-]+/base\.apk)",value)
    if not match or ".." in match[1].split("/"):
        raise ValueError("installed_main_path_invalid")
    with tempfile.TemporaryDirectory(prefix="privatechat-installed-apk-") as directory:
        path=Path(directory)/"installed.apk"
        adb(["pull",match[1],str(path)])
        if not path.is_file() or path.stat().st_size>52428800:
            raise ValueError("installed_main_apk_limit")
        return inspect(path)


def run(scope=TargetScope.E2E,pairing=None,control=None,cancelled=lambda:False):
    main_package,test_package=target_identity(scope)
    report={"schema_version":"android-socket-probe-v1","scope":"NONSECRET_SOCKET_PROBE_ONLY",
            "technical_status":"FAIL","acceptance_status":"PARTIAL","model_calls":"NOT_RUN",
            "token_transmitted":False,"full_android_e2e":"NOT_ASSESSED","run_id":os.environ.get("APP_RUN_ID","")}
    if pairing is not None:
        report["scope"]="NORMAL_INITIAL_PAIRING" if scope is TargetScope.NORMAL else "ISOLATED_ANDROID_PAIRING"
    own_forward=None
    instrument=None
    instrumentation_captured=False
    own_test=False
    own_main=False
    crash_baseline=None
    stage="apk_identity"
    name="privatechat_"+secrets.token_hex(12)
    try:
        paths=apks(scope)
        main_id,test_id=inspect(paths["main"]),inspect(paths["test"])
        validate_probe_identity(main_id,test_id,scope)
        stage="emulator_inventory"
        if adb(["get-state"])!="device":
            raise ValueError("selected_emulator_unavailable")
        inventory=adb(["shell","pm","list","packages",main_package]).splitlines()
        if "package:"+test_package in inventory or (scope is TargetScope.E2E and "package:"+main_package in inventory):
            raise ValueError("preexisting_emulator_package_preserved")
        # Plain install atomically refuses a concurrently installed namespace.
        # Ownership is claimed only after a successful fresh install.
        stage="fresh_main_install"
        install=["install",str(paths["main"])]
        if "package:"+main_package in inventory:
            installed=installed_main_identity(main_package)
            if (installed["package"]!=main_package or installed["certificate_sha256"]!=main_id["certificate_sha256"]
                    or installed["instrumentation"]):
                raise ValueError("foreign_normal_installation_preserved")
            install=["install","-r",str(paths["main"])]
        if "Success" not in adb(install):
            raise ValueError("fresh_main_install_unconfirmed")
        own_main=True
        stage="fresh_test_install"
        if "Success" not in adb(["install",str(paths["test"])]):
            raise ValueError("fresh_test_install_unconfirmed")
        own_test=True
        try:
            crash_baseline=adb(["logcat","-d","-b","crash","-t","120"])
        except ValueError:
            pass
        stage="instrumentation_start"
        config=toolchain()
        env=clean_environment()
        env.pop("APP_SHUTDOWN_TOKEN",None)
        instrument=subprocess.Popen([str(Path(config["sdk_path"])/"platform-tools/adb.exe"),"-s",SERIAL,
            "shell","am","instrument","-w","-e","socket",name,"-e","mode",
            "probe" if pairing is None else "e2e" if scope is TargetScope.E2E else "pairing",
            test_package+"/com.example.privatechat.ProbeInstrumentation"],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        stage="forward_create"
        value=adb(["forward","tcp:0","localabstract:"+name])
        if not re.fullmatch(r"[0-9]{1,5}",value) or not 1024<=int(value)<=65535:
            raise ValueError("forward_port_invalid")
        own_forward=int(value)
        stage="forward_loopback_inventory"
        loopback_forward_verified(own_forward)
        stage="public_peer_handshake"
        connection,ready,retries=connect_public_handshake(own_forward,instrument)
        report["public_handshake_readiness_retries"]=retries
        with connection:
            connection.settimeout(5)
            if (ready.get("kind")!="probe_ready" or ready.get("peer_uid") not in {0,2000}
                    or type(ready.get("ordinary_uid")) is not int or ready["ordinary_uid"]<10000
                    or ready.get("ordinary_uid_rejected") is not True):
                raise ValueError("peer_uid_boundary_unconfirmed")
            stage="public_probe_exchange"
            send_frame(connection,{"kind":"nonsecret_probe"})
            done=receive_frame(connection)
            if done!={"kind":"probe_pass","peer_uid":ready["peer_uid"],"ordinary_uid_rejected":True}:
                raise ValueError("probe_response_invalid")
            if pairing is not None:
                stage="configuration_preservation"
                connection.settimeout(45)
                send_frame(connection,{"kind":"check_configuration"})
                configured=receive_frame(connection)
                if set(configured)!={"kind","state"} or configured["kind"]!="configuration_state" or configured["state"] not in {"EMPTY","CONFIGURED","UNREADABLE"}:
                    raise ValueError("configuration_state_invalid")
                report["configuration_status"]=configured["state"]
                if configured["state"]=="UNREADABLE":
                    report["unreadable_configuration_preserved"]=True
                    raise ValueError("unreadable_configuration_preserved")
                if configured["state"]=="CONFIGURED":
                    if scope is not TargetScope.NORMAL:
                        raise ValueError("unexpected_e2e_configuration")
                    report["pairing_status"]="EXISTING_CONFIGURATION_PRESERVED"
                else:
                    stage="selected_payload_consume"
                    # Decryption is invoked ONLY after fresh UID and loopback proof
                    # and confirmation that no saved normal configuration exists.
                    payload=pairing.consume()
                    if payload.get("device_id")!=pairing.device_id:
                        raise ValueError("selected_pairing_device_mismatch")
                    stage="ordinary_settings_save"
                    report["token_transmitted"]=True
                    save_json(ROOT/"docs/artifacts"/("android-socket-probe.json" if scope is TargetScope.E2E else "android-normal-setup-verification.json"),report)
                    send_frame(connection,{"kind":"pairing","server_url":pairing.server_url,
                        "token":payload["token"],"expected_device_id":pairing.device_id})
                    del payload
                    paired=receive_frame(connection)
                    if paired!={"kind":"paired","device_id":pairing.device_id}:
                        raise ValueError("ordinary_settings_identity_unconfirmed")
                    pairing.confirm(pairing.device_id)
                    report.update(pairing_status="PAIRED",selected_payload_removed=True,
                        ordinary_settings_save=True,production_me_verified=True)
                    send_frame(connection,{"kind":"confirmed"})
                    if scope is TargetScope.E2E:
                        if control is None: raise ValueError("fixed_e2e_controller_required")
                        stage="real_ui_workflows"
                        while True:
                            message=receive_ui_frame(connection,cancelled)
                            if set(message)=={"kind","action"} and message["kind"]=="control":
                                send_frame(connection,control(message["action"]))
                            else:
                                if set(message)!={"kind","checks"} or message["kind"]!="e2e_done" or not isinstance(message["checks"],dict):
                                    raise ValueError("e2e_result_invalid")
                                checks=message["checks"]
                                required={"system_bar_insets","keyboard_ime_insets","draft_partition_switch_rotation_relaunch","keystore_fresh_activity_relaunch",
                                    "actual_transport_loss_same_key_one_post","actual_reply_selectable",
                                    "controlled_retry_after_no_touch_no_post","held_old_terminal_after_navigation_guard",
                                    "active_rename_then_stop_revision","stop_then_new_send_revision","active_delete_revision_no_revive"}
                                if set(checks)!=required|{"fixture_answer"} or any(checks[key] is not True for key in required):
                                    raise ValueError("e2e_checks_incomplete")
                                if not isinstance(checks["fixture_answer"],str) or not 1<=len(checks["fixture_answer"])<=200:
                                    raise ValueError("e2e_fixture_answer_invalid")
                                report.update(ui_checks=checks,full_android_e2e="PASS",scope="ISOLATED_ANDROID_UI_E2E",model_calls="REAL_SELECTED_MODEL")
                                send_frame(connection,{"kind":"e2e_confirmed"})
                                break
        stage="instrumentation_completion"
        output,error=instrument.communicate(timeout=10)
        instrumentation_captured=True
        report.update(instrumentation_evidence(output,error,instrument.returncode))
        if instrument.returncode or len(output)+len(error)>262144 or b"probe_status=PASS" not in output:
            raise ValueError("instrumentation_completion_unconfirmed")
        if control is not None and scope is TargetScope.E2E:
            stage="fresh_process_persistence"
            # First helper and transport are closed before an ordinary launcher
            # restart. The second test APK sends only a nonsecret expected ID.
            expected=SERIAL+" tcp:"+str(own_forward)+" localabstract:"+name
            if expected not in adb(["forward","--list"]).splitlines(): raise ValueError("forward_identity_changed")
            adb(["forward","--remove","tcp:"+str(own_forward)]); own_forward=None
            adb(["uninstall",test_package]); own_test=False
            adb(["shell","am","force-stop",main_package])
            adb(["shell","monkey","-p",main_package,"-c","android.intent.category.LAUNCHER","1"])
            if "Success" not in adb(["install",str(paths["test"])]): raise ValueError("persistence_test_install_failed")
            own_test=True; name="privatechat_"+secrets.token_hex(12)
            instrument=subprocess.Popen([str(Path(config["sdk_path"])/"platform-tools/adb.exe"),"-s",SERIAL,
                "shell","am","instrument","-w","-e","socket",name,"-e","mode","persistence",
                test_package+"/com.example.privatechat.ProbeInstrumentation"],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
            instrumentation_captured=False
            value=adb(["forward","tcp:0","localabstract:"+name])
            if not re.fullmatch(r"[0-9]{1,5}",value) or not 1024<=int(value)<=65535: raise ValueError("forward_port_invalid")
            own_forward=int(value); loopback_forward_verified(own_forward)
            connection,restarted,retries=connect_public_handshake(own_forward,instrument)
            before=control("job_count")["posts"]
            with connection:
                connection.settimeout(45)
                if (restarted.get("kind")!="probe_ready" or restarted.get("peer_uid") not in {0,2000}
                    or type(restarted.get("ordinary_uid")) is not int or restarted["ordinary_uid"]<10000
                    or restarted.get("ordinary_uid_rejected") is not True): raise ValueError("peer_uid_boundary_unconfirmed")
                send_frame(connection,{"kind":"nonsecret_probe"})
                if receive_frame(connection)!={"kind":"probe_pass","peer_uid":restarted["peer_uid"],"ordinary_uid_rejected":True}: raise ValueError("probe_response_invalid")
                send_frame(connection,{"kind":"check_configuration"})
                if receive_frame(connection)!={"kind":"configuration_state","state":"CONFIGURED"}: raise ValueError("persisted_configuration_missing")
                send_frame(connection,{"kind":"verify_persistence","device_id":pairing.device_id})
                saved=receive_frame(connection)
                required={"keystore_after_process_restart","sqlite_draft_after_process_restart","authoritative_history_after_process_restart"}
                if set(saved)!={"kind","checks"} or saved["kind"]!="persistence_pass" or set(saved["checks"])!=required or any(saved["checks"][key] is not True for key in required):
                    raise ValueError("fresh_process_persistence_unconfirmed")
            output,error=instrument.communicate(timeout=10); instrumentation_captured=True
            if instrument.returncode or b"persistence_status=PASS" not in output or len(output)+len(error)>262144:
                raise ValueError("persistence_instrumentation_failed")
            if control("job_count")["posts"]!=before: raise ValueError("relaunch_created_generation")
            report["process_restart_checks"]=saved["checks"]
            report["relaunch_new_submission_count"]=0
        report.update(technical_status="PASS",peer_uid=ready["peer_uid"],ordinary_uid=ready["ordinary_uid"],
            ordinary_uid_rejected_before_payload=True,loopback_forward_verified=True,
            same_certificate=True,tested_package=main_package,test_package=test_package,
            main_apk_sha256=main_id["sha256"],test_apk_sha256=test_id["sha256"])
    except Exception as error:
        report["error_code"]="nonsecret_socket_boundary_failed"
        report["failure_stage"]=stage
        report["failure_category"]=("timeout" if isinstance(error,(TimeoutError,subprocess.TimeoutExpired))
            else "transport" if isinstance(error,OSError) else "validation")
        if isinstance(error,ValueError) and str(error) in {
                "socket_frame_incomplete","socket_frame_limit","socket_frame_invalid",
                "peer_uid_boundary_unconfirmed","probe_response_invalid"}:
            report["boundary_error"]=str(error)
    finally:
        if instrument and not instrumentation_captured:
            try:
                if scope is TargetScope.E2E and instrument.poll() is None:
                    adb(["shell","am","force-stop",main_package])
                output,error=instrument.communicate(timeout=5 if scope is TargetScope.E2E else 35)
                report.update(instrumentation_evidence(output,error,instrument.returncode))
            except Exception:
                try:
                    instrument.terminate(); instrument.wait(timeout=5)
                except Exception:
                    pass
                report.update(technical_status="FAIL",instrumentation_cleanup="UNCONFIRMED")
        if instrument and report.get("instrumentation_process_crashed") and crash_baseline is not None:
            try:
                report.update(owned_crash_evidence(adb(["logcat","-d","-b","crash","-t","120"]),crash_baseline,main_package))
            except ValueError:
                report["owned_crash_diagnostic"]="UNAVAILABLE"
        if own_forward:
            try:
                expected=SERIAL+" tcp:"+str(own_forward)+" localabstract:"+name
                if expected not in adb(["forward","--list"]).splitlines():
                    raise ValueError("forward_identity_changed")
                adb(["forward","--remove","tcp:"+str(own_forward)])
                report["own_forward_removed"]=True
            except Exception:
                report.update(technical_status="FAIL",forward_cleanup="UNCONFIRMED")
        if own_test:
            try:
                adb(["uninstall",test_package])
                report["own_test_apk_removed"]=True
            except Exception:
                report.update(technical_status="FAIL",test_apk_cleanup="UNCONFIRMED")
        if own_main and scope is TargetScope.E2E:
            try:
                adb(["uninstall",main_package])
                report["own_main_apk_removed"]=True
            except Exception:
                report.update(technical_status="FAIL",main_apk_cleanup="UNCONFIRMED")
        elif own_main:
            report["normal_main_preserved"]=True
        if scope is TargetScope.NORMAL and report["technical_status"]=="PASS":
            try:
                adb(["shell","monkey","-p",main_package,"-c","android.intent.category.LAUNCHER","1"])
                report["normal_launcher_relaunch"]=True
            except Exception:
                report.update(technical_status="FAIL",normal_relaunch="UNCONFIRMED")
    report["target_scope"]=scope.value
    artifact="android-socket-probe.json" if scope is TargetScope.E2E else "android-normal-setup-verification.json"
    save_json(ROOT/"docs/artifacts"/artifact,report)
    label=("ANDROID_NORMAL_SETUP_STATUS" if scope is TargetScope.NORMAL
           else "ANDROID_UI_E2E_STATUS" if pairing is not None else "ANDROID_NONSECRET_PROBE_STATUS")
    print(label+": "+report["technical_status"])
    return 0 if report["technical_status"]=="PASS" else 1


def main():
    return run()
