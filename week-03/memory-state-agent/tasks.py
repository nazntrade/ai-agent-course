"""Pure task-state domain: constants, FSM, artifacts and text formatters.

The module performs no I/O and imports neither Streamlit, nor SQL, nor the
provider client. ``TaskStateMachine.apply`` returns a :class:`TransitionResult`
containing the next revision of the task plus the event and artifact drafts; the
repository commits that result in a single transaction.

``Task`` is treated as immutable: every transition builds a new revision with
``dataclasses.replace`` and bumps ``version`` by one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

# --- Stages and statuses ---------------------------------------------------

STAGE_PLANNING = "planning"
STAGE_EXECUTION = "execution"
STAGE_VALIDATION = "validation"
STAGE_DONE = "done"
STAGES = (STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION, STAGE_DONE)

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
)

# The label shown next to a stage while no plan step is current.
STAGE_STEP_LABELS = {
    STAGE_PLANNING: "Planning",
    STAGE_EXECUTION: "Execution",
    STAGE_VALIDATION: "Validation",
    STAGE_DONE: "Done",
}

# --- Events ----------------------------------------------------------------

EVENT_TASK_CREATED = "TASK_CREATED"
EVENT_PLAN_CREATED = "PLAN_CREATED"
EVENT_PLAN_REJECTED = "PLAN_REJECTED"
EVENT_PLAN_ACCEPTED = "PLAN_ACCEPTED"
EVENT_STEP_COMPLETED = "STEP_COMPLETED"
EVENT_EXECUTION_FINISHED = "EXECUTION_FINISHED"
EVENT_VALIDATION_PASSED = "VALIDATION_PASSED"
EVENT_VALIDATION_FAILED = "VALIDATION_FAILED"
EVENT_PAUSE = "PAUSE"
EVENT_RESUME = "RESUME"
EVENT_BLOCK = "BLOCK"
EVENT_UNBLOCK = "UNBLOCK"
EVENT_CANCEL = "CANCEL"
EVENT_API_ERROR = "API_ERROR"
EVENT_RETRY = "RETRY"

EVENTS = (
    EVENT_TASK_CREATED,
    EVENT_PLAN_CREATED,
    EVENT_PLAN_REJECTED,
    EVENT_PLAN_ACCEPTED,
    EVENT_STEP_COMPLETED,
    EVENT_EXECUTION_FINISHED,
    EVENT_VALIDATION_PASSED,
    EVENT_VALIDATION_FAILED,
    EVENT_PAUSE,
    EVENT_RESUME,
    EVENT_BLOCK,
    EVENT_UNBLOCK,
    EVENT_CANCEL,
    EVENT_API_ERROR,
    EVENT_RETRY,
)

# Events that change the task and therefore carry an idempotency key.
STATE_CHANGING_EVENTS = (
    EVENT_PLAN_CREATED,
    EVENT_PLAN_REJECTED,
    EVENT_PLAN_ACCEPTED,
    EVENT_STEP_COMPLETED,
    EVENT_EXECUTION_FINISHED,
    EVENT_VALIDATION_PASSED,
    EVENT_VALIDATION_FAILED,
    EVENT_PAUSE,
    EVENT_RESUME,
    EVENT_BLOCK,
    EVENT_UNBLOCK,
    EVENT_CANCEL,
)

# --- Artifact kinds --------------------------------------------------------

ARTIFACT_TASK_BRIEF = "task_brief"
ARTIFACT_SPECIFICATION = "specification"
ARTIFACT_PLAN = "plan"
ARTIFACT_EXECUTION_RESULT = "execution_result"
ARTIFACT_VALIDATION_RESULT = "validation_result"
ARTIFACT_FINAL_RESULT = "final_result"
ARTIFACT_KINDS = (
    ARTIFACT_TASK_BRIEF,
    ARTIFACT_SPECIFICATION,
    ARTIFACT_PLAN,
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_VALIDATION_RESULT,
    ARTIFACT_FINAL_RESULT,
)

# --- Actions ---------------------------------------------------------------

ACTION_RUN_PLANNING = "run_planning"
ACTION_REJECT_PLAN = "reject_plan"
ACTION_ACCEPT_PLAN = "accept_plan"
ACTION_RUN_STEP = "run_step"
ACTION_FINISH_EXECUTION = "finish_execution"
ACTION_RUN_VALIDATION = "run_validation"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_BLOCK = "block"
ACTION_UNBLOCK = "unblock"
ACTION_CANCEL = "cancel"
ACTION_RETRY = "retry"
DOMAIN_ACTIONS = (
    ACTION_RUN_PLANNING,
    ACTION_REJECT_PLAN,
    ACTION_ACCEPT_PLAN,
    ACTION_RUN_STEP,
    ACTION_FINISH_EXECUTION,
    ACTION_RUN_VALIDATION,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_BLOCK,
    ACTION_UNBLOCK,
    ACTION_CANCEL,
    ACTION_RETRY,
)

ACTION_OPEN_RESULT = "open_result"
ACTION_NEW_TASK = "new_task"
ACTION_OPEN_DIAGNOSTICS = "open_diagnostics"
UI_ACTIONS = (ACTION_OPEN_RESULT, ACTION_NEW_TASK, ACTION_OPEN_DIAGNOSTICS)

# The single source of the human action labels. The UI imports this mapping
# instead of keeping its own copy, so a new domain action cannot be rendered
# under a stale or missing label.
ACTION_LABELS = {
    ACTION_RUN_PLANNING: "Run planning",
    ACTION_ACCEPT_PLAN: "Accept plan",
    ACTION_REJECT_PLAN: "Reject plan",
    ACTION_RUN_STEP: "Run step",
    ACTION_FINISH_EXECUTION: "Finish execution",
    ACTION_RUN_VALIDATION: "Run validation",
    ACTION_PAUSE: "Pause",
    ACTION_RESUME: "Resume",
    ACTION_BLOCK: "Block",
    ACTION_UNBLOCK: "Unblock",
    ACTION_CANCEL: "Cancel",
    ACTION_RETRY: "Retry",
    ACTION_OPEN_RESULT: "Open full result",
    ACTION_NEW_TASK: "New task",
    ACTION_OPEN_DIAGNOSTICS: "Open diagnostics",
}

# --- Refusal reasons -------------------------------------------------------

# Stable machine-readable codes explaining why an action is not allowed. The
# empty code means the action is allowed; every other code belongs to a refusal
# and is both shown next to the action and stored in the append-only audit.
REASON_ALLOWED = ""
REASON_TASK_NOT_FOUND = "task_not_found"
REASON_TERMINAL = "task_is_terminal"
REASON_PAUSED = "status_paused"
REASON_BLOCKED = "status_blocked"
REASON_EXPECTED_ACTION_MISMATCH = "expected_action_mismatch"
REASON_PROGRESS_INCOMPLETE = "progress_incomplete"
REASON_RETRY_REQUIRES_API_ERROR = "retry_requires_api_error"
REASON_CONFIRMATION_REQUIRED = "confirmation_required"
REASON_NOT_ALLOWED = "action_not_allowed"
REASON_INVALID_TRANSITION = "invalid_transition"
REASON_INVALID_PAYLOAD = "invalid_payload"

REFUSAL_REASONS = (
    REASON_TASK_NOT_FOUND,
    REASON_TERMINAL,
    REASON_PAUSED,
    REASON_BLOCKED,
    REASON_EXPECTED_ACTION_MISMATCH,
    REASON_PROGRESS_INCOMPLETE,
    REASON_RETRY_REQUIRES_API_ERROR,
    REASON_CONFIRMATION_REQUIRED,
    REASON_NOT_ALLOWED,
    REASON_INVALID_TRANSITION,
    REASON_INVALID_PAYLOAD,
)

# English explanations shown to the user. They describe the state, never the
# internals of the implementation, and never contain secrets.
REASON_TEXTS = {
    REASON_ALLOWED: "",
    REASON_TASK_NOT_FOUND: "the task does not exist",
    REASON_TERMINAL: "the task is already finished or cancelled",
    REASON_PAUSED: "the task is paused; resume it first",
    REASON_BLOCKED: "the task is blocked; unblock it first",
    REASON_EXPECTED_ACTION_MISMATCH: "the task currently expects a different action",
    REASON_PROGRESS_INCOMPLETE: "not all plan steps are completed yet",
    REASON_RETRY_REQUIRES_API_ERROR: (
        "retry is only available right after a provider error"
    ),
    REASON_CONFIRMATION_REQUIRED: "this action requires explicit confirmation",
    REASON_NOT_ALLOWED: "the action is not allowed in the current state",
    REASON_INVALID_TRANSITION: "the transition is invalid for the current state",
    REASON_INVALID_PAYLOAD: "the transition payload is invalid",
}

# Stage-bound actions are allowed only while the task expects exactly that
# action; when one of them is refused the mismatch (not a generic "not allowed")
# is the useful explanation.
_STAGE_BOUND_ACTIONS = (
    ACTION_RUN_PLANNING,
    ACTION_ACCEPT_PLAN,
    ACTION_REJECT_PLAN,
    ACTION_RUN_STEP,
    ACTION_FINISH_EXECUTION,
    ACTION_RUN_VALIDATION,
)

# The domain action that produces a state-changing event. It names the
# idempotency key, so a resubmitted form never writes a second event.
EVENT_ACTIONS = {
    EVENT_PLAN_CREATED: ACTION_RUN_PLANNING,
    EVENT_PLAN_REJECTED: ACTION_REJECT_PLAN,
    EVENT_PLAN_ACCEPTED: ACTION_ACCEPT_PLAN,
    EVENT_STEP_COMPLETED: ACTION_RUN_STEP,
    EVENT_EXECUTION_FINISHED: ACTION_FINISH_EXECUTION,
    EVENT_VALIDATION_PASSED: ACTION_RUN_VALIDATION,
    EVENT_VALIDATION_FAILED: ACTION_RUN_VALIDATION,
    EVENT_PAUSE: ACTION_PAUSE,
    EVENT_RESUME: ACTION_RESUME,
    EVENT_BLOCK: ACTION_BLOCK,
    EVENT_UNBLOCK: ACTION_UNBLOCK,
    EVENT_CANCEL: ACTION_CANCEL,
}

# --- Expected actions ------------------------------------------------------

EXPECTED_RUN_PLANNING = "run_planning"
EXPECTED_CONFIRM_PLAN = "confirm_plan"
EXPECTED_RUN_STEP = "run_step"
EXPECTED_FINISH_EXECUTION = "finish_execution"
EXPECTED_RUN_VALIDATION = "run_validation"
EXPECTED_REVIEW_RESULT = "review_result"
EXPECTED_USER_ACTION = "user_action"
EXPECTED_NONE = "none"
EXPECTED_ACTIONS = (
    EXPECTED_RUN_PLANNING,
    EXPECTED_CONFIRM_PLAN,
    EXPECTED_RUN_STEP,
    EXPECTED_FINISH_EXECUTION,
    EXPECTED_RUN_VALIDATION,
    EXPECTED_REVIEW_RESULT,
    EXPECTED_USER_ACTION,
    EXPECTED_NONE,
)

EXPECTED_ACTION_TEXTS = {
    EXPECTED_RUN_PLANNING: "Run planning",
    EXPECTED_CONFIRM_PLAN: "Accept or reject the plan",
    EXPECTED_RUN_STEP: "Run the current step",
    EXPECTED_FINISH_EXECUTION: "Finish execution",
    EXPECTED_RUN_VALIDATION: "Run validation",
    EXPECTED_REVIEW_RESULT: "Review the result",
    EXPECTED_USER_ACTION: "",
    EXPECTED_NONE: "",
}

# The text of the next expected action per produced event (FR-08). The same
# event type may point at a different text than its expected-action type: a
# failed validation expects a rework step, not a plain next step. ``BLOCK`` has
# no entry on purpose: the expected text is the action the user supplies, so a
# fixed phrase here would contradict the stored value.
EXPECTED_ACTION_TEXT = {
    EVENT_TASK_CREATED: "Run planning",
    EVENT_PLAN_CREATED: "Accept or reject the plan",
    EVENT_PLAN_REJECTED: "Run planning",
    EVENT_PLAN_ACCEPTED: "Run the current step",
    EVENT_STEP_COMPLETED: "Run the current step",
    EVENT_EXECUTION_FINISHED: "Run validation",
    EVENT_VALIDATION_PASSED: "Review the result",
    EVENT_VALIDATION_FAILED: "Fix the defects and run the step",
    EVENT_CANCEL: "",
}

# --- Badges (FR-08) --------------------------------------------------------

BADGE_RUNNING = "RUNNING"
BADGE_PAUSED = "PAUSED"
BADGE_BLOCKED = "BLOCKED"
BADGE_COMPLETED = "COMPLETED"
BADGE_CANCELLED = "CANCELLED"

# --- API error kinds -------------------------------------------------------

API_ERROR_PROVIDER = "provider_error"
API_ERROR_STREAM = "stream_error"
API_ERROR_INVALID_RESPONSE = "invalid_response"
API_ERROR_TRUNCATED = "truncated"
API_ERROR_CONTEXT_OVERFLOW = "context_overflow"
API_ERROR_KINDS = (
    API_ERROR_PROVIDER,
    API_ERROR_STREAM,
    API_ERROR_INVALID_RESPONSE,
    API_ERROR_TRUNCATED,
    API_ERROR_CONTEXT_OVERFLOW,
)

ERROR_MESSAGE_MAX_LENGTH = 200


class InvalidTransitionError(Exception):
    """Raised when an event violates a stage/status precondition.

    Nothing is written when this error is raised: the caller must not create an
    event or artifact draft for a rejected transition.
    """


class TransitionPayloadError(ValueError):
    """Raised when the payload of an otherwise allowed event is invalid.

    Nothing is written in that case either; the caller reports the problem and
    the task keeps its previous revision.
    """


@dataclass
class Task:
    """The durable state of one task (one row of ``tasks``)."""

    id: int | None = None
    chat_id: int | None = None
    workflow_profile_id: int | None = None
    workflow_name: str | None = None
    title: str = ""
    goal: str = ""
    stage: str = STAGE_PLANNING
    status: str = STATUS_ACTIVE
    current_step: str = ""
    current_step_index: int | None = None
    expected_action_type: str = EXPECTED_NONE
    expected_action_text: str = ""
    pause_reason: str = ""
    version: int = 1
    created_at: str | None = None
    updated_at: str | None = None

    def evolve(self, **changes) -> "Task":
        """Return a copy with the given fields replaced."""
        return replace(self, **changes)


@dataclass
class TaskArtifact:
    """An immutable artifact; ``id``/``revision`` are assigned by the repository."""

    id: int | None = None
    task_id: int | None = None
    stage: str = ""
    kind: str = ""
    revision: int | None = None
    content: dict = field(default_factory=dict)
    created_at: str | None = None


@dataclass
class TaskEvent:
    """An append-only journal entry; ``id``/``created_at`` come from the store."""

    id: int | None = None
    task_id: int | None = None
    event_type: str = ""
    from_stage: str | None = None
    to_stage: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    payload: dict = field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class WorkflowProfile:
    """A workflow definition: stage order plus per-stage instructions."""

    id: int | None = None
    name: str = ""
    display_name: str = ""
    stages: tuple = STAGES
    instructions: dict = field(default_factory=dict)
    executors: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    is_default: bool = False


@dataclass(frozen=True)
class StepProgress:
    """Completion/rework state of one plan step, derived from artifacts."""

    step_index: int
    title: str = ""
    completed: bool = False
    awaiting_rework: bool = False
    round: int = 0
    defects: tuple[str, ...] = ()


@dataclass
class TransitionResult:
    """Outcome of an allowed transition: new task plus event/artifact drafts.

    Artifacts carry no ``id``/``revision``: the repository assigns both inside
    the committing transaction. ``idempotency_key`` is ``None`` for events that
    do not change the task (``API_ERROR``, ``RETRY``, ``TASK_CREATED``).
    """

    task: Task
    event: TaskEvent | None = None
    artifacts: list = field(default_factory=list)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class TransitionDecision:
    """Whether one action is allowed right now, and why not when refused.

    ``allowed_actions`` is the exact set the current state permits, so the UI and
    the audit can explain a refusal without re-deriving the FSM rules.
    ``message`` is the ready-to-show English sentence built by
    :func:`format_refusal`.
    """

    action: str
    allowed: bool
    reason: str = REASON_ALLOWED
    allowed_actions: tuple = ()
    message: str = ""


def _now() -> str:
    """Current UTC time in the format SQLite uses for the other tables."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _artifact_order(artifact) -> tuple:
    """Order artifacts by id, keeping drafts (no id) first and stable."""
    artifact_id = getattr(artifact, "id", None)
    return (artifact_id is None, artifact_id if artifact_id is not None else 0)


