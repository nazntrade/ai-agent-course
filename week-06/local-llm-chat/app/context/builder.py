"""Context building (SPEC 5.3, R6.1c).

Assembles the prompt from dialogue memory/history and — only in RAG mode —
retrieved document fragments. No-RAG never adds documents or source references.
A character budget prevents silent context overflow.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..errors import InvalidRequest
from ..providers.base import ChatMessage

SYSTEM_NO_RAG = (
    "You are a helpful assistant. Answer the user directly and honestly. "
    "Do not invent document sources; none are provided."
)
SYSTEM_RAG = (
    "You are a helpful assistant. Use the provided document fragments when they "
    "are relevant and cite them by their source label. If the fragments do not "
    "answer the question, say so."
)


class ContextBuilder:
    def __init__(self, *, max_context_chars: int = 12000) -> None:
        self.max_context_chars = max_context_chars

    def build(
        self,
        *,
        question: str,
        history: Sequence[Mapping[str, Any]] = (),
        fragments: Sequence[Mapping[str, Any]] = (),
        rag_enabled: bool = False,
    ) -> tuple[list[ChatMessage], dict[str, Any]]:
        system = SYSTEM_RAG if rag_enabled else SYSTEM_NO_RAG
        messages: list[ChatMessage] = [ChatMessage("system", system)]

        for turn in history:
            role = str(turn.get("role", ""))
            text = str(turn.get("text", ""))
            if role in ("user", "assistant") and text:
                messages.append(ChatMessage(role, text))

        context_block = ""
        sources: list[dict[str, Any]] = []
        if rag_enabled and fragments:
            lines = ["Document fragments:"]
            for index, fragment in enumerate(fragments, start=1):
                label = str(fragment.get("label") or fragment.get("source") or f"fragment {index}")
                lines.append(f"[{index}] ({label}) {fragment.get('text', '')}")
                sources.append(
                    {
                        "index": index,
                        "label": label,
                        "source": fragment.get("source"),
                        "chunk_id": fragment.get("chunk_id"),
                        "score": fragment.get("score"),
                    }
                )
            context_block = "\n".join(lines)

        user_content = question
        if context_block:
            user_content = f"{context_block}\n\nQuestion: {question}"
        messages.append(ChatMessage("user", user_content))

        used = sum(len(m.content) for m in messages)
        if used > self.max_context_chars:
            # Trim oldest non-system turns rather than silently truncating the
            # current question; refuse only when the question itself cannot fit.
            while len(messages) > 2 and used > self.max_context_chars:
                removed = messages.pop(1)
                used -= len(removed.content)
            if used > self.max_context_chars:
                raise InvalidRequest(
                    "The question and instructions do not fit the context budget.",
                    details={"limit_chars": self.max_context_chars, "used_chars": used},
                )

        trace = {
            "rag_enabled": bool(rag_enabled),
            "sources": sources,
            "history_turns": len([m for m in messages if m.role in ("user", "assistant")]) - 1,
        }
        return messages, trace
