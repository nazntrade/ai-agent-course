"""Pinned Android dependency preparation behind setup.bat; no model calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from harness.center_preflight import checked_json
from harness.runner import ROOT, clean_environment, save_json

DISTRIBUTION_SHA="b266d5ff6b90eada6dc3b20cb090e3731302e553a27c5d3e4df1f0d76beaff06"
WRAPPER_SHA="b3a875ddc1f044746e1b1a55f645584505f4a10438c1afea9f15e92a7c42ec13"


def toolchain():
    value=checked_json(ROOT/".runtime/android-toolchain.json",4096)
    if set(value)!={"schema_version","sdk_path","jdk_path","gradle_path"} or value["schema_version"]!="android-toolchain-v1":
        raise ValueError("invalid_android_toolchain")
    for field in ("sdk_path","jdk_path","gradle_path"):
        if not isinstance(value[field],str) or not Path(value[field]).is_absolute() or not Path(value[field]).exists():
            raise ValueError("missing_android_toolchain")
    sdk=Path(value["sdk_path"])
    platform=(sdk/"platforms/android-37.0/source.properties").read_text()
    if "AndroidVersion.ApiLevel=37.0" not in platform or "AndroidVersion.PreviewSdkInt=0" not in platform:
        raise ValueError("released_sdk37_minor0_required")
    if "Pkg.Revision=36.0.0" not in (sdk/"build-tools/36.0.0/source.properties").read_text():
        raise ValueError("pinned_build_tools_required")
    if 'JAVA_VERSION="21.' not in (Path(value["jdk_path"])/"release").read_text():
        raise ValueError("installed_jdk21_required")
    if not str(Path(value["gradle_path"]).parent.parent.name)=="gradle-9.3.1":
        raise ValueError("pinned_gradle_required")
    return value


def gradle(arguments,*,bootstrap=False,timeout=300):
    config=toolchain()
    env=clean_environment()
    env.pop("APP_SHUTDOWN_TOKEN",None)
    env.update(JAVA_HOME=config["jdk_path"],ANDROID_HOME=config["sdk_path"],ANDROID_SDK_ROOT=config["sdk_path"])
    if bootstrap:
        command=["cmd.exe","/d","/c",config["gradle_path"],*arguments]
    else:
        jar=ROOT/"android/gradle/wrapper/gradle-wrapper.jar"
        if hashlib.sha256(jar.read_bytes()).hexdigest()!=WRAPPER_SHA:
            raise ValueError("wrapper_digest_mismatch")
        command=[str(Path(config["jdk_path"])/"bin/java.exe"),"-classpath",str(jar),"org.gradle.wrapper.GradleWrapperMain",*arguments]
    try:
        result=subprocess.run(command,cwd=ROOT/"android",env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError("android_build_deadline") from None
    # Build diagnostics have machine paths; keep them private, never stdout.
    log=ROOT/".runtime/android-build.log"
    log.parent.mkdir(parents=True,exist_ok=True)
    log.write_bytes(result.stdout[-1048576:])
    if result.returncode:
        raise ValueError("pinned_android_build_failed")


def setup():
    from harness.generate_android_contract import main as generate_contract
    generate_contract()
    toolchain()
    gradle(["--no-daemon","wrapper","--gradle-version","9.3.1","--distribution-type","bin",
            "--gradle-distribution-sha256-sum",DISTRIBUTION_SHA],bootstrap=True)
    jar=ROOT/"android/gradle/wrapper/gradle-wrapper.jar"
    if hashlib.sha256(jar.read_bytes()).hexdigest()!=WRAPPER_SHA:
        raise ValueError("official_wrapper_digest_mismatch")
    gradle(["--no-daemon",":app:assembleLanE2e",":app:assembleLanE2eAndroidTest",":app:assembleHttpsDebug",
            "--write-locks","--write-verification-metadata","sha256"],timeout=600)
    gradle(["--no-daemon","-Pprivatechat.testTarget=normal",":app:assembleLanDebug",":app:assembleLanDebugAndroidTest",":app:assembleHttpsDebug",
            "--write-locks","--write-verification-metadata","sha256"],timeout=600)
    save_json(ROOT/"docs/artifacts/android-dependency-provenance.json",{
        "schema_version":"android-dependency-provenance-v1","technical_status":"PASS","scope":"fixed E2E and normal Android targets",
        "gradle":"9.3.1","distribution_sha256":DISTRIBUTION_SHA,"wrapper_sha256":WRAPPER_SHA,
        "agp":"9.1.1","builtin_kotlin":"2.2.10","jdk_major":21,"bytecode":17,
        "compile_sdk":"37.0","target_sdk":36,"min_sdk":26,"build_tools":"36.0.0",
        "model_calls":"NOT_RUN","full_acceptance":"PARTIAL"})
    print("ANDROID_SETUP_STATUS: PASS (pinned Android dependencies).")
    return 0
