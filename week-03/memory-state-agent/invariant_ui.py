"""Formatters and Streamlit rendering of the Day 14 structural invariants.

The pure formatters (``format_scope``, ``invariant_rows``,
``format_active_restrictions``, ``invariant_event_rows``, ...) build strings and
plain rows only and are covered without Streamlit. The render function draws the
``Invariants`` section of ``Diagnostics / Task`` and the rule management forms.
Rendering never calls the provider and never uses ``st.json``: the raw payload
stays an explicit checkbox option rendered as a markdown code block.
"""

from __future__ import annotations

import json

import streamlit as st

from invariants import (
    CHECK_KINDS,
    ENFORCEMENTS,
    Invariant,
    KINDS,
    SCOPES,
    SCOPE_TASK,
)
from invariant_storage import DuplicateInvariantCodeError

EDIT_TARGET_KEY = "invariant_edit_target"


# --- Pure formatters ------------------------------------------------------


def parse_surfaces(text) -> tuple:
    """Split a comma/newline separated field into a tuple of non-empty values."""
    raw = str(text or "").replace("\n", ",")
    values = [part.strip() for part in raw.split(",")]
    return tuple(value for value in values if value)


def format_scope(invariant) -> str:
    """Human scope of a rule: ``global`` or ``task #<id>``."""
    if getattr(invariant, "scope", None) == SCOPE_TASK:
        return f"task #{getattr(invariant, 'task_id', None)}"
    return "global"


def format_invariant_row(invariant) -> dict:
    """One dataframe row describing a rule (diagnostics view)."""
    return {
        "code": invariant.code,
        "title": invariant.title,
        "scope": format_scope(invariant),
        "kind": invariant.kind,
        "enforcement": invariant.enforcement,
        "version": invariant.version,
        "source": invariant.source,
        "active": "yes" if invariant.is_active else "no",
    }


def invariant_rows(invariants) -> list:
    """Return the dataframe rows of every rule, preserving the order."""
    return [format_invariant_row(invariant) for invariant in invariants or ()]


def format_active_restrictions(invariants) -> str:
    """Compact one-line caption of the active applicable restrictions.

    Returns an empty string when there is nothing to show, so the chat card
    stays exactly as before for a task without restrictions.
    """
    items = [
        invariant for invariant in (invariants or ()) if invariant.is_active
    ]
    if not items:
        return ""
    parts = [f"{invariant.code} ({invariant.enforcement})" for invariant in items]
    return "Active restrictions: " + " · ".join(parts)


def invariant_event_rows(events) -> list:
    """Return journal rows; the column names never collide with ``event``."""
    rows = []
    for event in events or ():
        details = event.details or {}
        rows.append(
            {
                "invariant_event": event.event_type,
                "code": event.code,
                "phase": details.get("phase", ""),
                "decision": details.get("decision", ""),
                "created": event.created_at or "no data",
            }
        )
    return rows


def format_invariant_details(invariant) -> str:
    """Multiline human description of one rule."""
    lines = [
        f"**{invariant.code}** · {invariant.title}",
        f"{invariant.text}",
        (
            f"Scope: {format_scope(invariant)} · Kind: {invariant.kind} · "
            f"Enforcement: {invariant.enforcement} · Version: {invariant.version} · "
            f"Source: {invariant.source} · "
            f"{'active' if invariant.is_active else 'inactive'}"
        ),
    ]
    if invariant.check_kind:
        lines.append(f"Check: {invariant.check_kind}")
    if invariant.triggers:
        lines.append("Triggers: " + ", ".join(invariant.triggers))
    if invariant.guard_actions:
        lines.append("Guard actions: " + ", ".join(invariant.guard_actions))
    if invariant.guard_events:
        lines.append("Guard events: " + ", ".join(invariant.guard_events))
    if invariant.alternative:
        lines.append(f"Alternative: {invariant.alternative}")
    return "\n\n".join(lines)


def _raw_json_block(repository) -> str:
    """Render every rule as a markdown JSON block (explicit UI option only)."""
    payload = [
        {
            "code": invariant.code,
            "scope": invariant.scope,
            "task_id": invariant.task_id,
            "enforcement": invariant.enforcement,
            "kind": invariant.kind,
            "check_kind": invariant.check_kind,
            "triggers": list(invariant.triggers),
            "guard_actions": list(invariant.guard_actions),
            "guard_events": list(invariant.guard_events),
            "alternative": invariant.alternative,
            "version": invariant.version,
            "is_active": invariant.is_active,
            "source": invariant.source,
        }
        for invariant in repository.list_invariants()
    ]
    try:
        body = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        body = str(payload)
    return f"```json\n{body}\n```"


