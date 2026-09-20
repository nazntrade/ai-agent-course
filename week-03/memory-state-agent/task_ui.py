"""Streamlit UI of the Day 13 task state machine.

The module has two layers. The pure formatters (``format_stage_indicator``,
``badge_label``, ``format_task_compact``, ``format_task_details``, ``shorten``)
build strings only and are covered by unit tests without Streamlit. The render
functions draw the task card in ``Chat`` and the ``Diagnostics / Task`` panel;
they read every piece of state through ``TaskOrchestrator`` and never call the
provider during rendering.

The card is purely presentational: the set of actions it shows is exactly
``TaskOrchestrator.allowed_actions`` (the single source of truth from
``tasks.can_apply`` plus the UI-only actions), and every click delegates to the
matching use case. ``Run step``/``Retry`` only start a process-level background
run through ``task_runner``; ``app.py`` then follows that run while the provider
call keeps going independently of the Streamlit reruns. The domain still rejects
a disallowed action, so a stale card cannot change the stage directly.
"""

from __future__ import annotations

import html
import json

import streamlit as st

from invariant_ui import format_active_restrictions, invariant_event_rows
from invariant_ui import render_invariants_section
from task_orchestrator import STATUS_SUCCESS
from task_runner import start_step_run, step_run_busy
from task_storage import DEFAULT_WORKFLOW_NAME
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_BLOCK,
    ACTION_CANCEL,
    ACTION_FINISH_EXECUTION,
    ACTION_LABELS,
    ACTION_NEW_TASK,
    ACTION_OPEN_DIAGNOSTICS,
    ACTION_OPEN_RESULT,
    ACTION_PAUSE,
    ACTION_REJECT_PLAN,
    ACTION_RESUME,
    ACTION_RETRY,
    ACTION_RUN_PLANNING,
    ACTION_RUN_STEP,
    ACTION_RUN_VALIDATION,
    ACTION_UNBLOCK,
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_FINAL_RESULT,
    EVENT_STEP_COMPLETED,
    EXPECTED_CONFIRM_PLAN,
    EXPECTED_FINISH_EXECUTION,
    EXPECTED_RUN_PLANNING,
    EXPECTED_RUN_STEP,
    EXPECTED_RUN_VALIDATION,
    STAGE_DONE,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_STEP_LABELS,
    STAGE_VALIDATION,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    badge_for,
    compute_step_progress,
    format_allowed_actions,
    plan_steps,
)

# The mode radio lives in ``app.py``; the task card must switch the mode when the
# user asks for the task diagnostics, so the two keys are defined here and
# imported by the app. Defining them in one place keeps the values in sync.
MODE_KEY = "ui_mode"
MODE_TASK = "Diagnostics / Task"

# A plain, non-widget key used to request the task diagnostics mode. Streamlit
# forbids writing the widget key ``MODE_KEY`` after the radio was created in the
# same run, so the card and the result dialog record the request here and rerun;
# ``app.py`` applies it before it creates the radio.
PENDING_MODE_KEY = "task_pending_mode"

STAGE_ORDER = (STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION, STAGE_DONE)

# Visual states of the vertical stage indicator (FR-33, NFR-07). Every state is
# encoded by contrast, font size, weight, strike-through and a marker symbol, so
# the indicator stays readable without colour perception.
_STAGE_STYLES = {
    "passed": "opacity:0.45; font-size:0.92rem; text-decoration:line-through;",
    "current": "opacity:1; font-size:1.10rem; font-weight:700;",
    "upcoming": "opacity:0.5; font-size:0.90rem; text-decoration:none;",
}
_STAGE_MARKERS = {"passed": "✓", "current": "●", "upcoming": "○"}

# Session-state keys of the modal dialogs and the two-step forms. The pop in the
# success path closes the dialog, so a rerun never reopens it. The keys are
# distinct from every widget key: Streamlit forbids assigning a widget key
# through ``st.session_state``.
DIALOG_CREATE_KEY = "task_create_dialog_open"
DIALOG_RESULT_KEY = "task_result_dialog_open"
DIALOG_DETAILS_KEY = "task_details_dialog_task"
DIALOG_PLAN_KEY = "task_plan_dialog_task"
BLOCK_FORM_KEY = "task_block_form_open"
CANCEL_CONFIRM_KEY = "task_cancel_confirm_task"
TASK_ERROR_KEY = "task_action_error"

# Plain (non-widget) key holding the last guard-probe result of this session as
# ``(task_id, TransitionProbeResult)``. The probe button stores and reruns, so
# the result survives the rerun and is rendered only for its own task.
GUARD_PROBE_KEY = "task_guard_probe_result"

# Session handle on the process-level step run started by ``Run step``/``Retry``.
# The call itself lives in ``task_runner`` and survives every rerun; this plain
# key only lets ``app.py`` follow the run this session started.
STEP_RUN_TOKEN_KEY = "task_step_run_token"

# Green used to highlight the recommended card action. The selector pins the
# exact widget key class (``class~=``), so ``run_step`` never matches a longer
# key such as ``run_step_extra``.
RECOMMENDED_ACTION_COLOR = "#2e7d32"


# --- Pure formatters ------------------------------------------------------


def shorten(text, limit=80) -> str:
    """Collapse whitespace and truncate ``text`` to ``limit`` characters.

    The card keeps long titles, goals and error texts on one line; the full text
    stays available in the dialogs and the diagnostics panels.
    """
    cleaned = " ".join(str(text or "").split())
    if limit is None or limit <= 0 or len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def badge_label(task) -> str:
    """Return the textual status badge of the task (FR-08)."""
    return badge_for(task)


def format_task_compact(task, plan=None) -> str:
    """Return one compact summary line of the task for the completed card."""
    if task is None:
        return ""
    parts = [shorten(task.title, 60)]
    parts.append(f"{task.stage}/{task.status}")
    steps = plan_steps(plan)
    if steps:
        parts.append(f"{len(steps)} steps")
    parts.append(f"v{task.version}")
    return " · ".join(part for part in parts if part)


def event_summary(event) -> str:
    """Return one short line describing a journal event, or a placeholder."""
    if event is None:
        return "no events yet"
    parts = [str(event.event_type or "unknown")]
    if event.from_stage or event.to_stage:
        parts.append(f"stage {event.from_stage or '—'} → {event.to_stage or '—'}")
    if event.from_status or event.to_status:
        parts.append(
            f"status {event.from_status or '—'} → {event.to_status or '—'}"
        )
    if event.created_at:
        parts.append(str(event.created_at))
    return " · ".join(parts)


