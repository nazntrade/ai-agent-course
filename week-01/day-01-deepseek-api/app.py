import os

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

st.set_page_config(page_title="Первый запрос к LLM", page_icon="🤖")
st.title("Первый запрос к LLM")

prompt = st.text_area(
    "Введите запрос",
    placeholder="Например: объясни простыми словами, что такое API",
)

if st.button("Отправить", type="primary"):
    if not prompt.strip():
        st.warning("Сначала введите запрос.")
    elif not (api_key := os.getenv("DEEPSEEK_API_KEY")):
        st.error("Добавьте DEEPSEEK_API_KEY в файл .env")
    else:
        try:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )

            st.subheader("Ответ")
            with st.spinner("DeepSeek думает..."):
                st.write_stream(
                    chunk.choices[0].delta.content
                    for chunk in response
                    if chunk.choices and chunk.choices[0].delta.content
                )
        except Exception as error:
            st.error(f"Ошибка запроса: {error}")