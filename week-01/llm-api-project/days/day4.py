import json
import time

import streamlit as st

from common import MODEL, get_client

DAY4_MAX_TOKENS = 250

DAY4_TEMPERATURES = [
    {"temperature": 0.0, "purpose": "точность и предсказуемость"},
    {"temperature": 0.7, "purpose": "сбалансированный режим"},
    {"temperature": 1.2, "purpose": "больше разнообразия"},
    {"temperature": 1.9, "purpose": "экспериментальный стресс-тест, возможна потеря связности"},
]

DAY4_DEFAULT_PROMPT = (
    "Объясни новичку, почему API-ключ нельзя хранить в публичном GitHub.\n"
    "\n"
    "Ответ строго до 80 слов:\n"
    "1. Один точный факт.\n"
    "2. Одна короткая аналогия.\n"
    "3. Три разных названия для памятки, каждое из 2–4 слов."
)

DAY4_RECOMMENDATIONS = (
    "- **0** — вычисления, код, извлечение фактов;\n"
    "- **0.7** — обычные объяснения и рабочие тексты;\n"
    "- **1.2** — идеи, варианты формулировок, мозговой штурм;\n"
    "- **1.9** — демонстрационный стресс-тест, не рекомендуемая рабочая настройка."
)


def count_words(text):
    if not text:
        return 0
    return len(text.split())


def run_day4_single(client, messages, temperature, placeholder):
    """Один потоковый вызов Дня 4. Ответы других вызовов в messages не добавляются."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": DAY4_MAX_TOKENS,
        "temperature": temperature,
        "stream": True,
        "thinking": {"type": "disabled"},
    }

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=DAY4_MAX_TOKENS,
        temperature=temperature,
        stream=True,
        extra_body={"thinking": {"type": "disabled"}},
    )

    collected = []
    finish_reason = None
    for chunk in response:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.delta and choice.delta.content:
            collected.append(choice.delta.content)
            placeholder.markdown("".join(collected))
        if choice.finish_reason:
            finish_reason = choice.finish_reason

    return "".join(collected), payload, finish_reason


def run_day4(client, prompt, status_text, progress_bar):
    messages = [{"role": "user", "content": prompt}]
    results = []
    total = len(DAY4_TEMPERATURES)

    for index, config in enumerate(DAY4_TEMPERATURES, start=1):
        status_text.markdown(
            f"Запрос {index} из {total}: temperature = {config['temperature']:g}"
        )
        start = time.perf_counter()
        placeholder = st.empty()
        try:
            text, payload, finish_reason = run_day4_single(
                client, messages, config["temperature"], placeholder
            )
        except Exception as error:
            text, payload, finish_reason = None, None, None
            request_error = str(error)
        else:
            request_error = None
        finally:
            placeholder.empty()

        results.append(
            {
                "temperature": config["temperature"],
                "purpose": config["purpose"],
                "text": text,
                "payload": payload,
                "time": time.perf_counter() - start,
                "finish_reason": finish_reason,
                "words": count_words(text),
                "error": request_error,
            }
        )
        progress_bar.progress(index / total)

    return results


def render_day4_results(results):
    columns = st.columns(4)
    for column, result in zip(columns, results):
        with column:
            st.markdown(f"**temperature = {result['temperature']:g}**")
            st.caption(result["purpose"])
            if result.get("error"):
                st.error(f"Ошибка запроса: {result['error']}")
            else:
                st.markdown(result["text"])
                st.markdown("---")
                st.markdown(f"Время: {result['time']:.2f} с")
                st.markdown(f"finish_reason: `{result['finish_reason'] or '—'}`")
                st.markdown(f"Слов: {result['words']}")
                if result["finish_reason"] == "length":
                    st.warning("Ответ оборван из-за лимита max_tokens")
                with st.expander("▶ Реальный payload запроса"):
                    payload = result["payload"] or {}
                    st.code(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        language="json",
                    )

    st.markdown("### Сравнительная таблица")
    rows = []
    for result in results:
        rows.append(
            {
                "temperature": result["temperature"],
                "Назначение": result["purpose"],
                "Время, с": f"{result['time']:.2f}",
                "finish_reason": result["finish_reason"] or "—",
                "Слов": result["words"],
                "Ошибка": result["error"] or "—",
            }
        )
    st.table(rows)

    st.markdown("### Ручная оценка ответов (1–5)")
    st.caption(
        "Оцените каждый ответ вручную: точность, креативность и разнообразие. "
        "Оценки не отправляются другой LLM; повторный запуск API возможен только по кнопке."
    )
    rating_columns = st.columns(4)
    for column, result in zip(rating_columns, results):
        temperature = result["temperature"]
        with column:
            st.markdown(f"**temperature = {temperature:g}**")
            st.number_input(
                "Точность",
                min_value=1,
                max_value=5,
                value=3,
                key=f"day4_accuracy_{temperature:g}",
            )
            st.number_input(
                "Креативность",
                min_value=1,
                max_value=5,
                value=3,
                key=f"day4_creativity_{temperature:g}",
            )
            st.number_input(
                "Разнообразие",
                min_value=1,
                max_value=5,
                value=3,
                key=f"day4_variety_{temperature:g}",
            )

    st.markdown("### Рекомендации по температуре")
    st.markdown(DAY4_RECOMMENDATIONS)


def render():
    st.session_state.setdefault("day4_results", None)
    st.session_state.setdefault("day4_run_prompt", None)

    st.subheader("Температура и характер ответа")
    st.markdown(
        "Один и тот же запрос отправляется 4 раза с разной `temperature`, чтобы "
        "сравнить точность, креативность и разнообразие ответов. Во всех четырёх "
        "запросах совпадают `model`, `messages`, `max_tokens` и остальные параметры; "
        "отличается только `temperature`. Отдельный лимит `max_tokens = 250`, "
        "а `thinking` отключён: во встроенном thinking-режиме DeepSeek может "
        "игнорировать `temperature`."
    )

    prompt = st.text_area(
        "Запрос (можно редактировать)",
        value=DAY4_DEFAULT_PROMPT,
        height=170,
    )

    st.markdown(
        "Запрос будет отправлен **4 раза последовательно и независимо**. "
        "Каждый вызов получает только исходный промпт — ответы предыдущих вызовов "
        "в `messages` не добавляются."
    )
    for config in DAY4_TEMPERATURES:
        st.markdown(f"- **{config['temperature']:g}** — {config['purpose']}.")

    if st.button("Сравнить температуры", type="primary"):
        if not prompt.strip():
            st.warning("Сначала введите запрос.")
        else:
            client = get_client()
            if client is None:
                st.error("Добавьте DEEPSEEK_API_KEY в файл .env")
            else:
                progress_bar = st.progress(0.0)
                status_text = st.empty()
                st.session_state["day4_results"] = run_day4(
                    client, prompt, status_text, progress_bar
                )
                st.session_state["day4_run_prompt"] = prompt
                status_text.empty()

    results = st.session_state["day4_results"]
    if results:
        if st.session_state["day4_run_prompt"] != prompt:
            st.caption(
                "Поле запроса изменено после последнего запуска — для нового текста "
                "нажмите «Сравнить температуры» ещё раз."
            )
        render_day4_results(results)
