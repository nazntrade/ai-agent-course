"""No-argument trusted local administration; secrets never enter CLI output."""
import json
from gateway.app.config import Settings
from gateway.app.database import Store
from gateway.app.manage import provision_device,revoke_device
from harness.runner import ROOT,save_json


def main(action):
    try:
        request_path=ROOT/".runtime/admin-request.json"
        if not request_path.is_file() or request_path.stat().st_size>4096:
            raise ValueError("explicit_admin_request_required")
        request=json.loads(request_path.read_text(encoding="utf-8"))
        fields={"schema_version","action","owner_id","label"} if action=="provision" else {"schema_version","action","device_id"}
        if set(request)!=fields or request["schema_version"]!="local-admin-v1" or request["action"]!=action:
            raise ValueError("invalid_admin_request")
        settings=Settings.from_environment("local")
        store=Store(settings.data_dir/"chat.sqlite3",settings)
        try:
            directory=ROOT/".runtime/pairing"
            if action=="provision":
                result=provision_device(store,directory,request["owner_id"],request["label"])
            else:
                revoke_device(store,directory,request["device_id"])
                result={"device_id":request["device_id"],"revoked":True}
            save_json(ROOT/".runtime/admin-result.json",result)
            print("DEVICE_ADMIN_STATUS: PASS (device metadata saved; credential output suppressed).")
            return 0
        finally:
            store.close()
    except Exception:
        print("DEVICE_ADMIN_STATUS: CONFIGURATION_OR_EXECUTION_ERROR")
        return 2
