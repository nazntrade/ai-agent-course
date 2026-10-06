"""D26-17: empty answer / broken JSON / transport error are classified as failure."""

from __future__ import annotations

import json

from app.errors import ProviderInvalidResponse, ProviderUnavailable
from app.providers.local_llama import LocalLlamaProvider


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def provider_with(handler):
    return LocalLlamaProvider(base_url="http://127.0.0.1:8791", model_id="gemma.gguf", opener=handler)


def test_empty_answer_is_not_success():
    def handler(request, timeout):
        return FakeResponse(json.dumps({"choices": [{"message": {"content": "   "}}]}).encode())

    try:
        provider_with(handler).chat([])
    except ProviderInvalidResponse:
        pass
    else:
        raise AssertionError("an empty answer must be an invalid response")


def test_missing_choice_is_not_success():
    def handler(request, timeout):
        return FakeResponse(json.dumps({"choices": []}).encode())

    try:
        provider_with(handler).chat([])
    except ProviderInvalidResponse:
        pass
    else:
        raise AssertionError("a missing choice must be an invalid response")


def test_broken_json_is_not_success():
    def handler(request, timeout):
        return FakeResponse(b"{not json")

    try:
        provider_with(handler).chat([])
    except ProviderInvalidResponse:
        pass
    else:
        raise AssertionError("broken JSON must be an invalid response")


def test_transport_error_is_unavailable():
    def handler(request, timeout):
        raise OSError("connection refused")

    try:
        provider_with(handler).chat([])
    except ProviderUnavailable:
        pass
    else:
        raise AssertionError("a transport error must be unavailable")


def test_valid_answer_returns_factual_model_and_usage():
    payload = {
        "model": "gemma-4-12b-it-qat-q4_0.gguf",
        "choices": [{"message": {"content": "43"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }

    def handler(request, timeout):
        return FakeResponse(json.dumps(payload).encode())

    result = provider_with(handler).chat([])
    assert result.text == "43"
    assert result.model == "gemma-4-12b-it-qat-q4_0.gguf"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 12
