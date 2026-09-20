"""Canonical Day 15 demonstration scenario.

The module describes the demo task and, above all, the boundary between the
external transitions around the execution stage and the execution plan itself:

* ``DEMO_EXTERNAL_TRANSITIONS`` lists the workflow transitions that happen
  outside the plan: ``create_task``, ``run_planning`` and ``accept_plan`` before
  execution, then ``finish_execution`` and ``run_validation`` after it;
* ``DEMO_EXECUTION_PLAN`` is a strictly valid plan whose steps are only
  execution-compatible actions and checks of the demo task. No step describes a
  workflow transition (planning, plan review/accept, execution start/finish,
  validation), because those transitions are not plan steps.

``workflow_step_titles`` is a thin, deterministic view over the runtime guard
:func:`task_prompts.plan_step_violations`: it returns the titles of the steps
that cannot run in the ``execution`` stage and an empty tuple for a clean plan.
The ``parse_plan_response`` parser applies the same guard while planning, so a
live, execution-incompatible plan is rejected before ``PLAN_CREATED`` and
retried once with corrective feedback; this module only supplies the canonical
fixtures and their test-time view.

The module is pure stdlib and I/O-free; it imports neither Streamlit, nor SQL,
nor the provider client.
"""

from __future__ import annotations

from dataclasses import dataclass

from task_prompts import (
    FUTURE_STAGE_DONE_PHRASES,
    FUTURE_STAGE_ID_MARKERS,
    FUTURE_STAGE_STAGE_WORDS,
    FUTURE_STAGE_STATUS_PHRASES,
    FUTURE_STAGE_STATUS_TOKENS,
    FUTURE_STAGE_STEMS,
    NONEXISTENT_TRANSITION_FRAMES,
    NONEXISTENT_TRANSITION_PLANNING_WORDS,
    NONEXISTENT_TRANSITION_STEMS,
    PLAN_STEP_HARD_MARKERS,
    PLAN_STEP_MISSING_ENTITY_MARKERS,
    PLAN_STEP_RETRO_MARKERS,
    PRIOR_STAGE_ENTITY_TOKENS,
    PRIOR_STAGE_PHRASES,
    plan_step_violations,
)
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_FINISH_EXECUTION,
    ACTION_RUN_PLANNING,
    ACTION_RUN_VALIDATION,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
)

DEMO_TASK_TITLE = "Проверить блокировку недопустимого перехода"
DEMO_TASK_GOAL = (
    "Убедиться, что действие вне текущего этапа отклоняется, не вызывает "
    "провайдера и не меняет состояние задачи."
)
DEMO_TASK_BRIEF = (
    "Демонстрационная задача: зафиксировать отказ недопустимого действия, "
    "убедиться в наличии аудита и в неизменности состояния и версии задачи."
)

# ``create_task`` is an orchestration action, not a domain action, so it is not
# imported from the FSM module.
ACTION_CREATE_TASK = "create_task"

PHASE_BEFORE_EXECUTION = "before_execution"
PHASE_AFTER_EXECUTION = "after_execution"


@dataclass(frozen=True)
class ExternalTransition:
    """One workflow transition that happens outside the execution plan.

    ``phase`` is ``before_execution`` for the transitions that must complete
    before the first ``run_step`` and ``after_execution`` for the transitions
    that follow the last completed step.
    """

    action: str
    stage: str
    phase: str
    label: str


DEMO_EXTERNAL_TRANSITIONS = (
    ExternalTransition(
        ACTION_CREATE_TASK,
        STAGE_PLANNING,
        PHASE_BEFORE_EXECUTION,
        "Create task",
    ),
    ExternalTransition(
        ACTION_RUN_PLANNING,
        STAGE_PLANNING,
        PHASE_BEFORE_EXECUTION,
        "Run planning",
    ),
    ExternalTransition(
        ACTION_ACCEPT_PLAN,
        STAGE_PLANNING,
        PHASE_BEFORE_EXECUTION,
        "Accept plan",
    ),
    ExternalTransition(
        ACTION_FINISH_EXECUTION,
        STAGE_EXECUTION,
        PHASE_AFTER_EXECUTION,
        "Finish execution",
    ),
    ExternalTransition(
        ACTION_RUN_VALIDATION,
        STAGE_VALIDATION,
        PHASE_AFTER_EXECUTION,
        "Run validation",
    ),
)