# --- Streamlit rendering --------------------------------------------------


def _invariant_from_fields(
    *,
    code,
    title,
    text,
    scope,
    task_id,
    enforcement,
    kind,
    check_kind,
    triggers,
    guard_actions,
    guard_events,
    alternative,
) -> Invariant:
    return Invariant(
        code=code,
        title=title,
        text=text,
        scope=scope,
        task_id=(
            int(task_id)
            if scope == SCOPE_TASK and task_id
            else None
        ),
        enforcement=enforcement,
        kind=kind,
        check_kind=check_kind,
        triggers=parse_surfaces(triggers),
        guard_actions=parse_surfaces(guard_actions),
        guard_events=parse_surfaces(guard_events),
        alternative=alternative,
    )


def render_create_invariant_form(repository, *, task=None, chat_id=None) -> None:
    """Render the create-rule form. Widget keys use the ``invariant_`` prefix."""
    st.markdown("Create a rule")
    code = st.text_input("Code", key="invariant_new_code")
    title = st.text_input("Title", key="invariant_new_title")
    text = st.text_area("Text", key="invariant_new_text")
    scope = st.selectbox("Scope", options=list(SCOPES), key="invariant_new_scope")
    default_task = getattr(task, "id", None) or 0
    task_id = st.number_input(
        "Task id (for a task-scoped rule)",
        min_value=0,
        value=int(default_task),
        step=1,
        key="invariant_new_task_id",
    )
    enforcement = st.selectbox(
        "Enforcement", options=list(ENFORCEMENTS), key="invariant_new_enforcement"
    )
    kind = st.selectbox("Kind", options=list(KINDS), key="invariant_new_kind")
    check_kind = st.selectbox(
        "Check", options=list(CHECK_KINDS), key="invariant_new_check_kind"
    )
    triggers = st.text_input(
        "Triggers (comma separated)", key="invariant_new_triggers"
    )
    guard_actions = st.text_input(
        "Guard actions (comma separated)", key="invariant_new_guard_actions"
    )
    guard_events = st.text_input(
        "Guard events (comma separated)", key="invariant_new_guard_events"
    )
    alternative = st.text_area("Alternative", key="invariant_new_alternative")

    if st.button("Create invariant", key="invariant_create_submit"):
        try:
            repository.create_invariant(
                _invariant_from_fields(
                    code=code,
                    title=title,
                    text=text,
                    scope=scope,
                    task_id=int(task_id),
                    enforcement=enforcement,
                    kind=kind,
                    check_kind=check_kind,
                    triggers=triggers,
                    guard_actions=guard_actions,
                    guard_events=guard_events,
                    alternative=alternative,
                ),
                chat_id=chat_id,
            )
        except DuplicateInvariantCodeError:
            st.error("A rule with this code already exists.")
        except ValueError as exc:
            st.error(f"Could not create the rule: {exc}")
        else:
            st.rerun()