def format_task_details(task, last_event=None) -> str:
    """Return the full, unshortened task text of the details dialog (FR-33).

    The card shortens long values to stay within its height budget; this
    formatter keeps every field intact, so the whole title, goal, step, block
    reason and expected user action stay readable in the modal window.
    """
    if task is None:
        return ""
    lines = [
        f"**{task.title}**",
        f"Goal: {task.goal or '—'}",
        f"Stage: {task.stage} · Status: {task.status} · Version: {task.version}",
    ]
    step = task.current_step or "—"
    if task.current_step_index is not None:
        step = f"{step} (step {task.current_step_index})"
    lines.append(f"Current step: {step}")
    if task.status == STATUS_BLOCKED:
        lines.append(f"Blocked: {task.pause_reason or '—'}")
        lines.append(f"Expected from you: {task.expected_action_text or '—'}")
    else:
        if task.pause_reason:
            lines.append(f"Pause reason: {task.pause_reason}")
        lines.append(f"Expected action: {task.expected_action_text or '—'}")
    lines.append(f"Last event: {event_summary(last_event)}")
    return "  \n".join(lines)


def _plan_summary_line(plan) -> str:
    return f"**Summary:** {plan.get('summary') or '—'}"


def _plan_criteria_block(criteria) -> str:
    """Render the acceptance criteria, or a placeholder when there are none."""
    cleaned = []
    if isinstance(criteria, list):
        for criterion in criteria:
            text = str(criterion).strip() if criterion is not None else ""
            if text:
                cleaned.append(text)
    if not cleaned:
        return "**Acceptance criteria:** —"
    lines = ["**Acceptance criteria**"]
    lines.extend(f"- {criterion}" for criterion in cleaned)
    return "\n".join(lines)


def _plan_steps_block(plan) -> str:
    """Render the normalized plan steps, or a placeholder when there are none."""
    steps = plan_steps(plan)
    if not steps:
        return "**Steps:** —"
    lines = []
    for position, step in enumerate(steps):
        if position:
            lines.append("")
        lines.append(f"**{step['index']}. {step['title']}**")
        description = str(step.get("description") or "").strip()
        if description:
            lines.append(description)
    return "\n".join(lines)


