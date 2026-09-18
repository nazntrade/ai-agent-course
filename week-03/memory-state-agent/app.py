import time

import streamlit as st

from agent import (
    AgentConfig,
    ApiContextOverflowError,
    ChatAgent,
    ContextLimitError,
    get_client,
)
from app_logic import WaitingIndicator, configs_equal, messages_tokens_est
from context import build_payload
from invariant_storage import InvariantRepository
from invariants import InvariantConflictError, format_conflict_message
from memory import (
    LONG_TERM_MEMORY_BLOCK_TITLE,
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
    WORKING_MEMORY_BLOCK_TITLE,
    format_memory_block,
    validate_memory_key,
    validate_memory_value,
)
from models import display_name
from profile import format_profile_block
from storage import (
    DEFAULT_CHAT_TITLE,
    BranchHasChildrenError,
    ChatStore,
    DuplicateBranchNameError,
    DuplicateMemoryKeyError,
    DuplicateProfileNameError,
)
from strategies import (
    STRATEGY_BRANCHING,
    STRATEGY_CHOICES,
    STRATEGY_FACTS,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    strategy_label,
)
from task_orchestrator import TaskOrchestrator
from task_runner import (
    STEP_RUN_DONE,
    STEP_RUN_ERROR,
    STEP_RUN_RUNNING,
    discard_step_run,
    step_run_record,
)
from task_storage import TaskRepository
from task_ui import (
    MODE_KEY,
    MODE_TASK,
    PENDING_MODE_KEY,
    STEP_RUN_TOKEN_KEY,
    render_diagnostics_task,
    render_task_card,
    render_task_results_panel,
    report_task_error,
    running_step_label,
)
from tokens import estimate_tokens

# Three mutually exclusive UI modes. Chat is the default landing mode and keeps
# only the conversation: the diagnostics panels move behind the other two modes
# so the main screen stays short. The mode lives in session state under a widget
# key, so it survives chat switches and reruns.
MODE_CHAT = "Chat"
MODE_DIAGNOSTICS = "Diagnostics / Memory"

# Where the compact task card lives (FR-38). "bottom" keeps it as the first
# element of the pinned chat bottom container. "sidebar" is the documented
# fallback for a window where the card would leave fewer than three history
# messages visible (for example 1024x768): the card moves above the chat
# settings instead, and the bottom container keeps only the profile row.
TASK_CARD_LOCATION = "bottom"

st.set_page_config(page_title="Memory State Agent", page_icon="🤖", layout="wide")
st.title("Memory State Agent")

if "client" not in st.session_state:
    st.session_state.client = get_client()

client = st.session_state.client

if client is None:
    st.error("Add DEEPSEEK_API_KEY to the .env file and restart the app.")
    st.stop()

if "store" not in st.session_state:
    st.session_state.store = ChatStore()

store = st.session_state.store

# The task state machine shares the chat database file. Both objects are created
# lazily: an injected session_state entry (AppTest, a future alternative
# repository) wins, and the orchestrator re-reads the chat configuration on every
# call, so it does not need to be rebuilt when the active chat changes.
if "task_repository" not in st.session_state:
    st.session_state.task_repository = TaskRepository(store.db_path)

# Structural invariants (Day 14) share the same database file. The repository is
# created lazily so an injected session_state entry wins; the seeded defaults
# live in SQLite and survive a full restart.
if "invariant_repository" not in st.session_state:
    st.session_state.invariant_repository = InvariantRepository(store.db_path)

invariant_repository = st.session_state.invariant_repository

if "task_orchestrator" not in st.session_state:
    st.session_state.task_orchestrator = TaskOrchestrator(
        store,
        repository=st.session_state.task_repository,
        client=client,
        invariants=invariant_repository,
    )

task_orchestrator = st.session_state.task_orchestrator

# Polling step of the background-run follower. The provider call runs in a
# worker thread of ``task_runner``; this function only reads its record and
# redraws the placeholder, so a short pause keeps the thread join cheap.
STEP_RUN_POLL_SECONDS = 0.3

# A summary update failure is reported on the next render, after the rerun that
# follows a successful turn; popping it here ensures it is shown exactly once.
pending_summary_error = st.session_state.pop("summary_error", None)
if pending_summary_error:
    st.warning(pending_summary_error)

pending_facts_error = st.session_state.pop("facts_error", None)
if pending_facts_error:
    st.warning(pending_facts_error)


def _open_diagnostics():
    """Callback: switch the UI to Diagnostics from the Chat profile row."""
    st.session_state[MODE_KEY] = MODE_DIAGNOSTICS


def _open_profile_create_form():
    """Callback: reveal the hidden profile create form in Diagnostics."""
    st.session_state["profile_create_open"] = True


def chat_exists(chat_id):
    return any(chat.id == chat_id for chat in store.list_chats())


def _build_agent(chat_id):
    """Build a chat agent bound to the structural-invariant repository.

    The task lookup lets the agent select the task-scoped invariants of the
    chat and record a refusal against the right task. Every call site that used
    to construct ``ChatAgent`` directly goes through here, so the wiring stays
    in one place.
    """
    return ChatAgent(
        client,
        store,
        chat_id,
        invariants=invariant_repository,
        task_lookup=task_orchestrator.current_task,
    )


def _fmt_num(value):
    return "no data" if value is None else str(value)


def _fmt_cost(value):
    return "no data" if value is None else f"≈${value:.6f}"


def _sum_optional(*values):
    if all(value is None for value in values):
        return None
    return sum(value or 0 for value in values)


def _chat_compact(chat):
    """Compact per-chat usage line for the sidebar."""
    if (
        chat.input_tokens is None
        and chat.output_tokens is None
        and chat.cost_usd is None
    ):
        return "no data"
    return (
        f"in {_fmt_num(chat.input_tokens)} · out {_fmt_num(chat.output_tokens)} · "
        f"{_fmt_cost(chat.cost_usd)}"
    )


def _turn_summary(turn):
    """Compact single-line summary of a completed turn."""
    return (
        f"context {_fmt_num(turn.request_tokens)} · "
        f"total {_fmt_num(turn.total_tokens)} · "
        f"cache hit {_fmt_num(turn.prompt_cache_hit_tokens)} / "
        f"miss {_fmt_num(turn.prompt_cache_miss_tokens)} · "
        f"{_fmt_cost(turn.cost_usd)} · "
        f"finish: {turn.finish_reason or 'no data'}"
    )


def _render_chat_history(store, chat_id):
    """Render the active line's messages with their turn statistics.

    Only the conversation is drawn here: no diagnostics panel is rendered in
    Chat mode, so the input field follows the history with nothing in between.
    """
    history = store.load_branch_history(chat_id)
    for message in history:
        with st.chat_message(message.role):
            st.markdown(message.content)
            turn = message.turn
            if turn is None:
                st.caption("turn stats: no data")
            elif message.role == "user":
                est = turn.user_message_tokens_est
                if est is not None:
                    st.caption(f"≈ {est} tokens of the message (estimate)")
                else:
                    st.caption("turn stats: no data")
            else:
                st.caption(f"response tokens: {_fmt_num(turn.response_tokens)}")
                if turn.finish_reason == "length":
                    st.caption(
                        "The response hit the max_tokens limit (output length "
                        "limit, not context)"
                    )
                st.caption(_turn_summary(turn))


def _branch_snippet(message):
    """One-line checkpoint preview: first line, collapsed spaces, 50 chars max."""
    content = (message.content if message is not None else "") or ""
    lines = content.splitlines()
    first_line = lines[0] if lines else ""
    text = " ".join(first_line.split())
    return text[:50] + "…" if len(text) > 50 else text


def _branch_parent_display(branch, branches_by_id):
    """Human-readable parent of a branch: the main line or the parent name."""
    if branch.parent_branch_id is None:
        return "main line"
    parent = branches_by_id.get(branch.parent_branch_id)
    if parent is None:
        return f"branch id {branch.parent_branch_id}"
    return f'"{parent.name}"'


