"""Canonical model identifiers and their display names (stdlib only).

This module is the single source of truth for model naming. It intentionally
imports nothing from ``agent``/``storage``/``pricing``/Streamlit so both
persistence and the UI can share it without creating a dependency cycle.
"""

from __future__ import annotations

DEFAULT_MODEL = "deepseek-flash"

# Provider-side legacy identifiers that map onto the canonical model. Matching
# is exact and case-sensitive so unrelated custom model IDs are never rewritten.
LEGACY_MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
}

MODEL_DISPLAY_NAMES = {
    "deepseek-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
}


def normalize_model(model: str) -> str:
    """Return the canonical model ID for a known legacy alias.

    Unknown IDs are returned unchanged: custom identifiers must survive
    round-trips through storage and the API payload untouched.
    """
    return LEGACY_MODEL_ALIASES.get(model, model)


def display_name(model: str) -> str | None:
    """Return the human-readable name, or ``None`` when the model is unknown.

    Legacy aliases resolve to the canonical name; any unrecognised ID yields
    ``None`` so the caller can show its own fallback instead of inventing a name.
    """
    return MODEL_DISPLAY_NAMES.get(normalize_model(model))
