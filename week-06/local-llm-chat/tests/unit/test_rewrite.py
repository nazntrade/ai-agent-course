"""D26-16/R1.3: query rewrite behavior falls back safely and rewrites on success."""

from __future__ import annotations

from app.providers.base import ChatResult, ChatUsage
from app.rag.rewrite import ChatQueryRewriter, no_rewrite


class StubModel:
    def __init__(self, text="", finish_reason="stop", raises=False):
        self._text = text
        self._finish = finish_reason
        self._raises = raises
        self.calls = 0

    def chat(self, messages, options=None):
        self.calls += 1
        if self._raises:
            raise RuntimeError("provider down")
        return ChatResult(text=self._text, model="m", finish_reason=self._finish, usage=ChatUsage(1, 1, 2), latency_ms=1.0)


def test_successful_rewrite_returns_search_query():
    result = ChatQueryRewriter(StubModel("capital of France")).rewrite("What is the capital of France?")
    assert result.used is True
    assert result.fallback is False
    assert result.search_query == "capital of France"


def test_multiline_rewrite_is_rejected_with_fallback():
    result = ChatQueryRewriter(StubModel("line one\nline two")).rewrite("q")
    assert result.fallback is True
    assert result.reason == "rewrite_invalid"
    assert result.search_query == "q"


def test_refusal_text_is_rejected():
    result = ChatQueryRewriter(StubModel("I cannot help with that")).rewrite("q")
    assert result.fallback is True
    assert result.reason == "rewrite_invalid"


def test_provider_error_falls_back_without_raising():
    result = ChatQueryRewriter(StubModel(raises=True)).rewrite("original")
    assert result.fallback is True
    assert result.reason == "rewrite_error"
    assert result.search_query == "original"


def test_no_rewrite_keeps_original():
    result = no_rewrite("hello")
    assert result.search_query == "hello"
    assert result.attempted is False
