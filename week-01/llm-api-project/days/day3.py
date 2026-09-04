import json
import re
import time

import streamlit as st

from common import MAX_TOKENS, MODEL, TEMPERATURE, get_client, stream_chunks

SPACE_TASK = (
    "Лунная исследовательская станция располагает максимум 14 единицами энергии "
    "и 10 часами работы.\n\n"
    "Доступные операции:\n\n"
    "A — картографирование кратеров: энергия 4, время 3 часа, ценность 9 баллов.\n\n"
    "B — анализ лунного льда: энергия 5, время 4 часа, ценность 12 баллов. "
    "Операцию B можно выполнить только при выборе операции A.\n\n"
    "C — радиационное сканирование: энергия 3, время 2 часа, ценность 7 баллов.\n\n"
    "D — бурение грунта: энергия 6, время 5 часов, ценность 15 баллов. "
    "Операцию D можно выполнить только при выборе операции A.\n\n"
    "E — наблюдение солнечной бури: энергия 4, время 4 часа, ценность 11 баллов. "
    "Операции E и C нельзя выполнять вместе.\n\n"
    "F — ремонт ретранслятора: энергия 7, время 3 часа, ценность 16 баллов. "
    "Операцию F можно выполнить только при выборе операции C.\n\n"
    "Необходимо выбрать набор операций с максимальной суммарной ценностью, "
    "не превышая ограничения по энергии и времени и соблюдая все зависимости.\n\n"
    "Последняя непустая строка каждого решения должна строго соответствовать формату:\n\n"
    "FINAL: <операции> = <баллы>\n\n"
    "Операции должны обозначаться латинскими буквами."
)

STEP_SYSTEM_PROMPT = "Решай задачу пошагово и проверяй каждый вывод и вычисление."

SELF_PROMPT_SYSTEM_PROMPT = (
    "Составь подробный промпт, который поможет решить задачу. "
    "Не решай задачу сам и не приводи ответ. "
    "Промпт должен содержать чёткие инструкции и шаги решения."
)

EXPERT_PANEL_SYSTEM_PROMPT = (
    "Ты работаешь как группа из трёх экспертов, совместно решающих задачу.\n"
    "Заключение каждого эксперта должно быть не длиннее 4 предложений.\n"
    "1. Аналитик: кратко предложи решение и стратегию его получения.\n"
    "2. Инженер: проверь энергию, время, зависимости и сумму баллов выбранного набора.\n"
    "3. Критик: проверь, существует ли более выгодный допустимый вариант.\n"
    "Правила:\n"
    "- Не перечисляй все возможные комбинации операций — рассмотри только перспективные.\n"
    "- Явно выдели разделы аналитика, инженера и критика.\n"
    "- Весь ответ должен быть не длиннее 500 слов; зарезервируй место для итоговой строки.\n"
    "- Последняя непустая строка ответа должна строго соответствовать формату:\n"
    "FINAL: <операции> = <баллы>"
)

OPERATIONS = [
    {"name": "A", "energy": 4, "time": 3, "value": 9, "requires": None, "conflicts": None},
    {"name": "B", "energy": 5, "time": 4, "value": 12, "requires": "A", "conflicts": None},
    {"name": "C", "energy": 3, "time": 2, "value": 7, "requires": None, "conflicts": None},
    {"name": "D", "energy": 6, "time": 5, "value": 15, "requires": "A", "conflicts": None},
    {"name": "E", "energy": 4, "time": 4, "value": 11, "requires": None, "conflicts": "C"},
    {"name": "F", "energy": 7, "time": 3, "value": 16, "requires": "C", "conflicts": None},
]


def calculate_optimum():
    best_ops = frozenset()
    best_value = 0
    for mask in range(1 << len(OPERATIONS)):
        ops = {op["name"] for i, op in enumerate(OPERATIONS) if mask & (1 << i)}
        energy = sum(op["energy"] for op in OPERATIONS if op["name"] in ops)
        work_time = sum(op["time"] for op in OPERATIONS if op["name"] in ops)
        value = sum(op["value"] for op in OPERATIONS if op["name"] in ops)

        if energy > 14 or work_time > 10:
            continue

        valid = True
        for op in OPERATIONS:
            if op["name"] not in ops:
                continue
            if op["requires"] and op["requires"] not in ops:
                valid = False
                break
            if op["conflicts"] and op["conflicts"] in ops:
                valid = False
                break
        if not valid:
            continue

        if value > best_value:
            best_value = value
            best_ops = ops

    return best_ops, best_value


def parse_final_answer(text):
    if not text:
        return None

    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if not re.match(r"final\s*:", stripped, re.IGNORECASE):
            continue

        body = re.sub(r"^final\s*:", "", stripped, flags=re.IGNORECASE).strip()
        if "=" not in body:
            return None

        left, right = body.split("=", 1)
        letters = re.findall(r"[A-Za-z]", left)
        if not letters:
            return None
        upper = [letter.upper() for letter in letters]
        if any(letter not in "ABCDEF" for letter in upper):
            return None
        ops_set = frozenset(upper)

        points_match = re.search(r"-?\d+", right)
        if not points_match:
            return None

        return ops_set, int(points_match.group())

    return None