def _content_of(item) -> dict:
    """Return a mapping's content, accepting plain dicts and artifact objects."""
    if isinstance(item, dict):
        return item
    content = getattr(item, "content", None)
    return content if isinstance(content, dict) else {}


def _defect_indexes(content: dict) -> list:
    indexes = []
    for defect in content.get("defects") or []:
        if isinstance(defect, dict) and isinstance(defect.get("step_index"), int):
            indexes.append(defect["step_index"])
    return indexes


def _defect_descriptions(content: dict, step_index: int) -> list:
    descriptions = []
    for defect in content.get("defects") or []:
        if not isinstance(defect, dict):
            continue
        if defect.get("step_index") != step_index:
            continue
        descriptions.append(str(defect.get("description") or "").strip())
    return [description for description in descriptions if description]


def plan_steps(plan_content) -> list:
    """Return the normalized steps of a plan, ordered by index.

    Unknown or malformed plans yield an empty list instead of raising, so the
    callers can treat "no plan yet" and "unreadable plan" the same way.
    """
    if not isinstance(plan_content, dict):
        return []
    steps = plan_content.get("steps")
    if not isinstance(steps, list):
        return []
    normalized = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        index = step.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        title = str(step.get("title") or "").strip()
        if not title:
            continue
        description = step.get("description")
        normalized.append(
            {
                "index": index,
                "title": title,
                "description": description if isinstance(description, str) else str(description or ""),
            }
        )
    return sorted(normalized, key=lambda item: item["index"])


