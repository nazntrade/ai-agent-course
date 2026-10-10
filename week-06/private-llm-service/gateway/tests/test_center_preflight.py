"""P01: explicit selection, child ownership and real-drain evidence boundaries."""
import json

import httpx
import pytest

from gateway.app.upstream import UpstreamError
from harness import center_preflight as helper
from harness.runner import save_json

pytestmark = pytest.mark.integration
MODEL = "a" * 16
OWN = "b" * 32
FOREIGN = "c" * 32


def selection(root, **delta):
    value = {"schema_version": "center-test-selection-v1", "live_policy": "allowed",
             "model_id": MODEL, "center_origin": helper.ORIGIN}
    value.update(delta)
    save_json(root / ".runtime/center-test-selection.json", value)


def reports(root, run_id=OWN, *, borrowed=False, shared=False):
    measurements = {"input_tokens": 38, "stream_prompt_tokens": 38,
                    "public_fixture_answer": "READY", "done_received": True,
                    "finish_reason": "stop", "actual_slot_capacity": 32768,
                    "count_method": "native-chat-input-tokens", "context_source": "runtime-slot-props"}
    for criterion, filename in (("C01", "model-verification.json"), ("C07", "context-verification.json"),
                                ("C12", "lifecycle-verification.json")):
        value = {"run_id": run_id, "criterion": criterion, "technical_status": "PASS",
                 "measurements": measurements, "renew": {"renewed": True},
                 "release": {"released": True, "disposition": "shared" if shared else "retained" if borrowed else "unloaded"}}
        save_json(root / "docs/artifacts" / filename, value)
    save_json(root / "docs/artifacts/preflight-progress.json", {
        "run_id": run_id, "state": "finished", "exit_code": 0})


class SnapshotAPI:
    def __init__(self, root, *, foreign=False, change_identity=False, fail_first_stop=False,
                 stale_report=False, borrowed=False, shared=False, fast_completion=False,
                 complete_before_stop=False):
        self.root = root
        self.active = FOREIGN if foreign else None
        self.change_identity = change_identity
        self.fail_first_stop = fail_first_stop
        self.stale_report = stale_report
        self.borrowed = borrowed
        self.shared = shared
        self.fast_completion = fast_completion
        self.complete_before_stop = complete_before_stop
        self.calls = []
        self.launched = False
        self.stopped = False

    async def request(self, method, path, body=None, *, timeout=15):
        self.calls.append((method, path))
        if method == "GET" and path == helper.CATALOG:
            state = "ready" if self.borrowed else "stopped" if not self.launched or self.stopped else "starting"
            return {"models": [{"id": MODEL, "status": state}]}
        if method == "GET" and path == helper.LAUNCH:
            if self.launched and self.change_identity:
                self.active = FOREIGN
            if self.launched and self.complete_before_stop and self.active == OWN:
                reports(self.root, FOREIGN if self.stale_report else OWN, borrowed=self.borrowed, shared=self.shared)
                self.stopped, self.active = True, None
            return {"configured": True, "running": bool(self.active), "runId": self.active}
        if method == "POST" and path == helper.LAUNCH:
            assert body == {"model": "ai-server-local-" + MODEL + "/local", "effort": "", "externalSearch": False}
            self.launched, self.active = True, OWN
            if self.fast_completion:
                reports(self.root, FOREIGN if self.stale_report else OWN, borrowed=self.borrowed, shared=self.shared)
                self.stopped, self.active = True, None
                return {"running": False, "runId": OWN, "completed": True}
            return {"running": True, "runId": OWN}
        assert method == "POST" and path == helper.STOP + OWN
        assert self.active == OWN
        if self.fail_first_stop:
            self.fail_first_stop = False
            raise UpstreamError("center_transport_error")
        reports(self.root, FOREIGN if self.stale_report else OWN, borrowed=self.borrowed, shared=self.shared)
        self.stopped, self.active = True, None
        return {"running": False, "runId": None}


async def test_cold_stop_waits_for_current_report_and_unloaded_state(tmp_path):
    api = SnapshotAPI(tmp_path)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "PASS"
    assert result["stop_requested_while_cold_start"]
    assert result["fresh_matching_run_id"]
    assert result["actual_slot_capacity"] == 32768 and result["public_fixture_answer"] == "READY"
    assert result["release_disposition"] == "unloaded" and result["selected_final_state"] == "stopped"
    assert sum(method == "POST" and path.startswith(helper.STOP) for method, path in api.calls) == 1


async def test_preloaded_model_is_borrowed_and_left_ready(tmp_path):
    result = await helper.observe(SnapshotAPI(tmp_path, borrowed=True), tmp_path, MODEL)
    assert result["technical_status"] == "PASS"
    assert not result["stop_requested_while_cold_start"]
    assert result["selected_final_state"] == "ready" and result["release_disposition"] == "retained"


async def test_ready_model_shared_with_another_lease_remains_ready(tmp_path):
    result = await helper.observe(SnapshotAPI(tmp_path, borrowed=True, shared=True), tmp_path, MODEL)
    assert result["technical_status"] == "PASS"
    assert result["selected_initial_state"] == "ready" and result["selected_final_state"] == "ready"
    assert result["release_disposition"] == "shared" and result["release_observed"] is True


