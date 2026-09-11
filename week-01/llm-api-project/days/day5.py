import json
import os
import re
import time
from datetime import datetime, timezone

import streamlit as st
from openai import OpenAI

from common import TEMPERATURE, get_client

DAY5_MAX_TOKENS = 512

DAY5_DEFAULT_PROMPT = (
    "Реши арифметическое выражение по обычным правилам приоритета операций\n"
    "(скобки, затем умножение/деление слева направо, затем сложение/вычитание):\n"
    "\n"
    "(48 + 12) / 6 * 3 - 9 + 2\n"
    "\n"
    "Последняя непустая строка ответа должна строго соответствовать формату:\n"
    "\n"
    "ANSWER: <число>"
)

ANSWER_EXPECTED = 23

ANSWER_PATTERN = re.compile(r"^answer\s*:\s*(-?\d+(?:\.\d+)?)$", re.IGNORECASE)

CACHE_MISS_ASSUMPTION = (
    "весь input по тарифу cache-miss (консервативная оценка: разбивка cache hit/miss "
    "от провайдера не получена)"
)
CACHE_SPLIT_ASSUMPTION = "учтена разбивка cache hit/miss из usage"

# Официальные тарифы DeepSeek, USD за 1M токенов
# (источник: https://api-docs.deepseek.com/quick_start/pricing).
FLASH_COST = {
    "input_cache_miss": {"off_peak": 0.22, "peak": 0.44},
    "input_cache_hit": {"off_peak": 0.007, "peak": 0.014},
    "output": {"off_peak": 0.66, "peak": 1.32},
}
PRO_COST = {
    "input_cache_miss": {"off_peak": 0.66, "peak": 1.32},
    "input_cache_hit": {"off_peak": 0.022, "peak": 0.044},
    "output": {"off_peak": 1.98, "peak": 3.96},
}

# Справочные факты конкретного локального запуска (не живые метрики).
LOCAL_RESOURCE_INFO = (
    "RTX 5060 Ti 16 GB; ~14 253 MiB из 16 311 MiB VRAM; прогретая генерация "
    "~21 токен/с; загрузка модели ~5–6 с (первый запрос может включать загрузку)."
)
LOCAL_SERVER_HINT = (
    "Запустите D:\\AI\\Scripts\\START_QWEN_27B.bat двойным щелчком и подождите "
    "~5–6 с, пока модель загрузится."
)

DAY5_MODELS = [
    {
        "key": "qwen_local",
        "label": "Qwen3.8 27B IQ4_XS (локальная)",
        "kind": "local",
        "model": "qwen3.8-27b-iq4xs",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key_env": "LOCAL_LLM_API_KEY",
        "thinking_disabled": False,
        "stream_options": None,
        "cost": None,
        "official_name": None,
        "resource_info": LOCAL_RESOURCE_INFO,
        "local_server_hint": LOCAL_SERVER_HINT,
    },
    {
        "key": "deepseek_flash",
        "label": "DeepSeek V4.1 Flash (средняя)",
        "kind": "deepseek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "thinking_disabled": True,
        "stream_options": {"include_usage": True},
        "cost": FLASH_COST,
        "official_name": "DeepSeek-V4.1-Flash",
        "resource_info": None,
        "local_server_hint": None,
    },
    {
        "key": "deepseek_pro",
        "label": "DeepSeek V4 Pro (сильная)",
        "kind": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "thinking_disabled": True,
        "stream_options": {"include_usage": True},
        "cost": PRO_COST,
        "official_name": "DeepSeek-V4-Pro-0813",
        "resource_info": None,
        "local_server_hint": None,
    },
]


