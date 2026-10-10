"""Explicit manual service launch; credentials remain in the Center-owned child."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Mapping

import httpx

from gateway.app.upstream import UpstreamError, bounded_json
from harness.center_preflight import CATALOG, ORIGIN, RUN_ID, model_state, selected_configuration
from harness.runner import ROOT

LAUNCH = "/api/projects/ai-agent-course/launch?profile=private-llm-service"
STOP = "/api/projects/ai-agent-course/launch/stop?profile=private-llm-service&expectedRunId="


class ServiceAPI:
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
            raise UpstreamError("center_unavailable_or_operation_unconfirmed") from None


async def deferred(task: asyncio.Task):
    """Finish bounded ownership/cleanup operations even after repeated Ctrl+C."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def launch_and_wait(api: ServiceAPI, model_id: str, *, poll_seconds: float = 1) -> int:
    own_id = None
    pending_launch = None
    cancelled = False
    result = 1
    try:
        model_state(await api.request("GET", CATALOG), model_id)
        status = await api.request("GET", LAUNCH)
        if status.get("configured") is not True or type(status.get("running")) is not bool:
            raise UpstreamError("service_profile_unavailable")
        if status["running"]:
            if not isinstance(status.get("runId"), str) or not RUN_ID.fullmatch(status["runId"]):
                raise UpstreamError("service_child_identity_missing")
            print("SERVICE_STATUS: ALREADY_RUNNING (existing launch preserved; no ownership).")
            return 0
        if status.get("runId"):
            raise UpstreamError("service_child_identity_invalid")
        # Center readiness is bounded at 60 s; failed-start cleanup may drain 330 s.
        pending_launch = asyncio.create_task(api.request("POST", LAUNCH,
            {"model": "ai-server-local-" + model_id + "/local", "effort": "", "externalSearch": False}, timeout=410))
        launched = await asyncio.shield(pending_launch)
        if (not isinstance(launched.get("runId"), str) or not RUN_ID.fullmatch(launched["runId"])):
            raise UpstreamError("service_child_identity_missing")
        own_id = launched["runId"]
        if launched.get("running") is not True:
            raise UpstreamError("service_exited_before_ready")
        status = await api.request("GET", LAUNCH)
        if (status.get("runId") != own_id or status.get("running") is not True
                or status.get("model") != "ai-server-local-" + model_id + "/local"):
            raise UpstreamError("service_child_identity_changed")
        # POST launch returns only after the profile's configured health check.
        print("SERVICE_STATUS: READY (manual launch; Ctrl+C stops this owned launch).", flush=True)
        while True:
            await asyncio.sleep(poll_seconds)
            status = await api.request("GET", LAUNCH)
            if status.get("running") is False and not status.get("runId"):
                print("SERVICE_STATUS: STOPPED")
                result = 0
                break
            if status.get("runId") != own_id or status.get("running") is not True:
                raise UpstreamError("service_child_identity_changed")
    except asyncio.CancelledError:
        cancelled = True
    except UpstreamError as error:
        print("SERVICE_STATUS: FAIL (" + error.code + ").")
    except Exception:
        print("SERVICE_STATUS: FAIL (bounded service operation failed).")
    finally:
        if pending_launch is not None and own_id is None:
            try:
                launched = await deferred(pending_launch)
                candidate = launched.get("runId")
                if isinstance(candidate, str) and RUN_ID.fullmatch(candidate):
                    own_id = candidate
            except Exception:
                print("SERVICE_CLEANUP: UNCONFIRMED (launch ownership was not returned).")
        if own_id:
            async def cleanup():
                status = await api.request("GET", LAUNCH)
                if status.get("running") is True and status.get("runId") == own_id:
                    stopped = await api.request("POST", STOP + own_id, {}, timeout=345)
                    if stopped.get("running") is not False or stopped.get("runId"):
                        raise UpstreamError("owned_service_stop_unconfirmed")
                elif status.get("running") is True or status.get("runId"):
                    print("SERVICE_CLEANUP: FOREIGN_LAUNCH_PRESERVED")
            try:
                await deferred(asyncio.create_task(cleanup()))
            except Exception:
                print("SERVICE_CLEANUP: UNCONFIRMED (owned stop could not be verified).")
                result = 1
    return 130 if cancelled else result


async def main(*, root: Path = ROOT, env: Mapping[str, str] | None = None,
               client: httpx.AsyncClient | None = None) -> int:
    try:
        selection = selected_configuration(root, os.environ if env is None else env)
    except UpstreamError:
        print("SERVICE_STATUS: CONFIGURATION_ERROR (explicit allowed selection required).")
        return 3
    if client is not None:
        return await launch_and_wait(ServiceAPI(client), selection["model_id"])
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as actual:
        return await launch_and_wait(ServiceAPI(actual), selection["model_id"])
