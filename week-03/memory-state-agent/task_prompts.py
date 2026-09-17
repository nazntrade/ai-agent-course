"""Stage prompt contracts: system prompts, budgets, builders and parsers.

The module owns the provider contract of the three task stages (planning,
execution, validation): the Russian service prompts and synthetic action
messages of the context packet, the token budgets of the calls, and the strict
parsers of the model's JSON replies. It is pure stdlib and performs no I/O.

``format_specification_markdown`` and ``format_final_result_markdown`` are
re-exported from :mod:`tasks`, which owns the artifact text of the domain.
"""

from __future__ import annotations

import json

from tasks import (  # noqa: F401  (re-exported: the artifact text lives in the domain)
    format_final_result_markdown,
    format_specification_markdown,
    normalize_verdict,
)

# Reasoning models spend part of the visible budget on hidden reasoning, so the
# base budget covers the common case and the retry budget is used once when the
# model truncates (FR-26).
TASK_PLAN_MAX_TOKENS = 2400
TASK_PLAN_RETRY_MAX_TOKENS = 4800
TASK_STEP_MAX_TOKENS = 2000
TASK_STEP_RETRY_MAX_TOKENS = 4000
TASK_VALIDATION_MAX_TOKENS = 1600
TASK_VALIDATION_RETRY_MAX_TOKENS = 3200

TASK_PLAN_TEMPERATURE = 0.2
TASK_STEP_TEMPERATURE = 0.2
TASK_VALIDATION_TEMPERATURE = 0.0

TRUNCATED_FINISH_REASONS = ("length", "max_tokens")

TASK_PLANNING_SYSTEM_PROMPT = (
    "Ты планируешь выполнение задачи. Верни СТРОГО один JSON-объект и ничего "
    "больше, без пояснений и без текста вокруг. Формат:\n"
    "{\"summary\": \"краткое описание плана\", "
    "\"acceptance_criteria\": [\"непустой критерий приёмки\", ...], "
    "\"steps\": [{\"index\": 1, \"title\": \"краткое название шага\", "
    "\"description\": \"что именно нужно сделать\"}, ...]}\n"
    "Правила: шагов не меньше одного; index идут подряд с 1 без пропусков; "
    "title и description каждого шага непустые; критериев приёмки не меньше "
    "одного; шаги проверяемы и не дублируют друг друга; не выдумывай данные, "
    "которых нет в цели и брифе."
)

TASK_EXECUTION_SYSTEM_PROMPT = (
    "Ты выполняешь один шаг плана и возвращаешь его результат. Отвечай по "
    "существу шага, без вступлений и извинений. Если в шаге перечислены "
    "замечания предыдущей проверки, устрани именно их. Не описывай другие шаги "
    "плана и не меняй сам план. Результат — это готовый текст шага, а не отчёт "
    "о процессе."
)

TASK_VALIDATION_SYSTEM_PROMPT = (
    "Ты проверяешь результат выполнения задачи по критериям приёмки. Верни "
    "СТРОГО один JSON-объект и ничего больше. Формат:\n"
    "{\"passed\": true|false, \"defects\": [{\"step_index\": 1, "
    "\"description\": \"что именно не выполнено\"}], \"notes\": \"краткие "
    "пояснения\"}\n"
    "Правила: passed=true только если все критерии выполнены, и тогда defects "
    "пуст; при passed=false перечисли defects, каждый с номером шага из плана "
    "и конкретным описанием; не выдумывай дефекты, которых нет в результатах."
)

_SYNTHETIC_ACTION_PREFIX = "Действие: "


def is_truncated(finish_reason) -> bool:
    """Return ``True`` when the provider stopped because of the output limit."""
    return finish_reason in TRUNCATED_FINISH_REASONS


def strip_code_fences(text) -> str:
    """Return the text without a single surrounding Markdown code fence."""
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _step_view(step) -> dict:
    """Normalize a plan step mapping or a ``StepProgress`` into a view dict."""
    if isinstance(step, dict):
        index = step.get("index")
        title = step.get("title")
        description = step.get("description")
    else:
        index = getattr(step, "step_index", None)
        title = getattr(step, "title", None)
        description = getattr(step, "description", None)
    return {
        "index": index,
        "title": str(title or "").strip(),
        "description": str(description or "").strip(),
    }


