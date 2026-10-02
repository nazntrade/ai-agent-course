"""D23-C03/C04: bounded rewrite, prompt isolation, fallback and timeout boundary."""

from __future__ import annotations

import json

from knowledge_agent.__main__ import build_chat_service
from knowledge_agent.chat.ollama_chat import OllamaChatModel, TransportTimeout
from knowledge_agent.chat.rewrite import ChatQueryRewriter
from knowledge_agent.config import load_settings
from knowledge_agent.domain.contracts import ChatMessage
from knowledge_agent.domain.errors import ChatTimeout
from tests.helpers import FakeChatModel

QUESTION = "How do agents use memory?"


def _rewriter(model: FakeChatModel) -> ChatQueryRewriter:
    return ChatQueryRewriter(model)


def test_rewrite_returns_a_distinct_single_line_query():
    model = FakeChatModel(text="agent memory retrieval")
    result = _rewriter(model).rewrite(QUESTION)
    assert result.attempted is True and result.used is True and result.fallback is False
    assert result.original_query == QUESTION
    assert result.search_query == "agent memory retrieval"
    assert result.search_query != result.original_query
    assert result.latency_ms is not None


def test_provider_error_falls_back_with_reason():
    model = FakeChatModel(error=ChatTimeout("down"))
    result = _rewriter(model).rewrite(QUESTION)
    assert result.fallback is True and result.used is False
    assert result.search_query == QUESTION
    assert result.reason == "chat_timeout"


def test_empty_answer_and_length_fall_back():
    for model in (FakeChatModel(text=""), FakeChatModel(text="answer", finish_reason="length")):
        result = _rewriter(model).rewrite(QUESTION)
        assert result.fallback is True
        assert result.reason == "rewrite_invalid"
        assert result.search_query == QUESTION


def test_multiline_and_refusal_fall_back():
    for text in ("line one\nline two", "I cannot rewrite this."):
        result = _rewriter(FakeChatModel(text=text)).rewrite(QUESTION)
        assert result.fallback is True and result.reason == "rewrite_invalid"


def test_rewrite_prompt_contains_only_instruction_and_question():
    model = FakeChatModel(text="memory retrieval")
    _rewriter(model).rewrite(QUESTION)
    messages = model.calls[0]
    assert [message.role for message in messages] == ["system", "user"]
    combined = "\n".join(message.content for message in messages)
    assert QUESTION in combined
    assert "expected_facts" not in combined
    assert "<context>" not in combined
    assert "with_rag" not in combined and "without_rag" not in combined


def test_adapter_applies_the_configured_timeout_to_the_transport():
    seen = {}

    def transport(method, url, payload, timeout):
        seen["timeout"] = timeout
        body = json.dumps(
            {
                "model": "m",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "done_reason": "stop",
            }
        ).encode()
        return 200, body

    model = OllamaChatModel(
        "http://127.0.0.1:11434", "m", timeout=7.5, transport=transport
    )
    model.chat([ChatMessage("user", QUESTION)])
    assert seen["timeout"] == 7.5


def test_transport_timeout_becomes_a_rewrite_fallback():
    def transport(method, url, payload, timeout):
        raise TransportTimeout("too slow")

    model = OllamaChatModel("http://127.0.0.1:11434", "m", timeout=0.2, transport=transport)
    result = _rewriter(model).rewrite(QUESTION)
    assert result.fallback is True and result.reason == "chat_timeout"


def test_build_chat_service_wires_a_dedicated_rewrite_model():
    settings = load_settings(
        {
            "RAG_REWRITE_TIMEOUT_SECONDS": "5",
            "RAG_REWRITE_MAX_OUTPUT_TOKENS": "32",
            "RAG_REWRITE_ENABLED": "1",
            "RAG_FILTER_ENABLED": "1",
        }
    )
    service = build_chat_service(settings, object())
    assert service.query_rewriter is not None
    assert service.query_rewriter.model.timeout == 5.0
    assert service.query_rewriter.model.max_output_tokens == 32
    assert service.chat_model.timeout == settings.chat_timeout_seconds
    assert service.rag_rewrite_enabled is True
    assert service.rag_filter_enabled is True
