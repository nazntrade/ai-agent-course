"""Pure user-profile model and payload block (stdlib only).

A user profile is a separate entity, not a memory item: it answers "how should
the assistant answer this particular user?". It owns the value object, the name
validation and the deterministic block inserted into the model payload; it
performs no I/O and imports neither Streamlit, storage, nor the provider client.
Invariants always outrank the profile and cannot be overridden by it.
"""

from __future__ import annotations

from dataclasses import dataclass

# The header is sent to the model, so it follows the Russian payload prompts of
# Days 9-10; the UI uses its own English labels. It states the priority rule
# explicitly so the model can never let a profile override the invariants.
PROFILE_BLOCK_TITLE = (
    "Профиль пользователя (как отвечать именно этому пользователю; "
    "инварианты выше по приоритету и не могут быть отменены профилем):"
)

# Field display order in the block: identity first, then the answer-shaping
# fields. Only non-empty fields are rendered.
_FIELD_LABELS = (
    ("name", "Профиль"),
    ("addressing", "Обращение"),
    ("style", "Стиль"),
    ("format", "Формат"),
    ("constraints", "Ограничения"),
    ("domain_context", "Контекст"),
)


@dataclass(frozen=True)
class UserProfile:
    """One user profile, manually assigned to a chat.

    The profile is data only: which chat it is applied to lives on the chat row,
    so the same profile can be shared by several chats.
    """

    id: int
    name: str
    addressing: str = ""
    style: str = ""
    format: str = ""
    constraints: str = ""
    domain_context: str = ""
    created_at: str | None = None
    updated_at: str | None = None


def validate_profile_name(name) -> str:
    """Return the stripped profile name or raise ``ValueError`` when it is empty."""
    if name is None:
        raise ValueError("Profile name must not be empty")
    cleaned = str(name).strip()
    if not cleaned:
        raise ValueError("Profile name must not be empty")
    return cleaned


def normalize_profile_name(name) -> str:
    """Fold a profile name for comparison: spaces collapsed, case-insensitive."""
    return " ".join(str(name or "").split()).casefold()


def format_profile_block(profile) -> str | None:
    """Render a profile as a deterministic system block.

    Returns ``None`` for a missing profile or an empty name, so the payload
    builder can omit the block entirely instead of sending an empty system
    message. Only non-empty stripped fields are rendered, in a fixed order.
    """
    if profile is None:
        return None
    name = (getattr(profile, "name", "") or "").strip()
    if not name:
        return None
    lines = [PROFILE_BLOCK_TITLE]
    for field, label in _FIELD_LABELS:
        value = (getattr(profile, field, "") or "").strip()
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)