def external_transition_actions(phase: str | None = None) -> tuple:
    """Return the ordered action names of the demo transitions.

    With ``phase`` set to ``before_execution`` or ``after_execution`` only that
    group is returned; without it the full ordered tuple is returned.
    """
    return tuple(
        transition.action
        for transition in DEMO_EXTERNAL_TRANSITIONS
        if phase is None or transition.phase == phase
    )


# The demo task is a genuine data/QA task whose execution steps are all
# executable after ``Accept plan``: no step starts work the FSM only allows in
# the planning or validation stage.
DEMO_EXECUTION_PLAN = {
    "summary": (
        "Демонстрационный план: проверить отказ недопустимого действия и "
        "неизменность состояния задачи."
    ),
    "acceptance_criteria": [
        "Отказ недопустимого действия не меняет состояние и версию задачи.",
        "Отказ зафиксирован в отдельном аудите, отличном от журнала успешных событий.",
    ],
    "steps": [
        {
            "index": 1,
            "title": "Зафиксировать доступные действия",
            "description": (
                "Открыть карточку задачи и перечислить действия, доступные в "
                "текущем состоянии."
            ),
        },
        {
            "index": 2,
            "title": "Проверить отклонение недопустимого действия",
            "description": (
                "Вызвать действие, которого нет в списке доступных, и "
                "зафиксировать отказ с причиной."
            ),
        },
        {
            "index": 3,
            "title": "Сверить состояние задачи и аудит",
            "description": (
                "Убедиться, что отказ записан в отдельном журнале, а "
                "состояние и версия задачи не изменились."
            ),
        },
    ],
}

# Anonymized real data of the reported Day 15 defect. After ``Run planning`` a
# plan rev 1 was stored whose steps re-ran the planning workflow itself. Once
# ``Accept plan`` moves the task to ``execution``, those steps are impossible:
# the plan required the task to be in ``planning`` and ``execution`` at once,
# so every check ended as a false "not confirmed".
REPORTED_TASK_GOAL = "Day 15 — ручная приёмка переходов"

REPORTED_PLAN_REV1 = {
    "summary": "План ручной проверки переходов задачи Day 15.",
    "acceptance_criteria": [
        "Стадия планирования отмечена как выполненная.",
        "План задачи сформирован.",
        "Переход в execution подтверждён.",
    ],
    "steps": [
        {
            "index": 1,
            "title": "Проверка планирования",
            "description": (
                "Проверить стадию планирования перед началом выполнения задачи."
            ),
        },
        {
            "index": 2,
            "title": "Формирование плана",
            "description": "Сформировать и сохранить план задачи как артефакт rev 1.",
        },
        {
            "index": 3,
            "title": "Принятие плана",
            "description": "Принять план и перейти к стадии execution.",
        },
    ],
}

# The full live plan rev 1 of the same reported task, anonymized. Unlike
# ``REPORTED_PLAN_REV1``, this is the complete five-step plan the provider
# returned: steps 1-2 are ordinary execution actions, while steps 3-5 are
# impossible after ``Accept plan`` (a validation transition, a done check and a
# non-existent "test copy of the state"). Step 5's exact wording is partially
# reconstructed; steps 3-4 and the defect shape are reported verbatim.
REPORTED_LIVE_PLAN = {
    "summary": "План ручной проверки переходов задачи Day 15 (живой дефект).",
    "acceptance_criteria": [
        "Текущая стадия и доступные действия зафиксированы.",
        "Переход в execution подтверждён.",
        "Результат проверен и задача завершена.",
    ],
    "steps": [
        {
            "index": 1,
            "title": "Зафиксировать текущую стадию задачи",
            "description": (
                "Открыть карточку задачи и записать её текущую стадию и статус."
            ),
        },
        {
            "index": 2,
            "title": "Проверить доступные действия",
            "description": (
                "Перечислить действия, доступные в текущем состоянии, и "
                "ожидаемый переход."
            ),
        },
        {
            "index": 3,
            "title": "Выполнить и проверить validation",
            "description": "Запустить проверку результата по критериям приёмки.",
        },
        {
            "index": 4,
            "title": "Проверить завершение задачи",
            "description": "Убедиться, что задача перешла в done.",
        },
        {
            "index": 5,
            "title": "На тестовой копии состояния проверить переходы",
            "description": (
                "Повторить переходы на тестовой копии состояния задачи."
            ),
        },
    ],
}

