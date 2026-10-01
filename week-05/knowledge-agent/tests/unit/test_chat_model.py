"""OllamaChatModel on an injected transport: provider boundary (D22-05/06)."""

from __future__ import annotations

import json

import pytest

from knowledge_agent.chat.ollama_chat import (
    OllamaChatModel,
    TransportError,
    TransportTimeout,
)
from knowledge_agent.domain.contracts import ChatMessage
from knowledge_agent.domain.errors import (
    ChatInvalidResponse,
    ChatLengthError,
    ChatModelMissing,
    ChatTimeout,
    ChatUnavailable,
)

MESSAGES = [ChatMessage("user", "hello")]


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _chat_body(**overrides) -> bytes:
    payload = {
        "model": "m",
        "message": {"role": "assistant", "content": "hello"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 10,
        "eval_count": 5,
        "eval_duration": 500_000_000,
    }
    payload.update(overrides)
    return _body(payload)


def _model(handler, calls=None):
    calls = calls if calls is not None else []

    def transport(method, url, payload, timeout):
        calls.append((method, url, payload))
        return handler(method, url, payload)

    return OllamaChatModel(base_url="http://stub", model="m", transport=transport), calls


def test_normal_answer_exposes_text_finish_reason_usage_and_rate():
    model, _ = _model(lambda method, url, payload: (200, _chat_body()))
    result = model.chat(MESSAGES)
    assert result.text == "hello"
    assert result.finish_reason == "stop"
    assert result.usage.to_dict() == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert result.output_tokens_per_second == 10.0


def test_done_reason_length_is_reported_not_raised():
    model, _ = _model(lambda method, url, payload: (200, _chat_body(done_reason="length")))
    result = model.chat(MESSAGES)
    assert result.finish_reason == "length"


def test_missing_usage_is_null_not_invented():
    response = json.loads(_chat_body())
    response.pop("prompt_eval_count")
    response.pop("eval_count")
    response.pop("eval_duration")
    model, _ = _model(lambda method, url, _payload: (200, _body(response)))
    result = model.chat(MESSAGES)
    assert result.usage is None
    assert result.output_tokens_per_second is None


def test_partial_usage_is_preserved_as_is():
    response = json.loads(_chat_body())
    response.pop("prompt_eval_count")
    response.pop("eval_duration")
    model, _ = _model(lambda method, url, _payload: (200, _body(response)))
    result = model.chat(MESSAGES)
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens == 5
    assert result.usage.total_tokens == 5
    assert result.output_tokens_per_second is None


def test_rate_requires_both_eval_count_and_duration():
    response = json.loads(_chat_body())
    response.pop("eval_duration")
    model, _ = _model(lambda method, url, _payload: (200, _body(response)))
    assert model.chat(MESSAGES).output_tokens_per_second is None


def test_missing_model_maps_to_chat_model_missing_with_hint():
    handler = lambda method, url, payload: (  # noqa: E731
        404,
        b'{"error":"model \'m\' not found, try pulling it first"}',
    )
    model, _ = _model(handler)
    with pytest.raises(ChatModelMissing) as excinfo:
        model.chat(MESSAGES)
    assert excinfo.value.details["hint"] == "ollama pull m"


def test_transport_timeout_maps_to_chat_timeout():
    def handler(method, url, payload):
        raise TransportTimeout("slow")

    model, _ = _model(handler)
    with pytest.raises(ChatTimeout):
        model.chat(MESSAGES)


def test_unreachable_transport_maps_to_chat_unavailable():
    def handler(method, url, payload):
        raise TransportError("down")

    model, _ = _model(handler)
    with pytest.raises(ChatUnavailable):
        model.chat(MESSAGES)


def test_http_500_maps_to_chat_unavailable():
    model, _ = _model(lambda method, url, payload: (500, b'{"error":"boom"}'))
    with pytest.raises(ChatUnavailable):
        model.chat(MESSAGES)


def test_length_error_is_distinct_from_unavailable():
    model, _ = _model(lambda method, url, payload: (400, b'{"error":"input is too long for context length"}'))
    with pytest.raises(ChatLengthError):
        model.chat(MESSAGES)


def test_malformed_or_empty_answer_is_invalid_response():
    model, _ = _model(lambda method, url, payload: (200, _body({"model": "m", "message": {"content": ""}})))
    with pytest.raises(ChatInvalidResponse):
        model.chat(MESSAGES)


def test_empty_answer_with_length_finish_reason_is_invalid():
    # Regression: a content-empty ``length`` answer must not become a false PASS.
    model, _ = _model(lambda method, url, payload: (200, _chat_body(
        message={"role": "assistant", "content": ""}, done_reason="length")))
    with pytest.raises(ChatInvalidResponse):
        model.chat(MESSAGES)


def _ndjson(*lines: dict) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def test_streaming_aggregates_tokens_and_done_usage():
    body = _ndjson(
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 4,
            "eval_count": 2,
            "eval_duration": 1_000_000_000,
        },
    )
    model, _ = _model(lambda method, url, payload: (200, body))
    events = list(model.stream_chat(MESSAGES))
    assert [event["type"] for event in events] == ["token", "token", "done"]
    assert "".join(event["text"] for event in events if event["type"] == "token") == "Hello"
    done = events[-1]["result"]
    assert done.text == "Hello"
    assert done.usage.total_tokens == 6
    assert done.output_tokens_per_second == 2.0


def test_streaming_without_done_is_invalid():
    body = _ndjson({"message": {"content": "partial"}, "done": False})
    model, _ = _model(lambda method, url, payload: (200, body))
    with pytest.raises(ChatInvalidResponse):
        list(model.stream_chat(MESSAGES))


def test_streaming_malformed_line_is_invalid():
    model, _ = _model(lambda method, url, payload: (200, b"not-json\n"))
    with pytest.raises(ChatInvalidResponse):
        list(model.stream_chat(MESSAGES))


def test_empty_stream_with_length_finish_reason_is_invalid():
    body = _ndjson({"message": {"role": "assistant", "content": ""}, "done": True,
                    "done_reason": "length", "eval_count": 3})
    model, _ = _model(lambda method, url, payload: (200, body))
    with pytest.raises(ChatInvalidResponse):
        list(model.stream_chat(MESSAGES))


def test_non_empty_stream_with_length_finish_reason_is_preserved():
    body = _ndjson(
        {"message": {"role": "assistant", "content": "partial"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True,
         "done_reason": "length", "eval_count": 3, "eval_duration": 1_000_000_000},
    )
    model, _ = _model(lambda method, url, payload: (200, body))
    events = list(model.stream_chat(MESSAGES))
    done = events[-1]["result"]
    assert done.text == "partial"
    assert done.finish_reason == "length"


def test_preflight_reads_version_tags_and_show_without_inference():
    calls = []

    def handler(method, url, payload):
        if url.endswith("/api/version"):
            return 200, _body({"version": "0.35.0"})
        if url.endswith("/api/tags"):
            return 200, _body({"models": [{"name": "m", "digest": "abc123"}]})
        if url.endswith("/api/show"):
            return 200, _body({"digest": "abc123", "model_info": {"llama.context_length": 4096}})
        raise AssertionError(url)

    model, _ = _model(handler, calls)
    info = model.preflight()
    assert info["reachable"] is True
    assert info["model_present"] is True
    assert info["context_length"] == 4096
    assert info["digest"] == "abc123"
    assert all(not url.endswith("/api/chat") for _, url, _ in calls)
    identity = model.identity()
    assert identity.context_length == 4096
    assert identity.default_options["num_predict"] == 1024
