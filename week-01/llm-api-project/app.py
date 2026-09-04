import streamlit as st

from days import DAYS

st.set_page_config(page_title="Запрос к LLM", page_icon="🤖", layout="wide")
st.title("Запрос к LLM")

day = st.radio(
    "Этап курса",
    [label for label, _ in DAYS],
    horizontal=True,
    key="day_selector",
)

for label, render in DAYS:
    if day == label:
        render()
        break
