import streamlit as st

from agent import ChatAgent, get_client

st.set_page_config(page_title="Первый агент", page_icon="🤖", layout="wide")
st.title("Первый агент")

if "client" not in st.session_state:
    st.session_state.client = get_client()

client = st.session_state.client

if client is None:
    st.error("Добавьте DEEPSEEK_API_KEY в файл .env и перезапустите приложение.")
    st.stop()

if "agent" not in st.session_state:
    st.session_state.agent = ChatAgent(client)

agent = st.session_state.agent

with st.sidebar:
    st.caption("System prompt:")
    st.write(agent.config.system_prompt)
    if st.button("Очистить историю"):
        agent.clear()
        st.rerun()

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