def validate_answer(text):
    optimum_ops, optimum_value = calculate_optimum()
    parsed = parse_final_answer(text)
    if parsed is None:
        return False, "нет строки FINAL в корректном формате"

    ops_set, points = parsed
    if ops_set != optimum_ops:
        return False, "набор операций не совпадает с оптимальным"
    if points != optimum_value:
        return False, "баллы не совпадают с оптимальными"
    return True, "совпадает с оптимальным решением"


def stream_completion(client, messages, step_label=None, on_step=None):
    payload = {
        "model": MODEL,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
        "thinking": {"type": "disabled"},
    }

    if on_step and step_label:
        on_step(step_label)

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        stream=True,
        extra_body={"thinking": {"type": "disabled"}},
    )

    text, finish_reason = stream_chunks(
        response,
        lambda placeholder, current: placeholder.markdown(current),
        "День 3 думает...",
    )

    return text, payload, finish_reason


def _error_result(api_calls, start, error):
    return {
        "text": None,
        "payloads": [],
        "api_calls": api_calls,
        "time": time.perf_counter() - start,
        "error": str(error),
        "finish_reason": None,
    }


def run_direct(client, task, on_step=None):
    start = time.perf_counter()
    try:
        messages = [{"role": "user", "content": task}]
        text, payload, finish_reason = stream_completion(
            client, messages, "Прямой ответ", on_step
        )
        return {
            "text": text,
            "payloads": [payload],
            "api_calls": 1,
            "time": time.perf_counter() - start,
            "error": None,
            "finish_reason": finish_reason,
        }
    except Exception as error:
        return _error_result(1, start, error)