def _line_label(branch):
    """Human-readable name of the active line (main line or one branch)."""
    if branch is None:
        return "main line"
    return f"branch «{branch.name}» (id {branch.id})"


def _memory_scope_label(chat_title, branch):
    """Working-memory scope: the chat title plus the exact active line."""
    if branch is None:
        return f"chat «{chat_title}» · line: main"
    return f"chat «{chat_title}» · branch «{branch.name}» (id {branch.id})"


def _memory_layer_tokens(items, title):
    """Estimated prompt tokens contributed by the included items of one layer.

    The included items are rendered through the same deterministic block the
    agent sends to the model, including its header (``title`` must be the
    layer's payload header from ``memory``). An empty or fully excluded layer
    contributes 0 tokens.
    """
    block = format_memory_block(items, title)
    return estimate_tokens(block) if block else 0


def _memory_item_key(prefix, scope_token, item_id):
    """Widget key that encodes the action, scope, line and item id."""
    return f"{prefix}_{scope_token}_{item_id}"


def _memory_scope_kwargs(scope, chat_id, branch_id):
    """Store keyword arguments for a scope: long-term is never scoped."""
    if scope == MEMORY_SCOPE_LONG_TERM:
        return {}
    return {"chat_id": chat_id, "branch_id": branch_id}


def _render_memory_items(
    scope, scope_token, items, store, agent, chat_id, branch_id, allow_promote
):
    """Render one layer's items with Include, Edit, Forget and Promote.

    Every mutation reloads the agent's cached memory and reruns, so the UI and
    the next request always see the persisted state. Validation errors keep the
    item untouched and are shown in English.
    """
    scope_kwargs = _memory_scope_kwargs(scope, chat_id, branch_id)
    edit_target = st.session_state.get("memory_edit_target")
    for item in items:
        include_key = _memory_item_key(
            f"mem_include_{scope}", scope_token, item.id
        )
        include = st.checkbox("Include", value=item.included, key=include_key)
        if include != item.included:
            try:
                store.set_memory_item_included(
                    scope, item.id, include, **scope_kwargs
                )
            except (KeyError, ValueError) as exc:
                st.error(f"Could not update the item: {exc}")
            else:
                agent.reload_memory()
                st.rerun()
        st.markdown(f"**{item.key}**: {item.value}")

        if allow_promote:
            promote_col, edit_col, forget_col = st.columns(3)
            if promote_col.button(
                "Promote",
                key=_memory_item_key(
                    f"mem_promote_{scope}", scope_token, item.id
                ),
            ):
                try:
                    store.promote_memory_item(chat_id, branch_id, item.id)
                except DuplicateMemoryKeyError:
                    st.error(
                        "Long-term memory already contains an item with this key."
                    )
                except (KeyError, ValueError) as exc:
                    st.error(f"Could not promote the item: {exc}")
                else:
                    agent.reload_memory()
                    st.rerun()
        else:
            edit_col, forget_col = st.columns(2)

        if edit_col.button(
            "Edit", key=_memory_item_key(f"mem_edit_{scope}", scope_token, item.id)
        ):
            st.session_state["memory_edit_target"] = (scope, scope_token, item.id)
            st.rerun()
        if forget_col.button(
            "Forget",
            key=_memory_item_key(f"mem_forget_{scope}", scope_token, item.id),
        ):
            try:
                store.forget_memory_item(scope, item.id, **scope_kwargs)
            except (KeyError, ValueError) as exc:
                st.error(f"Could not forget the item: {exc}")
            else:
                agent.reload_memory()
                st.rerun()

        if edit_target == (scope, scope_token, item.id):
            new_key = st.text_input(
                "Key",
                value=item.key,
                key=_memory_item_key(
                    f"mem_edit_key_{scope}", scope_token, item.id
                ),
            )
            new_value = st.text_input(
                "Value",
                value=item.value,
                key=_memory_item_key(
                    f"mem_edit_value_{scope}", scope_token, item.id
                ),
            )
            save_col, cancel_col = st.columns(2)
            if save_col.button(
                "Save",
                key=_memory_item_key(
                    f"mem_edit_save_{scope}", scope_token, item.id
                ),
            ):
                try:
                    validate_memory_key(new_key)
                    validate_memory_value(new_value)
                except ValueError:
                    st.error("Key and value must not be empty.")
                else:
                    try:
                        store.edit_memory_item(
                            scope, item.id, new_key, new_value, **scope_kwargs
                        )
                    except DuplicateMemoryKeyError:
                        st.error(
                            "A memory item with this key already exists in this "
                            "layer."
                        )
                    except KeyError:
                        st.error("This memory item no longer exists.")
                    except ValueError as exc:
                        st.error(f"Could not save the item: {exc}")
                    else:
                        st.session_state.pop("memory_edit_target", None)
                        agent.reload_memory()
                        st.rerun()
            if cancel_col.button(
                "Cancel",
                key=_memory_item_key(
                    f"mem_edit_cancel_{scope}", scope_token, item.id
                ),
            ):
                st.session_state.pop("memory_edit_target", None)
                st.rerun()


def _render_memory_add_form(scope, scope_token, store, agent, chat_id, branch_id):
    """Render the Key/Value add form for one layer and persist new items."""
    scope_kwargs = _memory_scope_kwargs(scope, chat_id, branch_id)
    key_field = f"mem_add_key_{scope}_{scope_token}"
    value_field = f"mem_add_value_{scope}_{scope_token}"
    new_key = st.text_input("Key", key=key_field)
    new_value = st.text_input("Value", key=value_field)
    if st.button("Add", key=f"mem_add_{scope}_{scope_token}"):
        try:
            validate_memory_key(new_key)
            validate_memory_value(new_value)
        except ValueError:
            st.error("Key and value must not be empty.")
        else:
            try:
                store.add_memory_item(scope, new_key, new_value, **scope_kwargs)
            except DuplicateMemoryKeyError:
                st.error(
                    "A memory item with this key already exists in this layer."
                )
            except KeyError:
                st.error("Could not add the item: the chat no longer exists.")
            except ValueError as exc:
                st.error(f"Could not add the item: {exc}")
            else:
                st.session_state["memory_add_clear"] = (key_field, value_field)
                agent.reload_memory()
                st.rerun()


def _profile_edit_form(store, agent, profile):
    """Edit one existing profile and persist the change."""
    name = st.text_input(
        "Name", value=profile.name, key=f"profile_edit_name_{profile.id}"
    )
    addressing = st.text_input(
        "Addressing",
        value=profile.addressing,
        key=f"profile_edit_addressing_{profile.id}",
    )
    style = st.text_input(
        "Style", value=profile.style, key=f"profile_edit_style_{profile.id}"
    )
    profile_format = st.text_input(
        "Format",
        value=profile.format,
        key=f"profile_edit_format_{profile.id}",
    )
    constraints = st.text_input(
        "Constraints",
        value=profile.constraints,
        key=f"profile_edit_constraints_{profile.id}",
    )
    domain_context = st.text_input(
        "Domain context",
        value=profile.domain_context,
        key=f"profile_edit_domain_context_{profile.id}",
    )
    col_save, col_cancel = st.columns(2)
    if col_save.button("Save profile", key=f"profile_edit_save_{profile.id}"):
        try:
            store.update_profile(
                profile.id,
                name,
                addressing,
                style,
                profile_format,
                constraints=constraints,
                domain_context=domain_context,
            )
        except DuplicateProfileNameError:
            st.error("A profile with this name already exists.")
        except KeyError:
            st.error("This profile no longer exists.")
        except ValueError:
            st.error("Profile name must not be empty.")
        else:
            st.session_state.pop("profile_edit_target", None)
            agent.reload_profile()
            st.rerun()
    if col_cancel.button("Cancel", key=f"profile_edit_cancel_{profile.id}"):
        st.session_state.pop("profile_edit_target", None)
        st.rerun()