def compute_step_progress(plan_content, artifacts) -> list:
    """Derive completion and rework state of every plan step from artifacts.

    A step is completed when its latest ``execution_result`` exists and no
    later ``validation_result`` with ``passed=false`` mentions it. It awaits
    rework when such a validation is newer than its latest successful revision
    (or when no revision exists yet). ``round`` counts the successful revisions
    of the step and ``defects`` carries the descriptions of the blocking
    validation for that step.
    """
    steps = plan_steps(plan_content)
    if not steps:
        return []

    executions: dict = {}
    failed_validations: list = []
    for artifact in artifacts or []:
        kind = getattr(artifact, "kind", None)
        content = _content_of(artifact)
        if kind == ARTIFACT_EXECUTION_RESULT:
            index = content.get("step_index")
            if isinstance(index, int) and not isinstance(index, bool):
                executions.setdefault(index, []).append(artifact)
        elif kind == ARTIFACT_VALIDATION_RESULT and content.get("passed") is False:
            failed_validations.append(artifact)

    failed_validations.sort(key=_artifact_order)

    progress = []
    for step in steps:
        index = step["index"]
        step_executions = sorted(executions.get(index, []), key=_artifact_order)
        latest_execution = step_executions[-1] if step_executions else None

        blocking = None
        for artifact in failed_validations:
            if index in _defect_indexes(_content_of(artifact)):
                blocking = artifact

        if latest_execution is None:
            completed = False
        elif blocking is None:
            completed = True
        else:
            completed = _artifact_order(blocking) < _artifact_order(latest_execution)

        if blocking is None:
            awaiting_rework = False
        elif latest_execution is None:
            awaiting_rework = True
        else:
            awaiting_rework = _artifact_order(blocking) > _artifact_order(latest_execution)

        # The defects describe what is still to fix, so they are reported only
        # while the step awaits rework; once a newer revision wins they are
        # history, not instructions.
        defects = (
            tuple(_defect_descriptions(_content_of(blocking), index))
            if awaiting_rework
            else ()
        )

        progress.append(
            StepProgress(
                step_index=index,
                title=step["title"],
                completed=completed,
                awaiting_rework=awaiting_rework,
                round=len(step_executions),
                defects=defects,
            )
        )
    return progress


def _is_terminal(task) -> bool:
    return task.stage == STAGE_DONE or task.status in (STATUS_COMPLETED, STATUS_CANCELLED)


def _require_stage(task, event_type, *stages) -> None:
    if task.stage not in stages:
        raise InvalidTransitionError(
            f"{event_type} requires stage in {stages}, got {task.stage}"
        )


def _require_status(task, event_type, *statuses) -> None:
    if task.status not in statuses:
        raise InvalidTransitionError(
            f"{event_type} requires status in {statuses}, got {task.status}"
        )


def _require_expected(task, event_type, *expected_types) -> None:
    if task.expected_action_type not in expected_types:
        raise InvalidTransitionError(
            f"{event_type} requires expected_action_type in {expected_types}, "
            f"got {task.expected_action_type}"
        )


def _evolve(task, **changes) -> Task:
    """Apply field changes, always bumping ``version`` by one."""
    changes.setdefault("version", task.version + 1)
    changes["updated_at"] = _now()
    return replace(task, **changes)


