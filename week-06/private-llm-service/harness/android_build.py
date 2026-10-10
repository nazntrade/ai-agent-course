"""Actual pinned APK build and safe signing/package inspection."""
import hashlib
from enum import Enum
from pathlib import Path
import re
import subprocess

from harness.android_setup import gradle,toolchain
from harness.runner import ROOT,clean_environment,save_json

BASE_PACKAGE="com.example.privatechat"
E2E_PACKAGE=BASE_PACKAGE+".e2e"
TEST_PACKAGE=E2E_PACKAGE+".test"
PROBE_RUNNER="com.example.privatechat.ProbeInstrumentation"


class TargetScope(str,Enum):
    E2E="e2e"
    NORMAL="normal"


def target_identity(scope):
    if not isinstance(scope,TargetScope):
        raise ValueError("fixed_typed_target_required")
    main=E2E_PACKAGE if scope is TargetScope.E2E else BASE_PACKAGE
    return main,main+".test"


def compiled_instrumentation(payload):
    if len(payload)>262144:
        raise ValueError("compiled_manifest_limit")
    entries=[]
    active=None
    depth=0
    for line in payload.decode("utf-8",errors="strict").splitlines():
        indentation=len(line)-len(line.lstrip())
        if active is not None and indentation<=depth:
            active=None
        if line.strip().startswith("E: instrumentation ") or line.strip()=="E: instrumentation":
            active={}
            entries.append(active)
            depth=indentation
        elif active is not None:
            match=re.search(r'A: android:(name|targetPackage)\b[^=]*="([a-zA-Z0-9_.]+)"',line)
            if match:
                if match[1] in active:
                    raise ValueError("duplicate_instrumentation_attribute")
                active[match[1]]=match[2]
    return entries


def validate_probe_identity(main,test,scope=TargetScope.E2E):
    expected_main,expected_test=target_identity(scope)
    if (main["package"]!=expected_main or test["package"]!=expected_test
            or main["certificate_sha256"]!=test["certificate_sha256"]
            or main.get("instrumentation")!=[]
            or test.get("instrumentation")!=[{"name":PROBE_RUNNER,"targetPackage":expected_main}]):
        raise ValueError("compiled_probe_identity_required")


def apks(scope=TargetScope.E2E):
    target_identity(scope)
    build="e2e" if scope is TargetScope.E2E else "debug"
    return {"main":ROOT/f"android/app/build/outputs/apk/lan/{build}/app-lan-{build}.apk",
            "test":ROOT/f"android/app/build/outputs/apk/androidTest/lan/{build}/app-lan-{build}-androidTest.apk",
            "https":ROOT/"android/app/build/outputs/apk/https/debug/app-https-debug.apk"}


def inspect(apk):
    config=toolchain()
    tools=Path(config["sdk_path"])/"build-tools/36.0.0"
    env=clean_environment()
    env["JAVA_HOME"]=config["jdk_path"]
    env.pop("APP_SHUTDOWN_TOKEN",None)
    result=subprocess.run([str(tools/"aapt.exe"),"dump","badging",str(apk)],capture_output=True,env=env,timeout=15)
    if result.returncode:
        raise ValueError("apk_inspection_failed")
    match=re.search(rb"package: name='([a-zA-Z0-9_.]+)'",result.stdout)
    signature=subprocess.run(["cmd.exe","/d","/c",str(tools/"apksigner.bat"),"verify","--print-certs",str(apk)],
                             capture_output=True,env=env,timeout=15)
    certificate=re.search(rb"Signer #1 certificate SHA-256 digest: ([a-fA-F0-9]{64})",signature.stdout)
    if not match or signature.returncode or not certificate:
        raise ValueError("apk_identity_or_signature_missing")
    manifest=subprocess.run([str(tools/"aapt.exe"),"dump","xmltree",str(apk),"AndroidManifest.xml"],
        capture_output=True,env=env,timeout=15)
    if manifest.returncode:
        raise ValueError("compiled_manifest_inspection_failed")
    return {"package":match[1].decode(),"certificate_sha256":certificate[1].decode().lower(),
            "instrumentation":compiled_instrumentation(manifest.stdout),
            "sha256":hashlib.sha256(apk.read_bytes()).hexdigest(),"bytes":apk.stat().st_size}


def main():
    try:
        from harness.generate_android_contract import verify
        verify()
        results={}
        for scope,build in ((TargetScope.E2E,"E2e"),(TargetScope.NORMAL,"Debug")):
            gradle(["--no-daemon","--offline","--dependency-verification","strict",
                "-Pprivatechat.testTarget="+scope.value,f":app:assembleLan{build}",f":app:assembleLan{build}AndroidTest",":app:assembleHttpsDebug"])
            inspected={key:inspect(path) for key,path in apks(scope).items()}
            validate_probe_identity(inspected["main"],inspected["test"],scope)
            if inspected["https"]["instrumentation"]:
                raise ValueError("main_apk_must_not_include_instrumentation")
            results[scope.value]=inspected
        save_json(ROOT/"docs/artifacts/android-build-verification.json",{
            "schema_version":"android-build-verification-v1","technical_status":"PASS",
            "scope":"fixed E2E and normal APK compiled identities","artifacts":results,
            "model_calls":"NOT_RUN","acceptance_status":"PARTIAL","full_ui_e2e":"NOT_ASSESSED"})
        print("ANDROID_BUILD_STATUS: PASS (fixed E2E and normal compiled APK identities).")
        return 0
    except Exception:
        print("ANDROID_BUILD_STATUS: FAIL (private build details suppressed).")
        return 1