async def test_fast_warm_completion_retains_launch_identity_and_avoids_stop(tmp_path):
    api = SnapshotAPI(tmp_path, borrowed=True, fast_completion=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "PASS" and result["natural_completion_observed"]
    assert result["run_id"] == OWN and result["fresh_matching_run_id"]
    assert result["release_disposition"] == "retained" and result["selected_final_state"] == "ready"
    assert not result["owned_drain_observed"]
    assert not any(path.startswith(helper.STOP) for _, path in api.calls)


async def test_warm_completion_between_launch_and_stop_is_verified_without_stop(tmp_path):
    api = SnapshotAPI(tmp_path, borrowed=True, shared=True, complete_before_stop=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "PASS" and result["natural_completion_observed"]
    assert result["release_disposition"] == "shared" and result["selected_final_state"] == "ready"
    assert not result["stop_requested_while_cold_start"]
    assert not any(path.startswith(helper.STOP) for _, path in api.calls)


async def test_completed_cold_job_does_not_prove_cold_stop(tmp_path):
    api = SnapshotAPI(tmp_path, fast_completion=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "FAIL" and result["error_code"] == "cold_start_not_observed"
    assert not any(path.startswith(helper.STOP) for _, path in api.calls)


async def test_fast_completion_still_rejects_stale_success(tmp_path):
    result = await helper.observe(SnapshotAPI(tmp_path, borrowed=True, fast_completion=True,
                                            stale_report=True), tmp_path, MODEL)
    assert result["technical_status"] == "FAIL"
    assert result["error_code"] == "preflight_evidence_not_current_success"


async def test_foreign_launch_after_fast_completion_is_never_stopped(tmp_path):
    api = SnapshotAPI(tmp_path, borrowed=True, fast_completion=True, change_identity=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "FAIL" and result["error_code"] == "preflight_child_identity_changed"
    assert result["finally_foreign_child_preserved"]
    assert not any(path.startswith(helper.STOP) for _, path in api.calls)


async def test_foreign_existing_profile_never_launched_or_stopped(tmp_path):
    api = SnapshotAPI(tmp_path, foreign=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "FAIL"
    assert result["error_code"] == "foreign_preflight_profile_active"
    assert not any(method == "POST" for method, _ in api.calls)


async def test_identity_replacement_does_not_stop_foreign_child(tmp_path):
    api = SnapshotAPI(tmp_path, change_identity=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["error_code"] == "preflight_child_identity_changed"
    assert result["finally_foreign_child_preserved"]
    assert not any(path.startswith(helper.STOP) for _, path in api.calls)


async def test_failed_stop_is_fail_even_if_finally_drains_owned_child(tmp_path):
    api = SnapshotAPI(tmp_path, fail_first_stop=True)
    result = await helper.observe(api, tmp_path, MODEL)
    assert result["technical_status"] == "FAIL"
    assert result["error_code"] == "center_transport_error"
    assert result["finally_owned_drain_observed"]
    assert sum(path.startswith(helper.STOP) for _, path in api.calls) == 2
    assert "release_observed" not in result


async def test_stale_success_cannot_accept_current_launch(tmp_path):
    result = await helper.observe(SnapshotAPI(tmp_path, stale_report=True), tmp_path, MODEL)
    assert result["technical_status"] == "FAIL"
    assert result["error_code"] == "preflight_evidence_not_current_success"


@pytest.mark.parametrize("policy", ["forbidden", "unknown"])
async def test_forbidden_or_unknown_policy_has_zero_network_io(tmp_path, policy):
    selection(tmp_path)
    calls = []

    async def network(request):
        calls.append(request)
        raise AssertionError("Network must not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(network)) as client:
        assert await helper.main(root=tmp_path, env={"AI_TEST_LIVE_POLICY": policy}, client=client) == 3
    assert calls == []
    value = json.loads((tmp_path / "docs/artifacts/center-preflight-drain-observation.json").read_text())
    assert value["network_calls"] == 0 and value["technical_status"] == "BLOCKED"


@pytest.mark.parametrize("delta", [{"extra": True}, {"center_origin": "http://example.invalid"},
                                  {"live_policy": "forbidden"}, {"model_id": "../model"}])
def test_selection_rejects_unknown_fields_routes_and_policy(tmp_path, delta):
    selection(tmp_path, **delta)
    with pytest.raises(UpstreamError, match="invalid_center_selection"):
        helper.selected_configuration(tmp_path, {})


def test_partial_profile_is_rejected_before_loading_selection(tmp_path):
    selection(tmp_path)
    with pytest.raises(UpstreamError, match="explicit_profile_requires_direct_preflight"):
        helper.selected_configuration(tmp_path, {"AI_TEST_LIVE_POLICY": "allowed", "AI_TEST_MODEL_NAME": ""})


def test_missing_selection_never_chooses_a_model(tmp_path):
    with pytest.raises(UpstreamError, match="invalid_or_missing_evidence"):
        helper.selected_configuration(tmp_path, {})


async def test_http_adapter_whitelists_requests_and_bounds_responses():
    calls = []

    async def network(request):
        calls.append(request)
        assert request.headers["x-ai-server-request"] == "1"
        return httpx.Response(200, content=b"x" * 262145)

    async with httpx.AsyncClient(transport=httpx.MockTransport(network), follow_redirects=False) as client:
        api = helper.CenterAPI(client)
        with pytest.raises(UpstreamError, match="unsupported_center_operation"):
            await api.request("POST", "/api/local-models/unload-all", {})
        assert not calls
        with pytest.raises(UpstreamError, match="provider_response_too_large"):
            await api.request("GET", helper.CATALOG)