def _profile_clone_form(store, agent, profile):
    """Clone one profile under a prefilled "<name> (copy)" name."""
    new_name = st.text_input(
        "New profile name",
        value=f"{profile.name} (copy)",
        key=f"profile_clone_name_{profile.id}",
    )
    col_save, col_cancel = st.columns(2)
    if col_save.button("Clone profile", key=f"profile_clone_save_{profile.id}"):
        try:
            store.clone_profile(profile.id, new_name)
        except DuplicateProfileNameError:
            st.error("A profile with this name already exists.")
        except KeyError:
            st.error("This profile no longer exists.")
        except ValueError:
            st.error("Profile name must not be empty.")
        else:
            st.session_state.pop("profile_clone_target", None)
            agent.reload_profile()
            st.rerun()
    if col_cancel.button("Cancel", key=f"profile_clone_cancel_{profile.id}"):
        st.session_state.pop("profile_clone_target", None)
        st.rerun()


def _profile_create_form(store, agent):
    """Create a new profile; a fresh nonce clears the form after success."""
    nonce = st.session_state.get("profile_create_nonce", 0)
    name = st.text_input("Name", key=f"profile_new_name_{nonce}")
    addressing = st.text_input("Addressing", key=f"profile_new_addressing_{nonce}")
    style = st.text_input("Style", key=f"profile_new_style_{nonce}")
    profile_format = st.text_input("Format", key=f"profile_new_format_{nonce}")
    constraints = st.text_input("Constraints", key=f"profile_new_constraints_{nonce}")
    domain_context = st.text_input(
        "Domain context", key=f"profile_new_domain_context_{nonce}"
    )
    if st.button("Create profile", key="profile_create"):
        try:
            store.create_profile(
                name,
                addressing,
                style,
                profile_format,
                constraints=constraints,
                domain_context=domain_context,
            )
        except DuplicateProfileNameError:
            st.error("A profile with this name already exists.")
        except ValueError:
            st.error("Profile name must not be empty.")
        else:
            st.session_state["profile_create_nonce"] = nonce + 1
            st.session_state.pop("profile_edit_target", None)
            agent.reload_profile()
            st.rerun()


def _render_active_profile_selector(store, agent, chat_id):
    """Render the active-profile selector and return the assigned profile.

    Both modes reuse this one widget, so the key stays unique per chat: Chat
    shows it compactly next to the message input, Diagnostics inside the
    "User profiles" panel. A deleted profile leaves a stale id in the widget
    state; it is dropped before the selectbox renders the current assignment.
    A changed selection is persisted and reloaded before the rerun, so the
    next request uses it. Reading and writing the assignment never calls the
    provider.
    """
    profiles = store.list_profiles()
    profile_names = {profile.id: profile.name for profile in profiles}
    options = [None] + [profile.id for profile in profiles]
    active = store.get_active_profile(chat_id)
    active_id = active.id if active is not None else None

    select_key = f"active_profile_{chat_id}"
    if st.session_state.get(select_key) not in options:
        st.session_state.pop(select_key, None)
    selected = st.selectbox(
        "Active profile for this chat",
        options=options,
        index=options.index(active_id) if active_id in options else 0,
        format_func=lambda value: (
            "No profile"
            if value is None
            else profile_names.get(value, str(value))
        ),
        key=select_key,
    )
    if selected != active_id:
        store.set_active_profile(chat_id, selected)
        agent.reload_profile()
        st.rerun()

    return active


def _render_user_profiles(store, agent, chat_id):
    """Render profile selection, preview and CRUD for the active chat.

    The section is separate from the memory layers: a profile is not memory and
    is never stored as a memory item. Every successful mutation reloads the
    agent's cached profile and reruns, so the next request sees the persisted
    state. No handler here calls the provider. The create form stays hidden
    until the toggle is clicked, so the panel opens with the profile list only.
    """
    active = _render_active_profile_selector(store, agent, chat_id)
    profiles = store.list_profiles()

    preview = format_profile_block(active)
    if preview is None:
        st.caption("No profile: no profile block is added to the request.")
    else:
        st.code(preview)
        st.caption(f"≈ {estimate_tokens(preview)} tokens in the request.")

    edit_target = st.session_state.get("profile_edit_target")
    clone_target = st.session_state.get("profile_clone_target")
    pending_delete = st.session_state.get("pending_profile_delete_id")

    st.markdown("Profiles")
    for profile in profiles:
        st.markdown(f"**{profile.name}**")
        col_edit, col_clone, col_delete = st.columns(3)
        if col_edit.button("Edit", key=f"profile_edit_{profile.id}"):
            st.session_state["profile_edit_target"] = profile.id
            st.session_state.pop("profile_clone_target", None)
            st.rerun()
        if col_clone.button("Clone", key=f"profile_clone_{profile.id}"):
            st.session_state["profile_clone_target"] = profile.id
            st.session_state.pop("profile_edit_target", None)
            st.rerun()
        if col_delete.button("Delete", key=f"profile_delete_{profile.id}"):
            st.session_state["pending_profile_delete_id"] = profile.id
            st.rerun()

    if edit_target is not None:
        target = next(
            (profile for profile in profiles if profile.id == edit_target), None
        )
        if target is None:
            st.session_state.pop("profile_edit_target", None)
            st.error("This profile no longer exists.")
        else:
            _profile_edit_form(store, agent, target)

    if clone_target is not None:
        target = next(
            (profile for profile in profiles if profile.id == clone_target), None
        )
        if target is None:
            st.session_state.pop("profile_clone_target", None)
            st.error("This profile no longer exists.")
        else:
            _profile_clone_form(store, agent, target)

    if pending_delete is not None:
        target = next(
            (profile for profile in profiles if profile.id == pending_delete), None
        )
        if target is None:
            st.session_state.pop("pending_profile_delete_id", None)
            st.error("This profile no longer exists.")
        else:
            st.warning(
                f'Delete profile "{target.name}"? Chats using it will switch to '
                "No profile."
            )
            col_yes, col_no = st.columns(2)
            if col_yes.button("Yes, delete profile", key="profile_delete_confirm"):
                try:
                    store.delete_profile(target.id)
                except KeyError:
                    st.error("This profile no longer exists.")
                else:
                    st.session_state.pop("pending_profile_delete_id", None)
                    agent.reload_profile()
                    st.rerun()
            if col_no.button("Cancel", key="profile_delete_cancel"):
                st.session_state.pop("pending_profile_delete_id", None)
                st.rerun()

    st.button(
        "➕ Create profile",
        key="profile_create_toggle",
        on_click=_open_profile_create_form,
    )
    if st.session_state.get("profile_create_open"):
        _profile_create_form(store, agent)


# Resolve the active chat on startup: keep the current chat if it still
# exists, otherwise open the last selected chat, otherwise the newest one.
# With no chats at all nothing is selected: the user creates the first chat
# explicitly, so the empty state offers only the "create chat" action.
current_id = st.session_state.get("chat_id")
if current_id is None or not chat_exists(current_id):
    last_id = store.get_last_selected_id()
    if last_id is not None and chat_exists(last_id):
        current_id = last_id
    else:
        chats = store.list_chats()
        current_id = chats[0].id if chats else None

# Rebuild the agent whenever the active chat changes; drop it when no chat
# is open so the UI renders the empty state.
if current_id is None:
    st.session_state.chat_id = None
    st.session_state.agent = None
    st.session_state.agent_chat_id = None
elif (
    "agent" not in st.session_state
    or st.session_state.get("agent_chat_id") != current_id
):
    st.session_state.chat_id = current_id
    st.session_state.agent = _build_agent(current_id)
    st.session_state.agent_chat_id = current_id
    store.set_last_selected(current_id)

chat_id = st.session_state.chat_id
agent = st.session_state.agent

# The streamed text of a task step is written to a placeholder in the main chat
# area: never into the pinned bottom container and never as a chat message. The
# sidebar fallback renders the card before the chat area exists, so in that
# layout the placeholder is created here; the default bottom layout creates it
# below the history instead, so the stream stays next to the conversation.
_task_stream_placeholder = (
    st.empty()
    if agent is not None and TASK_CARD_LOCATION == "sidebar"
    else None
)


