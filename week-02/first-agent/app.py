import streamlit as st

from agent import (
    AgentConfig,
    ApiContextOverflowError,
    ChatAgent,
    ContextLimitError,
    get_client,
)
from app_logic import WaitingIndicator, configs_equal
from context import build_payload
from models import display_name
from storage import (
    DEFAULT_CHAT_TITLE,
    BranchHasChildrenError,
    ChatStore,
    DuplicateBranchNameError,
)
from strategies import (
    STRATEGY_BRANCHING,
    STRATEGY_CHOICES,
    STRATEGY_FACTS,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    strategy_label,
)
from tokens import estimate_tokens

st.set_page_config(page_title="Первый агент", page_icon="🤖", layout="wide")
st.title("Первый агент")

if "client" not in st.session_state:
    st.session_state.client = get_client()

client = st.session_state.client

if client is None:
    st.error("Добавьте DEEPSEEK_API_KEY в файл .env и перезапустите приложение.")
    st.stop()

if "store" not in st.session_state:
    st.session_state.store = ChatStore()

store = st.session_state.store

# A summary update failure is reported on the next render, after the rerun that
# follows a successful turn; popping it here ensures it is shown exactly once.
pending_summary_error = st.session_state.pop("summary_error", None)
if pending_summary_error:
    st.warning(pending_summary_error)

pending_facts_error = st.session_state.pop("facts_error", None)
if pending_facts_error:
    st.warning(pending_facts_error)


def chat_exists(chat_id):
    return any(chat.id == chat_id for chat in store.list_chats())


def _fmt_num(value):
    return "нет данных" if value is None else str(value)


def _fmt_cost(value):
    return "нет данных" if value is None else f"≈${value:.6f}"


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
        return "нет данных"
    return (
        f"вх {_fmt_num(chat.input_tokens)} · вых {_fmt_num(chat.output_tokens)} · "
        f"{_fmt_cost(chat.cost_usd)}"
    )


def _turn_summary(turn):
    """Compact single-line summary of a completed turn."""
    return (
        f"контекст {_fmt_num(turn.request_tokens)} · "
        f"всего {_fmt_num(turn.total_tokens)} · "
        f"cache hit {_fmt_num(turn.prompt_cache_hit_tokens)} / "
        f"miss {_fmt_num(turn.prompt_cache_miss_tokens)} · "
        f"{_fmt_cost(turn.cost_usd)} · "
        f"finish: {turn.finish_reason or 'нет данных'}"
    )


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
        return "основная линия"
    parent = branches_by_id.get(branch.parent_branch_id)
    if parent is None:
        return f"ветка id {branch.parent_branch_id}"
    return f"«{parent.name}»"


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
    st.session_state.agent = ChatAgent(client, store, current_id)
    st.session_state.agent_chat_id = current_id
    store.set_last_selected(current_id)

chat_id = st.session_state.chat_id
agent = st.session_state.agent