def is_peak_time(dt=None):
    """Peak-часы DeepSeek: [01:00,04:00) и [06:00,10:00) UTC, только пн–пт."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.weekday() >= 5:  # суббота/воскресенье
        return False
    return (1 <= dt.hour < 4) or (6 <= dt.hour < 10)


def _to_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_usage(usage_obj):
    """Достать поля usage из объекта openai или dict. None, если ничего нет."""
    if usage_obj is None:
        return None

    def field(name):
        if isinstance(usage_obj, dict):
            return usage_obj.get(name)
        return getattr(usage_obj, name, None)

    prompt = _to_int(field("prompt_tokens"))
    completion = _to_int(field("completion_tokens"))
    total = _to_int(field("total_tokens"))
    hit = _to_int(field("prompt_cache_hit_tokens"))
    miss = _to_int(field("prompt_cache_miss_tokens"))

    details = field("prompt_tokens_details")
    cached = None
    if details is not None:
        if isinstance(details, dict):
            cached = _to_int(details.get("cached_tokens"))
        else:
            cached = _to_int(getattr(details, "cached_tokens", None))

    if hit is None and cached is not None:
        hit = cached
        if miss is None and prompt is not None:
            miss = prompt - cached

    result = {}
    if prompt is not None:
        result["prompt_tokens"] = prompt
    if completion is not None:
        result["completion_tokens"] = completion
    if total is not None:
        result["total_tokens"] = total
    if hit is not None:
        result["prompt_cache_hit_tokens"] = hit
    if miss is not None:
        result["prompt_cache_miss_tokens"] = miss

    return result or None


def parse_timings(timings_obj):
    """Нормализовать timings llama.cpp (dict или объект) в dict. None, если пусто."""
    if timings_obj is None:
        return None

    def field(name):
        if isinstance(timings_obj, dict):
            return timings_obj.get(name)
        return getattr(timings_obj, name, None)

    prompt = _to_int(field("prompt_n"))
    completion = _to_int(field("predicted_n"))
    predicted_per_second = _to_float(field("predicted_per_second"))
    prompt_per_second = _to_float(field("prompt_per_second"))

    result = {}
    if prompt is not None:
        result["prompt_tokens"] = prompt
    if completion is not None:
        result["completion_tokens"] = completion
    if prompt is not None and completion is not None:
        result["total_tokens"] = prompt + completion
    if predicted_per_second is not None:
        result["predicted_per_second"] = predicted_per_second
    if prompt_per_second is not None:
        result["prompt_per_second"] = prompt_per_second

    return result or None


def calculate_cost(usage_map, cost_map, is_peak):
    """Стоимость одного запроса. None, если нет usage или тарифа."""
    if not usage_map or not cost_map:
        return None

    window = "peak" if is_peak else "off_peak"
    prompt = usage_map.get("prompt_tokens") or 0
    completion = usage_map.get("completion_tokens") or 0

    hit = usage_map.get("prompt_cache_hit_tokens")
    miss = usage_map.get("prompt_cache_miss_tokens")

    miss_rate = cost_map["input_cache_miss"][window]
    hit_rate = cost_map["input_cache_hit"][window]
    output_rate = cost_map["output"][window]

    if hit is not None and miss is not None:
        input_usd = (miss * miss_rate + hit * hit_rate) / 1e6
        assumption = CACHE_SPLIT_ASSUMPTION
    else:
        input_usd = prompt * miss_rate / 1e6
        assumption = CACHE_MISS_ASSUMPTION

    output_usd = completion * output_rate / 1e6

    return {
        "input_usd": input_usd,
        "output_usd": output_usd,
        "total_usd": input_usd + output_usd,
        "assumption": assumption,
    }


def compute_tokens_per_second(usage_map, ttft, elapsed):
    if not usage_map or elapsed is None:
        return None
    completion = usage_map.get("completion_tokens")
    if completion is None:
        return None
    if ttft is None:
        gen_time = elapsed
    else:
        gen_time = elapsed - ttft
    if gen_time <= 0:
        return None
    return completion / gen_time


def parse_answer(text):
    """Вернуть число из последней непустой строки формата 'ANSWER: <число>'."""
    if not text:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    match = ANSWER_PATTERN.match(lines[-1].strip())
    if not match:
        return None
    return float(match.group(1))


def validate_answer(text):
    parsed = parse_answer(text)
    if parsed is None:
        return False, "нет строки ANSWER в корректном формате"
    if abs(parsed - ANSWER_EXPECTED) > 1e-6:
        return False, "число не совпадает с верным ответом (ожидалось 23)"
    return True, "ответ верный"


def get_day5_client(spec):
    if spec["kind"] == "deepseek":
        return get_client()
    api_key = os.getenv(spec["api_key_env"])
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=spec["base_url"])


def build_payload(spec, messages):
    """Реальные kwargs запроса (без ключа API)."""
    payload = {
        "model": spec["model"],
        "messages": messages,
        "max_tokens": DAY5_MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": True,
    }
    if spec["kind"] == "deepseek":
        payload["extra_body"] = {"thinking": {"type": "disabled"}}
        payload["stream_options"] = spec["stream_options"]
    return payload


def stream_day5_response(response, placeholder):
    """Свой цикл стрима: ttft, finish_reason, usage и timings из последнего chunk."""
    collected = []
    finish_reason = None
    usage = None
    timings = None
    ttft = None
    start = time.perf_counter()
    for chunk in response:
        if chunk.choices:
            choice = chunk.choices[0]
            if choice.delta and choice.delta.content:
                if ttft is None:
                    ttft = time.perf_counter() - start
                collected.append(choice.delta.content)
                placeholder.markdown("".join(collected))
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        chunk_timings = getattr(chunk, "timings", None)
        if chunk_timings is not None:
            timings = chunk_timings
    return "".join(collected), ttft, finish_reason, usage, timings


def _empty_result(spec, is_peak):
    return {
        "key": spec["key"],
        "label": spec["label"],
        "kind": spec["kind"],
        "model": spec["model"],
        "official_name": spec.get("official_name"),
        "text": None,
        "payload": None,
        "error": None,
        "ttft": None,
        "elapsed": None,
        "finish_reason": None,
        "usage": None,
        "timings": None,
        "is_peak": is_peak,
        "cost": None,
        "tokens_per_sec": None,
        "prompt_per_second": None,
        "checks": None,
        "resource_info": spec.get("resource_info"),
        "local_server_hint": spec.get("local_server_hint"),
    }


def _format_error(spec, error):
    message = str(error)
    if spec["kind"] == "local":
        message += (
            "\n\nЛокальная модель не отвечает. Запустите сервер двойным щелчком по "
            "D:\\AI\\Scripts\\START_QWEN_27B.bat и подождите ~5–6 с, пока модель "
            "загрузится, затем нажмите кнопку ещё раз."
        )
    return message


def run_day5_single(spec, messages, is_peak):
    result = _empty_result(spec, is_peak)

    client = get_day5_client(spec)
    if client is None:
        result["error"] = f"Не задан {spec['api_key_env']} в файле .env"
        return result

    placeholder = st.empty()
    start = time.perf_counter()
    try:
        payload = build_payload(spec, messages)
        response = client.chat.completions.create(**payload)
        text, ttft, finish_reason, usage_obj, timings_obj = stream_day5_response(
            response, placeholder
        )
        result["text"] = text
        result["payload"] = payload
        result["ttft"] = ttft
        result["elapsed"] = time.perf_counter() - start
        result["finish_reason"] = finish_reason
        usage_map = parse_usage(usage_obj)
        timings_map = parse_timings(timings_obj)
        result["usage"] = usage_map
        result["timings"] = timings_map
        result["cost"] = calculate_cost(usage_map, spec.get("cost"), is_peak)
        if spec["kind"] == "local":
            if not usage_map and timings_map:
                result["usage"] = {
                    key: timings_map[key]
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if key in timings_map
                } or None
            predicted = timings_map.get("predicted_per_second") if timings_map else None
            result["prompt_per_second"] = (
                timings_map.get("prompt_per_second") if timings_map else None
            )
            if predicted is not None:
                result["tokens_per_sec"] = predicted
            else:
                result["tokens_per_sec"] = compute_tokens_per_second(
                    usage_map, ttft, result["elapsed"]
                )
        else:
            result["tokens_per_sec"] = compute_tokens_per_second(
                usage_map, ttft, result["elapsed"]
            )
        if text is not None:
            ok, reason = validate_answer(text)
            result["checks"] = {"ok": ok, "reason": reason}
    except Exception as error:
        result["elapsed"] = time.perf_counter() - start
        result["error"] = _format_error(spec, error)
    finally:
        placeholder.empty()

    return result


def run_day5(prompt, status_text, progress_bar):
    messages = [{"role": "user", "content": prompt}]
    is_peak = is_peak_time()  # определяется один раз на прогон
    results = []
    total = len(DAY5_MODELS)
    for index, spec in enumerate(DAY5_MODELS, start=1):
        status_text.markdown(f"Запрос {index} из {total}: {spec['label']}")
        results.append(run_day5_single(spec, messages, is_peak))
        progress_bar.progress(index / total)
    return results


def _fmt_num(value, digits=2):
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def render_day5_results(results):
    columns = st.columns(len(results))
    for column, result in zip(columns, results):
        with column:
            st.markdown(f"**{result['label']}**")
            if result.get("error"):
                st.error(result["error"])
                continue

            checks = result.get("checks")
            if checks and checks["ok"]:
                st.success("✅ Ответ верный")
            elif checks:
                st.warning(f"❌ {checks['reason']}")

            st.markdown("---")
            st.markdown("**Ответ модели:**")
            st.markdown(result["text"] or "—")
            st.markdown("---")

            usage = result.get("usage") or {}
            st.markdown(f"TTFT: {_fmt_num(result.get('ttft'))} с")
            st.markdown(f"Время: {_fmt_num(result.get('elapsed'))} с")
            st.markdown(
                f"Токены: вх {usage.get('prompt_tokens', '—')} / "
                f"вых {usage.get('completion_tokens', '—')} / "
                f"всего {usage.get('total_tokens', '—')}"
            )
            st.markdown(f"Скорость: {_fmt_num(result.get('tokens_per_sec'))} ток/с")
            if result.get("prompt_per_second") is not None:
                st.markdown(
                    f"Скорость обработки prompt: "
                    f"{_fmt_num(result.get('prompt_per_second'))} ток/с"
                )
            st.markdown(f"finish_reason: `{result.get('finish_reason') or '—'}`")

            cost = result.get("cost")
            if cost:
                st.markdown(
                    f"Стоимость: вход ${cost['input_usd']:.6f} + "
                    f"выход ${cost['output_usd']:.6f} = ${cost['total_usd']:.6f}"
                )
                window = "peak" if result.get("is_peak") else "off-peak"
                st.caption(f"Окно тарифа: {window}. {cost['assumption']}")
            elif result["kind"] == "local":
                st.markdown("Стоимость: $0 (локальная модель, без тарифа API)")

            if result.get("resource_info"):
                st.markdown("**Ресурсы (справка):**")
                st.caption(result["resource_info"])

            with st.expander("▶ Реальный payload запроса"):
                payload = result.get("payload") or {}
                st.code(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    language="json",
                )

    st.markdown("### Сравнение моделей")
    rows = []
    for result in results:
        usage = result.get("usage") or {}
        checks = result.get("checks")
        if result.get("error"):
            verdict = "ошибка"
        elif checks:
            verdict = "✅" if checks["ok"] else "❌"
        else:
            verdict = "—"

        cost = result.get("cost")
        if cost:
            cost_str = f"${cost['total_usd']:.6f}"
        elif result["kind"] == "local":
            cost_str = "$0"
        else:
            cost_str = "—"

        rows.append(
            {
                "Модель": result["label"],
                "Вердикт": verdict,
                "TTFT, с": _fmt_num(result.get("ttft")),
                "Время, с": _fmt_num(result.get("elapsed")),
                "Токены вх": usage.get("prompt_tokens", "—"),
                "Токены вых": usage.get("completion_tokens", "—"),
                "Токены всего": usage.get("total_tokens", "—"),
                "Скорость, ток/с": _fmt_num(result.get("tokens_per_sec")),
                "finish_reason": result.get("finish_reason") or "—",
                "Стоимость, $": cost_str,
            }
        )
    st.table(rows)

    st.markdown("### Результат конкретного запуска (справка)")
    st.caption(
        "Справочные значения для локальной модели — результат конкретного запуска, "
        "не живые метрики."
    )
    st.markdown(LOCAL_RESOURCE_INFO)

    st.markdown("### Вывод")
    correct = [r["label"] for r in results if r.get("checks") and r["checks"]["ok"]]
    failed = [r["label"] for r in results if r.get("error")]
    lines = []
    if correct:
        lines.append(f"- Правильный ответ ({ANSWER_EXPECTED}) дали: {', '.join(correct)}.")
    else:
        lines.append("- Ни одна модель не дала правильный ответ.")
    if failed:
        lines.append(f"- С ошибкой завершились: {', '.join(failed)}.")
    lines.append(
        "- Один запуск не доказывает универсального превосходства какой-либо модели: "
        "скорость, стоимость и точность зависят от задачи и загрузки сервиса."
    )
    st.markdown("\n".join(lines))


def render():
    st.session_state.setdefault("day5_results", None)
    st.session_state.setdefault("day5_run_prompt", None)

    st.subheader("Версии моделей")
    st.markdown(
        "Один и тот же запрос отправляется последовательно трём моделям — локальной "
        "Qwen3.8 и двум моделям DeepSeek, — чтобы сравнить скорость (TTFT и токены/с), "
        "стоимость и точность ответа. Параметры запроса одинаковы для всех моделей."
    )
    st.caption(
        "Peak-часы DeepSeek: [01:00,04:00) и [06:00,10:00) UTC, только пн–пт. "
        "Окно определяется один раз на момент запуска."
    )

    prompt = st.text_area(
        "Запрос (можно редактировать)",
        value=DAY5_DEFAULT_PROMPT,
        height=220,
    )

    if st.button("Сравнить модели", type="primary"):
        if not prompt.strip():
            st.warning("Сначала введите запрос.")
        else:
            missing = []
            for spec in DAY5_MODELS:
                env_name = spec["api_key_env"]
                if env_name not in missing and get_day5_client(spec) is None:
                    missing.append(env_name)

            if missing:
                for env_name in missing:
                    st.error(f"Добавьте {env_name} в файл .env")
            else:
                progress_bar = st.progress(0.0)
                status_text = st.empty()
                st.session_state["day5_results"] = run_day5(
                    prompt, status_text, progress_bar
                )
                st.session_state["day5_run_prompt"] = prompt
                status_text.empty()

    results = st.session_state["day5_results"]
    if results:
        if st.session_state["day5_run_prompt"] != prompt:
            st.caption(
                "Поле запроса изменено после последнего запуска — для нового текста "
                "нажмите «Сравнить модели» ещё раз."
            )
        render_day5_results(results)
