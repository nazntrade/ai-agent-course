"""Neutral data containers shared by the agent, storage and UI layers.

These dataclasses carry no behaviour so they can be imported from any module
without introducing a dependency cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TurnStats:
    """Per-turn token usage, cost and finish reason.

    Fields without the ``_est`` suffix are exact API values: ``None`` means the
    provider reported nothing for that field, so they are never invented.
    Fields with ``_est`` are deterministic local estimates computed with
    :func:`tokens.estimate_tokens`.
    """

    user_message_tokens_est: int | None = None
    request_tokens: int | None = None
    response_tokens: int | None = None
    total_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    finish_reason: str | None = None
    cost_usd: float | None = None
    cost_assumption: str | None = None
    assistant_tokens_est: int | None = None


@dataclass
class AskResult:
    """The answer text plus the statistics collected for that turn."""

    text: str
    stats: TurnStats