with st.sidebar:
    st.header("Чаты")

    if st.button("➕ Создать чат"):
        st.session_state.chat_id = store.create_chat(AgentConfig())
        st.session_state.pending_delete_id = None
        st.session_state.pending_branch_delete_id = None
        st.session_state.rename_chat_id = None
        st.rerun()

    chats = store.list_chats()
    rename_id = st.session_state.get("rename_chat_id")
    for chat in chats:
        label = f"▶ {chat.title}" if chat.id == chat_id else chat.title
        # Stretch the buttons so long titles shrink and ellipsize inside their
        # own column instead of forcing the row wider and clipping the icons.
        col_name, col_rename, col_delete = st.columns(
            [0.6, 0.2, 0.2], gap="small"
        )
        if col_name.button(
            label, key=f"chat_{chat.id}", use_container_width=True
        ):
            if chat.id != chat_id:
                st.session_state.chat_id = chat.id
                st.session_state.pending_delete_id = None
                st.session_state.pending_branch_delete_id = None
                st.session_state.rename_chat_id = None
                st.rerun()
        if col_rename.button(
            "✏️",
            key=f"rename_{chat.id}",
            help="Переименовать чат",
            use_container_width=True,
        ):
            st.session_state.rename_chat_id = chat.id
            st.session_state.pending_delete_id = None
            st.rerun()
        if col_delete.button(
            "🗑️",
            key=f"delete_{chat.id}",
            help="Удалить чат",
            use_container_width=True,
        ):
            st.session_state.pending_delete_id = chat.id
            st.session_state.rename_chat_id = None
            st.rerun()

        if rename_id == chat.id:
            new_title = st.text_input(
                "Новое название",
                value=chat.title,
                key=f"rename_input_{chat.id}",
            )
            col_save, col_cancel = st.columns(2)
            if col_save.button("Сохранить", key=f"rename_save_{chat.id}"):
                if store.rename_chat(chat.id, new_title):
                    st.session_state.rename_chat_id = None
                    st.rerun()
                else:
                    st.warning("Название не должно быть пустым.")
            if col_cancel.button("Отмена", key=f"rename_cancel_{chat.id}"):
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
        st.warning(f"Удалить чат «{title}»? Это действие необратимо.")
        col_yes, col_no = st.columns(2)
        if col_yes.button("Да, удалить", key="delete_confirm"):
            store.delete_chat(pending_id)
            remaining = store.list_chats()
            st.session_state.chat_id = remaining[0].id if remaining else None
            st.session_state.agent = None
            st.session_state.agent_chat_id = None
            st.session_state.pending_delete_id = None
            st.session_state.pending_branch_delete_id = None
            st.session_state.rename_chat_id = None
            st.rerun()
        if col_no.button("Отмена", key="delete_cancel"):
            st.session_state.pending_delete_id = None
            st.rerun()

    if agent is not None:
        st.divider()

        with st.expander("Настройки чата"):
            cfg = agent.config
            system_prompt = st.text_area(
                "System prompt", value=cfg.system_prompt, key=f"system_prompt_{chat_id}"
            )
            model = st.text_input("Модель", value=cfg.model, key=f"model_{chat_id}")
            known_name = display_name(model)
            if known_name is not None:
                st.caption(f"Полное название модели: {known_name}")
            else:
                st.caption(
                    f"Неизвестное имя модели: в запрос уйдёт введённый ID «{model}»."
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
                "Потоковый вывод", value=bool(cfg.stream), key=f"stream_{chat_id}"
            )
            strategy_values = [value for value, _ in STRATEGY_CHOICES]
            strategy = st.selectbox(
                "Стратегия контекста",
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
                    "Последних ходов без сжатия",
                    min_value=0,
                    value=int(cfg.keep_recent_turns),
                    step=1,
                    key=f"keep_recent_turns_{chat_id}",
                )
                st.caption(
                    "Последние N ходов всегда уходят в модель без сжатия. Более "
                    "старые сворачиваются в сводку порциями (по 3 хода), поэтому "
                    "между сжатиями в запрос может уходить немного больше N ходов. "
                    "Полная переписка хранится в базе и отображается."
                )
            elif strategy == STRATEGY_SLIDING:
                sliding_window_messages = st.number_input(
                    "Размер скользящего окна, сообщения",
                    min_value=1,
                    value=int(cfg.sliding_window_messages),
                    step=1,
                    key=f"sliding_window_{chat_id}",
                )
                st.caption(
                    "В запрос уходит только последняя часть переписки вместе с "
                    "текущим запросом. System prompt не входит в окно. История "
                    "в базе не сокращается."
                )
            elif strategy == STRATEGY_FACTS:
                facts_window_messages = st.number_input(
                    "Размер окна Sticky Facts, сообщения",
                    min_value=1,
                    value=int(cfg.facts_window_messages),
                    step=1,
                    key=f"facts_window_{chat_id}",
                )
                st.caption(
                    "В запрос уходят активные факты и последняя часть переписки. "
                    "Факты обновляются отдельным вызовом после ответа."
                )
            elif strategy == STRATEGY_BRANCHING:
                st.caption(
                    "В запрос уходит история активной ветки без сжатия. Ветки "
                    "ответвляются от завершённого хода и изолированы друг от друга."
                )
            else:
                st.caption(
                    "В запрос уходит вся история без сжатия. Полная переписка "
                    "хранится в базе."
                )
            demo_limit_raw = st.number_input(
                "Демо-лимит контекста, токены",
                min_value=0,
                value=int(cfg.demo_context_limit or 0),
                step=1,
                key=f"demo_context_limit_{chat_id}",
            )
            st.caption(
                "0 = выключен. Локальная предпроверка «оценка входа + max_tokens» "
                "до запроса; реальный лимит API не заменяет. Проверяется payload "
                "выбранной стратегии."
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
            )
            if not configs_equal(cfg, updated_config):
                agent.set_config(updated_config)

        active_strategy = agent.config.context_strategy
        chat_stats = store.get_chat_stats(chat_id)
        st.caption(
            f"{strategy_label(active_strategy)} · "
            f"вх {_fmt_num(chat_stats.input_tokens)} · "
            f"вых {_fmt_num(chat_stats.output_tokens)} · "
            f"≈ история {_fmt_num(chat_stats.history_tokens_est)} · "
            f"{_fmt_cost(chat_stats.cost_usd)}"
        )

