"""Task memory extraction and the verifiable ``validate_patch`` contract.

Extraction is deterministic and offline: it recognises explicit user phrasings
(goal, constraints, term, clarification) and turns them into typed
:class:`MemoryOperation` candidates. Nothing is confirmed without non-empty
grounds that point at a real USER turn of the same dialogue. Assistant text,
retrieved documents and model guesses can never become grounds (SPEC D25 6.4,
7.3).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from ..domain.contracts import MemoryOperation, TaskMemory

# Explicit, testable trigger markers. A bare pronoun alone never confirms a
# memory item; ambiguity is reported instead of guessed.
GOAL_MARKERS = ("цель:", "моя цель:", "goal:", "my goal:")
CONSTRAINT_MARKERS = ("условие:", "constraint:", "требование:")
CANCEL_MARKERS = ("отмени условие", "убери условие", "отменить условие", "cancel constraint", "забудь про это")
UPDATE_MARKERS = ("измени условие", "замени условие", "update constraint", "поменяй условие")
TERM_MARKERS = ("термин:", "term:")
CLARIFICATION_MARKERS = ("уточнение:", "clarification:")
NEW_VALUE_MARKERS = ("теперь",)
_QUOTED = re.compile(r'[«"\u201c\u201d](.+?)[»"\u201c\u201d]')

# Ordinary explicit formulations without a special marker, e.g.
# ``Хочу разобраться в архитектуре агента. Объясняй кратко, по-русски и без кода.``
# A goal is a first-person statement or an explicit goal label; conditions come
# from an imperative list whose items look like verifiable user conditions. Only
# such explicit items are accepted, so a plain request as ``Расскажи про
# планирование`` never turns into confirmed memory (SPEC D25 7.1, 7.3).
GOAL_STATEMENTS = (
    re.compile(r"^(?:я\s+)?хочу\s+(?P<goal>.+)$", re.IGNORECASE),
    re.compile(r"^мне\s+нужно\s+(?P<goal>.+)$", re.IGNORECASE),
    re.compile(r"^(?:моя\s+)?(?:цель|задача)\s*[—:-]\s*(?P<goal>.+)$", re.IGNORECASE),
)
CONDITION_IMPERATIVE = re.compile(
    r"^(?:объясняй|отвечай|пиши|говори|давай|рассказывай)\s+(?P<body>.+)$",
    re.IGNORECASE,
)
KNOWN_CONDITIONS = frozenset(
    {"кратко", "подробно", "коротко", "по-русски", "по-английски", "без кода", "только python"}
)
_UPDATE_TARGET_STOPWORDS = frozenset(
    {
        "без", "только", "можно", "и", "но", "не", "по", "с", "в", "на", "это",
        "the", "a", "an", "with", "only", "no",
    }
)


def empty_memory(dialogue_id: str) -> TaskMemory:
    return TaskMemory(dialogue_id=dialogue_id, version=0)


def task_memory(value: Mapping[str, Any] | TaskMemory) -> TaskMemory:
    if isinstance(value, TaskMemory):
        return value
    return TaskMemory.from_dict(value)


def _after_marker(text: str, marker: str) -> str:
    return text[len(marker):].strip().strip('":').strip()


def _item_id(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def extract(
    memory: Mapping[str, Any] | TaskMemory,
    message: str,
    *,
    ground_turn_id: str,
    known_user_turn_ids: Iterable[str],
    now: str,
) -> dict[str, Any]:
    """Return candidate operations plus any clarification request.

    The current user message is ``ground_turn_id``; it must already be a known
    user turn id supplied by the caller so the grounds are verifiable.
    """

    current = task_memory(memory)
    text = str(message or "").strip()
    lower = text.lower()
    known = set(known_user_turn_ids)
    operations: list[MemoryOperation] = []
    clarification: dict[str, Any] | None = None

    if ground_turn_id not in known:
        # The caller must register the current user turn before extraction.
        return {"operations": [], "clarification": None, "rejected": [], "unregistered_ground": True}

    if _starts(lower, GOAL_MARKERS):
        goal_text = _after_marker(text, _matched_marker(text, GOAL_MARKERS))
        if goal_text:
            operations.append(
                MemoryOperation(op="set_goal", text=goal_text, grounds=[ground_turn_id])
            )
    elif _starts(lower, CONSTRAINT_MARKERS):
        constraint_text = _after_marker(text, _matched_marker(text, CONSTRAINT_MARKERS))
        if constraint_text:
            operations.append(
                MemoryOperation(
                    op="add_constraint", text=constraint_text, grounds=[ground_turn_id]
                )
            )
    elif any(marker in lower for marker in CANCEL_MARKERS):
        target = _parse_quoted(text)
        active = current.active_constraints()
        if target is None:
            if len(active) == 1:
                target_id = active[0]["item_id"]
                operations.append(
                    MemoryOperation(
                        op="cancel_constraint", target_item_id=target_id, grounds=[ground_turn_id]
                    )
                )
            elif not active:
                clarification = {
                    "question": "Which condition should I cancel?",
                    "reason": "no_active_constraint",
                }
            else:
                clarification = {
                    "question": "Which of the active conditions should I cancel?",
                    "reason": "ambiguous_target",
                }
        else:
            operations.append(
                MemoryOperation(op="cancel_constraint", match_text=target, grounds=[ground_turn_id])
            )
    elif any(marker in lower for marker in UPDATE_MARKERS):
        quoted = _QUOTED.findall(text)
        if len(quoted) >= 2:
            operations.append(
                MemoryOperation(
                    op="update_constraint",
                    match_text=quoted[0].strip(),
                    text=quoted[1].strip(),
                    grounds=[ground_turn_id],
                )
            )
        else:
            clarification = {
                "question": "Which condition should change, and to what?",
                "reason": "incomplete_update",
            }
    elif _starts(lower, NEW_VALUE_MARKERS):
        marker = _matched_marker(text, NEW_VALUE_MARKERS)
        new_text = _after_marker(text, marker)
        active = current.active_constraints()
        if new_text and not active:
            # "Now …" with no prior condition is a new condition, not a change.
            operations.append(
                MemoryOperation(op="add_constraint", text=new_text, grounds=[ground_turn_id])
            )
        elif new_text:
            target = _infer_update_target(active, new_text)
            if target is not None:
                operations.append(
                    MemoryOperation(
                        op="update_constraint",
                        target_item_id=target["item_id"],
                        text=new_text,
                        grounds=[ground_turn_id],
                    )
                )
            elif len(active) == 1:
                operations.append(
                    MemoryOperation(
                        op="update_constraint",
                        target_item_id=active[0]["item_id"],
                        text=new_text,
                        grounds=[ground_turn_id],
                    )
                )
            else:
                clarification = {
                    "question": "Which of the active conditions should change?",
                    "reason": "ambiguous_target",
                }
    elif _starts(lower, TERM_MARKERS):
        body = _after_marker(text, _matched_marker(text, TERM_MARKERS))
        term, definition = _split_pair(body)
        if term and definition:
            operations.append(
                MemoryOperation(
                    op="add_term",
                    term=term,
                    definition=definition,
                    grounds=[ground_turn_id],
                )
            )
    elif _starts(lower, CLARIFICATION_MARKERS):
        body = _after_marker(text, _matched_marker(text, CLARIFICATION_MARKERS))
        question, answer = _split_pair(body)
        if question and answer:
            operations.append(
                MemoryOperation(
                    op="add_clarification",
                    question=question,
                    answer=answer,
                    grounds=[ground_turn_id],
                )
            )
    else:
        operations.extend(_plain_operations(current, text, ground_turn_id))

    return {"operations": operations, "clarification": clarification, "rejected": []}


def _matched_marker(text: str, markers: Sequence[str]) -> str:
    lower = text.lower()
    for marker in markers:
        if lower.startswith(marker):
            return text[: len(marker)]
    return ""


def _starts(lower: str, markers: Sequence[str]) -> bool:
    return any(lower.startswith(marker) for marker in markers)


def _parse_quoted(text: str) -> str | None:
    quoted = _QUOTED.findall(text)
    if quoted:
        return quoted[0].strip()
    return None


def _split_pair(body: str) -> tuple[str, str]:
    for separator in ("=", " — ", " - ", "->", "→"):
        if separator in body:
            left, _, right = body.partition(separator)
            return left.strip().strip('"'), right.strip().strip('"')
    return body.strip().strip('"'), ""


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text or "") if part.strip()]


def _match_goal(sentence: str) -> str | None:
    for pattern in GOAL_STATEMENTS:
        match = pattern.match(sentence.strip())
        if match:
            goal = match.group("goal").strip().strip('".').strip()
            if goal:
                return goal
    return None


def _looks_like_condition(item: str) -> bool:
    value = item.strip().strip('".;,').lower()
    if not value:
        return False
    if value in KNOWN_CONDITIONS:
        return True
    if re.match(r"^без\s+\S+", value):
        return True
    if re.match(r"^только\s+\S+", value):
        return True
    if value.startswith("по-") and len(value) > 4:
        return True
    return False


def _extract_conditions(sentence: str) -> list[str]:
    match = CONDITION_IMPERATIVE.match(sentence.strip())
    body = match.group("body") if match else sentence.strip()
    items = re.split(r",|;|\s+и\s+", body)
    result: list[str] = []
    for item in items:
        cleaned = item.strip().strip('".;,').strip()
        if _looks_like_condition(cleaned):
            result.append(cleaned)
    return result


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.split(r"[\s,;:.!?()«»\"'\[\]]+", (text or "").lower())
        if token
    ]


def _word_root(token: str) -> str:
    value = re.sub(r"[^\w\-]+", "", token.lower())
    if len(value) <= 3:
        return value
    for suffix in (
        "ами", "ями", "ого", "ему", "ому", "ыми", "ими", "иях", "ах", "ях", "ов",
        "ев", "ей", "ой", "ый", "ий", "ая", "яя", "ое", "ее", "ые", "ие", "ам",
        "ям", "ом", "ем", "у", "ю", "а", "я", "ы", "и", "е", "о",
    ):
        if value.endswith(suffix) and len(value) - len(suffix) >= 3:
            return value[: -len(suffix)]
    return value


def _infer_update_target(
    active: Sequence[Mapping[str, Any]], new_text: str
) -> dict[str, Any] | None:
    """Find the single active condition the new value clearly replaces.

    ``Теперь код можно, но только Python`` shares the root ``код`` with the
    active condition ``без кода`` and therefore pinpoints it even when several
    conditions are active. Two plausible targets stay ambiguous and ask for a
    clarification instead of guessing.
    """

    new_roots = {_word_root(token) for token in _tokens(new_text)} - {""}
    if not new_roots:
        return None
    matches: list[dict[str, Any]] = []
    for item in active:
        item_roots = {
            _word_root(token)
            for token in _tokens(str(item.get("text") or ""))
            if token.lower() not in _UPDATE_TARGET_STOPWORDS
        } - {""}
        if item_roots & new_roots:
            matches.append(dict(item))
    return matches[0] if len(matches) == 1 else None


def _plain_operations(
    memory: TaskMemory, text: str, ground_turn_id: str
) -> list[MemoryOperation]:
    operations: list[MemoryOperation] = []
    goal_seen = bool((memory.goal or {}).get("text"))
    for sentence in _sentences(text):
        goal = _match_goal(sentence)
        if goal and not goal_seen:
            operations.append(
                MemoryOperation(op="set_goal", text=goal, grounds=[ground_turn_id])
            )
            goal_seen = True
            continue
        for condition in _extract_conditions(sentence):
            operations.append(
                MemoryOperation(op="add_constraint", text=condition, grounds=[ground_turn_id])
            )
    return operations


def validate_patch(
    memory: Mapping[str, Any] | TaskMemory,
    operations: Sequence[MemoryOperation | Mapping[str, Any]],
    *,
    known_user_turn_ids: Iterable[str],
    now: str,
) -> tuple[TaskMemory, dict[str, Any]]:
    """Apply typed operations that carry verifiable USER-message grounds.

    Returns the new memory (unchanged when nothing applies) and a report with
    ``applied``, ``rejected`` and ``clarification``. No operation is dropped
    silently: every rejection carries a concrete reason.
    """

    current = task_memory(memory)
    known = set(known_user_turn_ids)
    result = TaskMemory.from_dict(current.to_dict())
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    clarification: dict[str, Any] | None = None

    for raw in operations:
        operation = raw if isinstance(raw, MemoryOperation) else MemoryOperation.from_dict(raw)
        op = operation.op
        if op == "request_clarification":
            clarification = {
                "question": operation.question or "Could you clarify?",
                "reason": operation.reason or "ambiguous",
            }
            continue
        grounds = [str(item) for item in operation.grounds if str(item).strip()]
        if not grounds:
            rejected.append({"op": op, "reason": "missing_grounds"})
            continue
        unknown = [item for item in grounds if item not in known]
        if unknown:
            rejected.append({"op": op, "reason": "unknown_ground", "grounds": unknown})
            continue

        if op == "set_goal":
            if not (operation.text or "").strip():
                rejected.append({"op": op, "reason": "empty_text"})
                continue
            result.goal = {
                "item_id": (result.goal or {}).get("item_id") or _item_id("goal"),
                "text": operation.text.strip(),
                "status": "confirmed",
                "grounds": grounds,
                "updated_at": now,
            }
            applied.append({"op": op, "item_id": result.goal["item_id"]})
        elif op == "add_constraint":
            if not (operation.text or "").strip():
                rejected.append({"op": op, "reason": "empty_text"})
                continue
            item = {
                "item_id": _item_id("con"),
                "text": operation.text.strip(),
                "status": "active",
                "grounds": grounds,
                "updated_at": now,
                "superseded_by": None,
            }
            result.constraints.append(item)
            applied.append({"op": op, "item_id": item["item_id"]})
        elif op in ("update_constraint", "cancel_constraint"):
            target, reason = _find_constraint(result, operation)
            if target is None:
                rejected.append({"op": op, "reason": reason or "target_not_found"})
                continue
            if op == "cancel_constraint":
                target["status"] = "cancelled"
                target["grounds"] = grounds
                target["updated_at"] = now
                applied.append({"op": op, "item_id": target["item_id"]})
            else:
                if not (operation.text or "").strip():
                    rejected.append({"op": op, "reason": "empty_text"})
                    continue
                replacement = {
                    "item_id": _item_id("con"),
                    "text": operation.text.strip(),
                    "status": "active",
                    "grounds": grounds,
                    "updated_at": now,
                    "superseded_by": None,
                }
                target["status"] = "cancelled"
                target["superseded_by"] = replacement["item_id"]
                target["updated_at"] = now
                result.constraints.append(replacement)
                applied.append({"op": op, "item_id": replacement["item_id"], "cancelled": target["item_id"]})
        elif op == "add_clarification":
            if not (operation.question or "").strip():
                rejected.append({"op": op, "reason": "empty_question"})
                continue
            item = {
                "item_id": _item_id("clar"),
                "question": operation.question.strip(),
                "answer": (operation.answer or "").strip(),
                "grounds": grounds,
                "updated_at": now,
            }
            result.clarifications.append(item)
            applied.append({"op": op, "item_id": item["item_id"]})
        elif op == "add_term":
            if not (operation.term or "").strip():
                rejected.append({"op": op, "reason": "empty_term"})
                continue
            item = {
                "item_id": _item_id("term"),
                "term": operation.term.strip(),
                "definition": (operation.definition or "").strip(),
                "grounds": grounds,
                "updated_at": now,
            }
            result.terms.append(item)
            applied.append({"op": op, "item_id": item["item_id"]})
        else:
            rejected.append({"op": op, "reason": "unknown_op"})

    # Version grows monotonically only when a confirmed operation applied.
    if applied:
        result.version = int(current.version) + 1
    else:
        result.version = int(current.version)
    report = {"applied": applied, "rejected": rejected, "clarification": clarification}
    return result, report


def _find_constraint(
    memory: TaskMemory, operation: MemoryOperation
) -> tuple[dict[str, Any] | None, str | None]:
    active = memory.active_constraints()
    if operation.target_item_id:
        for item in active:
            if item.get("item_id") == operation.target_item_id:
                return item, None
        return None, "target_not_found"
    if operation.match_text:
        needle = operation.match_text.strip().lower()
        matches = [item for item in active if needle and needle in str(item.get("text", "")).lower()]
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, "target_not_found"
        return None, "ambiguous_target"
    return None, "target_not_found"


__all__ = [
    "CANCEL_MARKERS",
    "CLARIFICATION_MARKERS",
    "CONSTRAINT_MARKERS",
    "GOAL_MARKERS",
    "NEW_VALUE_MARKERS",
    "TERM_MARKERS",
    "UPDATE_MARKERS",
    "empty_memory",
    "extract",
    "task_memory",
    "validate_patch",
]
