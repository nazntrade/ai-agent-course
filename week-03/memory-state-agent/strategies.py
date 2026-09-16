"""Pure context-strategy identifiers and normalization (stdlib only).

A single chat-level setting selects how the message payload is assembled: the
full history, the Day 9 rolling summary, a sliding window, sticky facts or a
branch line. This module is the single source of truth for the machine values
and their display labels, so storage, the agent and the UI never invent them.
It performs no I/O and imports nothing from the rest of the app.
"""

from __future__ import annotations

STRATEGY_FULL = "full"
STRATEGY_SUMMARY = "summary"
STRATEGY_SLIDING = "sliding"
STRATEGY_FACTS = "sticky_facts"
STRATEGY_BRANCHING = "branching"

DEFAULT_STRATEGY = STRATEGY_SUMMARY

DEFAULT_SLIDING_WINDOW = 6
DEFAULT_FACTS_WINDOW = 6

# Machine value first, human-readable display label second. The order is the
# order shown in the UI selector.
STRATEGY_CHOICES = (
    (STRATEGY_FULL, "Full history"),
    (STRATEGY_SUMMARY, "Summary (compression)"),
    (STRATEGY_SLIDING, "Sliding window"),
    (STRATEGY_FACTS, "Sticky Facts (fact memory)"),
    (STRATEGY_BRANCHING, "Branching"),
)

_LABELS = dict(STRATEGY_CHOICES)
_VALID = set(_LABELS)


def normalize_strategy(value) -> str:
    """Return a known strategy; an unknown value falls back to ``summary``.

    Legacy rows may carry ``NULL`` or a value written by a future version; the
    app must keep working instead of failing, so unknown strategies behave as
    the default.
    """
    if value in _VALID:
        return value
    return DEFAULT_STRATEGY


def strategy_label(value) -> str:
    """Return the display label for a strategy, normalizing unknown values."""
    return _LABELS[normalize_strategy(value)]


def normalize_window(value, default: int) -> int:
    """Coerce a window size to an integer of at least 1.

    ``None``, non-numeric and out-of-range values collapse to ``default``; a
    valid value below 1 is clamped up. This keeps the payload builders free of
    their own validation.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return 1
    return number


def summary_compression_enabled(strategy, summarize) -> bool:
    """Whether the Day 9 summary pipeline should run for this configuration.

    The strategy is authoritative for which payload is built, while the legacy
    ``summarize`` flag can independently disable compression; both must agree
    for a summarisation call to be made.
    """
    return strategy == STRATEGY_SUMMARY and bool(summarize)
