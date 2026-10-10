"""Observe a fixed selected-model child through the trusted Center launch API."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Mapping

import httpx

from gateway.app.upstream import UpstreamError, bounded_json
from harness.runner import ROOT, save_json

CATALOG = "/api/local-models"
LAUNCH = "/api/projects/ai-agent-course/launch?profile=private-llm-preflight"
STOP = "/api/projects/ai-agent-course/launch/stop?profile=private-llm-preflight&expectedRunId="
ORIGIN = "http://127.0.0.1:8787"
RUN_ID = re.compile(r"[a-f0-9]{32}")


def checked_json(path: Path, cap: int = 262144) -> dict:
    try:
        for candidate in (path.parent, path):
            info = candidate.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ValueError()
        if not path.is_file() or path.stat().st_size > cap:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise UpstreamError("invalid_or_missing_evidence") from None


def selected_configuration(root: Path, env: Mapping[str, str]) -> dict:
    if env.get("AI_TEST_LIVE_POLICY", "") not in {"", "allowed"}:
        raise UpstreamError("live_policy_blocked")
    if any(key.startswith("AI_TEST_MODEL_") for key in env):
        raise UpstreamError("explicit_profile_requires_direct_preflight")
    value = checked_json(root / ".runtime/center-test-selection.json", 4096)
    if (set(value) != {"schema_version", "live_policy", "model_id", "center_origin"}
            or value["schema_version"] != "center-test-selection-v1"
            or value["live_policy"] != "allowed" or value["center_origin"] != ORIGIN
            or not isinstance(value["model_id"], str)
            or not re.fullmatch(r"[a-f0-9]{16}", value["model_id"])):
        raise UpstreamError("invalid_center_selection")
    return value


class CenterAPI:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def request(self, method: str, path: str, body: dict | None = None, *, timeout: float = 15) -> dict:
        allowed = ((method == "GET" and path in {CATALOG, LAUNCH})
                   or (method == "POST" and path == LAUNCH)
                   or (method == "POST" and path.startswith(STOP) and RUN_ID.fullmatch(path[len(STOP):])))
        if not allowed:
            raise UpstreamError("unsupported_center_operation")
        try:
            async with asyncio.timeout(timeout):
                async with self.client.stream(method, ORIGIN + path, json=body if method == "POST" else None,
                                              headers={"x-ai-server-request": "1"},
                                              timeout=httpx.Timeout(timeout, connect=5)) as response:
                    return await bounded_json(response, cap=262144)
        except (httpx.HTTPError, TimeoutError):
            raise UpstreamError("center_transport_error") from None


def model_state(catalog: dict, model_id: str) -> str:
    models = catalog.get("models")
    if not isinstance(models, list):
        raise UpstreamError("invalid_center_catalog")
    matches = [model for model in models if isinstance(model, dict) and model.get("id") == model_id]
    if len(matches) != 1 or matches[0].get("status") not in {"stopped", "starting", "ready", "error"}:
        raise UpstreamError("selected_model_unavailable")
    return matches[0]["status"]


def fresh_results(root: Path, run_id: str, initial_state: str) -> dict:
    reports = {criterion: checked_json(root / "docs/artifacts" / filename) for criterion, filename in
               (("C01", "model-verification.json"), ("C07", "context-verification.json"),
                ("C12", "lifecycle-verification.json"))}
    progress = checked_json(root / "docs/artifacts/preflight-progress.json")
    if (progress.get("run_id") != run_id or progress.get("state") != "finished"
            or progress.get("exit_code") != 0
            or any(value.get("run_id") != run_id or value.get("criterion") != criterion
                   or value.get("technical_status") != "PASS" for criterion, value in reports.items())):
        raise UpstreamError("preflight_evidence_not_current_success")
    result = reports["C01"].get("measurements", {})
    if (not isinstance(result, dict) or type(result.get("input_tokens")) is not int
            or result["input_tokens"] <= 0 or type(result.get("stream_prompt_tokens")) is not int
            or result.get("input_tokens") != result.get("stream_prompt_tokens")
            or result.get("public_fixture_answer") != "READY" or result.get("done_received") is not True
            or result.get("finish_reason") not in {"stop", "length"}
            or type(result.get("actual_slot_capacity")) is not int or result["actual_slot_capacity"] < 8192
            or result.get("count_method") != "native-chat-input-tokens"
            or result.get("context_source") != "runtime-slot-props"):
        raise UpstreamError("preflight_result_incomplete")
    lifecycle = reports["C12"]
    release = lifecycle.get("release", {})
    allowed_dispositions = {"unloaded"} if initial_state == "stopped" else {"retained", "shared"}
    if (lifecycle.get("renew", {}).get("renewed") is not True
            or release.get("released") is not True or release.get("disposition") not in allowed_dispositions):
        raise UpstreamError("preflight_lifecycle_incomplete")
    return {"public_fixture_answer": result["public_fixture_answer"], "input_tokens": result["input_tokens"],
            "stream_prompt_tokens": result["stream_prompt_tokens"],
            "actual_slot_capacity": result["actual_slot_capacity"], "finish_reason": result["finish_reason"],
            "done_received": True, "renew_observed": True, "release_observed": True,
            "release_disposition": release["disposition"], "fresh_matching_run_id": True}


async def natural_completion(api: CenterAPI, root: Path, model_id: str, run_id: str,
                             initial_state: str) -> dict:
    if initial_state != "ready":
        # A completed cold job cannot prove stop requested during cold startup.
        raise UpstreamError("cold_start_not_observed")
    current = await api.request("GET", LAUNCH)
    if current.get("running") is not False or current.get("runId"):
        raise UpstreamError("preflight_child_identity_changed")
    measured = fresh_results(root, run_id, initial_state)
    after = model_state(await api.request("GET", CATALOG), model_id)
    if after != "ready":
        raise UpstreamError("selected_model_lifecycle_state_mismatch")
    return dict(measured, selected_final_state=after, natural_completion_observed=True,
                owned_drain_observed=False, stop_requested_while_cold_start=False,
                technical_status="PASS", error_code=None)


async def observe(api: CenterAPI, root: Path, model_id: str, *, poll_seconds: float = 0.2,
                  startup_observation_seconds: float = 15) -> dict:
    observation: dict = {"technical_status": "FAIL", "acceptance_status": "PARTIAL"}
    own_run_id = None
    stop_confirmed = False
    try:
        before = model_state(await api.request("GET", CATALOG), model_id)
        if before not in {"stopped", "ready"}:
            raise UpstreamError("selected_model_already_transitioning")
        status = await api.request("GET", LAUNCH)
        if status.get("configured") is not True or status.get("running") is not False or status.get("runId"):
            raise UpstreamError("foreign_preflight_profile_active")
        launched = await api.request("POST", LAUNCH, {"model": "ai-server-local-" + model_id + "/local",
                                                       "effort": "", "externalSearch": False})
        if (type(launched.get("running")) is not bool or not isinstance(launched.get("runId"), str)
                or not RUN_ID.fullmatch(launched["runId"])):
            raise UpstreamError("launched_child_identity_missing")
        own_run_id = launched["runId"]
        observation.update(run_id=own_run_id, selected_initial_state=before, own_child_observed=True)
        if launched["running"] is False:
            observation.update(await natural_completion(api, root, model_id, own_run_id, before))
            stop_confirmed = True
            return observation
        starting_observed = False
        if before == "stopped":
            deadline = time.monotonic() + startup_observation_seconds
            while time.monotonic() < deadline:
                active = await api.request("GET", LAUNCH)
                if active.get("runId") != own_run_id or active.get("running") is not True:
                    raise UpstreamError("preflight_child_identity_changed")
                state = model_state(await api.request("GET", CATALOG), model_id)
                if state == "starting":
                    starting_observed = True
                    break
                if state in {"ready", "error"}:
                    break
                await asyncio.sleep(poll_seconds)
            if not starting_observed:
                raise UpstreamError("cold_start_not_observed")
        active = await api.request("GET", LAUNCH)
        if active.get("running") is False and not active.get("runId"):
            if starting_observed:
                # Stopped on its own before the intended cold-stop request.
                raise UpstreamError("cold_stop_not_requested")
            observation.update(await natural_completion(api, root, model_id, own_run_id, before))
            stop_confirmed = True
            return observation
        if active.get("runId") != own_run_id or active.get("running") is not True:
            raise UpstreamError("preflight_child_identity_changed")
        observation["stop_requested_while_cold_start"] = starting_observed
        stopped = await api.request("POST", STOP + own_run_id, {}, timeout=345)
        if stopped.get("running") is not False or stopped.get("runId"):
            raise UpstreamError("owned_preflight_drain_incomplete")
        stop_confirmed = True
        observation.update(fresh_results(root, own_run_id, before))
        after = model_state(await api.request("GET", CATALOG), model_id)
        if after != ("stopped" if before == "stopped" else "ready"):
            raise UpstreamError("selected_model_lifecycle_state_mismatch")
        observation.update(selected_final_state=after, owned_drain_observed=True,
                           technical_status="PASS", error_code=None)
    except UpstreamError as error:
        observation["error_code"] = error.code
    except asyncio.CancelledError:
        observation["error_code"] = "center_preflight_cancelled"
    except Exception:
        observation["error_code"] = "center_preflight_execution_error"
    finally:
        if own_run_id and not stop_confirmed:
            try:
                active = await api.request("GET", LAUNCH)
                if active.get("runId") == own_run_id and active.get("running") is True:
                    stopped = await api.request("POST", STOP + own_run_id, {}, timeout=345)
                    observation["finally_owned_drain_observed"] = stopped.get("running") is False and not stopped.get("runId")
                else:
                    observation["finally_foreign_child_preserved"] = bool(active.get("running"))
            except Exception:
                observation["finally_cleanup_status"] = "UNCONFIRMED"
    return observation


async def main(*, root: Path = ROOT, env: Mapping[str, str] | None = None,
               client: httpx.AsyncClient | None = None) -> int:
    try:
        selection = selected_configuration(root, os.environ if env is None else env)
    except UpstreamError as error:
        value = {"schema_version": "center-preflight-observation-v1", "technical_status": "BLOCKED",
                 "acceptance_status": "PARTIAL", "error_code": error.code, "network_calls": 0}
        save_json(root / "docs/artifacts/center-preflight-drain-observation.json", value)
        print("CENTER_PREFLIGHT_STATUS: BLOCKED (explicit selected configuration required).")
        return 3
    if client is None:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as actual:
            value = await observe(CenterAPI(actual), root, selection["model_id"])
    else:
        value = await observe(CenterAPI(client), root, selection["model_id"])
    value["schema_version"] = "center-preflight-observation-v1"
    save_json(root / "docs/artifacts/center-preflight-drain-observation.json", value)
    print("CENTER_PREFLIGHT_STATUS: " + value["technical_status"])
    return 0 if value["technical_status"] == "PASS" else 1