if agent is None:
    st.info("Чатов пока нет. Нажмите «Создать чат» в сайдбаре, чтобы начать.")
else:
    history = store.load_branch_history(chat_id)
    for message in history:
        with st.chat_message(message.role):
            st.markdown(message.content)
            turn = message.turn
            if turn is None:
                st.caption("статистика хода: нет данных")
            elif message.role == "user":
                est = turn.user_message_tokens_est
                if est is not None:
                    st.caption(f"≈ {est} ток. сообщения (оценка)")
                else:
                    st.caption("статистика хода: нет данных")
            else:
                st.caption(f"токенов ответа: {_fmt_num(turn.response_tokens)}")
                if turn.finish_reason == "length":
                    st.caption(
                        "Ответ достиг лимита max_tokens (лимит длины ответа, а не контекста)"
                    )
                st.caption(_turn_summary(turn))

    active_strategy = agent.config.context_strategy

    if active_strategy == STRATEGY_SUMMARY:
        with st.expander("Сжатие истории", expanded=False):
            chat_stats = store.get_chat_stats(chat_id)
            st.markdown(
                "Всего на попытки сжатия (включая оборванные и неуспешные): "
                f"вх {_fmt_num(chat_stats.summary_input_tokens)} · "
                f"вых {_fmt_num(chat_stats.summary_output_tokens)} · "
                f"cache hit {_fmt_num(chat_stats.summary_cache_hit_tokens)} / "
                f"miss {_fmt_num(chat_stats.summary_cache_miss_tokens)} · "
                f"{_fmt_cost(chat_stats.summary_cost_usd)}"
            )

            summary = store.load_summary(chat_id, branch_id=agent.active_branch_id)
            if summary is None:
                st.caption("Сводки ещё нет — она появится после первых ходов.")
            else:
                st.markdown(summary.content)
                covered_turns = summary.covered_messages_count // 2
                st.markdown(
                    f"Сводка заменяет первые {summary.covered_messages_count} сообщений "
                    f"({covered_turns} ходов) в запросе к модели; успешно обновлена: "
                    f"{summary.updated_at}"
                )
                st.markdown(
                    "Последнее успешное обновление сводки (агрегат попыток): "
                    f"вх {_fmt_num(summary.prompt_tokens)} · "
                    f"вых {_fmt_num(summary.response_tokens)} · "
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
                    f"≈ Контекст запроса без сжатия: {full_tokens} ток. · "
                    f"со сжатием: {compressed_tokens} ток."
                )
            st.caption(
                "Провайдер тарифицирует каждую выполненную попытку, даже если сводка "
                "не обновлена. Накопительная строка учитывает все попытки; дата "
                "успешного обновления относится только к самой сводке."
            )

    elif active_strategy == STRATEGY_FACTS:
        with st.expander("Sticky Facts", expanded=False):
            chat_stats = store.get_chat_stats(chat_id)
            st.markdown(
                f"Окно: {agent.config.facts_window_messages} сообщений · "
                f"граница покрытия (id сообщения): "
                f"{_fmt_num(chat_stats.facts_anchor_message_id)}"
            )
            active_facts = store.load_facts(chat_id)
            st.markdown("Активные факты")
            if active_facts:
                st.dataframe(
                    [
                        {
                            "категория": fact.category,
                            "ключ": fact.key,
                            "значение": fact.value,
                            "обновлён": fact.updated_at or "нет данных",
                        }
                        for fact in active_facts
                    ]
                )
            else:
                st.caption(
                    "Активных фактов пока нет — они появятся после первых ходов."
                )
            inactive = [
                fact
                for fact in store.load_facts(chat_id, include_inactive=True)
                if fact.status != "active"
            ]
            st.markdown("Отменённые и заменённые")
            if inactive:
                st.dataframe(
                    [
                        {
                            "статус": fact.status,
                            "категория": fact.category,
                            "ключ": fact.key,
                            "значение": fact.value,
                            "причина": fact.reason or "нет данных",
                            "обновлён": fact.updated_at or "нет данных",
                        }
                        for fact in inactive
                    ]
                )
            else:
                st.caption("Отменённых и заменённых фактов нет.")
            st.markdown(
                "Успешные вызовы (включая пустой список): "
                f"вх {_fmt_num(chat_stats.facts_ok_input_tokens)} · "
                f"вых {_fmt_num(chat_stats.facts_ok_output_tokens)} · "
                f"cache hit {_fmt_num(chat_stats.facts_ok_cache_hit_tokens)} / "
                f"miss {_fmt_num(chat_stats.facts_ok_cache_miss_tokens)} · "
                f"{_fmt_cost(chat_stats.facts_ok_cost_usd)}"
            )
            st.markdown(
                "Оборванные/ошибочные вызовы: "
                f"вх {_fmt_num(chat_stats.facts_fail_input_tokens)} · "
                f"вых {_fmt_num(chat_stats.facts_fail_output_tokens)} · "
                f"cache hit {_fmt_num(chat_stats.facts_fail_cache_hit_tokens)} / "
                f"miss {_fmt_num(chat_stats.facts_fail_cache_miss_tokens)} · "
                f"{_fmt_cost(chat_stats.facts_fail_cost_usd)}"
            )

    elif active_strategy == STRATEGY_BRANCHING:
        with st.expander("Ветки", expanded=False):
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
                    return f"id {fork_message_id} (сообщение не найдено)"
                return f"«{_branch_snippet(message)}» (id {fork_message_id})"

            pending_branch_error = st.session_state.pop("branch_error", None)
            if pending_branch_error:
                st.error(pending_branch_error)

            active_branch = store.get_active_branch(chat_id)
            if active_branch is None:
                st.markdown("Активная линия: Основная")
            else:
                st.markdown(
                    f"Активная ветка: «{active_branch.name}» · "
                    f"родитель: "
                    f"{_branch_parent_display(active_branch, branches_by_id)} · "
                    f"checkpoint: {fork_note(active_branch.fork_message_id)}"
                )

            st.markdown("Переключение линии")
            if st.button(
                "▶ Основная линия" if active_branch is None else "Основная линия",
                key=f"branch_main_{chat_id}",
            ):
                store.set_active_branch(chat_id, None)
                st.session_state.agent = ChatAgent(client, store, chat_id)
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
                    st.session_state.agent = ChatAgent(client, store, chat_id)
                    st.session_state.agent_chat_id = chat_id
                    st.session_state.pending_branch_delete_id = None
                    st.rerun()
                if col_delete.button(
                    "🗑️",
                    key=f"branch_delete_{branch.id}",
                    help="Удалить ветку",
                    use_container_width=True,
                ):
                    branch_children = children_by_parent.get(branch.id, [])
                    if branch_children:
                        names = ", ".join(
                            f"«{child.name}»" for child in branch_children
                        )
                        st.error(
                            f"Сначала удалите дочерние ветки: {names}. "
                            f"Ветка не удалена."
                        )
                    else:
                        st.session_state.pending_branch_delete_id = branch.id
                        st.rerun()
                st.caption(
                    f"родитель: "
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
                        f"Удалить ветку «{pending_branch.name}»? Её сообщения, "
                        f"ходы и сводка будут удалены. Основная линия и соседние "
                        f"ветки не изменятся. Это действие необратимо."
                    )
                    col_yes, col_no = st.columns(2)
                    if col_yes.button(
                        "Да, удалить ветку", key="branch_delete_confirm"
                    ):
                        try:
                            store.delete_branch(chat_id, pending_branch_id)
                        except BranchHasChildrenError as exc:
                            names = ", ".join(
                                f"«{child.name}»" for child in exc.children
                            )
                            st.session_state.branch_error = (
                                f"Сначала удалите дочерние ветки: {names}. "
                                f"Ветка не удалена."
                            )
                        else:
                            st.session_state.agent = ChatAgent(
                                client, store, chat_id
                            )
                            st.session_state.agent_chat_id = chat_id
                        st.session_state.pending_branch_delete_id = None
                        st.rerun()
                    if col_no.button(
                        "Отмена", key="branch_delete_cancel"
                    ):
                        st.session_state.pending_branch_delete_id = None
                        st.rerun()

            st.markdown("Создание ветки")
            parent_labels = ["Основная линия"] + [
                f"«{branch.name}» (id {branch.id})" for branch in branches
            ]
            label_to_parent = {"Основная линия": None}
            for branch in branches:
                label_to_parent[
                    f"«{branch.name}» (id {branch.id})"
                ] = branch.id
            parent_state_key = f"branch_parent_{chat_id}"
            if st.session_state.get(parent_state_key) not in parent_labels:
                st.session_state.pop(parent_state_key, None)
            active_label = "Основная линия"
            if active_branch is not None:
                active_label = (
                    f"«{active_branch.name}» (id {active_branch.id})"
                )
            default_index = (
                parent_labels.index(active_label)
                if active_label in parent_labels
                else 0
            )
            selected_parent_label = st.selectbox(
                "Родительская линия",
                options=parent_labels,
                index=default_index,
                key=parent_state_key,
            )
            selected_parent = label_to_parent[selected_parent_label]
            parent_token = (
                "main" if selected_parent is None else str(selected_parent)
            )
            parent_display = (
                "Основная линия"
                if selected_parent is None
                else f"«{branches_by_id[selected_parent].name}»"
            )

            checkpoints = store.load_line_checkpoints(
                chat_id, selected_parent
            )
            if not checkpoints:
                st.caption(
                    "На выбранной линии нет завершённых ходов — создать ветку "
                    "нельзя."
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
                    "Checkpoint (завершённый ход)",
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
                    f"Будет создана ветка от линии {parent_display}, ход: "
                    f"«{_branch_snippet(selected_message)}» "
                    f"(id {selected_checkpoint_id})."
                )
                nonce = st.session_state.get(
                    f"branch_name_nonce_{chat_id}", 0
                )
                branch_name = st.text_input(
                    "Имя новой ветки",
                    key=f"branch_name_{chat_id}_{nonce}",
                )
                if st.button(
                    "Создать ветку", key=f"branch_create_{chat_id}"
                ):
                    if not branch_name.strip():
                        st.error("Введите имя ветки.")
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
                                f"Ветка с именем «{branch_name.strip()}» уже "
                                f"существует в этом чате. Выберите другое имя."
                            )
                        except ValueError as exc:
                            st.error(
                                f"Не удалось создать ветку: {exc}"
                            )
                        else:
                            store.set_active_branch(
                                chat_id, new_branch_id
                            )
                            st.session_state.agent = ChatAgent(
                                client, store, chat_id
                            )
                            st.session_state.agent_chat_id = chat_id
                            st.session_state[
                                f"branch_name_nonce_{chat_id}"
                            ] = nonce + 1
                            st.session_state.pending_branch_delete_id = None
                            st.session_state.pop(parent_state_key, None)
                            st.rerun()

    with st.expander("Статистика текущего чата", expanded=False):
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
            f"Стратегия контекста: {strategy_label(chat_stats.context_strategy)}"
        )
        st.markdown(f"Ходов: {chat_stats.turns_count}")
        st.markdown(
            f"Последний контекст запроса: "
            f"{_fmt_num(chat_stats.last_context_tokens)} ток."
        )
        st.markdown(f"Накопленный вход: {_fmt_num(chat_stats.input_tokens)} ток.")
        st.markdown(f"Накопленный выход: {_fmt_num(chat_stats.output_tokens)} ток.")
        st.markdown(
            f"≈ Уникальная история: {_fmt_num(chat_stats.history_tokens_est)} ток."
        )
        st.markdown(f"≈ Стоимость ходов: {_fmt_cost(chat_stats.cost_usd)}")
        st.markdown(
            f"Накопленный вход на сжатие: "
            f"{_fmt_num(chat_stats.summary_input_tokens)} ток."
        )
        st.markdown(
            f"Накопленный выход на сжатие: "
            f"{_fmt_num(chat_stats.summary_output_tokens)} ток."
        )
        st.markdown(
            f"Накопленный cache hit на сжатие: "
            f"{_fmt_num(chat_stats.summary_cache_hit_tokens)} ток."
        )
        st.markdown(
            f"Накопленный cache miss на сжатие: "
            f"{_fmt_num(chat_stats.summary_cache_miss_tokens)} ток."
        )
        st.markdown(
            f"≈ Стоимость попыток сжатия (включая оборванные): "
            f"{_fmt_cost(chat_stats.summary_cost_usd)}"
        )
        st.markdown(
            "Факты, успешные вызовы: "
            f"вх {_fmt_num(chat_stats.facts_ok_input_tokens)} · "
            f"вых {_fmt_num(chat_stats.facts_ok_output_tokens)} · "
            f"{_fmt_cost(chat_stats.facts_ok_cost_usd)}"
        )
        st.markdown(
            "Факты, оборванные/ошибочные вызовы: "
            f"вх {_fmt_num(chat_stats.facts_fail_input_tokens)} · "
            f"вых {_fmt_num(chat_stats.facts_fail_output_tokens)} · "
            f"{_fmt_cost(chat_stats.facts_fail_cost_usd)}"
        )
        st.markdown(f"Итоговый вход: {_fmt_num(total_input)} ток.")
        st.markdown(f"≈ Итоговая стоимость: {_fmt_cost(total_cost)}")

        turn_rows = []
        for index, turn in enumerate(store.list_turns(chat_id), start=1):
            s = turn.stats
            turn_rows.append(
                {
                    "№": index,
                    "время": turn.created_at,
                    "токены сообщения ≈": _fmt_num(s.user_message_tokens_est),
                    "контекст": _fmt_num(s.request_tokens),
                    "ответ": _fmt_num(s.response_tokens),
                    "всего": _fmt_num(s.total_tokens),
                    "hit": _fmt_num(s.prompt_cache_hit_tokens),
                    "miss": _fmt_num(s.prompt_cache_miss_tokens),
                    "finish_reason": s.finish_reason or "нет данных",
                    "стоимость ≈": _fmt_cost(s.cost_usd),
                    "допущение": s.cost_assumption or "нет данных",
                }
            )
        if turn_rows:
            st.dataframe(turn_rows)
        else:
            st.caption("Ходов пока нет.")

    with st.expander("Сравнение диалогов", expanded=False):
        comparison_rows = []
        for chat in store.list_chats():
            comparison_rows.append(
                {
                    "Название": chat.title,
                    "Стратегия": strategy_label(chat.context_strategy),
                    "Ходы": chat.turns_count,
                    "Накопленный вход": _fmt_num(chat.input_tokens),
                    "Накопленный выход": _fmt_num(chat.output_tokens),
                    "≈ Последний контекст": _fmt_num(chat.last_context_tokens),
                    "≈ Уникальная история": _fmt_num(chat.history_tokens_est),
                    "≈ Стоимость ходов": _fmt_cost(chat.cost_usd),
                    "≈ Стоимость сжатия": _fmt_cost(chat.summary_cost_usd),
                    "≈ Стоимость Facts": _fmt_cost(chat.facts_cost_usd),
                    "≈ Итого": _fmt_cost(
                        _sum_optional(
                            chat.cost_usd, chat.summary_cost_usd, chat.facts_cost_usd
                        )
                    ),
                }
            )
        if comparison_rows:
            st.dataframe(comparison_rows)
        else:
            st.caption("Диалогов пока нет.")

    prompt = st.chat_input("Введите сообщение")
    if prompt is not None:
        if not prompt.strip():
            st.warning("Сообщение не должно быть пустым.")
        else:
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                if agent.config.stream:
                    placeholder = st.skeleton()
                    indicator = WaitingIndicator(st.spinner("Ожидание ответа..."), placeholder)

                    try:
                        result = agent.ask(
                            prompt,
                            on_chunk=indicator.show_chunk,
                            on_summarizing=lambda: st.spinner("Сжимаю историю..."),
                            on_facts_updating=lambda: st.spinner("Обновляю факты..."),
                        )
                    except ContextLimitError as exc:
                        indicator.clear()
                        st.warning(str(exc))
                    except ApiContextOverflowError as exc:
                        indicator.clear()
                        st.error(str(exc))
                        st.caption(
                            "Совет: включите демо-лимит контекста в настройках, "
                            "чтобы отлавливать переполнение до запроса."
                        )
                    except Exception as exc:
                        indicator.clear()
                        st.error(f"Ошибка: {exc}")
                    else:
                        indicator.show_chunk(result.text)
                        st.session_state.summary_error = result.summary_error
                        st.session_state.facts_error = result.facts_error
                        st.rerun()
                else:
                    try:
                        with st.spinner("Ожидание ответа..."):
                            result = agent.ask(
                                prompt,
                                on_summarizing=lambda: st.spinner("Сжимаю историю..."),
                                on_facts_updating=lambda: st.spinner("Обновляю факты..."),
                            )
                        st.markdown(result.text)
                        st.session_state.summary_error = result.summary_error
                        st.session_state.facts_error = result.facts_error
                        st.rerun()
                    except ContextLimitError as exc:
                        st.warning(str(exc))
                    except ApiContextOverflowError as exc:
                        st.error(str(exc))
                        st.caption(
                            "Совет: включите демо-лимит контекста в настройках, "
                            "чтобы отлавливать переполнение до запроса."
                        )
                    except Exception as exc:
                        st.error(f"Ошибка: {exc}")
