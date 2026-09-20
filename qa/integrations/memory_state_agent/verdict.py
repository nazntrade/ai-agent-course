"""SQLite-only verdict of the Day 15 memory-state-agent scenario.

The pass/fail decision is made by code from the stored database, never by the
model. The verdict checks the expected event order, the absence of error events,
the stored plan and the stored step texts (validated against the storage facts
the model actually received).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[3] / "week-03" / "memory-state-agent"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from task_prompts import (  # noqa: E402  (path is set above)
    StepEventFact,
    StepFacts,
    StepRefusalFact,
    parse_plan_response,
    validate_step_text,
)
from tasks import (  # noqa: E402  (path is set above)
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_PLAN,
    EVENT_API_ERROR,
    EVENT_EXECUTION_FINISHED,
    EVENT_PAUSE,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_PLAN_REJECTED,
    EVENT_RESUME,
    EVENT_STEP_COMPLETED,
    EVENT_TASK_CREATED,
    plan_steps,
)

STEP_FACTS_EVENTS_LIMIT = 20

EXPECTED_STEP_COUNT = 3


def expected_events_for(step_count) -> tuple:
    """Return the expected event order for ``step_count`` executed steps.

    A live plan may legitimately contain 4 or 6 steps, so the expected
    ``STEP_COMPLETED`` count is derived from the stored plan instead of being
    hard-coded; the MOCK contract keeps its fixed three steps.
    """
    count = max(int(step_count or 0), 0)
    return (
        EVENT_TASK_CREATED,
        EVENT_PLAN_CREATED,
        EVENT_PLAN_ACCEPTED,
        *(EVENT_STEP_COMPLETED for _ in range(count)),
        EVENT_EXECUTION_FINISHED,
        EVENT_PAUSE,
        EVENT_RESUME,
    )


EXPECTED_EVENTS = expected_events_for(EXPECTED_STEP_COUNT)


def _connect(db_path):
    connection = sqlite3.connect(str(db_path), timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _latest_task_id(db_path):
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT id FROM tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["id"] if row is not None else None


def _events(db_path, task_id):
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT id, event_type, payload_json FROM task_events "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        events.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    return events


def _artifacts(db_path, task_id):
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT id, kind, revision, content FROM task_artifacts "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    artifacts = []
    for row in rows:
        try:
            content = json.loads(row["content"] or "{}")
        except ValueError:
            content = {}
        artifacts.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "revision": row["revision"],
                "content": content if isinstance(content, dict) else {},
            }
        )
    return artifacts


def _attempts(db_path, task_id):
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT id, action, reason FROM task_transition_attempts "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def sum_task_attempts(db_path) -> int:
    """Sum the ``attempts`` of every task event (used by MOCK consistency)."""
    task_id = _latest_task_id(db_path)
    if task_id is None:
        return 0
    total = 0
    for event in _events(db_path, task_id):
        value = event["payload"].get("attempts")
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _subsequence(actual, expected):
    """Whether ``expected`` is an ordered subsequence of ``actual``."""
    position = 0
    for index, event_type in enumerate(actual):
        if position < len(expected) and event_type == expected[position]:
            position += 1
            if position == len(expected):
                return True, index
    return False, -1


def _latest_plan(artifacts):
    plans = [artifact for artifact in artifacts if artifact["kind"] == ARTIFACT_PLAN]
    if not plans:
        return None
    newest = max(plans, key=lambda artifact: (artifact["revision"], artifact["id"]))
    return newest["content"]


@dataclass
class ScenarioVerdict:
    """Result of the SQLite evaluation of one scenario."""

    ok: bool = False
    task_id: int | None = None
    events: list = field(default_factory=list)
    plan_attempts: int | None = None
    step_attempts: list = field(default_factory=list)
    expected_step_count: int | None = None
    transition_attempts: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    request_count: int = 0

    def as_report(self) -> dict:
        return {
            "task_id": self.task_id,
            "events": list(self.events),
            "plan_attempts": self.plan_attempts,
            "step_attempts": list(self.step_attempts),
            "expected_step_count": self.expected_step_count,
            "transition_attempt_count": len(self.transition_attempts),
            "transition_attempts": list(self.transition_attempts),
            "request_count": self.request_count,
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


def evaluate(
    db_path,
    *,
    expect_plan_attempts=None,
    expect_step_attempts=None,
    expected_events=None,
    live=False,
    expected_step_count=None,
) -> ScenarioVerdict:
    """Evaluate the scenario from the database only.

    The MOCK contract keeps its fixed three steps. A live run derives the
    expected step count from the stored valid plan, so a 4- or 6-step live plan
    is not failed by a hard-coded three.
    """
    verdict = ScenarioVerdict()
    task_id = _latest_task_id(db_path)
    if task_id is None:
        verdict.problems.append("no task was stored")
        return verdict
    verdict.task_id = task_id

    events = _events(db_path, task_id)
    verdict.events = [event["event_type"] for event in events]
    artifacts = _artifacts(db_path, task_id)
    verdict.transition_attempts = _attempts(db_path, task_id)
    plan = _latest_plan(artifacts)

    if live:
        valid_steps = len(plan_steps(plan)) if plan is not None else 0
        if valid_steps <= 0:
            verdict.problems.append(
                "the stored plan has no valid steps; the expected step count "
                "cannot be derived"
            )
            expected_step_count = None
        else:
            expected_step_count = valid_steps
    elif expected_step_count is None:
        expected_step_count = EXPECTED_STEP_COUNT

    if expected_step_count is not None:
        verdict.expected_step_count = int(expected_step_count)
        count = int(expected_step_count)
    else:
        count = 0
    if expected_events is None:
        expected_events = expected_events_for(count)

    matched, last_index = _subsequence(verdict.events, expected_events)
    if not matched:
        verdict.problems.append(
            "the expected event subsequence is missing: "
            f"expected {list(expected_events)}, got {verdict.events}"
        )

    if EVENT_API_ERROR in verdict.events:
        verdict.problems.append("an API_ERROR event was stored")
    if EVENT_PLAN_REJECTED in verdict.events:
        verdict.problems.append("a PLAN_REJECTED event was stored")

    plan_created = [e for e in events if e["event_type"] == EVENT_PLAN_CREATED]
    if plan_created:
        value = plan_created[-1]["payload"].get("attempts")
        verdict.plan_attempts = value if isinstance(value, int) else None
    if (
        expect_plan_attempts is not None
        and verdict.plan_attempts != expect_plan_attempts
    ):
        verdict.problems.append(
            f"planning attempts {verdict.plan_attempts} != {expect_plan_attempts}"
        )

    step_events = [e for e in events if e["event_type"] == EVENT_STEP_COMPLETED]
    verdict.step_attempts = [
        event["payload"].get("attempts") for event in step_events
    ]
    if expected_step_count is not None and len(step_events) != expected_step_count:
        verdict.problems.append(
            f"expected {expected_step_count} STEP_COMPLETED events, "
            f"got {len(step_events)}"
        )
    if expect_step_attempts is not None:
        for index, attempts in enumerate(verdict.step_attempts, start=1):
            if attempts != expect_step_attempts:
                verdict.problems.append(
                    f"step {index} attempts {attempts} != {expect_step_attempts}"
                )

    if plan is None:
        verdict.problems.append("no plan artifact was stored")
    else:
        try:
            parse_plan_response(json.dumps(plan, ensure_ascii=False))
        except ValueError as exc:
            verdict.problems.append(f"the stored plan is invalid: {exc}")
        else:
            verdict.notes.append(
                f"the stored plan has {len(plan_steps(plan))} valid steps"
            )

    _validate_step_texts(verdict, db_path, task_id, events, artifacts)

    verdict.request_count = len(verdict.transition_attempts)
    verdict.ok = not verdict.problems
    return verdict


def _validate_step_texts(verdict, db_path, task_id, events, artifacts):
    """Validate every stored step text against its reconstructed storage facts."""
    execution_results = [
        artifact
        for artifact in artifacts
        if artifact["kind"] == ARTIFACT_EXECUTION_RESULT
    ]
    refusals = _attempts(db_path, task_id)
    facts_refusals = tuple(
        StepRefusalFact(
            id=refusal["id"],
            action=str(refusal.get("action") or ""),
            reason=str(refusal.get("reason") or ""),
        )
        for refusal in refusals
    )
    step_events = [event for event in events if event["event_type"] == EVENT_STEP_COMPLETED]
    if not step_events or not execution_results:
        return
    for event in step_events:
        index = event["payload"].get("step_index")
        round_value = event["payload"].get("round")
        artifact = next(
            (
                item
                for item in execution_results
                if item["content"].get("step_index") == index
                and (
                    round_value is None
                    or item["content"].get("round") == round_value
                )
            ),
            None,
        )
        if artifact is None:
            verdict.problems.append(
                f"no execution result was stored for step {index}"
            )
            continue
        text = str(artifact["content"].get("text") or "")
        events_before = [item for item in events if item["id"] < event["id"]]
        facts = StepFacts(
            stage="execution",
            status="active",
            events=tuple(
                StepEventFact(id=item["id"], event_type=item["event_type"])
                for item in events_before[-STEP_FACTS_EVENTS_LIMIT:]
            ),
            refusals=facts_refusals,
        )
        try:
            validate_step_text(text, facts)
        except ValueError as exc:
            verdict.problems.append(
                f"the stored text of step {index} is invalid: {exc}"
            )
