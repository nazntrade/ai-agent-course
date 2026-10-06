"""Answer-model adapter contract (SPEC 5.1, R6.1a).

Two implementations share this interface: the local llama-server provider and
the network DeepSeek provider. The core service never sees transport details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ChatResult:
    text: str
    model: str
    finish_reason: str | None
    usage: ChatUsage | None
    latency_ms: float
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict() if self.usage else None,
            "latency_ms": self.latency_ms,
            "parameters": dict(self.parameters),
        }


@dataclass
class ProviderStatus:
    provider: str
    reachable: bool
    model: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "reachable": self.reachable,
            "model": self.model,
            "detail": self.detail,
        }


class AnswerProvider(ABC):
    """Common interface for the two answer-model providers."""

    name: str

    @abstractmethod
    def identity(self) -> dict[str, Any]:
        """Identity of the factual answer model (never invented)."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether enough own configuration exists to attempt a request."""

    @abstractmethod
    def missing_config(self) -> list[str]:
        """Names of missing configuration parameters (no secret values)."""

    @abstractmethod
    def status(self) -> ProviderStatus: ...

    @abstractmethod
    def chat(
        self, messages: Sequence[ChatMessage], options: Mapping[str, Any] | None = None
    ) -> ChatResult: ...

    def preflight(self) -> dict[str, Any]:
        return {"reachable": False, "model_present": False}
