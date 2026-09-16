"""Centralised DeepSeek tariffs and turn cost estimation (stdlib only).

The cost is computed exclusively from exact API token counts: estimates never
contribute to a monetary figure. When no exact input/output tokens are known,
or the model has no configured tariff, the cost is ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from stats import TurnStats

# Official DeepSeek tariffs, USD per 1M tokens
# (source: https://api-docs.deepseek.com/quick_start/pricing, checked 2026-09-10).
# Both the canonical ``deepseek-flash`` and its legacy ``deepseek-v4-flash``
# alias are served by DeepSeek-V4.1-Flash and billed at the Flash price. The
# legacy key is retained so historical rows can still be priced.
FLASH_COST = {
    "input_cache_miss": {"off_peak": 0.15, "peak": 0.30},
    "input_cache_hit": {"off_peak": 0.003, "peak": 0.006},
    "output": {"off_peak": 0.60, "peak": 1.20},
}
PRO_COST = {
    "input_cache_miss": {"off_peak": 0.66, "peak": 1.32},
    "input_cache_hit": {"off_peak": 0.022, "peak": 0.044},
    "output": {"off_peak": 1.98, "peak": 3.96},
}

_MODEL_TARIFFS = {
    "deepseek-flash": FLASH_COST,
    "deepseek-v4-flash": FLASH_COST,
    "deepseek-v4-pro": PRO_COST,
}

CACHE_MISS_ASSUMPTION = (
    "all input priced at the cache-miss rate (conservative estimate: no cache "
    "hit/miss split was reported by the provider)"
)
CACHE_SPLIT_ASSUMPTION = "cache hit/miss split from usage applied"


@dataclass
class CostEstimate:
    """Result of :func:`estimate_cost`.

    ``cost_usd`` is ``None`` when there is no tariff or no exact token counts.
    ``window`` is ``"peak"``/``"off_peak"``/``None`` and ``assumption`` records
    how the input tokens were priced.
    """

    cost_usd: float | None
    tariff_known: bool
    cache_split_known: bool
    window: str | None
    assumption: str | None


def is_peak_time(dt: datetime | None = None) -> bool:
    """DeepSeek peak windows: [01:00,04:00) and [06:00,10:00) UTC, Mon-Fri."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.weekday() >= 5:  # Saturday/Sunday
        return False
    return (1 <= dt.hour < 4) or (6 <= dt.hour < 10)


def estimate_cost(model: str, stats: TurnStats, dt: datetime | None = None) -> CostEstimate:
    """Estimate the USD cost of a turn from its exact token counts.

    A model without a configured tariff yields ``cost_usd=None`` and
    ``tariff_known=False``. With a tariff but no exact ``request_tokens`` and
    ``response_tokens`` the cost is also ``None``. When both cache hit and miss
    counts are present the input is split across the two rates; otherwise the
    whole input is priced at the cache-miss rate (conservative).
    """
    tariff = _MODEL_TARIFFS.get(model)
    if tariff is None:
        return CostEstimate(
            cost_usd=None,
            tariff_known=False,
            cache_split_known=False,
            window=None,
            assumption=None,
        )

    request = stats.request_tokens
    response = stats.response_tokens
    if request is None or response is None:
        return CostEstimate(
            cost_usd=None,
            tariff_known=True,
            cache_split_known=False,
            window=None,
            assumption=None,
        )

    window = "peak" if is_peak_time(dt) else "off_peak"

    hit = stats.prompt_cache_hit_tokens
    miss = stats.prompt_cache_miss_tokens

    miss_rate = tariff["input_cache_miss"][window]
    hit_rate = tariff["input_cache_hit"][window]
    output_rate = tariff["output"][window]

    if hit is not None and miss is not None:
        input_usd = (miss * miss_rate + hit * hit_rate) / 1e6
        cache_split_known = True
        assumption = CACHE_SPLIT_ASSUMPTION
    else:
        input_usd = request * miss_rate / 1e6
        cache_split_known = False
        assumption = CACHE_MISS_ASSUMPTION

    output_usd = response * output_rate / 1e6

    return CostEstimate(
        cost_usd=input_usd + output_usd,
        tariff_known=True,
        cache_split_known=cache_split_known,
        window=window,
        assumption=assumption,
    )
