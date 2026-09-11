"""Neutral data containers shared by the agent, storage and UI layers.

These dataclasses carry no behaviour so they can be imported from any module
without introducing a dependency cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    """The answer text plus the statistics collected for that turn.

    ``summary_error`` is set when post-turn history compression failed: the
    turn itself was still saved, but the summary could not be updated.
    """

    text: str
    stats: TurnStats
    summary_error: str | None = None


@dataclass
class SummaryOutcome:
    """Result of one history-compression run.

    ``attempts`` holds the statistics of every executed provider call, even
    when the summary was not updated; ``text`` is set only on success and
    ``error`` explains a failure.
    """

    attempts: list[TurnStats] = field(default_factory=list)
    text: str | None = None
    error: str | None = None


def sum_optional(values):
    """Sum known values; return None when every value is None (never invent 0)."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def aggregate_stats(attempts) -> TurnStats:
    """None-aware aggregate of several attempts; assumptions joined with '; '.

    Costs are summed from the already-priced attempts instead of being
    recomputed from the aggregate, so a partial usage never fabricates a full
    cost.
    """
    attempts = list(attempts)
    if not attempts:
        return TurnStats()
    assumptions = []
    for attempt in attempts:
        if attempt.cost_assumption and attempt.cost_assumption not in assumptions:
            assumptions.append(attempt.cost_assumption)
    return TurnStats(
        request_tokens=sum_optional([a.request_tokens for a in attempts]),
        response_tokens=sum_optional([a.response_tokens for a in attempts]),
        total_tokens=sum_optional([a.total_tokens for a in attempts]),
        prompt_cache_hit_tokens=sum_optional(
            [a.prompt_cache_hit_tokens for a in attempts]
        ),
        prompt_cache_miss_tokens=sum_optional(
            [a.prompt_cache_miss_tokens for a in attempts]
        ),
        cost_usd=sum_optional([a.cost_usd for a in attempts]),
        finish_reason=attempts[-1].finish_reason,
        cost_assumption="; ".join(assumptions) if assumptions else None,
    )