def _render_edit_form(repository, invariant, *, chat_id=None) -> None:
    """Inline edit form of one rule; the stable code is shown read-only."""
    st.markdown(f"Edit **{invariant.code}**")
    st.caption("The code is stable and cannot be changed; saving bumps the version.")
    title = st.text_input(
        "Title", value=invariant.title, key=f"invariant_edit_title_{invariant.id}"
    )
    text = st.text_area(
        "Text", value=invariant.text, key=f"invariant_edit_text_{invariant.id}"
    )
    scope_options = list(SCOPES)
    scope_index = (
        scope_options.index(invariant.scope) if invariant.scope in scope_options else 0
    )
    scope = st.selectbox(
        "Scope",
        options=scope_options,
        index=scope_index,
        key=f"invariant_edit_scope_{invariant.id}",
    )
    task_id = st.number_input(
        "Task id (for a task-scoped rule)",
        min_value=0,
        value=int(invariant.task_id or 0),
        step=1,
        key=f"invariant_edit_task_id_{invariant.id}",
    )
    enforcement_options = list(ENFORCEMENTS)
    enforcement = st.selectbox(
        "Enforcement",
        options=enforcement_options,
        index=(
            enforcement_options.index(invariant.enforcement)
            if invariant.enforcement in enforcement_options
            else 0
        ),
        key=f"invariant_edit_enforcement_{invariant.id}",
    )
    kind_options = list(KINDS)
    kind = st.selectbox(
        "Kind",
        options=kind_options,
        index=kind_options.index(invariant.kind) if invariant.kind in kind_options else 0,
        key=f"invariant_edit_kind_{invariant.id}",
    )
    check_options = list(CHECK_KINDS)
    check_kind = st.selectbox(
        "Check",
        options=check_options,
        index=(
            check_options.index(invariant.check_kind)
            if invariant.check_kind in check_options
            else 0
        ),
        key=f"invariant_edit_check_{invariant.id}",
    )
    triggers = st.text_input(
        "Triggers (comma separated)",
        value=", ".join(invariant.triggers),
        key=f"invariant_edit_triggers_{invariant.id}",
    )
    guard_actions = st.text_input(
        "Guard actions (comma separated)",
        value=", ".join(invariant.guard_actions),
        key=f"invariant_edit_guard_actions_{invariant.id}",
    )
    guard_events = st.text_input(
        "Guard events (comma separated)",
        value=", ".join(invariant.guard_events),
        key=f"invariant_edit_guard_events_{invariant.id}",
    )
    alternative = st.text_area(
        "Alternative",
        value=invariant.alternative,
        key=f"invariant_edit_alternative_{invariant.id}",
    )
    col_save, col_cancel = st.columns(2)
    if col_save.button("Save rule", key=f"invariant_edit_save_{invariant.id}"):
        try:
            repository.update_invariant(
                invariant.id,
                title=title,
                text=text,
                scope=scope,
                task_id=int(task_id) if scope == SCOPE_TASK and task_id else None,
                enforcement=enforcement,
                kind=kind,
                check_kind=check_kind,
                triggers=parse_surfaces(triggers),
                guard_actions=parse_surfaces(guard_actions),
                guard_events=parse_surfaces(guard_events),
                alternative=alternative,
                chat_id=chat_id,
            )
        except ValueError as exc:
            st.error(f"Could not save the rule: {exc}")
        else:
            st.session_state.pop(EDIT_TARGET_KEY, None)
            st.rerun()
    if col_cancel.button("Cancel", key=f"invariant_edit_cancel_{invariant.id}"):
        st.session_state.pop(EDIT_TARGET_KEY, None)
        st.rerun()


def render_invariants_section(repository, *, task=None, chat_id=None) -> None:
    """Render the ``Invariants`` panel of ``Diagnostics / Task``.

    Shows scope, kind, enforcement, version and source of every rule, the rules
    applicable to the current task, the create/edit/deactivate controls and the
    append-only journal of changes and conflicts. The raw JSON stays an explicit
    checkbox option.
    """
    if repository is None:
        return
    with st.expander("Invariants", expanded=False):
        st.caption(
            "Structural invariants are durable rules that survive F5, a chat "
            "switch and a full restart. They are not memory and not task state. "
            "Hard rules are enforced by code before an action and before a commit."
        )

        task_id = getattr(task, "id", None)
        if task is not None:
            applicable = repository.list_applicable(task_id)
            if applicable:
                st.markdown(format_active_restrictions(applicable))
            else:
                st.caption("No active invariants apply to this task.")
        else:
            st.caption("Select a task to see the restrictions that apply to it.")

        invariants = repository.list_invariants()
        st.markdown("Rules")
        if invariants:
            st.dataframe(invariant_rows(invariants))
        else:
            st.caption("No rules yet.")

        render_create_invariant_form(repository, task=task, chat_id=chat_id)

        st.markdown("Manage rules")
        edit_target = st.session_state.get(EDIT_TARGET_KEY)
        for invariant in invariants:
            st.markdown(
                f"**{invariant.code}** · {invariant.enforcement} · "
                f"v{invariant.version} · "
                f"{'active' if invariant.is_active else 'inactive'}"
            )
            col_edit, col_toggle = st.columns(2)
            if col_edit.button("Edit", key=f"invariant_edit_{invariant.id}"):
                st.session_state[EDIT_TARGET_KEY] = invariant.id
                st.rerun()
            toggle_label = "Deactivate" if invariant.is_active else "Activate"
            if col_toggle.button(
                toggle_label, key=f"invariant_toggle_{invariant.id}"
            ):
                repository.set_active(
                    invariant.id, not invariant.is_active, chat_id=chat_id
                )
                st.session_state.pop(EDIT_TARGET_KEY, None)
                st.rerun()

        if edit_target is not None:
            target = next(
                (item for item in invariants if item.id == edit_target), None
            )
            if target is None:
                st.session_state.pop(EDIT_TARGET_KEY, None)
            else:
                _render_edit_form(repository, target, chat_id=chat_id)

        events = repository.list_events(
            task_id=task_id if task is not None else None
        )
        st.markdown("Invariant events")
        if events:
            st.dataframe(invariant_event_rows(events))
        else:
            st.caption("No invariant events recorded yet.")

        if st.checkbox("Show raw JSON", key="invariant_raw_json"):
            st.markdown(_raw_json_block(repository))
