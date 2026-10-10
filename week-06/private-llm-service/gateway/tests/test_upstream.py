"""C01/C07/C12/C14: protocol and cancellation boundaries without real providers."""
import asyncio
import json
import os

import httpx
import pytest

from gateway.app.upstream import ChatStreamParser, LeaseManager, LocalProvider, ProviderConfig, UpstreamError, chat_body


def event(delta=None, reason=None, *, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}]}
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def config(parent=""):
    return ProviderConfig("http://127.0.0.1:9999/v1", "fake-provider-key", "local",
                          "http://127.0.0.1:9999/api/local-models/" + "a" * 16 + "/test-leases", parent)


@pytest.mark.unit
def test_offline_runner_does_not_expose_selected_model_profile():
    assert os.environ["AI_TEST_LIVE_POLICY"] == "forbidden"
    assert not any(key.startswith("AI_TEST_MODEL_") for key in os.environ)


@pytest.mark.unit
def test_utf8_split_stream_separates_reasoning_and_preserves_literal_text():
    parser = ChatStreamParser()
    stream = (b": heartbeat\r\n\r\n" + event({"role": "assistant"})
              + event({"reasoning_content": "private reasoning"})
              + event({"content": "Привет <think>literal</think>"})
              + event(reason="length")
              + b'data: {"choices":[],"usage":{"prompt_tokens":42}}\r\n\r\n'
              + b"data: [DONE]\r\n\r\n")
    deltas = []
    for byte in stream:
        deltas.extend(parser.feed(bytes([byte])))
    result = parser.finish()
    assert "".join(deltas) == result.content == "Привет <think>literal</think>"
    assert result.reasoning_characters == len("private reasoning")
    assert result.finish_reason == "length" and result.prompt_tokens == 42 and result.done


@pytest.mark.unit
def test_multiple_events_in_one_read_and_multiline_data():
    parser = ChatStreamParser()
    stream = (b'data: {"choices":\n'
              b'data: [{"index":0,"delta":{"content":"READY"},"finish_reason":null}]}\n\n'
              + event(reason="stop") + b"data: [DONE]\n\n")
    assert parser.feed(stream) == ["READY"]
    assert parser.finish().finish_reason == "stop"


@pytest.mark.unit
@pytest.mark.parametrize("bad_stream,code", [
    (event({"content": "partial"}), "stream_incomplete"),
    (event({"content": "partial"}) + b"data: [DONE]\n\n", "stream_early_done"),
    (b"data: {bad}\n\n", "stream_invalid_json"),
    (b"data: \xff\n\n", "stream_invalid_utf8"),
    (event({"content": "partial"}) + event(reason="tool_calls"), "stream_invalid_finish"),
    (event({"tool_calls": []}), "stream_unsupported_delta"),
    (event({"role": "user"}), "stream_invalid_role"),
    (event({"content": "partial"}) + event(reason="stop") + event({"content": "late"}), "stream_content_after_finish"),
    (event(reason="stop") + b"data: [DONE]\n\n", "stream_early_done"),
    (b'data: {"choices":[]}\n\n', "stream_invalid_choices"),
    (b'data: {"error":{"message":"never disclose"}}\n\n', "stream_invalid_event"),
])
def test_broken_stream_is_never_completed(bad_stream, code):
    parser = ChatStreamParser()
    with pytest.raises(UpstreamError, match="^" + code + "$"):
        parser.feed(bad_stream)
        parser.finish()
    assert not parser.result.done


@pytest.mark.unit
def test_event_and_response_limits_apply_to_actual_bytes():
    with pytest.raises(UpstreamError, match="stream_event_too_large"):
        ChatStreamParser(event_cap=20).feed("data: " .encode() + "я".encode() * 20)
    with pytest.raises(UpstreamError, match="stream_response_too_large"):
        ChatStreamParser(response_cap=10).feed(b": heartbeat\n\n")


@pytest.mark.unit
def test_profile_rejects_partial_or_forbidden_without_fallback():
    with pytest.raises(UpstreamError, match="live_policy_blocked"):
        ProviderConfig.from_environment({"AI_TEST_LIVE_POLICY": "forbidden"})
    with pytest.raises(UpstreamError, match="local_profile_missing_or_partial"):
        ProviderConfig.from_environment({"AI_TEST_LIVE_POLICY": "allowed", "AI_TEST_MODEL_KIND": "local"})


