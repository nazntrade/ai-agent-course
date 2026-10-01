"""Chat error taxonomy and core data contracts (SPEC D22 6, 7.2)."""

from __future__ import annotations

from knowledge_agent.domain.contracts import ChatMessage, ChatUsage
from knowledge_agent.domain.errors import (
    ChatInvalidResponse,
    ChatLengthError,
    ChatModelMissing,
    ChatTimeout,
    ChatUnavailable,
    ContextOverflow,
    InvalidRequest,
)


def test_chat_error_codes_and_http_status():
    cases = {
        ChatUnavailable: ("chat_unavailable", 503),
        ChatTimeout: ("chat_timeout", 503),
        ChatModelMissing: ("chat_model_missing", 503),
        ChatInvalidResponse: ("chat_invalid_response", 503),
        ChatLengthError: ("chat_length_error", 503),
        ContextOverflow: ("context_overflow", 422),
        InvalidRequest: ("invalid_request", 422),
    }
    for error_class, (code, status) in cases.items():
        error = error_class("boom", details={"hint": "ollama pull x"})
        assert error.code == code
        assert error.http_status == status
        assert error.to_dict()["code"] == code
        assert error.to_dict()["details"]["hint"] == "ollama pull x"


def test_chat_message_and_usage_serialization():
    assert ChatMessage("user", "hi").to_dict() == {"role": "user", "content": "hi"}
    usage = ChatUsage(input_tokens=None, output_tokens=4, total_tokens=4)
    assert usage.to_dict() == {"input_tokens": None, "output_tokens": 4, "total_tokens": 4}