def _usage_payload(payload: dict) -> dict:
    """Keep the small accounting fields that belong in the journal."""
    result = {}
    for key in ("usage", "finish_reason", "attempts", "model"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    return result


def _event(
    task,
    event_type: str,
    payload: dict,
    *,
    to_stage=None,
    to_status=None,
    version=None,
) -> TaskEvent:
    """Build the event draft, including the idempotency key of the transition."""
    action = EVENT_ACTIONS.get(event_type)
    key = None
    if action is not None and task.id is not None and version is not None:
        key = f"{action}:{task.id}:{version}"
    return TaskEvent(
        task_id=task.id,
        event_type=event_type,
        from_stage=task.stage,
        to_stage=to_stage if to_stage is not None else task.stage,
        from_status=task.status,
        to_status=to_status if to_status is not None else task.status,
        payload=dict(payload),
        idempotency_key=key,
    )


def _result(task, event, artifacts=None, idempotency_key=None) -> TransitionResult:
    return TransitionResult(
        task=task,
        event=event,
        artifacts=list(artifacts or []),
        idempotency_key=idempotency_key if idempotency_key is not None else event.idempotency_key,
    )


def _validate_plan(plan) -> dict:
    """Validate a plan mapping and return its normalized form."""
    if not isinstance(plan, dict):
        raise TransitionPayloadError("A plan mapping is required")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise TransitionPayloadError("A plan must contain at least one step")
    normalized_steps = []
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise TransitionPayloadError(f"Plan step #{position} must be a mapping")
        index = step.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != position:
            raise TransitionPayloadError(
                f"Plan step indexes must be continuous starting at 1; "
                f"expected {position}"
            )
        title = str(step.get("title") or "").strip()
        if not title:
            raise TransitionPayloadError(f"Plan step {index} has an empty title")
        description = step.get("description")
        normalized_steps.append(
            {
                "index": index,
                "title": title,
                "description": description if isinstance(description, str) else str(description or ""),
            }
        )
    criteria = plan.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise TransitionPayloadError(
            "A plan must contain at least one acceptance criterion"
        )
    cleaned_criteria = []
    for criterion in criteria:
        if not isinstance(criterion, str) or not criterion.strip():
            raise TransitionPayloadError(
                "Every acceptance criterion must be a non-empty string"
            )
        cleaned_criteria.append(criterion.strip())
    summary = plan.get("summary")
    return {
        "summary": summary if isinstance(summary, str) else str(summary or ""),
        "acceptance_criteria": cleaned_criteria,
        "steps": normalized_steps,
    }


def _resolve_progress(progress, payload: dict) -> list | None:
    """Return the step progress, falling back to a fresh read of the payload plan."""
    if progress is not None:
        return list(progress)
    plan = payload.get("plan")
    if isinstance(plan, dict):
        try:
            return compute_step_progress(_validate_plan(plan), [])
        except TransitionPayloadError:
            return None
    return None


def _require_progress(progress, event_type) -> list:
    if progress is None:
        raise TransitionPayloadError(
            f"{event_type} requires the current step progress"
        )
    return progress


def _advance_pointer(progress: list) -> tuple:
    """Next expected action and step pointer after a step finished or resumed.

    Rework is served first (the earliest awaiting step), then any remaining
    incomplete step; when nothing is left the task expects ``finish_execution``.
    """
    awaiting = [step for step in progress if step.awaiting_rework]
    if awaiting:
        step = min(awaiting, key=lambda item: item.step_index)
        return EXPECTED_RUN_STEP, EXPECTED_ACTION_TEXTS[EXPECTED_RUN_STEP], step.step_index, step.title

    incomplete = [step for step in progress if not step.completed]
    if incomplete:
        step = min(incomplete, key=lambda item: item.step_index)
        return EXPECTED_RUN_STEP, EXPECTED_ACTION_TEXTS[EXPECTED_RUN_STEP], step.step_index, step.title

    return (
        EXPECTED_FINISH_EXECUTION,
        EXPECTED_ACTION_TEXTS[EXPECTED_FINISH_EXECUTION],
        None,
        STAGE_STEP_LABELS[STAGE_EXECUTION],
    )


def normalize_verdict(payload: dict, *, expected_passed=None, steps_count=None) -> dict:
    """Validate and normalize a validation verdict.

    ``expected_passed`` binds the verdict to the event type (``VALIDATION_PASSED``
    must not carry failures); ``None`` accepts either verdict, which is what the
    provider-response parser needs. ``steps_count`` bounds every defect index.
    """
    if not isinstance(payload, dict):
        raise TransitionPayloadError("A validation verdict must be a mapping")
    raw_passed = payload.get("passed")
    if raw_passed is None:
        if expected_passed is None:
            raise TransitionPayloadError("The verdict must contain a boolean 'passed'")
        passed = expected_passed
    elif isinstance(raw_passed, bool):
        if expected_passed is not None and raw_passed != expected_passed:
            raise TransitionPayloadError(
                "The verdict does not match the validation event type"
            )
        passed = raw_passed
    else:
        raise TransitionPayloadError("The verdict 'passed' flag must be a boolean")

    raw_defects = payload.get("defects")
    if raw_defects is None:
        raw_defects = []
    if not isinstance(raw_defects, list):
        raise TransitionPayloadError("The verdict defects must be a list")

    defects = []
    for position, defect in enumerate(raw_defects, start=1):
        if not isinstance(defect, dict):
            raise TransitionPayloadError(f"Defect #{position} must be a mapping")
        description = str(defect.get("description") or "").strip()
        if not description:
            raise TransitionPayloadError(f"Defect #{position} has an empty description")
        step_index = defect.get("step_index")
        if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 1:
            raise TransitionPayloadError(
                f"Defect #{position} needs a 1-based step_index"
            )
        if steps_count is not None and step_index > steps_count:
            raise TransitionPayloadError(
                f"Defect #{position} refers to step {step_index}, "
                f"outside the plan of {steps_count} steps"
            )
        defects.append({"step_index": step_index, "description": description})

    if passed and defects:
        raise TransitionPayloadError("A passed verdict must not contain defects")
    if not passed and not defects:
        raise TransitionPayloadError("A failed verdict must contain at least one defect")

    notes = payload.get("notes")
    return {
        "passed": passed,
        "defects": defects,
        "notes": notes if isinstance(notes, str) else str(notes or ""),
    }


# --- Transition handlers ---------------------------------------------------


def _plan_created(task, payload, progress):
    _require_stage(task, EVENT_PLAN_CREATED, STAGE_PLANNING)
    _require_status(task, EVENT_PLAN_CREATED, STATUS_ACTIVE)
    _require_expected(task, EVENT_PLAN_CREATED, EXPECTED_RUN_PLANNING)

    plan = _validate_plan(payload.get("plan"))
    task_brief = payload.get("task_brief")
    if task_brief is None:
        task_brief = payload.get("brief")
    task_brief = str(task_brief or "").strip()

    markdown = payload.get("specification_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        markdown = format_specification_markdown(task.goal, task_brief, plan)

    artifacts = [
        TaskArtifact(
            task_id=task.id,
            stage=STAGE_PLANNING,
            kind=ARTIFACT_SPECIFICATION,
            content={"markdown": markdown},
        ),
        TaskArtifact(
            task_id=task.id,
            stage=STAGE_PLANNING,
            kind=ARTIFACT_PLAN,
            content=plan,
        ),
    ]
    first_step = plan["steps"][0]
    updated = _evolve(
        task,
        current_step=first_step["title"],
        current_step_index=first_step["index"],
        expected_action_type=EXPECTED_CONFIRM_PLAN,
        expected_action_text=EXPECTED_ACTION_TEXTS[EXPECTED_CONFIRM_PLAN],
    )
    event = _event(
        task,
        EVENT_PLAN_CREATED,
        {**_usage_payload(payload), "step_count": len(plan["steps"])},
        to_stage=STAGE_PLANNING,
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event, artifacts)


def _plan_rejected(task, payload, progress):
    _require_stage(task, EVENT_PLAN_REJECTED, STAGE_PLANNING)
    _require_status(task, EVENT_PLAN_REJECTED, STATUS_ACTIVE)
    _require_expected(task, EVENT_PLAN_REJECTED, EXPECTED_CONFIRM_PLAN)

    updated = _evolve(
        task,
        current_step=STAGE_STEP_LABELS[STAGE_PLANNING],
        current_step_index=None,
        expected_action_type=EXPECTED_RUN_PLANNING,
        expected_action_text=EXPECTED_ACTION_TEXTS[EXPECTED_RUN_PLANNING],
    )
    event = _event(
        task,
        EVENT_PLAN_REJECTED,
        {**_usage_payload(payload), "reason": payload.get("reason")},
        to_stage=STAGE_PLANNING,
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event)


def _plan_accepted(task, payload, progress):
    _require_stage(task, EVENT_PLAN_ACCEPTED, STAGE_PLANNING)
    _require_status(task, EVENT_PLAN_ACCEPTED, STATUS_ACTIVE)
    _require_expected(task, EVENT_PLAN_ACCEPTED, EXPECTED_CONFIRM_PLAN)

    steps = _require_progress(_resolve_progress(progress, payload), EVENT_PLAN_ACCEPTED)
    expected_type, expected_text, step_index, step_title = _advance_pointer(steps)
    updated = _evolve(
        task,
        stage=STAGE_EXECUTION,
        status=STATUS_ACTIVE,
        current_step=step_title,
        current_step_index=step_index,
        expected_action_type=expected_type,
        expected_action_text=expected_text,
    )
    event = _event(
        task,
        EVENT_PLAN_ACCEPTED,
        _usage_payload(payload),
        to_stage=STAGE_EXECUTION,
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event)


def _step_completed(task, payload, progress):
    _require_stage(task, EVENT_STEP_COMPLETED, STAGE_EXECUTION)
    _require_status(task, EVENT_STEP_COMPLETED, STATUS_ACTIVE)
    _require_expected(task, EVENT_STEP_COMPLETED, EXPECTED_RUN_STEP)

    steps = _require_progress(
        _resolve_progress(progress, payload), EVENT_STEP_COMPLETED
    )
    step_index = task.current_step_index
    if step_index is None:
        raise InvalidTransitionError("STEP_COMPLETED requires a current step index")
    payload_index = payload.get("step_index")
    if payload_index is not None and payload_index != step_index:
        raise InvalidTransitionError(
            f"STEP_COMPLETED is bound to step {step_index}, got {payload_index}"
        )

    current = next((step for step in steps if step.step_index == step_index), None)
    if current is None:
        raise InvalidTransitionError(f"Step {step_index} is not part of the plan")
    if current.completed:
        raise InvalidTransitionError(f"Step {step_index} is already completed")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TransitionPayloadError("STEP_COMPLETED requires the step result text")

    step_round = current.round + 1
    virtual = [
        replace(
            step,
            completed=True,
            awaiting_rework=False,
            round=step_round,
            defects=(),
        )
        if step.step_index == step_index
        else step
        for step in steps
    ]
    expected_type, expected_text, next_index, next_title = _advance_pointer(virtual)
    updated = _evolve(
        task,
        current_step=next_title,
        current_step_index=next_index,
        expected_action_type=expected_type,
        expected_action_text=expected_text,
    )
    artifacts = [
        TaskArtifact(
            task_id=task.id,
            stage=STAGE_EXECUTION,
            kind=ARTIFACT_EXECUTION_RESULT,
            content={
                "step_index": step_index,
                "round": step_round,
                "text": text,
            },
        )
    ]
    event = _event(
        task,
        EVENT_STEP_COMPLETED,
        {**_usage_payload(payload), "step_index": step_index, "round": step_round},
        to_stage=STAGE_EXECUTION,
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event, artifacts)


def _execution_finished(task, payload, progress):
    _require_stage(task, EVENT_EXECUTION_FINISHED, STAGE_EXECUTION)
    _require_status(task, EVENT_EXECUTION_FINISHED, STATUS_ACTIVE)
    _require_expected(task, EVENT_EXECUTION_FINISHED, EXPECTED_FINISH_EXECUTION)

    steps = _require_progress(
        _resolve_progress(progress, payload), EVENT_EXECUTION_FINISHED
    )
    if not steps or not all(step.completed for step in steps):
        raise InvalidTransitionError(
            "EXECUTION_FINISHED requires every plan step to be completed"
        )

    updated = _evolve(
        task,
        stage=STAGE_VALIDATION,
        status=STATUS_ACTIVE,
        current_step=STAGE_STEP_LABELS[STAGE_VALIDATION],
        current_step_index=None,
        expected_action_type=EXPECTED_RUN_VALIDATION,
        expected_action_text=EXPECTED_ACTION_TEXTS[EXPECTED_RUN_VALIDATION],
    )
    event = _event(
        task,
        EVENT_EXECUTION_FINISHED,
        _usage_payload(payload),
        to_stage=STAGE_VALIDATION,
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event)


def _validation_passed(task, payload, progress):
    _require_stage(task, EVENT_VALIDATION_PASSED, STAGE_VALIDATION)
    _require_status(task, EVENT_VALIDATION_PASSED, STATUS_ACTIVE)
    _require_expected(task, EVENT_VALIDATION_PASSED, EXPECTED_RUN_VALIDATION)

    verdict = normalize_verdict(payload, expected_passed=True)
    updated = _evolve(
        task,
        stage=STAGE_DONE,
        status=STATUS_COMPLETED,
        current_step=STAGE_STEP_LABELS[STAGE_DONE],
        current_step_index=None,
        expected_action_type=EXPECTED_REVIEW_RESULT,
        expected_action_text=EXPECTED_ACTION_TEXTS[EXPECTED_REVIEW_RESULT],
    )

    markdown = payload.get("final_result_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        markdown = format_final_result_markdown(
            updated,
            payload.get("plan"),
            payload.get("executions"),
            verdict,
        )

    artifacts = [
        TaskArtifact(
            task_id=task.id,
            stage=STAGE_VALIDATION,
            kind=ARTIFACT_VALIDATION_RESULT,
            content=verdict,
        ),
        TaskArtifact(
            task_id=task.id,
            stage=STAGE_DONE,
            kind=ARTIFACT_FINAL_RESULT,
            content={"markdown": markdown},
        ),
    ]
    event = _event(
        task,
        EVENT_VALIDATION_PASSED,
        {**_usage_payload(payload), "passed": True},
        to_stage=STAGE_DONE,
        to_status=STATUS_COMPLETED,
        version=task.version,
    )
    return _result(updated, event, artifacts)


def _validation_failed(task, payload, progress):
    _require_stage(task, EVENT_VALIDATION_FAILED, STAGE_VALIDATION)
    _require_status(task, EVENT_VALIDATION_FAILED, STATUS_ACTIVE)
    _require_expected(task, EVENT_VALIDATION_FAILED, EXPECTED_RUN_VALIDATION)

    steps = _require_progress(
        _resolve_progress(progress, payload), EVENT_VALIDATION_FAILED
    )
    verdict = normalize_verdict(payload, expected_passed=False, steps_count=len(steps))

    first_defect = min(verdict["defects"], key=lambda defect: defect["step_index"])
    step_index = first_defect["step_index"]
    step = next((item for item in steps if item.step_index == step_index), None)
    step_title = step.title if step is not None else ""

    updated = _evolve(
        task,
        stage=STAGE_EXECUTION,
        status=STATUS_ACTIVE,
        current_step=step_title,
        current_step_index=step_index,
        expected_action_type=EXPECTED_RUN_STEP,
        expected_action_text=EXPECTED_ACTION_TEXT[EVENT_VALIDATION_FAILED],
    )
    artifacts = [
        TaskArtifact(
            task_id=task.id,
            stage=STAGE_VALIDATION,
            kind=ARTIFACT_VALIDATION_RESULT,
            content=verdict,
        )
    ]
    event = _event(
        task,
        EVENT_VALIDATION_FAILED,
        {**_usage_payload(payload), "passed": False, "defects": verdict["defects"]},
        to_stage=STAGE_EXECUTION,
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event, artifacts)


def _pause(task, payload, progress):
    _require_stage(
        task, EVENT_PAUSE, STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION
    )
    _require_status(task, EVENT_PAUSE, STATUS_ACTIVE)

    reason = payload.get("reason")
    if reason is None:
        reason = payload.get("pause_reason")
    updated = _evolve(task, status=STATUS_PAUSED, pause_reason=str(reason or "").strip())
    event = _event(
        task,
        EVENT_PAUSE,
        {"reason": updated.pause_reason} if updated.pause_reason else {},
        to_status=STATUS_PAUSED,
        version=task.version,
    )
    return _result(updated, event)


def _resume(task, payload, progress):
    _require_status(task, EVENT_RESUME, STATUS_PAUSED)

    updated = _evolve(task, status=STATUS_ACTIVE, pause_reason="")
    event = _event(
        task,
        EVENT_RESUME,
        {},
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event)


def _block(task, payload, progress):
    _require_stage(
        task, EVENT_BLOCK, STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION
    )
    _require_status(task, EVENT_BLOCK, STATUS_ACTIVE, STATUS_PAUSED)

    reason = payload.get("reason")
    if reason is None:
        reason = payload.get("pause_reason")
    reason = str(reason or "").strip()
    if not reason:
        raise TransitionPayloadError("BLOCK requires a reason")
    expected_text = str(payload.get("expected_action_text") or "").strip()
    if not expected_text:
        raise TransitionPayloadError("BLOCK requires the expected user action")

    updated = _evolve(
        task,
        status=STATUS_BLOCKED,
        pause_reason=reason,
        expected_action_type=EXPECTED_USER_ACTION,
        expected_action_text=expected_text,
    )
    # The previous expected action is kept in the journal so UNBLOCK can restore
    # it; ``expected_action_type`` itself has to become ``user_action``.
    event = _event(
        task,
        EVENT_BLOCK,
        {
            "reason": reason,
            "expected_action_text": expected_text,
            "previous_expected_action_type": task.expected_action_type,
        },
        to_status=STATUS_BLOCKED,
        version=task.version,
    )
    return _result(updated, event)


def _unblock(task, payload, progress):
    _require_status(task, EVENT_UNBLOCK, STATUS_BLOCKED)

    stage = task.stage
    steps = _resolve_progress(progress, payload)
    expected_type = payload.get("expected_action_type")
    if expected_type is None:
        expected_type = payload.get("previous_expected_action_type")
    expected_text = task.expected_action_text
    current_step = task.current_step
    current_index = task.current_step_index

    if stage == STAGE_PLANNING:
        if expected_type not in (EXPECTED_RUN_PLANNING, EXPECTED_CONFIRM_PLAN):
            # Blocking overwrites the expected action, so the plan presence is
            # the only state left: a created plan awaits confirmation.
            expected_type = EXPECTED_CONFIRM_PLAN if steps else EXPECTED_RUN_PLANNING
        expected_text = EXPECTED_ACTION_TEXTS[expected_type]
    elif stage == STAGE_EXECUTION:
        if steps:
            expected_type, expected_text, current_index, current_step = _advance_pointer(steps)
        elif expected_type not in (EXPECTED_RUN_STEP, EXPECTED_FINISH_EXECUTION):
            expected_type = EXPECTED_RUN_STEP
            expected_text = EXPECTED_ACTION_TEXTS[expected_type]
        else:
            expected_text = EXPECTED_ACTION_TEXTS[expected_type]
    elif stage == STAGE_VALIDATION:
        expected_type = EXPECTED_RUN_VALIDATION
        expected_text = EXPECTED_ACTION_TEXTS[expected_type]
        current_step = STAGE_STEP_LABELS[STAGE_VALIDATION]
        current_index = None
    elif stage == STAGE_DONE:
        expected_type = EXPECTED_REVIEW_RESULT
        expected_text = EXPECTED_ACTION_TEXTS[expected_type]

    updated = _evolve(
        task,
        status=STATUS_ACTIVE,
        pause_reason="",
        current_step=current_step,
        current_step_index=current_index,
        expected_action_type=expected_type,
        expected_action_text=expected_text,
    )
    event = _event(
        task,
        EVENT_UNBLOCK,
        {},
        to_status=STATUS_ACTIVE,
        version=task.version,
    )
    return _result(updated, event)


def _cancel(task, payload, progress):
    _require_stage(
        task, EVENT_CANCEL, STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION
    )
    _require_status(
        task, EVENT_CANCEL, STATUS_ACTIVE, STATUS_PAUSED, STATUS_BLOCKED
    )
    if not payload.get("confirmed"):
        raise TransitionPayloadError("CANCEL requires confirmed=true")

    updated = _evolve(
        task,
        status=STATUS_CANCELLED,
        expected_action_type=EXPECTED_NONE,
        expected_action_text="",
    )
    event = _event(
        task,
        EVENT_CANCEL,
        {"confirmed": True},
        to_status=STATUS_CANCELLED,
        version=task.version,
    )
    return _result(updated, event)


def _api_error(task, payload):
    kind = payload.get("kind")
    if kind not in API_ERROR_KINDS:
        raise TransitionPayloadError(
            f"API_ERROR kind must be one of {API_ERROR_KINDS}, got {kind!r}"
        )

    event_payload = {"kind": kind}
    if payload.get("message") is not None:
        event_payload["message"] = validate_error_message(payload.get("message"))
    event_payload.update(_usage_payload(payload))

    event = _event(task, EVENT_API_ERROR, event_payload)
    event.idempotency_key = None
    return _result(replace(task), event, idempotency_key=None)


def _retry(task, payload, last_event_type):
    if last_event_type != EVENT_API_ERROR:
        raise InvalidTransitionError(
            "RETRY is allowed only when the last event is API_ERROR"
        )
    event = _event(task, EVENT_RETRY, _usage_payload(payload))
    event.idempotency_key = None
    return _result(replace(task), event, idempotency_key=None)


_TRANSITIONS = {
    EVENT_PLAN_CREATED: _plan_created,
    EVENT_PLAN_REJECTED: _plan_rejected,
    EVENT_PLAN_ACCEPTED: _plan_accepted,
    EVENT_STEP_COMPLETED: _step_completed,
    EVENT_EXECUTION_FINISHED: _execution_finished,
    EVENT_VALIDATION_PASSED: _validation_passed,
    EVENT_VALIDATION_FAILED: _validation_failed,
    EVENT_PAUSE: _pause,
    EVENT_RESUME: _resume,
    EVENT_BLOCK: _block,
    EVENT_UNBLOCK: _unblock,
    EVENT_CANCEL: _cancel,
}


def apply_transition(
    task, event_type, *, payload=None, progress=None, last_event_type=None
) -> TransitionResult:
    """Apply one event to a task and return the new revision and drafts.

    Preconditions are checked before anything is produced, so a rejected
    transition raises :class:`InvalidTransitionError` or
    :class:`TransitionPayloadError` and nothing is written. ``version`` grows by
    one for every state-changing event and stays unchanged for ``API_ERROR`` and
    ``RETRY``.
    """
    if task is None:
        raise InvalidTransitionError("A task is required")
    if event_type not in EVENTS:
        raise InvalidTransitionError(f"Unknown event type: {event_type}")
    payload = dict(payload or {})

    if _is_terminal(task):
        raise InvalidTransitionError(
            f"Task is terminal: stage={task.stage}, status={task.status}"
        )
    if event_type == EVENT_API_ERROR:
        return _api_error(task, payload)
    if event_type == EVENT_RETRY:
        return _retry(task, payload, last_event_type)
    if event_type == EVENT_TASK_CREATED:
        raise InvalidTransitionError(
            "TASK_CREATED is written by the repository, not applied to a task"
        )

    handler = _TRANSITIONS.get(event_type)
    if handler is None:
        raise InvalidTransitionError(f"Event {event_type} is not applicable")
    return handler(task, payload, progress)


class TaskStateMachine:
    """Single entry point of the FSM used by the use cases and the UI."""

    @staticmethod
    def apply(
        task, event_type, *, payload=None, progress=None, last_event_type=None
    ) -> TransitionResult:
        """See :func:`apply_transition`."""
        return apply_transition(
            task,
            event_type,
            payload=payload,
            progress=progress,
            last_event_type=last_event_type,
        )

    @staticmethod
    def can_apply(task, *, last_event_type=None, progress=None) -> tuple:
        """See :func:`can_apply`."""
        return can_apply(task, last_event_type=last_event_type, progress=progress)


def can_apply(task, *, last_event_type=None, progress=None) -> tuple:
    """Return the domain actions allowed in the task's current state (FR-09).

    UI-only actions are returned by :func:`ui_actions`; the union of both
    functions reproduces the action table of the specification. ``retry`` is
    added only for an active, non-terminal task whose last journal event is
    ``API_ERROR``. ``progress`` is accepted for callers that already hold it, but
    the expected-action type fully determines the allowed set.
    """
    if task is None:
        return ()

    if _is_terminal(task):
        actions: tuple = ()
    elif task.status == STATUS_PAUSED:
        actions = (ACTION_RESUME, ACTION_BLOCK, ACTION_CANCEL)
    elif task.status == STATUS_BLOCKED:
        actions = (ACTION_UNBLOCK, ACTION_CANCEL)
    elif task.status == STATUS_ACTIVE:
        if task.stage == STAGE_PLANNING and task.expected_action_type == EXPECTED_RUN_PLANNING:
            actions = (ACTION_RUN_PLANNING, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL)
        elif task.stage == STAGE_PLANNING and task.expected_action_type == EXPECTED_CONFIRM_PLAN:
            actions = (
                ACTION_ACCEPT_PLAN,
                ACTION_REJECT_PLAN,
                ACTION_PAUSE,
                ACTION_BLOCK,
                ACTION_CANCEL,
            )
        elif task.stage == STAGE_EXECUTION and task.expected_action_type == EXPECTED_RUN_STEP:
            actions = (ACTION_RUN_STEP, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL)
        elif (
            task.stage == STAGE_EXECUTION
            and task.expected_action_type == EXPECTED_FINISH_EXECUTION
        ):
            actions = (ACTION_FINISH_EXECUTION, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL)
        elif (
            task.stage == STAGE_VALIDATION
            and task.expected_action_type == EXPECTED_RUN_VALIDATION
        ):
            actions = (ACTION_RUN_VALIDATION, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL)
        else:
            actions = ()
    else:
        actions = ()

    # Retry repeats the action that the provider call interrupted, so it is
    # offered only while the task can actually run it again: a paused or
    # blocked task has no repeatable action and would write a RETRY event that
    # changes nothing.
    if (
        last_event_type == EVENT_API_ERROR
        and task.status == STATUS_ACTIVE
        and not _is_terminal(task)
    ):
        actions = actions + (ACTION_RETRY,)
    return actions


def ui_actions(task) -> tuple:
    """Return the UI-only actions available for the task's state (FR-09).

    ``open_result`` is offered once a final result exists; ``new_task`` is
    always available from the card; ``open_diagnostics`` is offered while the
    task is not terminal so the user can jump to the task diagnostics.
    """
    if task is None:
        return ()
    if task.stage == STAGE_DONE:
        return (ACTION_OPEN_RESULT, ACTION_NEW_TASK)
    if task.status == STATUS_CANCELLED:
        return (ACTION_NEW_TASK,)
    return (ACTION_NEW_TASK, ACTION_OPEN_DIAGNOSTICS)


def _refusal_reason(task, action, last_event_type, progress) -> str:
    """Return the most specific reason an action is not allowed (see FR-10)."""
    if task is None:
        return REASON_TASK_NOT_FOUND
    if _is_terminal(task):
        return REASON_TERMINAL
    if task.status == STATUS_PAUSED:
        return REASON_PAUSED
    if task.status == STATUS_BLOCKED:
        return REASON_BLOCKED
    if (
        action == ACTION_FINISH_EXECUTION
        and task.stage == STAGE_EXECUTION
        and progress
        and not all(step.completed for step in progress)
    ):
        return REASON_PROGRESS_INCOMPLETE
    if action == ACTION_RETRY and last_event_type != EVENT_API_ERROR:
        return REASON_RETRY_REQUIRES_API_ERROR
    if action in _STAGE_BOUND_ACTIONS:
        return REASON_EXPECTED_ACTION_MISMATCH
    return REASON_NOT_ALLOWED


def explain_transition(
    task, action, *, last_event_type=None, progress=None
) -> TransitionDecision:
    """Explain whether ``action`` is allowed for the task's current state.

    The allowed set comes from :func:`can_apply` (the single source of truth),
    so the decision can never drift from the FSM. Only a refused action gets a
    specific reason; an allowed one keeps :data:`REASON_ALLOWED`.
    """
    allowed_actions = can_apply(
        task, last_event_type=last_event_type, progress=progress
    )
    allowed = action in allowed_actions
    reason = REASON_ALLOWED if allowed else _refusal_reason(
        task, action, last_event_type, progress
    )
    decision = TransitionDecision(
        action=action,
        allowed=allowed,
        reason=reason,
        allowed_actions=tuple(allowed_actions),
    )
    return replace(decision, message=format_refusal(decision))


def format_allowed_actions(actions) -> str:
    """Render a set of actions as comma-separated labels, ``none`` when empty."""
    labels = [ACTION_LABELS.get(action, action) for action in (actions or ())]
    return ", ".join(labels) if labels else "none"


def format_refusal(decision) -> str:
    """Build the human refusal sentence of a decision.

    The positive sentence is used only when the decision is allowed *and* has no
    reason. An allowed action can still carry a non-empty reason: the
    orchestrator overrides the reason when a caller-specific precondition fails
    (for example a cancel that needs explicit confirmation while ``cancel`` is in
    the allowed set). That case is a refusal, so it is rendered with the reason
    and, when known, the actions that are allowed right now.
    """
    label = ACTION_LABELS.get(decision.action, decision.action)
    if decision.allowed and not decision.reason:
        return f"{label} is allowed in the current state."
    reason = REASON_TEXTS.get(decision.reason, decision.reason)
    if decision.allowed_actions:
        return (
            f"{label} is not allowed: {reason}. "
            f"Allowed now: {format_allowed_actions(decision.allowed_actions)}."
        )
    return (
        f"{label} is not allowed: {reason}. "
        "No action is allowed in the current state."
    )


def badge_for(task) -> str:
    """Return the textual badge of the task's status (FR-08)."""
    if task is None:
        return ""
    if task.status == STATUS_PAUSED:
        return BADGE_PAUSED
    if task.status == STATUS_BLOCKED:
        return BADGE_BLOCKED
    if task.status == STATUS_COMPLETED:
        return BADGE_COMPLETED
    if task.status == STATUS_CANCELLED:
        return BADGE_CANCELLED
    return BADGE_RUNNING


def validate_error_message(text) -> str:
    """Return a truncated error message that is safe to store.

    Provider errors can carry megabytes of echoed input; the journal keeps at
    most :data:`ERROR_MESSAGE_MAX_LENGTH` characters.
    """
    cleaned = str(text or "").strip()
    return cleaned[:ERROR_MESSAGE_MAX_LENGTH]


def creation_transition(
    chat_id,
    title,
    goal,
    *,
    workflow_profile_id=None,
    workflow_name=None,
    task_brief=None,
    task_id=None,
) -> TransitionResult:
    """Build the creation revision: a new planning/active task plus TASK_CREATED.

    The event has no idempotency key; a double form submission is guarded by the
    UI nonce. A non-empty brief becomes the first ``task_brief`` artifact.
    """
    cleaned_title = str(title or "").strip()
    cleaned_goal = str(goal or "").strip()
    if not cleaned_title:
        raise ValueError("Task title must not be empty")
    if not cleaned_goal:
        raise ValueError("Task goal must not be empty")

    task = Task(
        id=task_id,
        chat_id=chat_id,
        workflow_profile_id=workflow_profile_id,
        workflow_name=workflow_name,
        title=cleaned_title,
        goal=cleaned_goal,
        stage=STAGE_PLANNING,
        status=STATUS_ACTIVE,
        current_step=STAGE_STEP_LABELS[STAGE_PLANNING],
        current_step_index=None,
        expected_action_type=EXPECTED_RUN_PLANNING,
        expected_action_text=EXPECTED_ACTION_TEXTS[EXPECTED_RUN_PLANNING],
        pause_reason="",
        version=1,
    )
    event = TaskEvent(
        task_id=task_id,
        event_type=EVENT_TASK_CREATED,
        from_stage=None,
        to_stage=STAGE_PLANNING,
        from_status=None,
        to_status=STATUS_ACTIVE,
        payload={"title": cleaned_title, "goal": cleaned_goal},
        idempotency_key=None,
    )
    artifacts = []
    brief = str(task_brief or "").strip()
    if brief:
        artifacts.append(
            TaskArtifact(
                task_id=task_id,
                stage=STAGE_PLANNING,
                kind=ARTIFACT_TASK_BRIEF,
                content={"text": brief},
            )
        )
    return TransitionResult(
        task=task, event=event, artifacts=artifacts, idempotency_key=None
    )


# --- Text formatters -------------------------------------------------------


def format_snapshot_block(task) -> str:
    """Render the task snapshot used as a context-packet block (FR-27)."""
    lines = ["Снимок задачи (текущее состояние):"]
    lines.append(f"- Название: {task.title}")
    lines.append(f"- Цель: {task.goal}")
    lines.append(f"- Стадия: {task.stage}")
    lines.append(f"- Статус: {task.status}")
    lines.append(f"- Версия: {task.version}")
    current_step = (task.current_step or "").strip() or "не определён"
    if task.current_step_index is not None:
        current_step = f"{current_step} (шаг {task.current_step_index})"
    lines.append(f"- Текущий шаг: {current_step}")
    action = (task.expected_action_text or "").strip() or "не требуется"
    lines.append(f"- Ожидаемое действие: {action}")
    reason = (task.pause_reason or "").strip()
    if reason:
        lines.append(f"- Причина паузы/блокировки: {reason}")
    return "\n".join(lines)


def format_defects_block(progress) -> str:
    """Render the defects of the steps awaiting rework, or an empty string."""
    awaiting = [step for step in (progress or []) if getattr(step, "awaiting_rework", False)]
    if not awaiting:
        return ""
    lines = ["Дефекты, требующие переделки:"]
    for step in sorted(awaiting, key=lambda item: item.step_index):
        defects = list(getattr(step, "defects", ()) or ())
        if not defects:
            lines.append(f"- Шаг {step.step_index} «{step.title}»: требуется переделка")
            continue
        for defect in defects:
            lines.append(f"- Шаг {step.step_index} «{step.title}»: {defect}")
    return "\n".join(lines)


def _number(value) -> str:
    return str(value) if value is not None else "n/a"


def format_task_usage_line(usage) -> str:
    """Render one compact usage line for the task diagnostics panel."""
    if usage is None:
        return "No task usage recorded"
    cost = getattr(usage, "cost_usd", None)
    cost_text = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
    parts = [
        f"Calls: {_number(getattr(usage, 'calls', None))}",
        f"Attempts: {_number(getattr(usage, 'attempts', None))}",
        (
            f"Tokens: {_number(getattr(usage, 'input_tokens', None))} in / "
            f"{_number(getattr(usage, 'output_tokens', None))} out"
        ),
        f"Cost: {cost_text}",
    ]
    return " · ".join(parts)


def format_specification_markdown(goal, task_brief, plan) -> str:
    """Render the ``specification`` artifact markdown for a plan."""
    plan = plan if isinstance(plan, dict) else {}
    lines = ["# Task specification", "", "## Goal", str(goal or "").strip(), ""]
    brief = str(task_brief or "").strip()
    if brief:
        lines.extend(["## Task brief", brief, ""])
    summary = str(plan.get("summary") or "").strip()
    if summary:
        lines.extend(["## Plan summary", summary, ""])
    steps = plan.get("steps") or []
    if steps:
        lines.append("## Steps")
        for step in steps:
            lines.append(f"{step.get('index', '?')}. **{step.get('title', '')}**")
            description = str(step.get("description") or "").strip()
            if description:
                lines.append(f"   {description}")
        lines.append("")
    criteria = plan.get("acceptance_criteria") or []
    if criteria:
        lines.append("## Acceptance criteria")
        lines.extend(f"- {criterion}" for criterion in criteria)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_final_result_markdown(task, plan=None, executions=None, validation=None) -> str:
    """Render the ``final_result`` artifact markdown of a completed task."""
    title = (getattr(task, "title", "") or "Task").strip()
    lines = [f"# Task result: {title}", ""]
    goal = str(getattr(task, "goal", "") or "").strip()
    if goal:
        lines.extend(["## Goal", goal, ""])

    results: dict = {}
    for item in executions or []:
        content = _content_of(item)
        index = content.get("step_index")
        if isinstance(index, int) and not isinstance(index, bool):
            results.setdefault(index, []).append(content)

    steps = (plan or {}).get("steps") if isinstance(plan, dict) else None
    if steps:
        lines.append("## Steps and results")
        for step in steps:
            index = step.get("index")
            lines.append(f"### {index}. {step.get('title', '')}")
            entries = results.get(index, [])
            text = str(entries[-1].get("text") or "").strip() if entries else ""
            lines.append(text or "_No result recorded._")
            lines.append("")

    verdict = validation if isinstance(validation, dict) else {}
    if verdict:
        passed = verdict.get("passed")
        lines.extend(["## Validation", f"Verdict: {'passed' if passed else 'failed'}"])
        for defect in verdict.get("defects") or []:
            lines.append(f"- Step {defect.get('step_index')}: {defect.get('description')}")
        notes = str(verdict.get("notes") or "").strip()
        if notes:
            lines.extend(["", notes])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