@pytest.mark.integration
async def test_late_acquire_survives_request_and_shutdown_cancellation():
    acquired, allow = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            acquired.set()
            await allow.wait()
            return httpx.Response(201, json={"lease_id": "b" * 48, "expires_in_seconds": 180})
        assert request.method == "DELETE"
        return httpx.Response(200, json={"released": True, "disposition": "unloaded"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        lease = LeaseManager(config(), client)
        request = asyncio.create_task(lease.ensure())
        await acquired.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        closing = asyncio.create_task(lease.close())
        await asyncio.sleep(0)
        closing.cancel()
        allow.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert calls == ["POST", "DELETE"]
        assert lease.release_observation["disposition"] == "unloaded"


@pytest.mark.integration
async def test_parent_lease_is_used_but_never_released_or_renewed_by_child():
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        assert request.headers["x-ai-test-model-lease-id"] == "b" * 48
        return httpx.Response(200, json={"input_tokens": 7, "context_capacity": 8192,
            "method": "native-chat-input-tokens", "context_source": "runtime-slot-props"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        lease = LeaseManager(config("b" * 48), client)
        assert (await LocalProvider(config("b" * 48), client, lease).count(chat_body([])))["input_tokens"] == 7
        assert (await lease.renew())["parent_owned"]
        assert (await lease.close())["status"] == "PARENT_OWNED"
        assert calls == ["/v1/token-count"]


@pytest.mark.integration
async def test_count_and_inference_use_identical_body_and_correct_lease():
    bodies = []

    async def handle(request):
        if request.method == "DELETE":
            return httpx.Response(200, json={"released": True, "disposition": "retained"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"renewed": True, "expires_in_seconds": 180})
        if request.url.path.endswith("test-leases"):
            return httpx.Response(201, json={"lease_id": "b" * 48, "expires_in_seconds": 180})
        assert request.headers["x-ai-test-model-lease-id"] == "b" * 48
        bodies.append(json.loads(request.content))
        if request.url.path.endswith("token-count"):
            return httpx.Response(200, json={"input_tokens": 19, "context_capacity": 16384,
                "method": "native-chat-input-tokens", "context_source": "runtime-slot-props"})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=
            event({"content": "READY"}) + event(reason="stop", usage={"prompt_tokens": 19})
            + b"data: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        lease = LeaseManager(config(), client)
        provider = LocalProvider(config(), client, lease)
        body = chat_body([{"role": "user", "content": "test"}], output_tokens=32)
        try:
            count = await provider.count(body)
            response = await provider.stream(body)
            assert count["input_tokens"] == response.prompt_tokens
            assert bodies == [body, body]
            assert (await lease.renew())["renewed"]
        finally:
            assert (await lease.close())["disposition"] == "retained"


@pytest.mark.integration
async def test_malformed_native_count_fails_without_guessing_and_releases():
    methods = []

    async def handle(request):
        methods.append(request.method)
        if request.url.path.endswith("test-leases"):
            return httpx.Response(201, json={"lease_id": "b" * 48, "expires_in_seconds": 180})
        if request.method == "DELETE":
            return httpx.Response(200, json={"released": True, "disposition": "unloaded"})
        return httpx.Response(200, json={"input_tokens": True, "context_capacity": 8192})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        lease = LeaseManager(config(), client)
        try:
            with pytest.raises(UpstreamError, match="invalid_token_count"):
                await LocalProvider(config(), client, lease).count(chat_body([]))
        finally:
            await lease.close()
    assert methods == ["POST", "POST", "DELETE"]


@pytest.mark.integration
async def test_failed_renewal_prevents_new_inference():
    async def handle(request):
        if request.method == "POST":
            return httpx.Response(201, json={"lease_id": "b" * 48, "expires_in_seconds": 180})
        if request.method == "PATCH":
            return httpx.Response(409, json={"error": "private provider details"})
        return httpx.Response(200, json={"released": True, "disposition": "unloaded"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        lease = LeaseManager(config(), client, renew_seconds=0.001)
        await lease.ensure()
        for _ in range(50):
            if lease.renew_failed:
                break
            await asyncio.sleep(0.001)
        assert lease.renew_failed
        with pytest.raises(UpstreamError, match="lease_unavailable"):
            await LocalProvider(config(), client, lease).stream(chat_body([]))
        await lease.close()


@pytest.mark.integration
async def test_whole_preflight_deadline_releases_and_records_bounded_failure(monkeypatch):
    from harness import model_preflight

    observed = []
    saved = []

    class DelayedLease:
        def __init__(self, *_args):
            pass

        async def ensure(self):
            observed.append("acquire_started")
            await asyncio.sleep(10)

        async def close(self):
            observed.append("cleanup_finished")
            return {"status": "OBSERVED", "released": True, "disposition": "unloaded"}

    monkeypatch.setattr(model_preflight.ProviderConfig, "from_environment", lambda: config())
    monkeypatch.setattr(model_preflight, "LeaseManager", DelayedLease)
    monkeypatch.setattr(model_preflight, "save_json", lambda _path, value: saved.append(value))
    assert await model_preflight.main(operation_deadline=0.01) == 1
    assert observed == ["acquire_started", "cleanup_finished"]
    assert len(saved) == 3
    assert all(value["error_code"] == "preflight_deadline_exceeded" for value in saved)
    assert all(value["technical_status"] == "FAIL" for value in saved)
    lifecycle = next(value for value in saved if value["criterion"] == "C12")
    assert lifecycle["release"]["released"] is True


@pytest.mark.integration
async def test_whole_preflight_deadline_also_covers_running_generation(monkeypatch):
    from harness import model_preflight

    observed = []
    saved = []

    class ReadyLease:
        def __init__(self, *_args):
            pass

        async def ensure(self):
            observed.append("acquire_ready")

        async def close(self):
            observed.append("cleanup_finished")
            return {"status": "OBSERVED", "released": True, "disposition": "retained"}

    class DelayedProvider:
        def __init__(self, *_args):
            pass

        async def count(self, _body):
            return {"context_capacity": 8192}

        async def stream(self, _body):
            observed.append("generation_started")
            await asyncio.sleep(10)

    monkeypatch.setattr(model_preflight.ProviderConfig, "from_environment", lambda: config())
    monkeypatch.setattr(model_preflight, "LeaseManager", ReadyLease)
    monkeypatch.setattr(model_preflight, "LocalProvider", DelayedProvider)
    monkeypatch.setattr(model_preflight, "save_json", lambda _path, value: saved.append(value))
    assert await model_preflight.main(operation_deadline=0.01) == 1
    assert observed == ["acquire_ready", "generation_started", "cleanup_finished"]
    assert saved[0]["error_code"] == "preflight_deadline_exceeded"
