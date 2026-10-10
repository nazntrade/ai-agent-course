"""Fixed owned Android E2E fixture: ordinary Settings Save and real UI actions."""
from harness.integration_child import fixture_configuration
from harness.pairing_bridge import run
from harness.android_build import TargetScope
from harness.android_gateway import gateway_fixture
from harness.runner import ROOT,save_json
import json
import os

def main():
    fixture=fixture_configuration()
    if (fixture/"CANCEL").exists():
        return 130
    code=1
    runtime={}
    try:
        with gateway_fixture() as (selection,control,runtime):
            code=run(TargetScope.E2E,selection,control,lambda:(fixture/"CANCEL").exists())
    except Exception:
        runtime["error_code"]="owned_android_gateway_fixture_failed"
    path=ROOT/"docs/artifacts/android-socket-probe.json"
    value=json.loads(path.read_text()) if path.exists() else {}
    if value.get("run_id")!=os.environ.get("APP_RUN_ID"):
        value={"run_id":os.environ.get("APP_RUN_ID"),"technical_status":"FAIL","acceptance_status":"PARTIAL"}
    value["gateway_fixture"]=runtime
    if runtime.get("gateway_cleanup")!="CONFIRMED" or runtime.get("relay_cleanup")!="CONFIRMED":
        value["technical_status"]="FAIL"; code=1
    save_json(path,value)
    return code
