import streamlit as st

from agent import AgentConfig, ChatAgent, get_client
from storage import DEFAULT_CHAT_TITLE, ChatStore

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


def chat_exists(chat_id):
    return any(chat.id == chat_id for chat in store.list_chats())


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

            changed = (
                system_prompt != cfg.system_prompt
                or model != cfg.model
                or abs(temperature - cfg.temperature) > 1e-9
                or max_tokens != cfg.max_tokens
                or stream != cfg.stream
            )
            if changed:
                agent.set_config(
                    AgentConfig(
                        model=model,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=stream,
                    )
                )

        usage = store.get_usage(chat_id)
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        total = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )

        def fmt(value):
            return "нет данных" if value is None else str(value)

        st.caption(
            f"Токены: ввод {fmt(input_tokens)} · вывод {fmt(output_tokens)} · всего {fmt(total)}"
        )

if agent is None:
    st.info("Чатов пока нет. Нажмите «Создать чат» в сайдбаре, чтобы начать.")
else:
    for message in agent.history:
        if message["role"] == "system":
            continue
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    prompt = st.chat_input("Введите сообщение")
    if prompt is not None:
        if not prompt.strip():
            st.warning("Сообщение не должно быть пустым.")
        else:
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                if agent.config.stream:
                    placeholder = st.empty()

                    def on_chunk(text):
                        placeholder.markdown(text)

                    try:
                        answer = agent.ask(prompt, on_chunk=on_chunk)
                    except Exception as exc:
                        placeholder.empty()
                        st.error(f"Ошибка: {exc}")
                    else:
                        placeholder.markdown(answer)
                        st.rerun()
                else:
                    try:
                        with st.spinner("Ожидание ответа..."):
                            answer = agent.ask(prompt)
                        st.markdown(answer)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Ошибка: {exc}")
