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
from storage import DEFAULT_CHAT_TITLE, ChatStore
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


def chat_exists(chat_id):
    return any(chat.id == chat_id for chat in store.list_chats())


def _fmt_num(value):
    return "нет данных" if value is None else str(value)


def _fmt_cost(value):
    return "нет данных" if value is None else f"≈${value:.6f}"


def _sum_optional(a, b):
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


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
        st.rerun()

    chats = store.list_chats()
    for chat in chats:
        label = f"▶ {chat.title}" if chat.id == chat_id else chat.title
        if st.button(label, key=f"chat_{chat.id}"):
            if chat.id != chat_id:
                st.session_state.chat_id = chat.id
                st.session_state.pending_delete_id = None
                st.rerun()
        st.caption(_chat_compact(chat))

    st.divider()

    if agent is not None:
        if st.button("Удалить чат"):
            st.session_state.pending_delete_id = chat_id

        pending_id = st.session_state.get("pending_delete_id")
        if pending_id is not None:
            title = next(
                (chat.title for chat in chats if chat.id == pending_id),
                DEFAULT_CHAT_TITLE,
            )
            st.warning(f"Удалить чат «{title}»? Это действие необратимо.")
            col_yes, col_no = st.columns(2)
            if col_yes.button("Да, удалить"):
                store.delete_chat(pending_id)
                remaining = store.list_chats()
                st.session_state.chat_id = remaining[0].id if remaining else None
                st.session_state.agent = None
                st.session_state.agent_chat_id = None
                st.session_state.pending_delete_id = None
                st.rerun()
            if col_no.button("Отмена"):
                st.session_state.pending_delete_id = None
                st.rerun()

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
            summarize = st.checkbox(
                "Сжимать историю автоматически",
                value=bool(cfg.summarize),
                key=f"summarize_{chat_id}",
            )
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
            demo_limit_raw = st.number_input(
                "Демо-лимит контекста, токены",
                min_value=0,
                value=int(cfg.demo_context_limit or 0),
                step=1,
                key=f"demo_context_limit_{chat_id}",
            )
            st.caption(
                "0 = выключен. Локальная предпроверка «оценка входа + max_tokens» "
                "до запроса; реальный лимит API не заменяет."
            )

            demo_context_limit = demo_limit_raw if demo_limit_raw > 0 else None

            updated_config = AgentConfig(
                model=model,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                demo_context_limit=demo_context_limit,
                summarize=summarize,
                keep_recent_turns=keep_recent_turns,
            )
            if not configs_equal(cfg, updated_config):
                agent.set_config(updated_config)

        chat_stats = store.get_chat_stats(chat_id)
        st.caption(
            f"вх {_fmt_num(chat_stats.input_tokens)} · "
            f"вых {_fmt_num(chat_stats.output_tokens)} · "
            f"≈ история {_fmt_num(chat_stats.history_tokens_est)} · "
            f"{_fmt_cost(chat_stats.cost_usd)}"
        )

if agent is None:
    st.info("Чатов пока нет. Нажмите «Создать чат» в сайдбаре, чтобы начать.")
else:
    history = store.load_history(chat_id)
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

        summary = store.load_summary(chat_id)
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
            messages = store.load_messages(chat_id)
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

    with st.expander("Статистика текущего чата", expanded=False):
        chat_stats = store.get_chat_stats(chat_id)
        total_input = _sum_optional(
            chat_stats.input_tokens, chat_stats.summary_input_tokens
        )
        total_cost = _sum_optional(chat_stats.cost_usd, chat_stats.summary_cost_usd)
        st.markdown(f"Ходов: {chat_stats.turns_count}")
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
                    "Ходы": chat.turns_count,
                    "Накопленный вход": _fmt_num(chat.input_tokens),
                    "Накопленный выход": _fmt_num(chat.output_tokens),
                    "≈ Уникальная история": _fmt_num(chat.history_tokens_est),
                    "≈ Стоимость ходов": _fmt_cost(chat.cost_usd),
                    "≈ Стоимость сжатия": _fmt_cost(chat.summary_cost_usd),
                    "≈ Итого": _fmt_cost(
                        _sum_optional(chat.cost_usd, chat.summary_cost_usd)
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
                        st.rerun()
                else:
                    try:
                        with st.spinner("Ожидание ответа..."):
                            result = agent.ask(
                                prompt,
                                on_summarizing=lambda: st.spinner("Сжимаю историю..."),
                            )
                        st.markdown(result.text)
                        st.session_state.summary_error = result.summary_error
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
