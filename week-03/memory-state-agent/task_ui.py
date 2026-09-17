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
matching use case. The domain still rejects a disallowed action, so a stale card
cannot change the stage directly.
"""

from __future__ import annotations

import html
import json

import streamlit as st

from task_orchestrator import STATUS_SUCCESS
from task_storage import DEFAULT_WORKFLOW_NAME
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_BLOCK,
    ACTION_CANCEL,
    ACTION_FINISH_EXECUTION,
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
    ARTIFACT_FINAL_RESULT,
    STAGE_DONE,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_STEP_LABELS,
    STAGE_VALIDATION,
    STATUS_BLOCKED,
    badge_for,
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
BLOCK_FORM_KEY = "task_block_form_open"
CANCEL_CONFIRM_KEY = "task_cancel_confirm_task"
TASK_ERROR_KEY = "task_action_error"

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
    if details_task_id is None:
        return
    task = orchestrator.repository.get_task(details_task_id)
    if task is None:
        st.session_state.pop(DIALOG_DETAILS_KEY, None)
        return
    render_task_details_dialog(orchestrator, task)


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


def _dispatch_action(orchestrator, task, action, on_run_step):
    """Run one card action and return its result (``None`` when handled here)."""
    if action in (ACTION_RUN_STEP, ACTION_RETRY):
        # The streamed text belongs to the main chat area, not to the pinned
        # bottom container, so the app owns the placeholder through the callback.
        return on_run_step(task.id, action)
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


def _render_action_button(orchestrator, task, action, on_run_step):
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

    if not st.button(label, key=key):
        return
    result = _dispatch_action(orchestrator, task, action, on_run_step)
    if result is None or result.ok:
        st.rerun()
    # A failed action reruns the card with the message, so the fresh action set
    # (for example Retry after API_ERROR) is computed after the failed attempt.
    st.session_state[TASK_ERROR_KEY] = (
        result.error_message or "The action could not be completed."
    )
    st.rerun()


def _render_actions(orchestrator, task, allowed, on_run_step):
    actions = [action for action in allowed if action in ACTION_LABELS]
    if not actions:
        return
    columns = st.columns(len(actions))
    for column, action in zip(columns, actions):
        with column:
            _render_action_button(orchestrator, task, action, on_run_step)


def _render_active_card(orchestrator, task, on_run_step):
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

    if st.session_state.get(CANCEL_CONFIRM_KEY) == task.id:
        _render_cancel_confirmation(orchestrator, task)
        return

    _render_actions(
        orchestrator,
        task,
        orchestrator.allowed_actions(task.id),
        on_run_step,
    )

    if st.session_state.get(BLOCK_FORM_KEY) == task.id:
        _render_block_form(orchestrator, task)


def report_task_error(message) -> None:
    """Store an action error for the next card render to show.

    The app's run-step callback uses this for an unexpected exception, so the
    message survives the rerun that refreshes the action set.
    """
    st.session_state[TASK_ERROR_KEY] = str(message or "The action failed.")


def render_task_card(store, orchestrator, chat_id, *, on_run_step) -> None:
    """Render the compact task card in the chat bottom container (FR-33).

    ``on_run_step`` is called as ``on_run_step(task_id, action)`` for the model
    actions that may stream (``run_step`` and ``retry``); it must draw the
    spinner/placeholder in the main chat area and return the
    ``TaskActionResult``. Rendering itself never calls the provider.
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
        _render_active_card(orchestrator, task, on_run_step)

    _render_pending_dialogs(orchestrator, chat_id)


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
            return
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


def _render_artifacts(orchestrator, task_id):
    with st.expander("Artifacts", expanded=False):
        artifacts = orchestrator.repository.list_artifacts(task_id)
        if not artifacts:
            st.caption("No artifacts yet.")
            return
        for artifact in artifacts:
            with st.expander(f"{artifact.kind} rev {artifact.revision}", expanded=False):
                st.json(artifact.content or {})


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


def render_diagnostics_task(store, orchestrator, chat_id) -> None:
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
    _render_timeline(orchestrator, task.id)
    _render_artifacts(orchestrator, task.id)
    _render_workflow(orchestrator, task)
    _render_packet_preview(orchestrator, task.id)
    _render_task_usage(orchestrator, task.id)
