"""Explicit initial normal installation/pairing; never starts a service/model."""
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit
import httpx

from gateway.app.config import Settings
from gateway.app.database import Store
from gateway.app.manage import consume_pairing,confirm_pairing,pairing_transaction
from harness.android_build import TargetScope
from harness.pairing_bridge import run
from harness.runner import ROOT,save_json


@dataclass(frozen=True)
class PairingSelection:
    server_url: str
    device_id: str
    consume: object
    confirm: object


def validate_request(value):
    fields={"schema_version","target","server_url","expected_device_id"}
    if (not isinstance(value,dict) or set(value)!=fields or value["schema_version"]!="android-normal-setup-v1"
            or value["target"]!="normal" or not isinstance(value["expected_device_id"],str)
            or not re.fullmatch(r"[a-f0-9]{32}",value["expected_device_id"])):
        raise ValueError("invalid_normal_setup_request")
    server=value["server_url"]
    if not isinstance(server,str) or not 1<=len(server)<=512 or server!=server.strip():
        raise ValueError("invalid_normal_setup_address")
    uri=urlsplit(server)
    if uri.scheme not in {"http","https"} or not uri.hostname or uri.username is not None or uri.password is not None or uri.query or uri.fragment or uri.path not in {"","/"}:
        raise ValueError("invalid_normal_setup_address")
    if uri.port is not None and not 1<=uri.port<=65535:
        raise ValueError("invalid_normal_setup_port")
    if uri.scheme=="http":
        address=ipaddress.ip_address(uri.hostname)
        if address.version==4:
            allowed=any(address in ipaddress.ip_network(network) for network in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","127.0.0.0/8"))
        else:
            allowed=address==ipaddress.ip_address("::1") or address in ipaddress.ip_network("fc00::/7")
        if not allowed:
            raise ValueError("normal_setup_private_http_required")
    return value


def selected_payload(store,directory,expected):
    with pairing_transaction(directory):
        metadata=directory/"pending.json"
        if not metadata.is_file() or metadata.stat().st_size>4096:
            raise ValueError("selected_pending_pairing_required")
        pending=json.loads(metadata.read_text())
        if pending.get("device_id")!=expected:
            raise ValueError("selected_pairing_metadata_mismatch")
        # Reuse the administrative implementation while holding its existing
        # serialization boundary over BOTH expected-ID validation and decrypt.
        payload=consume_pairing.__wrapped__(store,directory)
        if payload["device_id"]!=expected:
            raise ValueError("selected_pairing_payload_mismatch")
        return payload


def manual_service_ready(server):
    url=httpx.URL(server)
    if url.host=="10.0.2.2":
        url=url.copy_with(host="127.0.0.1")
    with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
        with client.stream("GET",str(url.copy_with(path="/healthz"))) as response:
            data=bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data)>4096:
                    raise ValueError("manual_service_health_limit")
            if response.status_code!=200 or json.loads(data)!={"status":"ok"}:
                raise ValueError("manually_running_service_required")


def main():
    original_run=os.environ.get("APP_RUN_ID")
    store=None
    try:
        if os.environ.get("D30_FIXTURE_ROOT"):
            raise ValueError("normal_scope_forbidden_in_fixture")
        path=ROOT/".runtime/android-normal-setup-request.json"
        if not path.is_file() or path.stat().st_size>4096:
            raise ValueError("explicit_normal_setup_request_required")
        request=validate_request(json.loads(path.read_text()))
        manual_service_ready(request["server_url"])
        settings=Settings.from_environment("local")
        if settings.data_dir!=(ROOT/".runtime/service").resolve() or settings.pairing_dir!=(ROOT/".runtime/pairing").resolve():
            raise ValueError("normal_administrative_location_required")
        store=Store(settings.data_dir/"chat.sqlite3",settings)
        os.environ["APP_RUN_ID"]=secrets.token_hex(16)
        selection=PairingSelection(request["server_url"],request["expected_device_id"],
            lambda:selected_payload(store,settings.pairing_dir,request["expected_device_id"]),
            lambda device:confirm_pairing(store,settings.pairing_dir,device))
        return run(TargetScope.NORMAL,selection)
    except Exception:
        artifact=ROOT/"docs/artifacts/android-normal-setup-verification.json"
        attempted=False
        try:
            previous=json.loads(artifact.read_text())
            attempted=(previous.get("run_id")==os.environ.get("APP_RUN_ID") and previous.get("token_transmitted") is True)
        except (OSError,ValueError):
            pass
        save_json(artifact,{
            "schema_version":"android-normal-setup-v1","technical_status":"FAIL","acceptance_status":"PARTIAL",
            "scope":"NORMAL_INITIAL_PAIRING","error_code":"configuration_or_execution_error","model_calls":"NOT_RUN",
            "token_transmitted":attempted,"pairing_status":"NOT_CONFIRMED"})
        print("ANDROID_NORMAL_SETUP_STATUS: CONFIGURATION_OR_EXECUTION_ERROR")
        return 2
    finally:
        if store:
            store.close()
        if original_run is None:
            os.environ.pop("APP_RUN_ID",None)
        else:
            os.environ["APP_RUN_ID"]=original_run
