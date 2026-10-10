"""Manual service ownership, cancellation and environment dispatch boundaries."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from gateway.app.upstream import ProviderConfig, UpstreamError
from harness import center_service as helper, runner
from harness.runner import save_json

pytestmark = pytest.mark.integration
MODEL = "a" * 16
OWN = "b" * 32
FOREIGN = "c" * 32


class ServiceSnapshot:
    def __init__(self, *, existing=False, replace=False, late=False, stop_failure=False):
        self.active = FOREIGN if existing else None
        self.replace, self.late, self.stop_failure = replace, late, stop_failure
        self.calls = []
        self.gets_after_launch = 0
        self.launch_started = asyncio.Event()
        self.launch_finish = asyncio.Event()
        self.ready = asyncio.Event()

    async def request(self, method, path, body=None, *, timeout=15):
        self.calls.append((method, path))
        if path == helper.CATALOG:
            return {"models": [{"id": MODEL, "status": "stopped"}]}
        if path.startswith(helper.STOP):
            assert path == helper.STOP + OWN and self.active == OWN
            if self.stop_failure:
                raise UpstreamError("center_transport_error")
            self.active = None
            return {"running": False, "runId": None}
        assert path == helper.LAUNCH
        if method == "POST":
            assert body == {"model": "ai-server-local-" + MODEL + "/local", "effort": "", "externalSearch": False}
            self.launch_started.set()
            if self.late:
                await self.launch_finish.wait()
            self.active = OWN
            return {"running": True, "runId": OWN}
        if self.active == OWN:
            self.gets_after_launch += 1
            if self.gets_after_launch == 1:
                self.ready.set()
            elif self.replace:
                self.active = FOREIGN
        return {"configured": True, "running": bool(self.active), "runId": self.active,
                "model": "ai-server-local-" + MODEL + "/local" if self.active else None}


async def test_existing_foreign_service_is_observed_without_launch_or_stop():
    api = ServiceSnapshot(existing=True)
    assert await helper.launch_and_wait(api, MODEL, poll_seconds=0) == 0
    assert not any(method == "POST" for method, _ in api.calls)
    assert api.active == FOREIGN


async def test_manual_owned_service_ctrl_c_drains_only_matching_child():
    api = ServiceSnapshot()
    task = asyncio.create_task(helper.launch_and_wait(api, MODEL, poll_seconds=0.01))
    await api.ready.wait()
    task.cancel()
    assert await task == 130
    assert api.active is None
    assert sum(path.startswith(helper.STOP) for _, path in api.calls) == 1


async def test_cancellation_during_launch_waits_for_identity_then_stops_own_child():
    api = ServiceSnapshot(late=True)
    task = asyncio.create_task(helper.launch_and_wait(api, MODEL, poll_seconds=0))
    await api.launch_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    api.launch_finish.set()
    assert await task == 130
    assert api.active is None
    assert sum(path.startswith(helper.STOP) for _, path in api.calls) == 1


async def test_identity_replacement_is_never_stopped():
    api = ServiceSnapshot(replace=True)
    assert await helper.launch_and_wait(api, MODEL, poll_seconds=0) == 1
    assert api.active == FOREIGN
    assert not any(path.startswith(helper.STOP) for _, path in api.calls)


async def test_owned_stop_failure_is_reported_unconfirmed(capsys):
    api = ServiceSnapshot(stop_failure=True)
    task = asyncio.create_task(helper.launch_and_wait(api, MODEL, poll_seconds=0.01))
    await api.ready.wait()
    task.cancel()
    assert await task == 130
    assert "SERVICE_CLEANUP: UNCONFIRMED" in capsys.readouterr().out
    assert api.active == OWN


@pytest.mark.parametrize("env", [{"AI_TEST_LIVE_POLICY": "forbidden"},
                                  {"AI_TEST_LIVE_POLICY": "unknown"},
                                  {"AI_TEST_MODEL_NAME": "local", "AI_TEST_LIVE_POLICY": "allowed"}])
async def test_blocked_selection_has_zero_network_calls(tmp_path, env):
    save_json(tmp_path / ".runtime/center-test-selection.json", {
        "schema_version": "center-test-selection-v1", "live_policy": "allowed",
        "model_id": MODEL, "center_origin": helper.ORIGIN})
    calls = []
    def network(request):
        calls.append(request)
        raise AssertionError("Blocked configuration must not touch network")
    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as client:
        assert await helper.main(root=tmp_path, env=env, client=client) == 3
    assert calls == []


async def test_service_adapter_whitelist_and_actual_response_bound():
    calls = []
    def network(request):
        calls.append(request)
        assert request.headers["x-ai-server-request"] == "1"
        return httpx.Response(200, content=b"x" * 262145)
    async with httpx.AsyncClient(transport=httpx.MockTransport(network), follow_redirects=False) as client:
        api = helper.ServiceAPI(client)
        with pytest.raises(UpstreamError, match="unsupported_center_operation"):
            await api.request("POST", "/api/projects/other/launch", {})
        with pytest.raises(UpstreamError, match="unsupported_center_operation"):
            await api.request("POST", helper.STOP + "invalid", {})
        assert not calls
        with pytest.raises(UpstreamError, match="provider_response_too_large"):
            await api.request("GET", helper.CATALOG)


@pytest.mark.parametrize("env", [{"AI_TEST_LIVE_POLICY": "forbidden"},
                                  {"AI_TEST_LIVE_POLICY": "unknown"},
                                  {"AI_TEST_MODEL_NAME": "local", "AI_TEST_LIVE_POLICY": "allowed"}])
def test_network_runner_rejects_policy_or_partial_profile_before_import(monkeypatch, env):
    monkeypatch.setattr(runner.os, "environ", env)
    def fail_import(_name):
        raise AssertionError("Blocked profile must not import the gateway")
    monkeypatch.setattr(runner.importlib, "import_module", fail_import)
    assert runner.run("network") == 3


def test_complete_allowed_profile_runs_gateway_directly_without_recursion(monkeypatch):
    monkeypatch.setattr(runner.os, "environ", {"AI_TEST_MODEL_NAME": "local", "AI_TEST_LIVE_POLICY": "allowed"})
    monkeypatch.setattr(ProviderConfig, "from_environment", lambda: SimpleNamespace())
    invoked = []
    monkeypatch.setattr(runner.importlib, "import_module", lambda _name: SimpleNamespace(main=lambda mode: invoked.append(mode) or 0))
    async def forbidden_delegate():
        raise AssertionError("Complete Center child must never launch another child")
    monkeypatch.setattr(helper, "main", forbidden_delegate)
    assert runner.run("network") == 0 and invoked == ["network"]


def test_profile_absent_network_runner_delegates_only_explicit_request(monkeypatch):
    monkeypatch.setattr(runner.os, "environ", {})
    called = []
    async def delegated():
        called.append(True)
        return 3
    monkeypatch.setattr(helper, "main", delegated)
    assert called == []
    assert runner.run("network") == 3 and called == [True]