def _follow_step_run():
    """Show the background step run of this session and settle its outcome.

    The card starts the provider call in a worker thread of ``task_runner``;
    this function only polls the record, so the call keeps going independently
    of any Streamlit rerun and the script is never the owner of the request. A
    run whose thread disappeared without a status is reported as stopped, while
    a finished one is discarded before the rerun that reads its stored result.
    """
    global _task_stream_placeholder

    token = st.session_state.get(STEP_RUN_TOKEN_KEY)
    if token is None:
        return

    record = step_run_record(token)
    if record is None:
        st.session_state.pop(STEP_RUN_TOKEN_KEY, None)
        return

    task = task_orchestrator.current_task(chat_id)
    if record.chat_id != chat_id or task is None or task.id != record.task_id:
        # The run belongs to another chat or task: keep the token for its owner
        # and do not draw its stream under the wrong card.
        return

    if record.status == STEP_RUN_RUNNING and not record.is_live():
        st.session_state.pop(STEP_RUN_TOKEN_KEY, None)
        discard_step_run(token)
        report_task_error(
            "The previous step run stopped unexpectedly. Run the step again."
        )
        st.rerun()

    plan = task_orchestrator.repository.load_plan(task.id)
    placeholder = _task_stream_placeholder
    if placeholder is None:
        placeholder = st.empty()
        _task_stream_placeholder = placeholder

    indicator = WaitingIndicator(
        placeholder.spinner(running_step_label(task, plan)), placeholder
    )
    while record.status == STEP_RUN_RUNNING and record.is_live():
        indicator.show_chunk(record.snapshot()["text"])
        time.sleep(STEP_RUN_POLL_SECONDS)

    st.session_state.pop(STEP_RUN_TOKEN_KEY, None)
    discard_step_run(token)
    if record.status == STEP_RUN_DONE:
        indicator.show_chunk(record.text)
    elif record.status == STEP_RUN_ERROR:
        indicator.clear()
        report_task_error(record.error)
    st.rerun()


def _render_task_card_in_sidebar(store, orchestrator, chat_id):
    """Render the compact task card above the chat settings (FR-38 fallback)."""
    render_task_card(store, orchestrator, chat_id)


# Apply a mode switch requested by the task card or the result dialog. The
# request is a plain key because `MODE_KEY` belongs to the radio: Streamlit
# forbids writing a widget key during the run that created the widget, so the
# request is stored during that run and applied here, before the radio exists.
_requested_mode = st.session_state.pop(PENDING_MODE_KEY, None)
if _requested_mode in (MODE_CHAT, MODE_DIAGNOSTICS, MODE_TASK):
    st.session_state[MODE_KEY] = _requested_mode