def build_plan_messages(goal, task_brief=None) -> list:
    """Build the planning call: planning instruction plus the synthetic action."""
    lines = [_SYNTHETIC_ACTION_PREFIX + "run_planning", "", "Цель задачи:", str(goal or "").strip()]
    brief = str(task_brief or "").strip()
    if brief:
        lines.extend(["", "Бриф задачи:", brief])
    lines.extend(["", "Составь план выполнения этой задачи."])
    return [
        {"role": "system", "content": TASK_PLANNING_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def build_step_messages(step, defects=None) -> list:
    """Build the execution call for one step, including its rework defects."""
    view = _step_view(step)
    lines = [_SYNTHETIC_ACTION_PREFIX + "run_step", ""]
    lines.append(f"Шаг {view['index']}: {view['title']}")
    if view["description"]:
        lines.extend(["", view["description"]])
    defect_lines = [str(defect).strip() for defect in (defects or []) if str(defect).strip()]
    if defect_lines:
        lines.extend(["", "Устрани замечания предыдущей проверки:"])
        lines.extend(f"- {defect}" for defect in defect_lines)
    return [
        {"role": "system", "content": TASK_EXECUTION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def build_validation_messages(
    goal, acceptance_criteria=None, steps=None, executions=None
) -> list:
    """Build the validation call: the criteria plus the strict-JSON instruction.

    The goal, the plan steps and the step results are already carried by the
    packet's artifact blocks (``task_snapshot``, ``plan``, ``execution_results``),
    so inlining them here duplicated most of the prompt. ``goal``, ``steps`` and
    ``executions`` stay in the signature because ``task_context`` passes them
    positionally; only the acceptance criteria are rendered, and the system
    prompt owns the strict JSON format the verdict must follow.
    """
    lines = [
        _SYNTHETIC_ACTION_PREFIX + "run_validation",
        "",
        "Проверь результат выполнения задачи по критериям приёмки и верни СТРОГО "
        "один JSON-вердикт в формате системного сообщения, без пояснений и без "
        "текста вокруг.",
    ]
    criteria = [
        str(criterion).strip()
        for criterion in (acceptance_criteria or [])
        if str(criterion).strip()
    ]
    if criteria:
        lines.extend(["", "Критерии приёмки:"])
        lines.extend(f"- {criterion}" for criterion in criteria)
    return [
        {"role": "system", "content": TASK_VALIDATION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def parse_plan_response(text) -> dict:
    """Parse and strictly validate the planning reply.

    Rejects empty, fenced-invalid, truncated and structurally wrong JSON with a
    ``ValueError`` the caller reports as ``API_ERROR(invalid_response)``.
    """
    stripped = strip_code_fences(text)
    if not stripped:
        raise ValueError("Plan response is empty")
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid plan JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Plan response must be a JSON object")

    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Plan must contain at least one step")
    normalized_steps = []
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"Plan step #{position} must be an object")
        index = step.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != position:
            raise ValueError(
                "Plan step indexes must be continuous starting at 1; "
                f"expected {position}"
            )
        title = str(step.get("title") or "").strip()
        if not title:
            raise ValueError(f"Plan step {index} has an empty title")
        description = step.get("description")
        normalized_steps.append(
            {
                "index": index,
                "title": title,
                "description": description if isinstance(description, str) else str(description or ""),
            }
        )

    criteria = payload.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("Plan must contain at least one acceptance criterion")
    cleaned_criteria = []
    for criterion in criteria:
        if not isinstance(criterion, str) or not criterion.strip():
            raise ValueError("Every acceptance criterion must be a non-empty string")
        cleaned_criteria.append(criterion.strip())

    summary = payload.get("summary")
    return {
        "summary": summary if isinstance(summary, str) else str(summary or ""),
        "acceptance_criteria": cleaned_criteria,
        "steps": normalized_steps,
    }


def parse_validation_response(text, steps_count=None) -> dict:
    """Parse and strictly validate the validation reply.

    ``steps_count`` bounds the defect indexes to the plan. Invalid verdicts
    raise ``ValueError`` so the caller can retry once and then record
    ``API_ERROR(invalid_response)``.
    """
    stripped = strip_code_fences(text)
    if not stripped:
        raise ValueError("Validation response is empty")
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid validation JSON: {exc}") from exc
    return normalize_verdict(payload, steps_count=steps_count)
