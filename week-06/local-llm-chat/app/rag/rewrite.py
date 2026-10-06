"""Query rewrite for RAG search — transferred logic (SPEC R1.3, R5.2, D23).

A short, bounded rewrite call turns the user's question into one search query.
Any provider failure or a malformed rewrite falls back to the original question
with a stable reason; a rewrite never raises into the chat path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from ..providers.base import ChatMessage

REWRITE_TEMPLATE_ID = "rewrite-v1"
REWRITE_SYSTEM = (
    "Rewrite the user's question into one short search query for semantic "
    "retrieval. Return only the rewritten query on a single line: no "
    "explanation, no quotes, no reference answer."
)
MAX_REWRITE_CHARS = 500
_REFUSAL_MARKERS = (
    "i cannot",
    "i can't",
    "i can not",
    "as an ai",
    "unable to",
    "sorry",
    "не могу",
    "извините",
)


@dataclass
class RewriteResult:
    original_query: str
    search_query: str
    attempted: bool
    used: bool
    fallback: bool
    reason: str | None = None
    template_id: str = REWRITE_TEMPLATE_ID
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "used": self.used,
            "fallback": self.fallback,
            "reason": self.reason,
            "original_query": self.original_query,
            "search_query": self.search_query,
            "template_id": self.template_id,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
        }


def no_rewrite(question: str) -> RewriteResult:
    return RewriteResult(
        original_query=question,
        search_query=question,
        attempted=False,
        used=False,
        fallback=False,
        reason=None,
    )


class ChatQueryRewriter:
    """Rewrite via the answer model; always returns a result, never raises."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def rewrite(self, question: str) -> RewriteResult:
        started = perf_counter()
        messages = [ChatMessage("system", REWRITE_SYSTEM), ChatMessage("user", question)]
        try:
            result = self.model.chat(messages)
        except Exception:  # noqa: BLE001 - a rewrite failure must never break chat
            return self._fallback(question, "rewrite_error", _elapsed(started))

        latency = _elapsed(started)
        text = (result.text or "").strip()
        reason = self._invalid_reason(text, result.finish_reason)
        if reason is not None:
            return self._fallback(
                question,
                reason,
                latency,
                finish_reason=result.finish_reason,
                usage=result.usage.to_dict() if result.usage else None,
            )
        return RewriteResult(
            original_query=question,
            search_query=text,
            attempted=True,
            used=True,
            fallback=False,
            reason=None,
            finish_reason=result.finish_reason,
            usage=result.usage.to_dict() if result.usage else None,
            latency_ms=latency,
        )

    @staticmethod
    def _invalid_reason(text: str, finish_reason: str | None) -> str | None:
        if finish_reason == "length":
            return "rewrite_invalid"
        if not text or "\n" in text or len(text) > MAX_REWRITE_CHARS:
            return "rewrite_invalid"
        lowered = text.lower()
        if any(marker in lowered for marker in _REFUSAL_MARKERS):
            return "rewrite_invalid"
        return None

    @staticmethod
    def _fallback(
        question: str,
        reason: str,
        latency: float,
        *,
        finish_reason: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> RewriteResult:
        return RewriteResult(
            original_query=question,
            search_query=question,
            attempted=True,
            used=False,
            fallback=True,
            reason=reason,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency,
        )


def _elapsed(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)