def _json_text(value) -> str:
    """Serialize ``value`` to compact JSON, falling back to ``str``."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def format_plan(plan) -> str:
    """Return the full, readable plan text (FR: plan review).

    Every known field is rendered as readable markdown; unknown fields are
    appended as a JSON list so no information of a future plan shape is lost.
    The function is pure and never raises: anything that is not a mapping is
    treated as "no plan yet", and malformed plans degrade to placeholders.
    """
    if not isinstance(plan, dict):
        return "No plan yet."

    blocks = [
        _plan_summary_line(plan),
        _plan_criteria_block(plan.get("acceptance_criteria")),
        _plan_steps_block(plan),
    ]

    known = ("summary", "acceptance_criteria", "steps")
    extra = [
        (key, value) for key, value in plan.items() if key not in known
    ]
    if extra:
        lines = ["**Other fields**"]
        lines.extend(f"- {key}: {_json_text(value)}" for key, value in extra)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- Step results ---------------------------------------------------------


def running_step_label(task, plan) -> str:
    """Spinner text "Running step N of M: <title>", or a generic fallback."""
    if task is None:
        return "Running the task step..."
    index = task.current_step_index
    title = str(task.current_step or "").strip()
    steps = plan_steps(plan)
    if isinstance(index, int) and not isinstance(index, bool) and steps and title:
        return f"Running step {index} of {len(steps)}: {title}"
    return "Running the task step..."


def _result_order(artifact) -> tuple:
    """Sort key ``(revision, id)`` of a stored artifact.

    A non-integer revision contributes 0, so an artifact with a missing or
    malformed revision sorts before the stored revisions and never wins as the
    latest view by accident.
    """
    revision = getattr(artifact, "revision", None)
    artifact_id = getattr(artifact, "id", None)
    return (
        revision if isinstance(revision, int) and not isinstance(revision, bool) else 0,
        artifact_id if isinstance(artifact_id, int) and not isinstance(artifact_id, bool) else 0,
    )


def build_step_results(plan, artifacts, events=()) -> list:
    """Merge plan steps, progress and the latest result artifact per step.

    Only steps that have a non-empty stored result are returned. ``is_latest``
    marks the view of the most recent artifact, so the panel can expand exactly
    the last completed step. Every view also carries ``journal``: the stored
    ``STEP_COMPLETED`` event of that step revision, so the result panel shows a
    code-rendered identifier instead of the model's text. The view shape is
    stable for the UI and the tests.
    """
    steps = plan_steps(plan)
    if not steps:
        return []
    progress = {
        item.step_index: item for item in compute_step_progress(plan, artifacts)
    }
    latest_by_step: dict = {}
    for artifact in artifacts or []:
        if getattr(artifact, "kind", None) != ARTIFACT_EXECUTION_RESULT:
            continue
        content = getattr(artifact, "content", None)
        if not isinstance(content, dict):
            continue
        index = content.get("step_index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        current = latest_by_step.get(index)
        if current is None or _result_order(artifact) > _result_order(current):
            latest_by_step[index] = artifact

    journal = _journal_events_by_step(events)
    views = []
    latest_position = None
    latest_order = None
    for step in steps:
        index = step["index"]
        artifact = latest_by_step.get(index)
        if artifact is None:
            continue
        text = str((artifact.content or {}).get("text") or "").strip()
        if not text:
            continue
        step_progress = progress.get(index)
        round_value = (artifact.content or {}).get("round")
        if not isinstance(round_value, int) or isinstance(round_value, bool):
            round_value = step_progress.round if step_progress is not None else 0
        views.append(
            {
                "step_index": index,
                "total": len(steps),
                "title": (
                    step_progress.title
                    if step_progress is not None and step_progress.title
                    else step["title"]
                ),
                "round": round_value,
                "text": text,
                "awaiting_rework": bool(
                    step_progress.awaiting_rework
                ) if step_progress is not None else False,
                "defects": list(step_progress.defects) if step_progress is not None else [],
                "journal": journal.get((index, round_value)),
                "is_latest": False,
            }
        )
        order = _result_order(artifact)
        if latest_order is None or order > latest_order:
            latest_order = order
            latest_position = len(views) - 1
    if latest_position is not None:
        views[latest_position]["is_latest"] = True
    return views


def _journal_events_by_step(events) -> dict:
    """Map ``(step_index, round)`` to its stored ``STEP_COMPLETED`` event."""
    journal: dict = {}
    for event in events or ():
        if getattr(event, "event_type", None) != EVENT_STEP_COMPLETED:
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        index = payload.get("step_index")
        round_value = payload.get("round")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not isinstance(round_value, int) or isinstance(round_value, bool):
            continue
        journal[(index, round_value)] = event
    return journal


def format_result_journal_line(event) -> str:
    """Render the stored journal event of a step result as a single line.

    The identifier, event type, timestamp and stage transition are taken from
    the stored event only, so the line can never inherit an invented reference
    from the model's step text. Without an event the line is empty.
    """
    if event is None:
        return ""
    parts = [
        f"#{getattr(event, 'id', None)}",
        str(getattr(event, "event_type", "") or ""),
    ]
    created = getattr(event, "created_at", None)
    if created:
        parts.append(str(created))
    from_stage = getattr(event, "from_stage", None) or "—"
    to_stage = getattr(event, "to_stage", None) or "—"
    parts.append(f"{from_stage}→{to_stage}")
    return "Journal: " + " · ".join(part for part in parts if part)


def step_result_label(view) -> str:
    """One-line expander label of a stored step result."""
    label = f"Step {view['step_index']} of {view['total']}: {view['title']}"
    if view.get("round", 0) > 1:
        label += f" · revision {view['round']}"
    if view.get("awaiting_rework"):
        label += " · awaiting rework"
    return label


def format_next_action(task, plan) -> str:
    """Readable "what happens next" line for the results panel and diagnostics."""
    if task is None:
        return "No active task."
    if task.status == STATUS_CANCELLED:
        return "Task cancelled."
    if task.stage == STAGE_DONE or task.status == STATUS_COMPLETED:
        return "Task completed."
    if task.status == STATUS_BLOCKED:
        return f"Expected from you: {task.expected_action_text or '—'}"
    if task.status == STATUS_PAUSED:
        return f"Paused. Next action: {task.expected_action_text or '—'}"

    expected = task.expected_action_type
    if expected == EXPECTED_RUN_STEP:
        steps = plan_steps(plan)
        index = task.current_step_index
        title = str(task.current_step or "").strip()
        if isinstance(index, int) and not isinstance(index, bool) and steps and title:
            return f"Next: Run step {index} of {len(steps)}: {title}"
        return "Next: Run the current step"
    if expected == EXPECTED_RUN_VALIDATION:
        return "Next: Run validation"
    if expected == EXPECTED_CONFIRM_PLAN:
        return "Next: Accept or reject the plan"
    if expected == EXPECTED_RUN_PLANNING:
        return "Next: Run planning"
    if expected == EXPECTED_FINISH_EXECUTION:
        return "Next: Finish execution"
    text = str(task.expected_action_text or "").strip()
    return f"Next: {text}" if text else ""


# --- Transition guard formatters (Day 15) ---------------------------------

# The expected action of each FSM state maps to the single action the card
# should suggest. A missing or terminal expectation yields no recommendation.
_RECOMMENDED_ACTIONS = {
    EXPECTED_CONFIRM_PLAN: ACTION_ACCEPT_PLAN,
    EXPECTED_RUN_PLANNING: ACTION_RUN_PLANNING,
    EXPECTED_RUN_STEP: ACTION_RUN_STEP,
    EXPECTED_FINISH_EXECUTION: ACTION_FINISH_EXECUTION,
    EXPECTED_RUN_VALIDATION: ACTION_RUN_VALIDATION,
}


def recommended_action(task) -> str | None:
    """Return the action the current state suggests, or ``None``."""
    if task is None:
        return None
    return _RECOMMENDED_ACTIONS.get(task.expected_action_type)


def is_review_plan_recommended(task) -> bool:
    """Whether the plan review is the decision the state asks for."""
    return task is not None and task.expected_action_type == EXPECTED_CONFIRM_PLAN


def recommended_action_css(task) -> str:
    """Return the CSS that highlights the recommended button, or an empty string.

    The selector matches the exact Streamlit key class (``class~=``) of the
    recommended card action and of the plan review button, so a key prefix
    cannot highlight an unrelated widget.
    """
    selectors = []
    action = recommended_action(task)
    if action:
        selectors.append(f'div[class~="st-key-task_action_{action}"] button')
    if is_review_plan_recommended(task):
        selectors.append('div[class~="st-key-task_review_plan"] button')
    if not selectors:
        return ""
    joined = ",\n".join(selectors)
    return (
        "<style>\n"
        f"{joined} {{\n"
        f"    background-color: {RECOMMENDED_ACTION_COLOR} !important;\n"
        "    color: #ffffff !important;\n"
        f"    border-color: {RECOMMENDED_ACTION_COLOR} !important;\n"
        "}\n"
        "</style>"
    )


def guard_decision_rows(decisions) -> list:
    """Dataframe rows explaining whether every domain action is allowed."""
    rows = []
    for decision in decisions or ():
        rows.append(
            {
                "Action": ACTION_LABELS.get(decision.action, decision.action),
                "Allowed": "yes" if decision.allowed else "no",
                "Reason": decision.reason,
            }
        )
    return rows


def transition_attempt_rows(attempts) -> list:
    """Dataframe rows of the refusal audit.

    The column names deliberately avoid ``event`` and ``invariant_event``, so a
    consumer that looks for the task timeline or the invariant journal never
    confuses them with the Day 15 refusal audit.
    """
    rows = []
    for attempt in attempts or ():
        rows.append(
            {
                "attempt": attempt.id,
                "action": ACTION_LABELS.get(attempt.action, attempt.action),
                "from state": (
                    f"{attempt.from_stage or '—'}/{attempt.from_status or '—'}"
                ),
                "reason": attempt.reason,
                "allowed then": format_allowed_actions(attempt.allowed_actions),
                "created": attempt.created_at or "no data",
            }
        )
    return rows


def format_guard_probe_line(probe) -> str:
    """Render one guard-probe result from the stored probe fact only.

    A soft refusal shows the append-only audit id and the reason read back from
    ``task_transition_attempts``; a hard invariant refusal or a missing audit
    row keeps an honest message without inventing an id, and an allowed action
    says that nothing was written.
    """
    if probe is None:
        return ""
    action = str(getattr(probe, "action", "") or "")
    if getattr(probe, "allowed", False):
        return f"{action}: allowed now; nothing was written."
    error = str(getattr(probe, "error", "") or "")
    if error:
        return f"{action}: {error}"
    reason = str(getattr(probe, "reason", "") or "")
    audit_id = getattr(probe, "audit_id", None)
    if audit_id is not None:
        return f"Refusal audit #{audit_id}: {action} — {reason}"
    message = str(getattr(probe, "message", "") or "")
    if message:
        return f"{action}: {message}"
    return f"{action}: refused ({reason})." if reason else f"{action}: refused."


def _json_block(content) -> str:
    """Return ``content`` as a fenced JSON code block.

    Used instead of ``st.json`` so long artifacts stay in the normal document
    flow and can be scrolled to the end inside the page.
    """
    try:
        body = json.dumps(content, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        body = str(content)
    return f"```json\n{body}\n```"


def _stage_state(position, current) -> str:
    if position < current:
        return "passed"
    if position == current:
        return "current"
    return "upcoming"


def _current_stage_details(task) -> str:
    """Return the ``current_step``/expected-action line of the current stage.

    A blocked task shows the reason and the action the user owes instead of the
    next model action, because the task will not continue until the user
    unblocks it.
    """
    parts = []
    step = shorten(task.current_step, 60)
    if step:
        parts.append(f"Step: {html.escape(step)}")
    if task.status == STATUS_BLOCKED:
        reason = shorten(task.pause_reason, 70)
        if reason:
            parts.append(f"Blocked: {html.escape(reason)}")
        expected = shorten(task.expected_action_text, 70)
        if expected:
            parts.append(f"Expected from you: {html.escape(expected)}")
    else:
        expected = shorten(task.expected_action_text, 70)
        if expected:
            parts.append(f"Next: {html.escape(expected)}")
    return " · ".join(parts)


def _stage_row(stage, state, task, show_details) -> str:
    """Render one stage of the vertical indicator as a single ``div``."""
    label = STAGE_STEP_LABELS.get(stage, stage)
    style = _STAGE_STYLES[state]
    marker = _STAGE_MARKERS[state]
    badge = ""
    if show_details:
        badge = (
            " <span style=\"font-size:0.80rem; font-weight:600; "
            "letter-spacing:0.04em;\">["
            f"{html.escape(badge_label(task))}]</span>"
        )
    details = ""
    if show_details:
        text = _current_stage_details(task)
        if text:
            details = f'<br><span style="font-size:0.85rem;">{text}</span>'
    return (
        f'<div class="task-stage task-stage-{state}" style="{style}">'
        f'<span aria-hidden="true">{marker}</span> {html.escape(label)}'
        f"{badge}{details}</div>"
    )


def format_stage_indicator(task, stages=STAGE_ORDER) -> str:
    """Render the vertical stage indicator with the badge and the current details.

    Passed stages are struck through and dimmed, the current stage is larger and
    bolder with an active marker, and future stages are dimmed with an empty
    marker. A thin vertical line connects the markers. The whole indicator is
    escaped HTML, so user text cannot break the card.
    """
    if task is None:
        return ""
    ordered = tuple(stages) or STAGE_ORDER
    try:
        current = ordered.index(task.stage)
    except ValueError:
        current = 0

    rows = []
    for position, stage in enumerate(ordered):
        state = _stage_state(position, current)
        rows.append(_stage_row(stage, state, task, state == "current"))
        if position < len(ordered) - 1:
            rows.append(
                '<div class="task-stage-link" style="border-left:1px solid '
                'rgba(128, 128, 128, 0.6); height:4px; margin-left:0.45em;">'
                "</div>"
            )
    return '<div class="task-stage-indicator">' + "".join(rows) + "</div>"


# --- Small helpers --------------------------------------------------------


def _fmt_num(value):
    return "no data" if value is None else str(value)


def _fmt_cost(value):
    return "no data" if value is None else f"≈${value:.6f}"


def _final_result_text(orchestrator, task_id) -> str:
    """Return the stored ``final_result`` markdown of a completed task."""
    artifacts = orchestrator.repository.list_artifacts(
        task_id, kind=ARTIFACT_FINAL_RESULT
    )
    if not artifacts:
        return ""
    content = artifacts[-1].content or {}
    return str(content.get("markdown") or "")


def _task_details_line(task) -> str:
    return (
        f"{shorten(task.title, 60)} · v{task.version} · "
        f"updated {task.updated_at or 'no data'}"
    )


# --- Dialogs and shared forms --------------------------------------------


def request_task_mode() -> None:
    """Request the ``Diagnostics / Task`` mode for the next run.

    ``MODE_KEY`` belongs to the mode radio, so it cannot be written while the
    radio of the current run exists. The request goes into ``PENDING_MODE_KEY``
    and is applied by ``app.py`` before the radio is created.
    """
    st.session_state[PENDING_MODE_KEY] = MODE_TASK


def _dismiss_create_dialog():
    st.session_state.pop(DIALOG_CREATE_KEY, None)
    # A fresh nonce clears the fields, so reopening the dialog starts empty.
    nonce = st.session_state.get("task_dialog_create_nonce", 0)
    st.session_state["task_dialog_create_nonce"] = nonce + 1


def _dismiss_result_dialog():
    st.session_state.pop(DIALOG_RESULT_KEY, None)


def _dismiss_details_dialog():
    st.session_state.pop(DIALOG_DETAILS_KEY, None)


def _dismiss_plan_dialog():
    st.session_state.pop(DIALOG_PLAN_KEY, None)


def render_create_task_form(
    orchestrator, chat_id, *, key_prefix, dialog_key=None
):
    """Render the create-task fields and persist a valid submission.

    The same form serves the modal dialog and the ``Diagnostics / Task``
    expander, so creating a task stays available and testable without opening a
    dialog. A nonce resets the fields after a successful creation.
    """
    nonce = st.session_state.get(f"{key_prefix}_nonce", 0)
    title = st.text_input("Title", key=f"{key_prefix}_title_{nonce}")
    goal = st.text_input("Goal", key=f"{key_prefix}_goal_{nonce}")
    task_brief = st.text_area("Task brief", key=f"{key_prefix}_brief_{nonce}")

    workflows = orchestrator.repository.list_workflows() or []
    workflow_names = [
        workflow.display_name or workflow.name for workflow in workflows
    ]
    if not workflow_names:
        workflow_names = [DEFAULT_WORKFLOW_NAME]
    st.selectbox("Workflow", options=workflow_names, key=f"{key_prefix}_workflow_{nonce}")
    st.caption("Day 13 ships the default workflow; every new task uses it.")

    if st.button("Create task", key=f"{key_prefix}_submit_{nonce}"):
        if not title.strip():
            st.error("Title must not be empty.")
        elif not goal.strip():
            st.error("Goal must not be empty.")
        else:
            result = orchestrator.create_task(
                chat_id, title, goal, task_brief=task_brief
            )
            if result.status == STATUS_SUCCESS:
                st.session_state[f"{key_prefix}_nonce"] = nonce + 1
                if dialog_key is not None:
                    st.session_state.pop(dialog_key, None)
                st.rerun()
            else:
                st.error(
                    "Could not create the task: "
                    f"{result.error_message or 'unknown error'}"
                )


@st.dialog("New task", on_dismiss=_dismiss_create_dialog)
def render_create_task_dialog(orchestrator, chat_id):
    """Modal create-task dialog opened from the chat card (FR-34)."""
    render_create_task_form(
        orchestrator,
        chat_id,
        key_prefix="task_dialog_create",
        dialog_key=DIALOG_CREATE_KEY,
    )


@st.dialog("Task result", on_dismiss=_dismiss_result_dialog)
def render_open_result_dialog(task, final_result):
    """Modal with the full final result and a jump to the task diagnostics (FR-36)."""
    st.markdown(f"**{task.title}**")
    st.caption("Full text; use the copy icon inside the code block.")
    st.code(final_result or "")
    if st.button("Open task diagnostics", key="task_result_diagnostics"):
        request_task_mode()
        st.session_state.pop(DIALOG_RESULT_KEY, None)
        st.rerun()


@st.dialog("Task details", on_dismiss=_dismiss_details_dialog)
def render_task_details_dialog(orchestrator, task):
    """Modal with the full, unshortened task text of the card (FR-33)."""
    events = orchestrator.repository.list_events(task.id)
    st.markdown(format_task_details(task, events[-1] if events else None))


@st.dialog("Review plan", on_dismiss=_dismiss_plan_dialog)
def render_plan_dialog(orchestrator, task, plan):
    """Modal plan review with the accept/reject actions on one spot.

    The dialog keys are deliberately distinct from the card's ``task_action_*``
    keys: both the card and the dialog are rendered in the same run, and a
    duplicate key would make the Streamlit widget state ambiguous.
    """
    if not isinstance(plan, dict):
        st.caption("No plan yet.")
        return

    st.markdown(format_plan(plan))
    col_accept, col_reject = st.columns(2)
    if col_accept.button("Accept plan", key="task_plan_dialog_accept"):
        result = _dispatch_action(orchestrator, task, ACTION_ACCEPT_PLAN)
        if result is not None and result.ok:
            st.session_state.pop(DIALOG_PLAN_KEY, None)
            st.rerun()
        message = getattr(result, "error_message", None)
        st.error(message or "The plan could not be accepted.")
    if col_reject.button("Reject plan", key="task_plan_dialog_reject"):
        result = _dispatch_action(orchestrator, task, ACTION_REJECT_PLAN)
        if result is not None and result.ok:
            st.session_state.pop(DIALOG_PLAN_KEY, None)
            st.rerun()
        message = getattr(result, "error_message", None)
        st.error(message or "The plan could not be rejected.")


# --- Chat card ------------------------------------------------------------


def _render_pending_dialogs(orchestrator, chat_id):
    """Open at most one modal per run, only when its trigger flag is set."""
    if st.session_state.get(DIALOG_CREATE_KEY) and chat_id is not None:
        render_create_task_dialog(orchestrator, chat_id)
        return

    result_task_id = st.session_state.get(DIALOG_RESULT_KEY)
    if result_task_id is not None:
        task = orchestrator.repository.get_task(result_task_id)
        if task is None:
            st.session_state.pop(DIALOG_RESULT_KEY, None)
        else:
            render_open_result_dialog(
                task, _final_result_text(orchestrator, result_task_id)
            )
            return

    details_task_id = st.session_state.get(DIALOG_DETAILS_KEY)
    if details_task_id is not None:
        task = orchestrator.repository.get_task(details_task_id)
        if task is None:
            st.session_state.pop(DIALOG_DETAILS_KEY, None)
        else:
            render_task_details_dialog(orchestrator, task)
            return

    plan_task_id = st.session_state.get(DIALOG_PLAN_KEY)
    if plan_task_id is None:
        return
    task = orchestrator.repository.get_task(plan_task_id)
    if task is None:
        st.session_state.pop(DIALOG_PLAN_KEY, None)
        return
    render_plan_dialog(
        orchestrator, task, orchestrator.repository.load_plan(task.id)
    )


def _render_invitation(orchestrator, chat_id):
    """Compact "no task yet" line with the create action on the same spot."""
    st.markdown("No active task")
    if st.button("New task", key="task_new_task"):
        st.session_state[DIALOG_CREATE_KEY] = True
        st.rerun()


def _render_cancel_confirmation(orchestrator, task):
    """Two-step cancel: the first click only asks for confirmation (FR-25)."""
    st.warning(f'Cancel task "{shorten(task.title, 60)}"? This action is irreversible.')
    col_yes, col_no = st.columns(2)
    if col_yes.button("Yes, cancel task", key="task_cancel_confirm"):
        result = orchestrator.cancel(task.id, confirmed=True)
        if result.ok:
            st.session_state.pop(CANCEL_CONFIRM_KEY, None)
            st.rerun()
        else:
            st.error(result.error_message or "The task could not be cancelled.")
    if col_no.button("Keep task", key="task_cancel_keep"):
        st.session_state.pop(CANCEL_CONFIRM_KEY, None)
        st.rerun()


def _render_block_form(orchestrator, task):
    """Block form: the reason and the action the user has to provide (FR-24)."""
    reason = st.text_input("Block reason", key="task_block_reason")
    expected = st.text_input(
        "Expected action from you", key="task_block_expected_action"
    )
    col_block, col_cancel = st.columns(2)
    if col_block.button("Block task", key="task_block_submit"):
        if not reason.strip():
            st.error("A block reason is required.")
        elif not expected.strip():
            st.error("The expected user action is required.")
        else:
            result = orchestrator.block(task.id, reason, expected)
            if result.ok:
                st.session_state.pop(BLOCK_FORM_KEY, None)
                st.rerun()
            else:
                st.error(result.error_message or "The task could not be blocked.")
    if col_cancel.button("Cancel", key="task_block_cancel"):
        st.session_state.pop(BLOCK_FORM_KEY, None)
        st.rerun()


def _dispatch_action(orchestrator, task, action):
    """Run one card action and return its result (``None`` when handled here)."""
    if action == ACTION_RUN_PLANNING:
        with st.spinner("Running planning..."):
            return orchestrator.run_planning(task.id)
    if action == ACTION_RUN_VALIDATION:
        with st.spinner("Running validation..."):
            return orchestrator.run_validation(task.id)
    if action == ACTION_ACCEPT_PLAN:
        return orchestrator.accept_plan(task.id)
    if action == ACTION_REJECT_PLAN:
        return orchestrator.reject_plan(task.id)
    if action == ACTION_FINISH_EXECUTION:
        return orchestrator.finish_execution(task.id)
    if action == ACTION_PAUSE:
        return orchestrator.pause(task.id)
    if action == ACTION_RESUME:
        return orchestrator.resume(task.id)
    if action == ACTION_UNBLOCK:
        return orchestrator.unblock(task.id)
    if action == ACTION_CANCEL:
        return orchestrator.cancel(task.id, confirmed=True)
    return None


def _render_action_button(orchestrator, task, action):
    label = ACTION_LABELS.get(action, action)
    key = f"task_action_{action}"

    if action == ACTION_CANCEL:
        if st.button(label, key=key):
            st.session_state[CANCEL_CONFIRM_KEY] = task.id
            st.rerun()
        return
    if action == ACTION_BLOCK:
        if st.button(label, key=key):
            st.session_state[BLOCK_FORM_KEY] = task.id
            st.rerun()
        return
    if action == ACTION_NEW_TASK:
        if st.button(label, key=key):
            st.session_state[DIALOG_CREATE_KEY] = True
            st.rerun()
        return
    if action == ACTION_OPEN_RESULT:
        if st.button(label, key=key):
            st.session_state[DIALOG_RESULT_KEY] = task.id
            st.rerun()
        return
    if action == ACTION_OPEN_DIAGNOSTICS:
        if st.button(label, key=key):
            request_task_mode()
            st.rerun()
        return

    if action in (ACTION_RUN_STEP, ACTION_RETRY):
        # The click starts a process-level background run and returns at once;
        # while that run is live the button stays disabled, so a second click
        # can neither restart nor cancel the first provider call.
        def _start_run():
            record = start_step_run(orchestrator, task.id, task.chat_id, action)
            if record is not None:
                st.session_state[STEP_RUN_TOKEN_KEY] = record.token

        st.button(
            label,
            key=key,
            disabled=step_run_busy(task.id),
            on_click=_start_run,
        )
        return

    if not st.button(label, key=key):
        return
    result = _dispatch_action(orchestrator, task, action)
    if result is None or result.ok:
        st.rerun()
    # A failed action reruns the card with the message, so the fresh action set
    # (for example Retry after API_ERROR) is computed after the failed attempt.
    st.session_state[TASK_ERROR_KEY] = (
        result.error_message or "The action could not be completed."
    )
    st.rerun()


def _render_actions(orchestrator, task, allowed):
    actions = [action for action in allowed if action in ACTION_LABELS]
    if not actions:
        return
    columns = st.columns(len(actions))
    for column, action in zip(columns, actions):
        with column:
            _render_action_button(orchestrator, task, action)


def _render_active_card(orchestrator, task):
    st.markdown(format_stage_indicator(task), unsafe_allow_html=True)

    # The card shortens long values to fit its height budget; the full text is
    # always one click away, for every task state (FR-33).
    if st.button("Details", key="task_details"):
        st.session_state[DIALOG_DETAILS_KEY] = task.id
        st.rerun()

    if task.stage == STAGE_DONE:
        plan = orchestrator.repository.load_plan(task.id)
        st.caption(format_task_compact(task, plan))
    else:
        st.caption(_task_details_line(task))

    # Compact visibility of the hard/advisory rules that apply to this task.
    # A task without restrictions keeps exactly the pre-Day-14 card.
    applicable = getattr(orchestrator, "applicable_invariants", None)
    if applicable is not None:
        restrictions = format_active_restrictions(applicable(task.id))
        if restrictions:
            st.caption(restrictions)

    # The provider call runs in a background thread; the card only reports that
    # it is in flight. The disabled step button above stays unavailable until the
    # run finishes, so no second call can start from a repeated click.
    if step_run_busy(task.id):
        st.caption(
            running_step_label(task, orchestrator.repository.load_plan(task.id))
        )

    if st.session_state.get(CANCEL_CONFIRM_KEY) == task.id:
        _render_cancel_confirmation(orchestrator, task)
        return

    # Highlight the single action the state suggests. The card still shows
    # exactly the allowed action set; only the recommended button is styled.
    highlight = recommended_action_css(task)
    if highlight:
        st.markdown(highlight, unsafe_allow_html=True)

    # The plan is the decision this state asks for, so the review dialog is
    # offered right above the action row; accepting or rejecting stays on the
    # same spot inside the dialog.
    if task.expected_action_type == EXPECTED_CONFIRM_PLAN:
        if st.button("Review plan", key="task_review_plan"):
            st.session_state[DIALOG_PLAN_KEY] = task.id
            st.rerun()

    _render_actions(
        orchestrator,
        task,
        orchestrator.allowed_actions(task.id),
    )

    if st.session_state.get(BLOCK_FORM_KEY) == task.id:
        _render_block_form(orchestrator, task)


def report_task_error(message) -> None:
    """Store an action error for the next card render to show.

    ``app.py`` uses this for the error of a background step run, so the message
    survives the rerun that refreshes the action set.
    """
    st.session_state[TASK_ERROR_KEY] = str(message or "The action failed.")


def render_task_card(store, orchestrator, chat_id) -> None:
    """Render the compact task card in the chat bottom container (FR-33).

    The card is purely presentational: ``run_step``/``retry`` start a
    process-level background run through their button callback, and ``app.py``
    follows that run while it streams. Rendering itself never calls the provider.
    """
    if chat_id is None or store is None:
        return

    # A failed action stores its message and reruns, so the error is shown next
    # to the card whose action set already reflects the failed attempt.
    pending_error = st.session_state.pop(TASK_ERROR_KEY, None)
    if pending_error:
        st.error(pending_error)

    task = orchestrator.current_task(chat_id)
    if task is None:
        _render_invitation(orchestrator, chat_id)
    else:
        _render_active_card(orchestrator, task)

    _render_pending_dialogs(orchestrator, chat_id)


def render_task_results_panel(orchestrator, chat_id) -> None:
    """Readable step results on the main Chat screen.

    The raw JSON stays available in ``Diagnostics / Artifacts``; this panel is
    the human-readable counterpart. It reads only stored artifacts, so the
    results survive a rerun and an application restart. The panel renders
    nothing when the chat has no task, the task has no plan, or no step has a
    stored result yet.
    """
    if orchestrator is None:
        return
    task = orchestrator.current_task(chat_id)
    if task is None:
        return
    plan = orchestrator.repository.load_plan(task.id)
    if not isinstance(plan, dict):
        return
    views = build_step_results(
        plan,
        orchestrator.repository.list_artifacts(task.id),
        orchestrator.repository.list_events(task.id),
    )
    if not views:
        return

    st.subheader("Execution results")
    for view in views:
        with st.expander(step_result_label(view), expanded=view["is_latest"]):
            st.markdown(view["text"])
            journal_line = format_result_journal_line(view.get("journal"))
            if journal_line:
                st.caption(journal_line)
            if view["awaiting_rework"] and view["defects"]:
                st.markdown("Defects to fix:")
                for defect in view["defects"]:
                    st.markdown(f"- {defect}")
    st.markdown(format_next_action(task, plan))


# --- Diagnostics / Task ---------------------------------------------------


def _render_task_list(orchestrator, chat_id):
    st.markdown("Tasks in this chat")
    tasks = orchestrator.repository.list_tasks(chat_id)
    if not tasks:
        st.caption("No tasks in this chat yet. Create one below.")
        return
    st.dataframe(
        [
            {
                "title": task.title,
                "stage": task.stage,
                "status": task.status,
                "version": task.version,
                "updated": task.updated_at or "no data",
            }
            for task in tasks
        ]
    )
    columns = st.columns(min(len(tasks), 4))
    for index, task in enumerate(tasks):
        with columns[index % len(columns)]:
            if st.button("Open", key=f"task_open_{task.id}"):
                orchestrator.select_task(chat_id, task.id)
                st.rerun()


def _render_task_fields(task):
    """Render the full task fields of ``Diagnostics / Task`` (FR-37).

    Unlike the compact card, the diagnostics panel never shortens the text: it
    is the place where the whole step, reason and expected action stay readable.
    """
    rows = [
        f"**{task.title}**",
        f"Goal: {task.goal}",
        f"Stage: {task.stage} · Status: {task.status} · Version: {task.version}",
    ]
    if task.current_step:
        step = task.current_step
        if task.current_step_index is not None:
            step = f"{step} (step {task.current_step_index})"
        rows.append(f"Current step: {step}")
    if task.expected_action_text:
        rows.append(f"Expected action: {task.expected_action_text}")
    if task.pause_reason:
        rows.append(f"Pause/block reason: {task.pause_reason}")
    st.markdown("  \n".join(rows))


def _render_timeline(orchestrator, task_id):
    with st.expander("Event timeline", expanded=False):
        events = orchestrator.repository.list_events(task_id)
        if not events:
            st.caption("No events yet.")
        else:
            st.dataframe(
                [
                    {
                        "id": event.id,
                        "event": event.event_type,
                        "from stage": event.from_stage or "",
                        "to stage": event.to_stage or "",
                        "from status": event.from_status or "",
                        "to status": event.to_status or "",
                        "created": event.created_at or "no data",
                    }
                    for event in events
                ]
            )
        _render_invariant_events(orchestrator, task_id)


def _render_invariant_events(orchestrator, task_id):
    """Append the invariant journal next to the task journal.

    The column names deliberately avoid ``event`` and the other task-journal
    columns, so a consumer that looks for the task timeline never confuses the
    two tables.
    """
    repository = getattr(orchestrator, "invariants", None)
    if repository is None:
        return
    list_events = getattr(repository, "list_events", None)
    if list_events is None:
        return
    invariant_events = list_events(task_id=task_id)
    if not invariant_events:
        return
    st.markdown("Invariant events")
    st.dataframe(invariant_event_rows(invariant_events))


def _render_artifacts(orchestrator, task_id):
    with st.expander("Artifacts", expanded=False):
        artifacts = orchestrator.repository.list_artifacts(task_id)
        if not artifacts:
            st.caption("No artifacts yet.")
            return
        # One level of expanders only: a nested expander or ``st.json`` inside
        # the page would keep a long artifact from scrolling to its end.
        for artifact in artifacts:
            st.markdown(f"**{artifact.kind} rev {artifact.revision}**")
            st.markdown(_json_block(artifact.content or {}))


def _render_guard_probe(orchestrator, report, task):
    """Render the explicit, safe "Test Finish execution" guard probe.

    When the FSM refuses ``finish_execution`` the panel offers one button that
    runs the production guard through
    :meth:`TaskOrchestrator.probe_refused_transition`. The click is the only
    action of the whole panel that writes: one append-only refusal-audit row,
    with no change to the task. When the action is allowed there is no button,
    because the probe would execute nothing and write no audit.
    """
    probe_key = f"task_guard_probe_finish_execution_{task.id}"
    if ACTION_FINISH_EXECUTION not in report.allowed:
        if st.button("Test Finish execution", key=probe_key):
            probe = orchestrator.probe_refused_transition(task.id)
            st.session_state[GUARD_PROBE_KEY] = (task.id, probe)
            st.rerun()
    else:
        st.caption(
            "Finish execution is currently allowed; the test executes nothing "
            "and writes no audit."
        )
    stored = st.session_state.get(GUARD_PROBE_KEY)
    if (
        isinstance(stored, tuple)
        and len(stored) == 2
        and stored[0] == task.id
    ):
        line = format_guard_probe_line(stored[1])
        if line:
            st.caption(line)


def _render_transition_guard(orchestrator, task):
    """``Transition guard`` panel of ``Diagnostics / Task`` (Day 15).

    It explains the allowed actions and lists recent refusals from the separate
    append-only audit. Rendering never calls the provider, never changes the
    task and draws no transition button or form: the only widget is the explicit
    ``Test Finish execution`` probe, whose single click may append one
    append-only refusal-audit row through the production guard.
    """
    report = orchestrator.transition_guard_report(task.id)
    with st.expander("Transition guard", expanded=False):
        st.caption(
            "Read-only diagnostics of the task state machine: it never changes "
            "the task and never calls the provider."
        )
        current = report.task if report.task is not None else task
        st.markdown(
            f"Current state: `{current.stage}` / `{current.status}` · "
            f"expected action: `{current.expected_action_type or 'none'}`"
        )
        st.markdown(f"Allowed now: {format_allowed_actions(report.allowed)}")
        st.markdown("Action decisions")
        st.dataframe(guard_decision_rows(report.decisions))
        _render_guard_probe(orchestrator, report, task)
        st.markdown("Recent refusals")
        if report.refusals:
            st.dataframe(transition_attempt_rows(report.refusals))
        else:
            st.caption("No refusals recorded for this task yet.")
        st.caption(
            "Refusals are stored in a separate append-only audit and are not "
            "task events: they never change the task state or its version."
        )


def _render_plan(orchestrator, task):
    """Render the readable plan above the raw JSON of ``Diagnostics / Task``.

    The plan is read through the repository and rendered without any provider
    call. The raw JSON stays a collapsed-by-default technical option, so the
    whole plan is visible before the user accepts or rejects it.
    """
    plan = orchestrator.repository.load_plan(task.id)
    expanded = task.expected_action_type == EXPECTED_CONFIRM_PLAN
    with st.expander("Plan", expanded=expanded):
        if not isinstance(plan, dict):
            st.caption("No plan yet.")
            return
        st.markdown(format_plan(plan))
        if st.checkbox("Show raw JSON", key=f"task_plan_raw_{task.id}"):
            st.markdown(_json_block(plan))


def _render_workflow(orchestrator, task):
    with st.expander("Workflow", expanded=False):
        workflow = orchestrator.repository.get_workflow(
            task.workflow_name or DEFAULT_WORKFLOW_NAME
        )
        if workflow is None:
            st.caption("No workflow profile is available.")
            return
        st.markdown(f"**Name:** {workflow.name} ({workflow.display_name})")
        st.markdown(f"**Stages:** {' → '.join(workflow.stages)}")
        if workflow.instructions:
            st.markdown("**Instructions:**")
            for stage, text in workflow.instructions.items():
                st.markdown(f"- `{stage}`: {text}")
        st.markdown(f"**Executors:** `{json.dumps(workflow.executors, ensure_ascii=False)}`")
        st.markdown(f"**Validation policy:** `{json.dumps(workflow.validation, ensure_ascii=False)}`")
        st.caption("Read-only: Day 13 provides the default workflow.")


def _render_packet_preview(orchestrator, task_id):
    with st.expander("Context packet preview", expanded=False):
        packet = orchestrator.preview_packet(task_id)
        if not packet.blocks:
            st.caption("No packet: the current state calls no model.")
            return
        st.dataframe(
            [
                {
                    "Block": block.name,
                    "Role": block.role,
                    "Content preview": shorten(block.content, 120),
                    "≈ tokens": block.tokens,
                    "≈ cost": _fmt_cost(block.cost_usd),
                }
                for block in packet.blocks
            ]
        )
        st.caption(
            f"≈ {packet.total_tokens} tokens · "
            f"{_fmt_cost(packet.total_cost_usd)} · estimate, not billing · "
            "0 API calls"
        )


def _render_task_usage(orchestrator, task_id):
    with st.expander("Task usage", expanded=False):
        usage = orchestrator.repository.get_task_usage(task_id)
        st.markdown(
            f"Calls: {usage.calls} · Attempts: {usage.attempts} · "
            f"Tokens: {_fmt_num(usage.input_tokens)} in / "
            f"{_fmt_num(usage.output_tokens)} out · "
            f"cache hit {_fmt_num(usage.cache_hit_tokens)} / "
            f"miss {_fmt_num(usage.cache_miss_tokens)}"
        )
        st.markdown(f"≈ Cost: {_fmt_cost(usage.cost_usd)}")
        reasons = ", ".join(usage.finish_reasons) if usage.finish_reasons else "no data"
        st.markdown(f"Finish reasons: {reasons}")
        st.caption(
            "Task calls are counted separately from the chat statistics: they "
            "never change the chat counters, memory or the stored history."
        )


def _render_results(orchestrator, task):
    """Readable ``Execution results`` section of ``Diagnostics / Task``.

    A single expander keeps the technical panel free of nested expanders: the
    stored step texts and the next-action line are rendered as markdown, and the
    raw JSON stays in the ``Artifacts`` expander.
    """
    plan = orchestrator.repository.load_plan(task.id)
    views = build_step_results(
        plan,
        orchestrator.repository.list_artifacts(task.id),
        orchestrator.repository.list_events(task.id),
    )
    with st.expander("Execution results", expanded=False):
        if not views:
            st.caption("No step results yet.")
            return
        for view in views:
            st.markdown(f"**{step_result_label(view)}**")
            st.markdown(view["text"])
            journal_line = format_result_journal_line(view.get("journal"))
            if journal_line:
                st.caption(journal_line)
            if view["awaiting_rework"] and view["defects"]:
                st.markdown("Defects to fix:")
                for defect in view["defects"]:
                    st.markdown(f"- {defect}")
        st.markdown(format_next_action(task, plan))


def render_diagnostics_task(
    store, orchestrator, chat_id, invariant_repository=None
) -> None:
    """Render the ``Diagnostics / Task`` panel (FR-37)."""
    st.subheader("Task state")
    chat = next(
        (item for item in store.list_chats() if item.id == chat_id), None
    )
    if chat is not None:
        st.caption(f"Chat: {chat.title}")

    with st.expander("Create task", expanded=False):
        render_create_task_form(
            orchestrator, chat_id, key_prefix="task_diag_create"
        )

    _render_task_list(orchestrator, chat_id)

    task = orchestrator.current_task(chat_id)
    if task is None:
        st.caption("Select a task to see its indicator, timeline and artifacts.")
        return

    st.markdown(format_stage_indicator(task), unsafe_allow_html=True)
    _render_task_fields(task)
    _render_transition_guard(orchestrator, task)
    _render_plan(orchestrator, task)
    _render_results(orchestrator, task)
    _render_timeline(orchestrator, task.id)
    _render_artifacts(orchestrator, task.id)
    _render_workflow(orchestrator, task)
    render_invariants_section(
        invariant_repository, task=task, chat_id=chat_id
    )
    _render_packet_preview(orchestrator, task.id)
    _render_task_usage(orchestrator, task.id)
