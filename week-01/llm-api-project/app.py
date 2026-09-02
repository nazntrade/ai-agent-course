import json
import os

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = "deepseek-v4-flash"
MAX_TOKENS = 1500

JSON_SYSTEM_PROMPT = (
    "Ты отвечаешь только строгим JSON, без какого-либо текста до или после него.\n"
    "Формат ответа:\n"
    "{\n"
    '  "title": "string",\n'
    '  "ingredients": [{"name": "string", "amount": "string"}],\n'
    '  "steps": [{"order": 1, "text": "string"}]\n'
    "}\n"
    "Правила:\n"
    "- Не используй markdown, комментарии или пояснения.\n"
    "- Заверши ответ сразу после закрывающей фигурной скобки }."
)

st.set_page_config(page_title="Запрос к LLM", page_icon="🤖")
st.title("Запрос к LLM")

prompt = st.text_area(
    "Введите запрос",
    value="Предложи простой завтрак из обычных продуктов",
)

mode = st.radio(
    "Режим ответа",
    ["Свободный", "Контролируемый (JSON)"],
    horizontal=True,
)

st.session_state.setdefault("free_result", None)
st.session_state.setdefault("controlled_result", None)
st.session_state.setdefault("controlled_error", None)
st.session_state.setdefault("free_stopped", False)
st.session_state.setdefault("controlled_stopped", False)


def validate_recipe(data):
    if not isinstance(data, dict):
        return "корень должен быть объектом"
    if not isinstance(data.get("title"), str):
        return "поле 'title' должно быть строкой"

    ingredients = data.get("ingredients")
    if not isinstance(ingredients, list):
        return "поле 'ingredients' должно быть списком"
    for item in ingredients:
        if not isinstance(item, dict):
            return "каждый элемент 'ingredients' должен быть объектом"
        if not isinstance(item.get("name"), str):
            return "в 'ingredients' поле 'name' должно быть строкой"
        if not isinstance(item.get("amount"), str):
            return "в 'ingredients' поле 'amount' должно быть строкой"

    steps = data.get("steps")
    if not isinstance(steps, list):
        return "поле 'steps' должно быть списком"
    for item in steps:
        if not isinstance(item, dict):
            return "каждый элемент 'steps' должен быть объектом"
        if not isinstance(item.get("order"), int) or isinstance(item.get("order"), bool):
            return "в 'steps' поле 'order' должно быть целым числом"
        if not isinstance(item.get("text"), str):
            return "в 'steps' поле 'text' должно быть строкой"

    return None


def run_free(client, prompt):
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
        stream=True,
    )

    placeholder = st.empty()
    collected = []
    finish_reason = None
    with st.spinner("DeepSeek думает..."):
        for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta and choice.delta.content:
                collected.append(choice.delta.content)
                placeholder.markdown("".join(collected))
            if choice.finish_reason:
                finish_reason = choice.finish_reason

    placeholder.empty()
    st.session_state["free_stopped"] = finish_reason == "length"
    return "".join(collected)


def run_controlled(client, prompt):
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": JSON_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=MAX_TOKENS,
        stream=True,
    )

    placeholder = st.empty()
    collected = []
    finish_reason = None
    with st.spinner("DeepSeek думает..."):
        for chunk in response:
            if chunk.choices:
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    collected.append(choice.delta.content)
                    placeholder.code("".join(collected), language="json")
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

    raw = "".join(collected)
    placeholder.empty()

    st.session_state["controlled_stopped"] = finish_reason == "length"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        st.session_state["controlled_result"] = None
        st.session_state["controlled_error"] = (
            f"Модель вернула некорректный JSON: {error}"
        )
        return

    error_message = validate_recipe(data)
    if error_message:
        st.session_state["controlled_result"] = None
        st.session_state["controlled_error"] = (
            f"Структура ответа не соответствует ожидаемой: {error_message}"
        )
        return

    st.session_state["controlled_result"] = data
    st.session_state["controlled_error"] = None


if st.button("Отправить", type="primary"):
    if not prompt.strip():
        st.warning("Сначала введите запрос.")
    elif not (api_key := os.getenv("DEEPSEEK_API_KEY")):
        st.error("Добавьте DEEPSEEK_API_KEY в файл .env")
    else:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        try:
            if mode == "Свободный":
                st.session_state["free_result"] = run_free(client, prompt)
            else:
                run_controlled(client, prompt)
        except Exception as error:
            st.error(f"Ошибка запроса: {error}")

left, right = st.columns(2)

with left:
    st.subheader("Свободный режим")
    if st.session_state["free_result"] is not None:
        st.markdown(st.session_state["free_result"])
    if st.session_state["free_stopped"]:
        st.warning("Ответ остановлен из-за ограничения длины")

with right:
    st.subheader("Контролируемый режим (JSON)")
    if st.session_state["controlled_result"] is not None:
        st.json(st.session_state["controlled_result"])
    elif st.session_state["controlled_error"] is not None:
        st.error(st.session_state["controlled_error"])
    if st.session_state["controlled_stopped"]:
        st.warning("Ответ остановлен из-за ограничения длины")
