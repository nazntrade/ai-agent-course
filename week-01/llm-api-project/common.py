import os

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = "deepseek-v4-flash"
MAX_TOKENS = 1500
TEMPERATURE = 0.2
BASE_URL = "https://api.deepseek.com"


def get_client():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=BASE_URL)


def stream_chunks(response, render, spinner_label):
    placeholder = st.empty()
    collected = []
    finish_reason = None
    with st.spinner(spinner_label):
        for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta and choice.delta.content:
                collected.append(choice.delta.content)
                render(placeholder, "".join(collected))
            if choice.finish_reason:
                finish_reason = choice.finish_reason

    placeholder.empty()
    return "".join(collected), finish_reason
