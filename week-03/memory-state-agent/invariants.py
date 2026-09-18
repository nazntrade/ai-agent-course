"""Pure structural-invariant domain (Day 14).

An invariant is a durable rule the agent refuses to break even when the user
asks it directly. It is not memory, not a profile and not task state: it
answers "which decisions and actions are forbidden at all". This module owns
the value object, the validation/normalization, the applicability selection,
the deterministic payload block, the conflict predicates and the refusal
message. It performs no I/O and imports neither Streamlit, nor SQL, nor the
provider client; only the action/event constants are reused from :mod:`tasks`.

The domain distinguishes the code-enforced ``hard`` rules from ``advisory``
ones: ``hard`` rules are checked by the application before an action, before a
provider call and before a commit, while advisory rules only contribute to the
model context and the human review. Only ``hard`` rules can produce a conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tasks import ACTION_RUN_VALIDATION, EVENT_VALIDATION_PASSED

# --- Scopes, enforcement and kinds ----------------------------------------

SCOPE_GLOBAL = "global"
SCOPE_TASK = "task"
SCOPES = (SCOPE_GLOBAL, SCOPE_TASK)

ENFORCEMENT_HARD = "hard"
ENFORCEMENT_ADVISORY = "advisory"
ENFORCEMENTS = (ENFORCEMENT_HARD, ENFORCEMENT_ADVISORY)

KIND_GUARD = "guard"
KIND_DATA = "data"
KIND_POLICY = "policy"
KINDS = (KIND_GUARD, KIND_DATA, KIND_POLICY)

# A check is an extra code-enforced predicate attached to a hard rule. An empty
# check kind means "no structural check": the rule uses its trigger/guard
# surfaces only.
CHECK_NONE = ""
CHECK_NO_VALIDATION_BYPASS = "no_validation_bypass"
CHECK_NO_BULK_RESET = "no_bulk_reset"
CHECK_KINDS = (CHECK_NONE, CHECK_NO_VALIDATION_BYPASS, CHECK_NO_BULK_RESET)

SOURCE_SEED = "seed"
SOURCE_USER = "user"
SOURCES = (SOURCE_SEED, SOURCE_USER)

# Journal event types of ``invariant_events``.
EVENT_CREATED = "created"
EVENT_UPDATED = "updated"
EVENT_ACTIVATED = "activated"
EVENT_DEACTIVATED = "deactivated"
EVENT_CONFLICT = "conflict"
EVENT_TYPES = (
    EVENT_CREATED,
    EVENT_UPDATED,
    EVENT_ACTIVATED,
    EVENT_DEACTIVATED,
    EVENT_CONFLICT,
)

# Conflict phases: which enforcement point refused the request.
PHASE_REQUEST = "request"
PHASE_ACTION = "action"
PHASE_COMMIT = "commit"
PHASES = (PHASE_REQUEST, PHASE_ACTION, PHASE_COMMIT)

DECISION_REFUSED = "refused"

# A user request stored in a conflict event is truncated, because a chat turn
# can be long and the journal only needs the matching instruction.
CONFLICT_REQUEST_MAX_LENGTH = 200

STRUCTURAL_INVARIANTS_BLOCK_TITLE = (
    "Структурные инварианты (жёсткие правила, соблюдай всегда):"
)

_CODE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# Operations that a bulk-reset check treats as a forbidden mass wipe.
BULK_RESET_OPERATIONS = ("reset_all", "wipe_all", "clear_all", "bulk_delete")


@dataclass
class Invariant:
    """One durable rule (one row of ``invariants``).

    ``code`` is the stable human identifier and is immutable after creation;
    ``version`` grows by one on every edit. ``scope=task`` requires ``task_id``
    and a global rule must not carry one. A ``hard`` rule must declare at least
    one enforcement surface (``triggers`` or a guard list); an ``advisory`` rule
    may only carry triggers and is never code-enforced.
    """

    id: int | None = None
    code: str = ""
    title: str = ""
    text: str = ""
    scope: str = SCOPE_GLOBAL
    task_id: int | None = None
    enforcement: str = ENFORCEMENT_ADVISORY
    kind: str = KIND_POLICY
    check_kind: str = CHECK_NONE
    triggers: tuple = ()
    guard_actions: tuple = ()
    guard_events: tuple = ()
    alternative: str = ""
    version: int = 1
    is_active: bool = True
    source: str = SOURCE_USER
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class InvariantConflict:
    """A refused request/action/transition caused by one hard invariant."""

    invariant: Invariant
    phase: str
    trigger: str = ""
    action: str | None = None
    event_type: str | None = None
    blocked: str = ""

    @property
    def code(self) -> str:
        return self.invariant.code


class InvariantConflictError(Exception):
    """Raised by the agent when a hard invariant refuses the request.

    Nothing is written when this error is raised: the caller must not save a
    message, a turn or a statistic for the refused request.
    """

    def __init__(self, conflict: InvariantConflict):
        self.conflict = conflict
        super().__init__(format_conflict_message(conflict))


# --- Validation and normalization -----------------------------------------


def validate_code(code) -> str:
    """Return the stripped code or raise ``ValueError`` when it is invalid."""
    cleaned = str(code or "").strip()
    if not cleaned or _CODE_PATTERN.match(cleaned) is None:
        raise ValueError(
            "Invariant code must match [A-Za-z0-9][A-Za-z0-9._-]* "
            f"and not be empty: {code!r}"
        )
    return cleaned


def _clean_tuple(values) -> tuple:
    """Return a tuple of non-empty stripped strings, preserving order."""
    cleaned = []
    for value in values or ():
        text = str(value).strip()
        if text:
            cleaned.append(text)
    return tuple(cleaned)


def normalize_invariant(invariant) -> Invariant:
    """Validate ``invariant`` and return a normalized copy, or raise."""
    if not isinstance(invariant, Invariant):
        raise ValueError("An Invariant instance is required")

    code = validate_code(invariant.code)

    title = str(invariant.title or "").strip()
    if not title:
        raise ValueError("Invariant title must not be empty")
    text = str(invariant.text or "").strip()
    if not text:
        raise ValueError("Invariant text must not be empty")

    if invariant.scope not in SCOPES:
        raise ValueError(
            f"Invariant scope must be one of {SCOPES}, got {invariant.scope!r}"
        )
    scope = invariant.scope

    if invariant.enforcement not in ENFORCEMENTS:
        raise ValueError(
            "Invariant enforcement must be one of "
            f"{ENFORCEMENTS}, got {invariant.enforcement!r}"
        )
    enforcement = invariant.enforcement

    if invariant.kind not in KINDS:
        raise ValueError(
            f"Invariant kind must be one of {KINDS}, got {invariant.kind!r}"
        )
    kind = invariant.kind

    check_kind = invariant.check_kind or CHECK_NONE
    if check_kind not in CHECK_KINDS:
        raise ValueError(
            f"Invariant check_kind must be one of {CHECK_KINDS}, got {check_kind!r}"
        )

    task_id = invariant.task_id
    if scope == SCOPE_TASK:
        if task_id is None:
            raise ValueError("A task-scoped invariant requires task_id")
        task_id = int(task_id)
    else:
        if task_id is not None:
            raise ValueError("A global invariant must not carry task_id")
        task_id = None

    triggers = _clean_tuple(invariant.triggers)
    guard_actions = _clean_tuple(invariant.guard_actions)
    guard_events = _clean_tuple(invariant.guard_events)

    if enforcement == ENFORCEMENT_ADVISORY:
        if check_kind:
            raise ValueError("An advisory invariant cannot declare check_kind")
        if guard_actions or guard_events:
            raise ValueError(
                "An advisory invariant cannot declare guard actions or guard events"
            )
    else:
        if not (triggers or guard_actions or guard_events):
            raise ValueError(
                "A hard invariant needs at least one surface: "
                "triggers, guard_actions or guard_events"
            )

    return replace(
        invariant,
        code=code,
        title=title,
        text=text,
        scope=scope,
        task_id=task_id,
        enforcement=enforcement,
        kind=kind,
        check_kind=check_kind,
        triggers=triggers,
        guard_actions=guard_actions,
        guard_events=guard_events,
        alternative=str(invariant.alternative or "").strip(),
        version=max(1, int(invariant.version or 1)),
        is_active=bool(invariant.is_active),
        source=(
            invariant.source if invariant.source in SOURCES else SOURCE_USER
        ),
    )


# --- Applicability and formatting -----------------------------------------


def _applicable_sort_key(invariant) -> tuple:
    """Global before task, hard before advisory, then the stable code."""
    return (
        0 if invariant.scope == SCOPE_GLOBAL else 1,
        0 if invariant.enforcement == ENFORCEMENT_HARD else 1,
        str(invariant.code),
    )


def select_applicable(invariants, task_id=None) -> list:
    """Return the active invariants that apply to ``task_id``, sorted.

    A global rule always applies; a task rule only applies when its
    ``task_id`` matches. Inactive rules are never selected.
    """
    selected = []
    for invariant in invariants or ():
        if not getattr(invariant, "is_active", True):
            continue
        scope = getattr(invariant, "scope", SCOPE_GLOBAL)
        if scope == SCOPE_GLOBAL:
            selected.append(invariant)
        elif scope == SCOPE_TASK and task_id is not None:
            if getattr(invariant, "task_id", None) == task_id:
                selected.append(invariant)
    return sorted(selected, key=_applicable_sort_key)


def format_structural_invariants_block(invariants) -> str | None:
    """Render the applicable invariants as the structural system block.

    Returns ``None`` when there is nothing to send, so a chat without
    invariants keeps its payload byte-identical. The block lists every active
    applicable rule with its code, enforcement and scope; the alternative is
    included because the model must offer a lawful way forward instead of just
    refusing.
    """
    selected = [
        invariant
        for invariant in (invariants or ())
        if getattr(invariant, "is_active", True)
    ]
    if not selected:
        return None
    selected = sorted(selected, key=_applicable_sort_key)

    lines = [STRUCTURAL_INVARIANTS_BLOCK_TITLE]
    for invariant in selected:
        scope = invariant.scope
        if scope == SCOPE_TASK and invariant.task_id is not None:
            scope = f"{scope}:{invariant.task_id}"
        lines.append(
            f"- [{invariant.code}] {invariant.title} "
            f"({invariant.enforcement}, {scope}, v{invariant.version})"
        )
        text = str(invariant.text or "").strip()
        if text:
            lines.append(f"  {text}")
        alternative = str(invariant.alternative or "").strip()
        if alternative:
            lines.append(f"  If blocked, the allowed alternative: {alternative}")
    return "\n".join(lines)


# --- Conflict predicates ---------------------------------------------------


def _hard(invariants):
    return [
        invariant
        for invariant in invariants or ()
        if getattr(invariant, "enforcement", None) == ENFORCEMENT_HARD
        and getattr(invariant, "is_active", True)
    ]


def _matches(text: str, trigger: str) -> bool:
    return str(trigger).casefold() in str(text).casefold()


def find_request_conflict(invariants, message) -> InvariantConflict | None:
    """Return the first hard invariant whose trigger matches ``message``.

    Only hard rules block: an advisory rule is context and self-check, never a
    code-enforced refusal.
    """
    text = str(message or "")
    if not text.strip():
        return None
    for invariant in _hard(invariants):
        for trigger in getattr(invariant, "triggers", ()) or ():
            if _matches(text, trigger):
                return InvariantConflict(
                    invariant=invariant,
                    phase=PHASE_REQUEST,
                    trigger=str(trigger),
                    blocked=(
                        "the request matches the forbidden instruction "
                        f'"{trigger}"'
                    ),
                )
    return None


def find_action_conflict(invariants, action) -> InvariantConflict | None:
    """Return the first hard invariant that forbids ``action``."""
    if not action:
        return None
    for invariant in _hard(invariants):
        if action in (getattr(invariant, "guard_actions", ()) or ()):
            return InvariantConflict(
                invariant=invariant,
                phase=PHASE_ACTION,
                trigger=str(action),
                action=action,
                blocked=f'the action "{action}" is forbidden by this invariant',
            )
    return None


def evaluate_check(check_kind, context=None) -> str | None:
    """Evaluate a structural check; return a violation reason or ``None``.

    ``context`` carries the parameters the check needs (``event_type``,
    ``action``, ``operation``). An unknown or empty check passes, so a rule
    without a check only relies on its declared surfaces.
    """
    context = context or {}
    if check_kind == CHECK_NO_VALIDATION_BYPASS:
        if (
            context.get("event_type") == EVENT_VALIDATION_PASSED
            and context.get("action") != ACTION_RUN_VALIDATION
        ):
            return "validation was not executed before the task was passed"
        return None
    if check_kind == CHECK_NO_BULK_RESET:
        if context.get("operation") in BULK_RESET_OPERATIONS:
            return "a bulk data reset was requested"
        return None
    return None


def find_transition_conflict(
    invariants, event_type, *, context=None
) -> InvariantConflict | None:
    """Return the first hard invariant that forbids a transition.

    A rule with a ``check_kind`` is evaluated by :func:`evaluate_check`; a rule
    without one is matched by its ``guard_events``. Legacy hard rules that only
    declare triggers never block a transition.
    """
    context = dict(context or {})
    for invariant in _hard(invariants):
        check_kind = getattr(invariant, "check_kind", CHECK_NONE)
        if check_kind:
            reason = evaluate_check(
                check_kind, {**context, "event_type": event_type}
            )
            if reason:
                return InvariantConflict(
                    invariant=invariant,
                    phase=PHASE_COMMIT,
                    trigger=check_kind,
                    action=context.get("action"),
                    event_type=event_type,
                    blocked=f'the transition "{event_type}" is forbidden: {reason}',
                )
            continue
        if event_type and event_type in (
            getattr(invariant, "guard_events", ()) or ()
        ):
            return InvariantConflict(
                invariant=invariant,
                phase=PHASE_COMMIT,
                trigger=str(event_type),
                action=context.get("action"),
                event_type=event_type,
                blocked=f'the transition "{event_type}" is forbidden by this invariant',
            )
    return None


def format_conflict_message(conflict) -> str:
    """Return the human refusal message of a conflict.

    The message names the rule and version, what exactly was not done, a lawful
    alternative and how the owner can change the rule.
    """
    invariant = conflict.invariant
    scope = invariant.scope
    if scope == SCOPE_TASK and invariant.task_id is not None:
        scope = f"{scope}:{invariant.task_id}"
    lines = [
        "Invariant conflict: "
        f"{invariant.code} (v{invariant.version}, {scope}, {invariant.enforcement})",
    ]
    detail = conflict.blocked or str(invariant.text or "").strip()
    if detail:
        lines.append(f"Nothing was done: {detail}.")
    alternative = str(invariant.alternative or "").strip()
    if alternative:
        lines.append(f"Allowed alternative: {alternative}")
    else:
        lines.append("No alternative is declared for this rule.")
    lines.append(
        "To change this rule, open Diagnostics / Task → Invariants "
        "(owner action)."
    )
    return "\n".join(lines)
