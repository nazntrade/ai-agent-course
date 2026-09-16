"""Pure explicit-memory model and payload blocks (stdlib only).

Working memory is scoped to a chat line (chat plus optional branch), long-term
memory is global. This module owns the value object, the input validation and
the deterministic text blocks that are inserted into the model payload; it
performs no I/O and imports neither Streamlit, storage, nor the provider client.
"""

from __future__ import annotations

from dataclasses import dataclass

MEMORY_SCOPE_WORKING = "working"
MEMORY_SCOPE_LONG_TERM = "long_term"

# Block headers are sent to the model, so they follow the Russian payload
# prompts of Days 9-10; the UI uses its own English labels.
WORKING_MEMORY_BLOCK_TITLE = "Рабочая память (краткосрочный контекст):"
LONG_TERM_MEMORY_BLOCK_TITLE = "Долговременная память (учитывай и не противоречь):"


@dataclass(frozen=True)
class MemoryItem:
    """One explicit memory entry, scoped by the caller.

    ``included`` marks whether the entry is currently sent to the model;
    excluded entries stay visible to the UI but never reach the payload.
    """

    id: int
    key: str
    value: str
    included: bool = True
    updated_at: str | None = None


def validate_memory_key(key) -> str:
    """Return the stripped key or raise ``ValueError`` when it is empty."""
    if key is None:
        raise ValueError("Memory key must not be empty")
    cleaned = str(key).strip()
    if not cleaned:
        raise ValueError("Memory key must not be empty")
    return cleaned


def validate_memory_value(value) -> str:
    """Return the stripped value or raise ``ValueError`` when it is empty."""
    if value is None:
        raise ValueError("Memory value must not be empty")
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError("Memory value must not be empty")
    return cleaned


def _item_id(item):
    """Sort key that keeps items without an id stable instead of crashing."""
    item_id = getattr(item, "id", None)
    return (item_id is None, item_id if item_id is not None else 0)


def format_memory_block(items, title) -> str | None:
    """Render included items as a deterministic system block.

    Returns ``None`` when the set is empty or every item is excluded, so the
    payload builder can omit the block entirely. Ordering is by item id.
    """
    included = [item for item in items if getattr(item, "included", True)]
    if not included:
        return None
    lines = [title]
    for item in sorted(included, key=_item_id):
        lines.append(f"- {item.key}: {item.value}")
    return "\n".join(lines)


def build_memory_blocks(working_items, long_term_items) -> list[str]:
    """Return the payload blocks in fixed order: working, then long-term.

    Empty or fully excluded scopes are skipped, so an absent memory layer never
    adds an empty system message.
    """
    blocks = []
    working = format_memory_block(working_items, WORKING_MEMORY_BLOCK_TITLE)
    if working:
        blocks.append(working)
    long_term = format_memory_block(long_term_items, LONG_TERM_MEMORY_BLOCK_TITLE)
    if long_term:
        blocks.append(long_term)
    return blocks


def insert_system_blocks(payload, blocks) -> list[dict]:
    """Insert system blocks right after the first system message.

    With no blocks the original payload is returned unchanged. Otherwise a new
    list is built (the input is never mutated) and the blocks are inserted in
    the given order. When the payload has no system message the blocks are
    prepended.
    """
    if not blocks:
        return payload
    result = [dict(message) for message in payload]
    insert_at = 0
    for index, message in enumerate(result):
        if message.get("role") == "system":
            insert_at = index + 1
            break
    extra = [{"role": "system", "content": block} for block in blocks]
    result[insert_at:insert_at] = extra
    return result