with st.sidebar:
    # The mode switch is the first sidebar control and renders even with no
    # chats, so Diagnostics stays reachable and the empty state remains usable.
    st.radio(
        "Mode",
        options=(MODE_CHAT, MODE_DIAGNOSTICS, MODE_TASK),
        key=MODE_KEY,
    )
    mode = st.session_state[MODE_KEY]

    st.header("Chats")

    # The icon buttons must keep their intrinsic width on every sidebar state
    # and keep a 4px inner padding on both sides. They are targeted through the
    # CSS class Streamlit derives from `key` (`st-key-<key>`): the chat
    # rename/delete keys and the branch delete key. The `delete_` prefix also
    # matches the delete-confirmation buttons, which is a harmless widening of
    # the same 4px rule.
    st.markdown(
        """
        <style>
        div[class*="st-key-rename_"] button,
        div[class*="st-key-delete_"] button,
        div[class*="st-key-branch_delete_"] button {
            padding-left: 4px !important;
            padding-right: 4px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if st.button("➕ Create chat"):
        st.session_state.chat_id = store.create_chat(AgentConfig())
        st.session_state.pending_delete_id = None
        st.session_state.pending_branch_delete_id = None
        st.session_state.rename_chat_id = None
        st.rerun()

    chats = store.list_chats()
    rename_id = st.session_state.get("rename_chat_id")
    for chat in chats:
        label = f"▶ {chat.title}" if chat.id == chat_id else chat.title
        # Stretch the chat button so long titles shrink and ellipsize inside
        # their own column instead of forcing the row wider and clipping the
        # icons. The two icon buttons form a single right-aligned horizontal
        # group (`wrap=False` keeps it on one row), so the pair stays pinned to
        # the right edge of the sidebar with a fixed inner gap and the distance
        # to the right edge does not change when the sidebar is widened or
        # narrowed.
        col_name, col_actions = st.columns([0.6, 0.4], gap="small")
        if col_name.button(
            label, key=f"chat_{chat.id}", use_container_width=True
        ):
            if chat.id != chat_id:
                st.session_state.chat_id = chat.id
                st.session_state.pending_delete_id = None
                st.session_state.pending_branch_delete_id = None
                st.session_state.rename_chat_id = None
                st.rerun()
        with col_actions:
            with st.container(
                horizontal=True,
                horizontal_alignment="right",
                wrap=False,
            ):
                if st.button(
                    "",
                    key=f"rename_{chat.id}",
                    icon=":material/edit:",
                    help="Rename chat",
                    width="content",
                ):
                    st.session_state.rename_chat_id = chat.id
                    st.session_state.pending_delete_id = None
                    st.rerun()
                if st.button(
                    "",
                    key=f"delete_{chat.id}",
                    icon=":material/delete:",
                    help="Delete chat",
                    width="content",
                ):
                    st.session_state.pending_delete_id = chat.id
                    st.session_state.rename_chat_id = None
                    st.rerun()

        if rename_id == chat.id:
            new_title = st.text_input(
                "New title",
                value=chat.title,
                key=f"rename_input_{chat.id}",
            )
            col_save, col_cancel = st.columns(2)
            if col_save.button("Save", key=f"rename_save_{chat.id}"):
                if store.rename_chat(chat.id, new_title):
                    st.session_state.rename_chat_id = None
                    st.rerun()
                else:
                    st.warning("Title must not be empty.")
            if col_cancel.button("Cancel", key=f"rename_cancel_{chat.id}"):
                st.session_state.rename_chat_id = None
                st.rerun()

        st.caption(_chat_compact(chat))

    st.divider()

    pending_id = st.session_state.get("pending_delete_id")
    if pending_id is not None:
        title = next(
            (chat.title for chat in chats if chat.id == pending_id),
            DEFAULT_CHAT_TITLE,
        )
        st.warning(f'Delete chat "{title}"? This action is irreversible.')
        col_yes, col_no = st.columns(2)
        if col_yes.button("Yes, delete", key="delete_confirm"):
            store.delete_chat(pending_id)
            # The deleted chat cascades to its tasks; the remembered selection
            # lives in app_state and has to be dropped explicitly.
            task_orchestrator.repository.clear_selection(pending_id)
            remaining = store.list_chats()
            st.session_state.chat_id = remaining[0].id if remaining else None
            st.session_state.agent = None
            st.session_state.agent_chat_id = None
            st.session_state.pending_delete_id = None
            st.session_state.pending_branch_delete_id = None
            st.session_state.rename_chat_id = None
            st.rerun()
        if col_no.button("Cancel", key="delete_cancel"):
            st.session_state.pending_delete_id = None
            st.rerun()

    if agent is not None:
        st.divider()

        if TASK_CARD_LOCATION == "sidebar" and mode == MODE_CHAT:
            # Documented fallback (FR-38): the compact task section moves above
            # the chat settings when the bottom card does not fit.
            _render_task_card_in_sidebar(store, task_orchestrator, chat_id)
            st.divider()

        with st.expander("Chat settings"):
            cfg = agent.config
            system_prompt = st.text_area(
                "System prompt", value=cfg.system_prompt, key=f"system_prompt_{chat_id}"
            )
            invariants = st.text_area(
                "Invariants",
                value=cfg.invariants,
                key=f"invariants_{chat_id}",
            )
            st.caption(
                "Invariants are always enforced and are not memory: they are never "
                "stored as memory items."
            )
            model = st.text_input("Model", value=cfg.model, key=f"model_{chat_id}")
            known_name = display_name(model)
            if known_name is not None:
                st.caption(f"Full model name: {known_name}")
            else:
                st.caption(
                    f'Unknown model name: the entered ID "{model}" will be sent '
                    "in the request."
                )
            temperature = st.slider(
                "Temperature",
                min_value=0.0,
                max_value=2.0,
                value=float(cfg.temperature),
                step=0.1,
                key=f"temperature_{chat_id}",
            )
            max_tokens = st.number_input(
                "Max tokens",
                min_value=1,
                value=int(cfg.max_tokens),
                step=1,
                key=f"max_tokens_{chat_id}",
            )
            stream = st.checkbox(
                "Streaming output", value=bool(cfg.stream), key=f"stream_{chat_id}"
            )
            strategy_values = [value for value, _ in STRATEGY_CHOICES]
            strategy = st.selectbox(
                "Context strategy",
                options=strategy_values,
                index=strategy_values.index(cfg.context_strategy),
                format_func=strategy_label,
                key=f"context_strategy_{chat_id}",
            )
            keep_recent_turns = int(cfg.keep_recent_turns)
            sliding_window_messages = int(cfg.sliding_window_messages)
            facts_window_messages = int(cfg.facts_window_messages)
            if strategy == STRATEGY_SUMMARY:
                keep_recent_turns = st.number_input(
                    "Recent turns without compression",
                    min_value=0,
                    value=int(cfg.keep_recent_turns),
                    step=1,
                    key=f"keep_recent_turns_{chat_id}",
                )
                st.caption(
                    "The last N turns always go to the model without compression. "
                    "Older ones are folded into a summary in batches (3 turns at a "
                    "time), so a few more than N turns may be sent between "
                    "compressions. The full conversation is stored in the database "
                    "and displayed."
                )
            elif strategy == STRATEGY_SLIDING:
                sliding_window_messages = st.number_input(
                    "Sliding window size, messages",
                    min_value=1,
                    value=int(cfg.sliding_window_messages),
                    step=1,
                    key=f"sliding_window_{chat_id}",
                )
                st.caption(
                    "Only the most recent part of the conversation plus the current "
                    "request is sent. The system prompt is not part of the window. "
                    "The stored history is not shortened."
                )
            elif strategy == STRATEGY_FACTS:
                facts_window_messages = st.number_input(
                    "Sticky Facts window size, messages",
                    min_value=1,
                    value=int(cfg.facts_window_messages),
                    step=1,
                    key=f"facts_window_{chat_id}",
                )
                st.caption(
                    "Active facts and the most recent part of the conversation are "
                    "sent. Facts are updated by a separate call after the response."
                )
            elif strategy == STRATEGY_BRANCHING:
                st.caption(
                    "The active branch history is sent without compression. "
                    "Branches fork from a completed turn and are isolated from "
                    "each other."
                )
            else:
                st.caption(
                    "The full history is sent without compression. The full "
                    "conversation is stored in the database."
                )
            demo_limit_raw = st.number_input(
                "Demo context limit, tokens",
                min_value=0,
                value=int(cfg.demo_context_limit or 0),
                step=1,
                key=f"demo_context_limit_{chat_id}",
            )
            st.caption(
                '0 = disabled. Local pre-check of "estimated input + max_tokens" '
                "before the request; it does not replace the real API limit. The "
                "payload of the selected strategy is checked."
            )

            demo_context_limit = demo_limit_raw if demo_limit_raw > 0 else None

            updated_config = AgentConfig(
                model=model,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                demo_context_limit=demo_context_limit,
                summarize=(strategy == STRATEGY_SUMMARY),
                keep_recent_turns=keep_recent_turns,
                context_strategy=strategy,
                sliding_window_messages=sliding_window_messages,
                facts_window_messages=facts_window_messages,
                invariants=invariants,
            )
            if not configs_equal(cfg, updated_config):
                agent.set_config(updated_config)

        # Always visible: which profile the agent will apply to every request in
        # this chat. Reading it is a local store lookup and never calls the API.
        sidebar_profile = store.get_active_profile(chat_id)
        sidebar_profile_name = (
            sidebar_profile.name if sidebar_profile is not None else "No profile"
        )
        st.caption(
            f"Active profile: {sidebar_profile_name} · applied to every request "
            "in this chat automatically."
        )

        active_strategy = agent.config.context_strategy
        chat_stats = store.get_chat_stats(chat_id)
        st.caption(
            f"{strategy_label(active_strategy)} · "
            f"in {_fmt_num(chat_stats.input_tokens)} · "
            f"out {_fmt_num(chat_stats.output_tokens)} · "
            f"≈ history {_fmt_num(chat_stats.history_tokens_est)} · "
            f"{_fmt_cost(chat_stats.cost_usd)}"
        )

if agent is None:
    st.info('No conversations yet. Click "Create chat" in the sidebar to start.')
elif mode == MODE_DIAGNOSTICS:
    # Day 11 explicit memory layers. Working memory is bound to this chat and
    # the exact active line; long-term memory is global and never receives a
    # chat or branch id, so it stays visible from every chat.
    current_chat = next(
        (chat for chat in store.list_chats() if chat.id == chat_id), None
    )
    chat_title = (
        current_chat.title if current_chat is not None else DEFAULT_CHAT_TITLE
    )
    active_branch = store.get_active_branch(chat_id)
    branch_id = agent.active_branch_id
    scope_token = "main" if branch_id is None else str(branch_id)
    pending_clear = st.session_state.pop("memory_add_clear", None)
    if pending_clear is not None:
        for field in pending_clear:
            st.session_state.pop(field, None)

    line_messages = store.load_branch_line(chat_id)
    line_tokens = messages_tokens_est(
        [
            {"role": message.role, "content": message.content}
            for message in line_messages
        ]
    )
    working_items = store.list_memory_items(
        MEMORY_SCOPE_WORKING, chat_id=chat_id, branch_id=branch_id
    )
    long_term_items = store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
    working_tokens = _memory_layer_tokens(
        working_items, WORKING_MEMORY_BLOCK_TITLE
    )
    long_term_tokens = _memory_layer_tokens(
        long_term_items, LONG_TERM_MEMORY_BLOCK_TITLE
    )
    system_prompt = agent.config.system_prompt or ""
    invariants = agent.config.invariants or ""

    st.subheader("Memory layers")

    with st.expander("Short-term memory · current line", expanded=False):
        st.markdown(f"Active line: **{_line_label(active_branch)}**")
        st.caption(
            f"Messages: {len(line_messages)} · ≈ {line_tokens} tokens"
        )
        if line_messages:
            st.dataframe(
                [
                    {
                        "id": message.id,
                        "role": message.role,
                        "≈ tokens": estimate_tokens(message.content),
                        "preview": _branch_snippet(message),
                    }
                    for message in line_messages
                ]
            )
        else:
            st.caption("No messages on the active line yet.")
        st.caption(
            "The full conversation is shown in the chat; only the part selected "
            "by the context strategy is sent in the request."
        )

    with st.expander("Working memory · this chat & line", expanded=False):
        st.caption(f"Scope: {_memory_scope_label(chat_title, active_branch)}")
        st.caption(
            f"≈ {working_tokens} tokens in the prompt · "
            f"{sum(1 for item in working_items if item.included)} of "
            f"{len(working_items)} included"
        )
        if working_items:
            _render_memory_items(
                MEMORY_SCOPE_WORKING,
                scope_token,
                working_items,
                store,
                agent,
                chat_id,
                branch_id,
                allow_promote=True,
            )
        else:
            st.caption("No working memory items for this line yet.")
        st.markdown("Add an item")
        _render_memory_add_form(
            MEMORY_SCOPE_WORKING, scope_token, store, agent, chat_id, branch_id
        )

    with st.expander("Long-term memory · shared across chats", expanded=False):
        st.caption("Shared by all chats; survives chat and branch deletion.")
        st.caption(
            f"≈ {long_term_tokens} tokens in the prompt · "
            f"{sum(1 for item in long_term_items if item.included)} of "
            f"{len(long_term_items)} included"
        )
        if long_term_items:
            _render_memory_items(
                MEMORY_SCOPE_LONG_TERM,
                "shared",
                long_term_items,
                store,
                agent,
                chat_id,
                branch_id,
                allow_promote=False,
            )
        else:
            st.caption("No long-term memory items yet.")
        st.markdown("Add an item")
        _render_memory_add_form(
            MEMORY_SCOPE_LONG_TERM, "shared", store, agent, chat_id, branch_id
        )

    with st.expander("System prompt & invariants (not memory)", expanded=False):
        st.caption(
            "These are not memory layers and are never stored as memory items."
        )
        st.markdown(
            f"System prompt · ≈ {estimate_tokens(system_prompt)} tokens"
        )
        st.code(system_prompt)
        st.markdown(f"Invariants · ≈ {estimate_tokens(invariants)} tokens")
        st.code(invariants)
        st.caption(
            "Structural invariants are separate hard/advisory rules stored in "
            "SQLite; see Diagnostics / Task → Invariants."
        )

    with st.expander("User profiles", expanded=False):
        st.caption(
            "A profile describes how to answer this user. It is not memory, and "
            "the invariants above always outrank it."
        )
        _render_user_profiles(store, agent, chat_id)

    active_strategy = agent.config.context_strategy

    if active_strategy == STRATEGY_SUMMARY:
        with st.expander("History compression", expanded=False):
            chat_stats = store.get_chat_stats(chat_id)
            st.markdown(
                "Total compression attempts (including truncated and failed): "
                f"in {_fmt_num(chat_stats.summary_input_tokens)} · "
                f"out {_fmt_num(chat_stats.summary_output_tokens)} · "
                f"cache hit {_fmt_num(chat_stats.summary_cache_hit_tokens)} / "
                f"miss {_fmt_num(chat_stats.summary_cache_miss_tokens)} · "
                f"{_fmt_cost(chat_stats.summary_cost_usd)}"
            )

            summary = store.load_summary(chat_id, branch_id=agent.active_branch_id)
            if summary is None:
                st.caption("No summary yet — it will appear after the first turns.")
            else:
                st.markdown(summary.content)
                covered_turns = summary.covered_messages_count // 2
                st.markdown(
                    f"The summary replaces the first "
                    f"{summary.covered_messages_count} messages "
                    f"({covered_turns} turns) in the request to the model; "
                    f"last updated: {summary.updated_at}"
                )
                st.markdown(
                    "Last successful summary update (attempts aggregate): "
                    f"in {_fmt_num(summary.prompt_tokens)} · "
                    f"out {_fmt_num(summary.response_tokens)} · "
                    f"{_fmt_cost(summary.cost_usd)}"
                )
                messages = [
                    {"role": message.role, "content": message.content}
                    for message in store.load_branch_line(chat_id)
                ]
                system_prompt = agent.config.system_prompt
                full_payload = build_payload(
                    system_prompt, messages, "", summarize_enabled=False
                )
                compressed_payload = build_payload(
                    system_prompt,
                    messages,
                    "",
                    summary_content=summary.content,
                    covered_messages_count=summary.covered_messages_count,
                    summarize_enabled=True,
                )
                full_tokens = sum(
                    estimate_tokens(m["content"]) for m in full_payload
                )
                compressed_tokens = sum(
                    estimate_tokens(m["content"]) for m in compressed_payload
                )
                st.markdown(
                    f"≈ Request context without compression: {full_tokens} tokens · "
                    f"with compression: {compressed_tokens} tokens"
                )
            st.caption(
                "The provider bills every executed attempt, even when the summary "
                "is not updated. The cumulative line covers all attempts; the "
                "successful update date refers to the summary itself."
            )

    elif active_strategy == STRATEGY_FACTS:
        with st.expander("Sticky Facts", expanded=False):
            chat_stats = store.get_chat_stats(chat_id)
            st.markdown(
                f"Window: {agent.config.facts_window_messages} messages · "
                f"coverage boundary (message id): "
                f"{_fmt_num(chat_stats.facts_anchor_message_id)}"
            )
            active_facts = store.load_facts(chat_id)
            st.markdown("Active facts")
            if active_facts:
                st.dataframe(
                    [
                        {
                            "category": fact.category,
                            "key": fact.key,
                            "value": fact.value,
                            "updated": fact.updated_at or "no data",
                        }
                        for fact in active_facts
                    ]
                )
            else:
                st.caption(
                    "No active facts yet — they will appear after the first turns."
                )
            inactive = [
                fact
                for fact in store.load_facts(chat_id, include_inactive=True)
                if fact.status != "active"
            ]
            st.markdown("Cancelled and replaced")
            if inactive:
                st.dataframe(
                    [
                        {
                            "status": fact.status,
                            "category": fact.category,
                            "key": fact.key,
                            "value": fact.value,
                            "reason": fact.reason or "no data",
                            "updated": fact.updated_at or "no data",
                        }
                        for fact in inactive
                    ]
                )
            else:
                st.caption("No cancelled or replaced facts.")
            st.markdown(
                "Successful calls (including an empty list): "
                f"in {_fmt_num(chat_stats.facts_ok_input_tokens)} · "
                f"out {_fmt_num(chat_stats.facts_ok_output_tokens)} · "
                f"cache hit {_fmt_num(chat_stats.facts_ok_cache_hit_tokens)} / "
                f"miss {_fmt_num(chat_stats.facts_ok_cache_miss_tokens)} · "
                f"{_fmt_cost(chat_stats.facts_ok_cost_usd)}"
            )
            st.markdown(
                "Truncated/failed calls: "
                f"in {_fmt_num(chat_stats.facts_fail_input_tokens)} · "
                f"out {_fmt_num(chat_stats.facts_fail_output_tokens)} · "
                f"cache hit {_fmt_num(chat_stats.facts_fail_cache_hit_tokens)} / "
                f"miss {_fmt_num(chat_stats.facts_fail_cache_miss_tokens)} · "
                f"{_fmt_cost(chat_stats.facts_fail_cost_usd)}"
            )

    elif active_strategy == STRATEGY_BRANCHING:
        with st.expander("Branches", expanded=False):
            branches = store.list_branches(chat_id)
            branches_by_id = {branch.id: branch for branch in branches}
            messages_by_id = {
                message.id: message
                for message in store.load_history(chat_id)
                if message.id is not None
            }

            def fork_note(fork_message_id):
                message = messages_by_id.get(fork_message_id)
                if message is None:
                    return f"id {fork_message_id} (message not found)"
                return f'"{_branch_snippet(message)}" (id {fork_message_id})'

            pending_branch_error = st.session_state.pop("branch_error", None)
            if pending_branch_error:
                st.error(pending_branch_error)

            active_branch = store.get_active_branch(chat_id)
            if active_branch is None:
                st.markdown("Active line: Main")
            else:
                st.markdown(
                    f'Active branch: "{active_branch.name}" · '
                    f"parent: "
                    f"{_branch_parent_display(active_branch, branches_by_id)} · "
                    f"checkpoint: {fork_note(active_branch.fork_message_id)}"
                )

            st.markdown("Switch line")
            if st.button(
                "▶ Main line" if active_branch is None else "Main line",
                key=f"branch_main_{chat_id}",
            ):
                store.set_active_branch(chat_id, None)
                st.session_state.agent = _build_agent(chat_id)
                st.session_state.agent_chat_id = chat_id
                st.session_state.pending_branch_delete_id = None
                st.rerun()

            children_by_parent = {}
            for branch in branches:
                children_by_parent.setdefault(
                    branch.parent_branch_id, []
                ).append(branch)

            visited = set()

            def render_branch(branch, depth):
                visited.add(branch.id)
                indent = "\u00a0\u00a0\u00a0\u00a0" * depth
                is_active = (
                    active_branch is not None
                    and branch.id == active_branch.id
                )
                label = f"{indent}{'▶ ' if is_active else ''}{branch.name}"
                col_toggle, col_delete = st.columns([0.8, 0.2], gap="small")
                if col_toggle.button(
                    label, key=f"branch_{branch.id}", use_container_width=True
                ):
                    store.set_active_branch(chat_id, branch.id)
                    st.session_state.agent = _build_agent(chat_id)
                    st.session_state.agent_chat_id = chat_id
                    st.session_state.pending_branch_delete_id = None
                    st.rerun()
                if col_delete.button(
                    "",
                    key=f"branch_delete_{branch.id}",
                    icon=":material/delete:",
                    help="Delete branch",
                    width="content",
                ):
                    branch_children = children_by_parent.get(branch.id, [])
                    if branch_children:
                        names = ", ".join(
                            f'"{child.name}"' for child in branch_children
                        )
                        st.error(
                            f"Delete child branches first: {names}. "
                            f"The branch was not deleted."
                        )
                    else:
                        st.session_state.pending_branch_delete_id = branch.id
                        st.rerun()
                st.caption(
                    f"parent: "
                    f"{_branch_parent_display(branch, branches_by_id)} · "
                    f"checkpoint: {fork_note(branch.fork_message_id)}"
                )
                for child in sorted(
                    children_by_parent.get(branch.id, []),
                    key=lambda item: item.id,
                ):
                    if child.id not in visited:
                        render_branch(child, depth + 1)

            roots = [
                branch
                for branch in sorted(branches, key=lambda item: item.id)
                if branch.parent_branch_id is None
                or branch.parent_branch_id not in branches_by_id
            ]
            for root in roots:
                if root.id not in visited:
                    render_branch(root, 1)
            # A cyclic or corrupt parent link must never hide a branch.
            for branch in sorted(branches, key=lambda item: item.id):
                if branch.id not in visited:
                    render_branch(branch, 1)

            pending_branch_id = st.session_state.get(
                "pending_branch_delete_id"
            )
            if pending_branch_id is not None:
                pending_branch = branches_by_id.get(pending_branch_id)
                if pending_branch is None:
                    st.session_state.pending_branch_delete_id = None
                else:
                    st.warning(
                        f'Delete branch "{pending_branch.name}"? Its messages, '
                        f"turns and summary will be deleted. The main line and "
                        f"sibling branches will not change. This action is "
                        f"irreversible."
                    )
                    col_yes, col_no = st.columns(2)
                    if col_yes.button(
                        "Yes, delete branch", key="branch_delete_confirm"
                    ):
                        try:
                            store.delete_branch(chat_id, pending_branch_id)
                        except BranchHasChildrenError as exc:
                            names = ", ".join(
                                f'"{child.name}"' for child in exc.children
                            )
                            st.session_state.branch_error = (
                                f"Delete child branches first: {names}. "
                                f"The branch was not deleted."
                            )
                        else:
                            st.session_state.agent = _build_agent(chat_id)
                            st.session_state.agent_chat_id = chat_id
                        st.session_state.pending_branch_delete_id = None
                        st.rerun()
                    if col_no.button(
                        "Cancel", key="branch_delete_cancel"
                    ):
                        st.session_state.pending_branch_delete_id = None
                        st.rerun()

            st.markdown("Create branch")
            parent_labels = ["Main line"] + [
                f'"{branch.name}" (id {branch.id})' for branch in branches
            ]
            label_to_parent = {"Main line": None}
            for branch in branches:
                label_to_parent[
                    f'"{branch.name}" (id {branch.id})'
                ] = branch.id
            parent_state_key = f"branch_parent_{chat_id}"
            if st.session_state.get(parent_state_key) not in parent_labels:
                st.session_state.pop(parent_state_key, None)
            active_label = "Main line"
            if active_branch is not None:
                active_label = (
                    f'"{active_branch.name}" (id {active_branch.id})'
                )
            default_index = (
                parent_labels.index(active_label)
                if active_label in parent_labels
                else 0
            )
            selected_parent_label = st.selectbox(
                "Parent line",
                options=parent_labels,
                index=default_index,
                key=parent_state_key,
            )
            selected_parent = label_to_parent[selected_parent_label]
            parent_token = (
                "main" if selected_parent is None else str(selected_parent)
            )
            parent_display = (
                "Main line"
                if selected_parent is None
                else f'"{branches_by_id[selected_parent].name}"'
            )

            checkpoints = store.load_line_checkpoints(
                chat_id, selected_parent
            )
            if not checkpoints:
                st.caption(
                    "The selected line has no completed turns — a branch "
                    "cannot be created."
                )
            else:
                checkpoint_options = [
                    f"id {message.id}: {_branch_snippet(message)}"
                    for message in checkpoints
                ]
                checkpoint_to_id = {
                    f"id {message.id}: {_branch_snippet(message)}": message.id
                    for message in checkpoints
                }
                selected_checkpoint = st.selectbox(
                    "Checkpoint (completed turn)",
                    options=checkpoint_options,
                    key=f"branch_checkpoint_{chat_id}_{parent_token}",
                )
                selected_checkpoint_id = checkpoint_to_id[
                    selected_checkpoint
                ]
                selected_message = next(
                    message
                    for message in checkpoints
                    if message.id == selected_checkpoint_id
                )
                st.caption(
                    f"A branch will be created from line {parent_display}, turn: "
                    f'"{_branch_snippet(selected_message)}" '
                    f"(id {selected_checkpoint_id})."
                )
                nonce = st.session_state.get(
                    f"branch_name_nonce_{chat_id}", 0
                )
                branch_name = st.text_input(
                    "New branch name",
                    key=f"branch_name_{chat_id}_{nonce}",
                )
                if st.button(
                    "Create branch", key=f"branch_create_{chat_id}"
                ):
                    if not branch_name.strip():
                        st.error("Enter a branch name.")
                    else:
                        try:
                            new_branch_id = store.create_branch(
                                chat_id,
                                branch_name,
                                parent_branch_id=selected_parent,
                                fork_message_id=selected_checkpoint_id,
                            )
                        except DuplicateBranchNameError:
                            st.error(
                                f'A branch named "{branch_name.strip()}" already '
                                f"exists in this chat. Choose another name."
                            )
                        except ValueError as exc:
                            st.error(
                                f"Could not create branch: {exc}"
                            )
                        else:
                            store.set_active_branch(
                                chat_id, new_branch_id
                            )
                            st.session_state.agent = _build_agent(chat_id)
                            st.session_state.agent_chat_id = chat_id
                            st.session_state[
                                f"branch_name_nonce_{chat_id}"
                            ] = nonce + 1
                            st.session_state.pending_branch_delete_id = None
                            st.session_state.pop(parent_state_key, None)
                            st.rerun()

    with st.expander("Current chat statistics", expanded=False):
        chat_stats = store.get_chat_stats(chat_id)
        total_input = _sum_optional(
            chat_stats.input_tokens,
            chat_stats.summary_input_tokens,
            chat_stats.facts_ok_input_tokens,
            chat_stats.facts_fail_input_tokens,
        )
        total_cost = _sum_optional(
            chat_stats.cost_usd,
            chat_stats.summary_cost_usd,
            chat_stats.facts_ok_cost_usd,
            chat_stats.facts_fail_cost_usd,
        )
        st.markdown(
            f"Context strategy: {strategy_label(chat_stats.context_strategy)}"
        )
        st.markdown(f"Turns: {chat_stats.turns_count}")
        st.markdown(
            f"Last request context: "
            f"{_fmt_num(chat_stats.last_context_tokens)} tokens."
        )
        st.markdown(f"Cumulative input: {_fmt_num(chat_stats.input_tokens)} tokens.")
        st.markdown(f"Cumulative output: {_fmt_num(chat_stats.output_tokens)} tokens.")
        st.markdown(
            f"≈ Unique history: {_fmt_num(chat_stats.history_tokens_est)} tokens."
        )
        st.markdown(f"≈ Cost of turns: {_fmt_cost(chat_stats.cost_usd)}")
        st.markdown(
            f"Cumulative input for compression: "
            f"{_fmt_num(chat_stats.summary_input_tokens)} tokens."
        )
        st.markdown(
            f"Cumulative output for compression: "
            f"{_fmt_num(chat_stats.summary_output_tokens)} tokens."
        )
        st.markdown(
            f"Cumulative cache hit for compression: "
            f"{_fmt_num(chat_stats.summary_cache_hit_tokens)} tokens."
        )
        st.markdown(
            f"Cumulative cache miss for compression: "
            f"{_fmt_num(chat_stats.summary_cache_miss_tokens)} tokens."
        )
        st.markdown(
            f"≈ Cost of compression attempts (including truncated): "
            f"{_fmt_cost(chat_stats.summary_cost_usd)}"
        )
        st.markdown(
            "Facts, successful calls: "
            f"in {_fmt_num(chat_stats.facts_ok_input_tokens)} · "
            f"out {_fmt_num(chat_stats.facts_ok_output_tokens)} · "
            f"{_fmt_cost(chat_stats.facts_ok_cost_usd)}"
        )
        st.markdown(
            "Facts, truncated/failed calls: "
            f"in {_fmt_num(chat_stats.facts_fail_input_tokens)} · "
            f"out {_fmt_num(chat_stats.facts_fail_output_tokens)} · "
            f"{_fmt_cost(chat_stats.facts_fail_cost_usd)}"
        )
        st.markdown(f"Total input: {_fmt_num(total_input)} tokens.")
        st.markdown(f"≈ Total cost: {_fmt_cost(total_cost)}")

        turn_rows = []
        for index, turn in enumerate(store.list_turns(chat_id), start=1):
            s = turn.stats
            turn_rows.append(
                {
                    "#": index,
                    "time": turn.created_at,
                    "message tokens ≈": _fmt_num(s.user_message_tokens_est),
                    "context": _fmt_num(s.request_tokens),
                    "response": _fmt_num(s.response_tokens),
                    "total": _fmt_num(s.total_tokens),
                    "hit": _fmt_num(s.prompt_cache_hit_tokens),
                    "miss": _fmt_num(s.prompt_cache_miss_tokens),
                    "finish_reason": s.finish_reason or "no data",
                    "cost ≈": _fmt_cost(s.cost_usd),
                    "assumption": s.cost_assumption or "no data",
                }
            )
        if turn_rows:
            st.dataframe(turn_rows)
        else:
            st.caption("No turns yet.")

    with st.expander("Conversation comparison", expanded=False):
        comparison_rows = []
        for chat in store.list_chats():
            comparison_rows.append(
                {
                    "Title": chat.title,
                    "Strategy": strategy_label(chat.context_strategy),
                    "Turns": chat.turns_count,
                    "Cumulative input": _fmt_num(chat.input_tokens),
                    "Cumulative output": _fmt_num(chat.output_tokens),
                    "≈ Last context": _fmt_num(chat.last_context_tokens),
                    "≈ Unique history": _fmt_num(chat.history_tokens_est),
                    "≈ Cost of turns": _fmt_cost(chat.cost_usd),
                    "≈ Compression cost": _fmt_cost(chat.summary_cost_usd),
                    "≈ Facts cost": _fmt_cost(chat.facts_cost_usd),
                    "≈ Total": _fmt_cost(
                        _sum_optional(
                            chat.cost_usd, chat.summary_cost_usd, chat.facts_cost_usd
                        )
                    ),
                }
            )
        if comparison_rows:
            st.dataframe(comparison_rows)
        else:
            st.caption("No conversations yet.")

elif mode == MODE_CHAT:
    _render_chat_history(store, chat_id)
    # Readable step results, read from the stored artifacts: they survive a
    # rerun and an application restart and stay next to the conversation.
    render_task_results_panel(task_orchestrator, chat_id)

    if TASK_CARD_LOCATION != "sidebar":
        # The placeholder sits between the history and the pinned bottom
        # container, so the streamed step text appears in the conversation area.
        _task_stream_placeholder = st.empty()

    # Compact profile row: the active profile is the only profile control Chat
    # mode needs; the full list and CRUD live in Diagnostics behind the button.
    # The row and the message input share one bottom-pinned container: on its
    # own, st.chat_input is pinned to the window bottom while the row would stay
    # in the flow after the history, so the gap between them grew with the
    # history length. Inside st.bottom the input renders inline, directly below
    # the row, so both stay attached to the bottom of the main area. Bottom
    # alignment puts the button on the selectbox baseline: the selector is
    # taller than the button, so top alignment would pin it to the label line.
    with st.bottom:
        if TASK_CARD_LOCATION == "bottom":
            # The task card is the first element of the bottom container: it
            # never wraps the profile row, which stays a direct child rendered
            # between the card and the message input (FR-33).
            render_task_card(store, task_orchestrator, chat_id)

        col_profile, col_manage = st.columns(
            [0.75, 0.25], vertical_alignment="bottom"
        )
        with col_profile:
            _render_active_profile_selector(store, agent, chat_id)
        with col_manage:
            st.button(
                "Manage profiles",
                key="manage_profiles",
                on_click=_open_diagnostics,
            )

        prompt = st.chat_input("Enter a message")

    # A background step run started by the card is followed here, before the
    # chat prompt is handled: the click callback only launched the worker.
    _follow_step_run()

    # The reply stays in the main area: rendering it inside st.bottom would
    # draw the streamed answer into the pinned container instead of the history.
    if prompt is not None:
        if not prompt.strip():
            st.warning("Message must not be empty.")
        else:
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                if agent.config.stream:
                    placeholder = st.skeleton()
                    indicator = WaitingIndicator(
                        st.spinner("Waiting for a response..."), placeholder
                    )

                    try:
                        result = agent.ask(
                            prompt,
                            on_chunk=indicator.show_chunk,
                            on_summarizing=lambda: st.spinner(
                                "Compressing history..."
                            ),
                            on_facts_updating=lambda: st.spinner(
                                "Updating facts..."
                            ),
                        )
                    except ContextLimitError as exc:
                        indicator.clear()
                        st.warning(str(exc))
                    except InvariantConflictError as exc:
                        # No provider call happened and nothing was saved: show
                        # the refusal in place, without a rerun and without a
                        # fabricated assistant answer.
                        indicator.clear()
                        st.error(format_conflict_message(exc.conflict))
                    except ApiContextOverflowError as exc:
                        indicator.clear()
                        st.error(str(exc))
                        st.caption(
                            "Tip: enable the demo context limit in the settings "
                            "to catch an overflow before the request."
                        )
                    except Exception as exc:
                        indicator.clear()
                        st.error(f"Error: {exc}")
                    else:
                        indicator.show_chunk(result.text)
                        st.session_state.summary_error = result.summary_error
                        st.session_state.facts_error = result.facts_error
                        st.rerun()
                else:
                    try:
                        with st.spinner("Waiting for a response..."):
                            result = agent.ask(
                                prompt,
                                on_summarizing=lambda: st.spinner(
                                    "Compressing history..."
                                ),
                                on_facts_updating=lambda: st.spinner(
                                    "Updating facts..."
                                ),
                            )
                        st.markdown(result.text)
                        st.session_state.summary_error = result.summary_error
                        st.session_state.facts_error = result.facts_error
                        st.rerun()
                    except ContextLimitError as exc:
                        st.warning(str(exc))
                    except InvariantConflictError as exc:
                        # The refusal is shown in place; nothing was saved.
                        st.error(format_conflict_message(exc.conflict))
                    except ApiContextOverflowError as exc:
                        st.error(str(exc))
                        st.caption(
                            "Tip: enable the demo context limit in the settings "
                            "to catch an overflow before the request."
                        )
                    except Exception as exc:
                        st.error(f"Error: {exc}")

else:
    # Day 13 task diagnostics. User profiles stay in Diagnostics / Memory: this
    # mode only shows the task state, its journal, artifacts, workflow, packet
    # preview and usage. Rendering here never calls the provider.
    render_diagnostics_task(
        store, task_orchestrator, chat_id, invariant_repository
    )