def run_step_by_step(client, task, on_step=None):
    start = time.perf_counter()
    try:
        messages = [
            {"role": "system", "content": STEP_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        text, payload, finish_reason = stream_completion(
            client, messages, "Пошаговое решение", on_step
        )
        return {
            "text": text,
            "payloads": [payload],
            "api_calls": 1,
            "time": time.perf_counter() - start,
            "error": None,
            "finish_reason": finish_reason,
        }
    except Exception as error:
        return _error_result(1, start, error)


def run_self_prompt(client, task, on_step=None):
    start = time.perf_counter()
    try:
        prompt_messages = [
            {"role": "system", "content": SELF_PROMPT_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        generated_prompt, prompt_payload, _ = stream_completion(
            client, prompt_messages, "Самостоятельный промпт (создание промпта)", on_step
        )

        solve_messages = [
            {"role": "system", "content": generated_prompt},
            {"role": "user", "content": task},
        ]
        text, payload, finish_reason = stream_completion(
            client, solve_messages, "Самостоятельный промпт (решение задачи)", on_step
        )

        return {
            "text": text,
            "payloads": [prompt_payload, payload],
            "api_calls": 2,
            "time": time.perf_counter() - start,
            "error": None,
            "finish_reason": finish_reason,
            "generated_prompt": generated_prompt,
        }
    except Exception as error:
        return _error_result(2, start, error)


def run_expert_panel(client, task, on_step=None):
    start = time.perf_counter()
    try:
        messages = [
            {"role": "system", "content": EXPERT_PANEL_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        text, payload, finish_reason = stream_completion(
            client, messages, "Группа экспертов", on_step
        )
        return {
            "text": text,
            "payloads": [payload],
            "api_calls": 1,
            "time": time.perf_counter() - start,
            "error": None,
            "finish_reason": finish_reason,
        }
    except Exception as error:
        return _error_result(1, start, error)


def _render_method_tab(result):
    if result is None:
        st.info("Нет данных — запустите сравнение.")
        return

    if result.get("error"):
        st.error(f"Ошибка запроса: {result['error']}")
    else:
        text = result.get("text") or ""
        parsed = parse_final_answer(text)
        is_valid, reason = validate_answer(text)

        if parsed:
            ops, points = parsed
            st.markdown(f"**Финальный ответ:** `{', '.join(sorted(ops))} = {points}`")
        else:
            st.markdown("**Финальный ответ:** не найден")

        if is_valid:
            st.success("✅ Ответ прошёл детерминированную проверку")
        else:
            st.warning(f"❌ Ответ не прошёл проверку: {reason}")

        if result.get("finish_reason") == "length":
            st.warning("Ответ остановлен из-за ограничения длины")

        st.markdown("---")
        st.markdown("**Ответ модели:**")
        st.markdown(text)

    if result.get("generated_prompt"):
        with st.expander("▶ Показать сгенерированный промпт"):
            st.markdown(result["generated_prompt"])

    payloads = result.get("payloads") or []
    if len(payloads) == 2:
        expander_labels = ["▶ Запрос на создание промпта", "▶ Запрос на решение задачи"]
    else:
        expander_labels = ["▶ Показать отправленный запрос к API"]

    for label, payload in zip(expander_labels, payloads):
        with st.expander(label):
            st.code(json.dumps(payload, ensure_ascii=False, indent=2), language="json")


def _render_comparison_table(results):
    optimum_ops, optimum_value = calculate_optimum()

    rows = []
    for label, result in results.items():
        if result is None:
            continue
        if result.get("error"):
            final_str = "ошибка"
            correct = "—"
        else:
            parsed = parse_final_answer(result.get("text") or "")
            if parsed:
                ops, points = parsed
                final_str = f"{', '.join(sorted(ops))} = {points}"
            else:
                final_str = "нет FINAL"
            correct = "✅" if validate_answer(result.get("text") or "")[0] else "❌"

        rows.append(
            {
                "Метод": label,
                "Финальный ответ": final_str,
                "Правильно": correct,
                "Время": f"{result['time']:.2f} с",
                "API-вызовов": result["api_calls"],
            }
        )

    st.subheader("Сравнение способов")
    st.table(rows)
    st.caption(f"Оптимальное решение: {', '.join(sorted(optimum_ops))} = {optimum_value}")


def _render_summary(results):
    optimum_ops, optimum_value = calculate_optimum()
    optimum_str = f"{', '.join(sorted(optimum_ops))} = {optimum_value}"

    parsed_answers = {}
    correct_methods = []
    fastest = None
    fastest_time = None

    for label, result in results.items():
        if result is None:
            continue
        if result.get("error"):
            parsed_answers[label] = "ошибка"
            continue

        parsed = parse_final_answer(result.get("text") or "")
        if parsed:
            ops, points = parsed
            parsed_answers[label] = f"{', '.join(sorted(ops))} = {points}"
        else:
            parsed_answers[label] = "нет FINAL"

        if validate_answer(result.get("text") or "")[0]:
            correct_methods.append(label)

        if fastest_time is None or result["time"] < fastest_time:
            fastest_time = result["time"]
            fastest = label

    unique_answers = set(parsed_answers.values())
    lines = []
    if len(unique_answers) > 1:
        details = "; ".join(f"{label}: {answer}" for label, answer in parsed_answers.items())
        lines.append(f"- Ответы различались: {details}.")
    elif unique_answers:
        lines.append(f"- Все способы дали одинаковый ответ: {next(iter(unique_answers))}.")

    if correct_methods:
        lines.append(f"- Правильный результат ({optimum_str}) дали: {', '.join(correct_methods)}.")
    else:
        lines.append("- Ни один способ не дал правильный результат.")

    if fastest:
        lines.append(f"- Самым быстрым оказался способ «{fastest}» ({fastest_time:.2f} с).")

    lines.append(
        "- Один эксперимент не доказывает универсального превосходства какого-либо "
        "способа: результат зависит от задачи и случайности модели."
    )

    st.markdown("### Выводы")
    st.markdown("\n".join(lines))


def render():
    st.session_state.setdefault("day3_direct", None)
    st.session_state.setdefault("day3_step", None)
    st.session_state.setdefault("day3_prompt", None)
    st.session_state.setdefault("day3_expert", None)

    st.subheader("Космическая задача")
    st.markdown("Выберите оптимальный набор операций лунной станции")

    with st.expander("Показать полное условие задачи"):
        st.markdown(SPACE_TASK)

    if st.button("Запустить сравнение", type="primary"):
        client = get_client()
        if client is None:
            st.error("Добавьте DEEPSEEK_API_KEY в файл .env")
        else:
            total_calls = 5
            counter = [0]
            progress_bar = st.progress(0.0)
            status_text = st.empty()

            def on_step(label):
                counter[0] += 1
                status_text.markdown(f"Запрос {counter[0]} из {total_calls}: {label}")
                progress_bar.progress(counter[0] / total_calls)

            st.session_state["day3_direct"] = run_direct(client, SPACE_TASK, on_step)
            st.session_state["day3_step"] = run_step_by_step(client, SPACE_TASK, on_step)
            st.session_state["day3_prompt"] = run_self_prompt(client, SPACE_TASK, on_step)
            st.session_state["day3_expert"] = run_expert_panel(client, SPACE_TASK, on_step)

            progress_bar.progress(1.0)
            status_text.empty()

    results = {
        "Прямой ответ": st.session_state["day3_direct"],
        "Пошаговое решение": st.session_state["day3_step"],
        "Самостоятельный промпт": st.session_state["day3_prompt"],
        "Группа экспертов": st.session_state["day3_expert"],
    }

    tabs = st.tabs(list(results.keys()))
    for tab, result in zip(tabs, results.values()):
        with tab:
            _render_method_tab(result)

    if not all(result is None for result in results.values()):
        _render_comparison_table(results)
        _render_summary(results)