# The third reported defect (iteration 3): the guard accepted a plan whose
# execution steps and acceptance criteria depend on transitions that happen only
# after execution (validation/done) or that do not exist at all (returning to
# planning). Steps 1-2 are ordinary execution actions; step 3 loops back to
# planning, step 4 confirms a future VALIDATION_PASSED, step 5 confirms a future
# done. Anonymized reconstruction (no secrets or personal data).
REPORTED_LOOPBACK_PLAN = {
    "summary": "План ручной проверки переходов задачи Day 15 (loopback).",
    "acceptance_criteria": [
        "Событие VALIDATION_PASSED подтверждено.",
        "Задача завершена.",
    ],
    "steps": [
        {
            "index": 1,
            "title": "Зафиксировать текущую стадию задачи",
            "description": (
                "Открыть карточку задачи и записать её текущую стадию и статус."
            ),
        },
        {
            "index": 2,
            "title": "Проверить доступные действия",
            "description": (
                "Перечислить действия, доступные в текущем состоянии, и "
                "ожидаемый переход."
            ),
        },
        {
            "index": 3,
            "title": "Вернуть задачу в planning и повторить переход",
            "description": (
                "Вернуть задачу в planning и повторить переход в execution."
            ),
        },
        {
            "index": 4,
            "title": "Подтвердить событие VALIDATION_PASSED",
            "description": (
                "Подтвердить, что событие VALIDATION_PASSED наступило."
            ),
        },
        {
            "index": 5,
            "title": "Убедиться, что задача завершена (done)",
            "description": "Убедиться, что задача завершена и находится в done.",
        },
    ],
}

# The corrected plan for the same goal: previous stages are verified as facts
# that already happened (Event timeline, stored artifacts), the forbidden
# loopback is checked only through the framework diagnostics, and no criterion
# demands a future validation/done transition.
CORRECTED_PLAN_EXAMPLE = {
    "summary": (
        "Проверка ручной приёмки переходов задачи Day 15 по уже зафиксированным "
        "событиям и артефактам."
    ),
    "acceptance_criteria": [
        "Событие PLAN_ACCEPTED зафиксировано в Event timeline.",
        "Артефакт plan rev 1 доступен для сверки.",
        "Возврат в planning отклонён и записан в аудит отказов.",
    ],
    "steps": [
        {
            "index": 1,
            "title": "Проверить событие PLAN_ACCEPTED в Event timeline",
            "description": (
                "Убедиться по журналу событий, что событие PLAN_ACCEPTED уже "
                "зафиксировано как состоявшийся факт предыдущей стадии."
            ),
        },
        {
            "index": 2,
            "title": "Сверить артефакт plan rev 1",
            "description": (
                "Открыть сохранённый артефакт plan rev 1 и сверить его шаги с "
                "фактическим состоянием задачи."
            ),
        },
        {
            "index": 3,
            "title": (
                "Проверить через Transition guard, что возврат в planning "
                "отклоняется"
            ),
            "description": (
                "Убедиться через диагностику переходов, что действие возврата "
                "в planning запрещено, а отказ записан в аудит отказов."
            ),
        },
    ],
}

# Case-insensitive phrase/stem markers of the runtime plan guard. The tuple is a
# derived export of the marker constants that ``task_prompts`` checks, kept for
# import compatibility (old marker names are preserved); ``workflow_step_titles``
# delegates to that guard instead of matching these markers itself.
WORKFLOW_STEP_MARKERS = (
    PLAN_STEP_HARD_MARKERS
    + PLAN_STEP_RETRO_MARKERS
    + PLAN_STEP_MISSING_ENTITY_MARKERS
    + FUTURE_STAGE_ID_MARKERS
    + FUTURE_STAGE_STAGE_WORDS
    + FUTURE_STAGE_STEMS
    + FUTURE_STAGE_STATUS_TOKENS
    + FUTURE_STAGE_STATUS_PHRASES
    + FUTURE_STAGE_DONE_PHRASES
    + PRIOR_STAGE_PHRASES
    + PRIOR_STAGE_ENTITY_TOKENS
    + NONEXISTENT_TRANSITION_STEMS
    + NONEXISTENT_TRANSITION_PLANNING_WORDS
    + NONEXISTENT_TRANSITION_FRAMES
)


def workflow_step_titles(plan) -> tuple:
    """Return the titles of plan steps that cannot run in ``execution``.

    The check delegates to :func:`task_prompts.plan_step_violations` and keeps
    the plan order; a clean execution plan yields an empty tuple.
    """
    return tuple(violation.title for violation in plan_step_violations(plan))
