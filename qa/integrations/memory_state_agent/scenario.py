"""Integration descriptor of the ``week-03/memory-state-agent`` scenario.

The generic ``qa`` core knows nothing about the application; this module owns the
scenario: the anonymized fixtures the mock answers with, the task text, the
browser recipe and the SQLite verdict of the Day 15 regression defects.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[3] / "week-03" / "memory-state-agent"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from task_demo import (  # noqa: E402  (path is set above)
    DEMO_EXECUTION_PLAN,
    DEMO_TASK_BRIEF,
    DEMO_TASK_GOAL,
    DEMO_TASK_TITLE,
    REPORTED_PLAN_REV1,
)
from task_prompts import (  # noqa: E402  (path is set above)
    TASK_EXECUTION_SYSTEM_PROMPT,
    TASK_PLANNING_SYSTEM_PROMPT,
    TASK_VALIDATION_SYSTEM_PROMPT,
)

from lib.mock_provider import MockFixtures  # noqa: E402  (path is set above)
from lib.metrics import (  # noqa: E402
    PROVIDER_LLAMA_SERVER,
    PROVIDER_MOCK,
)
from integrations.memory_state_agent import recipe, verdict  # noqa: E402

TASK_TITLE = DEMO_TASK_TITLE
TASK_GOAL = DEMO_TASK_GOAL
TASK_BRIEF = DEMO_TASK_BRIEF

# The stage of a task call is recognized by the stage system prompt the code
# attaches to the packet; the first sentence of each prompt is stable.
PLANNING_MARKER = TASK_PLANNING_SYSTEM_PROMPT.split(".")[0].strip()
EXECUTION_MARKER = TASK_EXECUTION_SYSTEM_PROMPT.split(".")[0].strip()
VALIDATION_MARKER = TASK_VALIDATION_SYSTEM_PROMPT.split(".")[0].strip()

# The reported Day 15 defects, anonymized and deterministic. The first planning
# reply is the incompatible plan rev 1 of the report and the first execution
# reply invents a journal identifier, claims the planning stage and names a
# refused action (``reject_plan``) that is absent from the attached refusal
# audit, so the guard fires even after ``Test Finish execution`` already wrote
# one ``finish_execution`` refusal.
BAD_STEP_TEXT = (
    "Я проверил Transition guard. Событие EVT-EXEC-001 подтверждает переход. "
    "Стадия: planning. Попытка перехода reject_plan была отклонена и записана "
    "в аудит отказов."
)

# The corrected step text: it only describes the step result and the facts of
# the attached snapshot, so it passes ``validate_step_text``.
GOOD_STEP_TEXT = (
    "Шаг выполнен. Доступные действия и текущая стадия задачи зафиксированы в "
    "отчёте шага: работа идёт в стадии execution."
)

VALIDATION_PAYLOAD = {
    "passed": True,
    "defects": [],
    "notes": "mock validation passed",
}

MOCK_PLAN_ATTEMPTS = 2
MOCK_STEP_ATTEMPTS = 2


def build_mock_fixtures() -> MockFixtures:
    """Build the deterministic replies of the mock provider."""
    return MockFixtures(
        bad_plan=REPORTED_PLAN_REV1,
        good_plan=DEMO_EXECUTION_PLAN,
        bad_step_text=BAD_STEP_TEXT,
        good_step_text=GOOD_STEP_TEXT,
        validation_payload=VALIDATION_PAYLOAD,
        planning_markers=(PLANNING_MARKER,),
        execution_markers=(EXECUTION_MARKER,),
        validation_markers=(VALIDATION_MARKER,),
    )


def provider_metadata(live: bool) -> dict:
    """Return the provider/model labels of the metrics block."""
    return {
        "provider": PROVIDER_LLAMA_SERVER if live else PROVIDER_MOCK,
        "local_model_used": bool(live),
    }


def run_browser_scenario(page, *, db_path, run_dir, app, timeout_ms=recipe.DEFAULT_TIMEOUT_MS):
    """Run the Day 15 recipe (thin wrapper over ``recipe.run_recipe``)."""
    return recipe.run_recipe(
        page,
        db_path=db_path,
        run_dir=run_dir,
        app=app,
        title=TASK_TITLE,
        goal=TASK_GOAL,
        brief=TASK_BRIEF,
        timeout_ms=timeout_ms,
    )


def evaluate(db_path, *, live: bool):
    """Evaluate the stored scenario; MOCK also pins the retry attempts.

    A live run derives the expected step count from the stored plan, so a live
    4- or 6-step plan is judged by its own steps; MOCK keeps the fixed three.
    """
    if live:
        return verdict.evaluate(db_path, live=True)
    return verdict.evaluate(
        db_path,
        expect_plan_attempts=MOCK_PLAN_ATTEMPTS,
        expect_step_attempts=MOCK_STEP_ATTEMPTS,
    )


def sum_task_attempts(db_path) -> int:
    """Sum the task-event attempts (MOCK call-count consistency check)."""
    return verdict.sum_task_attempts(db_path)
