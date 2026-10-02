"""ChatQueryRewriter: a short bounded rewrite call over the chat contract.

The rewrite prompt contains only the rewrite instruction and the original
question. Reference facts, ready answers and neighbouring-mode history never
enter it (SPEC D23 7.2). Any provider failure becomes a fallback to the
original question with a stable reason, never an HTTP error.
"""

from __future__ import annotations

from time import perf_counter

from ..domain.contracts import ChatResult, QueryRewriter, RewriteResult
from ..domain.errors import KnowledgeError
from .prompts import REWRITE, PromptTemplate

_MAX_REWRITE_CHARS = 500
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


class ChatQueryRewriter(QueryRewriter):
    def __init__(self, model, *, template: PromptTemplate = REWRITE) -> None:
        self.model = model
        self.template = template

    def rewrite(self, question: str) -> RewriteResult:
        started = perf_counter()
        messages = self.template.build(question)
        try:
            result = self.model.chat(messages)
        except KnowledgeError as exc:
            latency = _elapsed(started)
            return self._fallback(question, getattr(exc, "code", "chat_error"), latency)
        except Exception:  # noqa: BLE001 - a rewrite failure must never break chat
            latency = _elapsed(started)
            return self._fallback(question, "rewrite_error", latency)

        latency = _elapsed(started)
        text = (result.text or "").strip()
        reason = self._invalid_reason(text, result)
        if reason is not None:
            return self._fallback(
                question,
                reason,
                latency,
                finish_reason=result.finish_reason,
                usage=result.usage,
            )
        return RewriteResult(
            original_query=question,
            search_query=text,
            attempted=True,
            used=True,
            fallback=False,
            reason=None,
            template_id=self.template.template_id,
            template_hash=self.template.content_hash(),
            finish_reason=result.finish_reason,
            usage=result.usage,
            latency_ms=latency,
        )

    def _invalid_reason(self, text: str, result: ChatResult) -> str | None:
        if result.finish_reason == "length":
            return "rewrite_invalid"
        if not text or "\n" in text or len(text) > _MAX_REWRITE_CHARS:
            return "rewrite_invalid"
        lowered = text.lower()
        if any(marker in lowered for marker in _REFUSAL_MARKERS):
            return "rewrite_invalid"
        return None

    def _fallback(
        self,
        question: str,
        reason: str,
        latency: float,
        *,
        finish_reason: str | None = None,
        usage=None,
    ) -> RewriteResult:
        return RewriteResult(
            original_query=question,
            search_query=question,
            attempted=True,
            used=False,
            fallback=True,
            reason=reason,
            template_id=self.template.template_id,
            template_hash=self.template.content_hash(),
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency,
        )


def _elapsed(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)
