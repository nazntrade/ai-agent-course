"""Pure sticky-facts model, parsing and prompt assembly (stdlib only).

The model is asked to return a strict JSON object ``{"facts": [...]}``. This
module owns the provider contract (categories, statuses, limits, system prompt)
and the deterministic parsing/formatting of that response, so the agent and the
UI share one definition. It performs no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from stats import TurnStats

FACT_CATEGORIES = (
    "goal",
    "constraint",
    "preference",
    "decision",
    "agreement",
    "parameter",
    "other",
)

FACT_CATEGORY_LABELS = {
    "goal": "цель",
    "constraint": "ограничение",
    "preference": "предпочтение",
    "decision": "решение",
    "agreement": "договорённость",
    "parameter": "параметр",
    "other": "прочее",
}

FACT_STATUS_ACTIVE = "active"
FACT_STATUS_CANCELLED = "cancelled"
FACT_STATUS_REPLACED = "replaced"

# Budgets mirror the summary pipeline: a base call, then one larger retry when
# the model truncates. Reasoning models spend part of the visible budget on
# hidden reasoning, so the retry exists for the same reason as in the summary.
FACTS_MAX_TOKENS = 800
FACTS_RETRY_MAX_TOKENS = 1600
FACTS_TEMPERATURE = 0.2

FACTS_SYSTEM_PROMPT = (
    "Ты извлекаешь долговременные факты о пользователе из диалога. Верни СТРОГО "
    "один JSON-объект вида {\"facts\": [...]} и ничего больше. Каждый элемент — "
    "объект с полями: key (краткое имя факта), value (значение), category (одно "
    "из: goal, constraint, preference, decision, agreement, parameter, other), "
    "status (active или cancelled) и необязательное reason. "
    "Отмена или исправление ранее известного факта оформляется как status "
    "cancelled; не выдумывай факты, которых не было в диалоге. Если новых "
    "фактов нет, верни {\"facts\": []}."
)

_CATEGORY_ORDER = {category: index for index, category in enumerate(FACT_CATEGORIES)}


@dataclass(frozen=True)
class FactOperation:
    """One parsed instruction to add, replace or cancel a fact.

    ``value`` is empty for a cancellation; ``reason`` is optional and only used
    for cancellations.
    """

    key: str
    value: str
    category: str = "other"
    status: str = FACT_STATUS_ACTIVE
    reason: str | None = None


@dataclass
class Fact:
    """A persisted fact row."""

    id: int
    category: str
    key: str
    value: str
    status: str
    reason: str | None = None
    updated_at: str | None = None


@dataclass
class FactsOutcome:
    """Result of one facts-extraction run.

    ``attempts`` holds one ``(stats, ok)`` pair per executed provider call,
    where ``ok`` marks a validly parsed response (including an empty list); this
    lets the caller bill interrupted and invalid calls into the fail bucket.
    ``operations`` is ``None`` on failure and ``[]`` for a valid no-op.
    """

    attempts: list[tuple[TurnStats, bool]] = field(default_factory=list)
    operations: list[FactOperation] | None = None
    error: str | None = None


def parse_facts_response(text) -> list[FactOperation]:
    """Parse the model's JSON response into fact operations.

    A single Markdown code fence around the JSON is tolerated. Anything that is
    not a JSON object with a ``facts`` array of well-formed objects raises
    ``ValueError`` so the caller can keep the previous facts untouched. An
    empty list is a valid no-op.
    """
    if text is None:
        raise ValueError("Empty facts response")

    stripped = str(text).strip()
    if not stripped:
        raise ValueError("Empty facts response")

    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    stripped = "\n".join(lines).strip()

    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid facts JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Facts response must be a JSON object")
    raw_facts = payload.get("facts")
    if not isinstance(raw_facts, list):
        raise ValueError("Facts response must contain a 'facts' array")

    operations = []
    for index, item in enumerate(raw_facts):
        if not isinstance(item, dict):
            raise ValueError(f"Fact #{index} must be an object")
        raw_key = item.get("key")
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not key:
            raise ValueError(f"Fact #{index} has an empty key")
        status = item.get("status", FACT_STATUS_ACTIVE)
        if status not in (FACT_STATUS_ACTIVE, FACT_STATUS_CANCELLED):
            raise ValueError(f"Fact #{index} has an invalid status")
        category = item.get("category")
        if category not in FACT_CATEGORIES:
            category = "other"
        raw_value = item.get("value")
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if status == FACT_STATUS_ACTIVE and not value:
            raise ValueError(f"Fact #{index} has an empty value")
        reason = item.get("reason")
        reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
        operations.append(
            FactOperation(
                key=key,
                value=value,
                category=category,
                status=status,
                reason=reason,
            )
        )
    return operations


def format_facts_block(facts) -> str | None:
    """Render the active facts as a deterministic system block.

    Returns ``None`` when there are no active facts, so the payload builder can
    omit the block entirely. Only ``active`` facts are included; ordering is by
    category then key to keep the payload stable between calls.
    """
    active = [fact for fact in facts if getattr(fact, "status", None) == FACT_STATUS_ACTIVE]
    if not active:
        return None
    ordered = sorted(
        active,
        key=lambda fact: (
            _CATEGORY_ORDER.get(fact.category, len(FACT_CATEGORIES)),
            fact.key,
        ),
    )
    lines = ["Известные факты о пользователе (учитывай их, не противоречь):"]
    for fact in ordered:
        label = FACT_CATEGORY_LABELS.get(fact.category, fact.category)
        lines.append(f"- [{label}] {fact.key}: {fact.value}")
    return "\n".join(lines)


def build_facts_extraction_messages(active_facts, messages) -> list[dict]:
    """Build the prompt asking the model to update the known facts.

    The current active facts are supplied so the model can refine or cancel
    them; ``messages`` are the new (uncovered) dialogue messages. The output
    format is fixed by :data:`FACTS_SYSTEM_PROMPT`.
    """
    payload = [{"role": "system", "content": FACTS_SYSTEM_PROMPT}]
    if active_facts:
        lines = [
            f"- [{fact.category}] {fact.key}: {fact.value}" for fact in active_facts
        ]
        payload.append(
            {
                "role": "user",
                "content": "Текущие активные факты:\n" + "\n".join(lines),
            }
        )
    lines = [
        f"[{message.get('role', 'unknown')}]: {message.get('content', '')}"
        for message in messages
    ]
    payload.append(
        {"role": "user", "content": "Новые сообщения диалога:\n" + "\n".join(lines)}
    )
    return payload
