"""Unit tests of the scenario guards and the SQLite verdict.

The regression fixtures are checked directly (bad plan / bad step text) and the
verdict is exercised against a synthetic database, so no browser and no provider
are involved.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from integrations.memory_state_agent import recipe, scenario, verdict
from task_demo import DEMO_EXECUTION_PLAN, REPORTED_PLAN_REV1
from task_prompts import (
    StepEventFact,
    StepFacts,
    StepRefusalFact,
    parse_plan_response,
    validate_step_text,
)

_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    stage TEXT, status TEXT, current_step TEXT, current_step_index INTEGER,
    expected_action_type TEXT, version INTEGER
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY, task_id INTEGER, event_type TEXT, payload_json TEXT
);
CREATE TABLE task_artifacts (
    id INTEGER PRIMARY KEY, task_id INTEGER, kind TEXT, revision INTEGER,
    content TEXT
);
CREATE TABLE task_transition_attempts (
    id INTEGER PRIMARY KEY, task_id INTEGER, action TEXT, reason TEXT
);
"""


def _plan_with_steps(count):
    """A valid execution plan with ``count`` steps (a live plan may have 4-6)."""
    if count == 3:
        return DEMO_EXECUTION_PLAN
    plan = json.loads(json.dumps(DEMO_EXECUTION_PLAN))
    template = plan["steps"][0]
    plan["steps"] = [
        {
            "index": index,
            "title": template["title"],
            "description": template["description"],
        }
        for index in range(1, max(count, 0) + 1)
    ]
    return plan


def _build_db(path, *, with_api_error=False, step_count=3, plan_step_count=None,
              with_plan=True):
    if plan_step_count is None:
        plan_step_count = step_count
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO tasks (id, stage, status, current_step, current_step_index, "
        "expected_action_type, version) VALUES (1, 'validation', 'paused', "
        "'Validation', NULL, 'run_validation', 12)"
    )
    events = [
        ("TASK_CREATED", {}),
        ("PLAN_CREATED", {"attempts": 2}),
        ("PLAN_ACCEPTED", {}),
    ]
    for step in range(1, step_count + 1):
        events.append(
            ("STEP_COMPLETED", {"step_index": step, "round": 1, "attempts": 2})
        )
    events.append(("EXECUTION_FINISHED", {}))
    events.append(("PAUSE", {}))
    if with_api_error:
        events.append(("API_ERROR", {"kind": "provider_error"}))
    events.append(("RESUME", {}))
    for index, (event_type, payload) in enumerate(events, start=1):
        connection.execute(
            "INSERT INTO task_events (id, task_id, event_type, payload_json) "
            "VALUES (?, 1, ?, ?)",
            (index, event_type, json.dumps(payload)),
        )
    if with_plan:
        connection.execute(
            "INSERT INTO task_artifacts (id, task_id, kind, revision, content) "
            "VALUES (1, 1, 'plan', 1, ?)",
            (json.dumps(_plan_with_steps(plan_step_count)),),
        )
    for step in range(1, step_count + 1):
        connection.execute(
            "INSERT INTO task_artifacts (id, task_id, kind, revision, content) "
            "VALUES (?, 1, 'execution_result', 1, ?)",
            (
                1 + step,
                json.dumps(
                    {
                        "step_index": step,
                        "round": 1,
                        "text": scenario.GOOD_STEP_TEXT,
                    }
                ),
            ),
        )
    connection.execute(
        "INSERT INTO task_transition_attempts (id, task_id, action, reason) "
        "VALUES (1, 1, 'finish_execution', 'expected_action_mismatch')"
    )
    connection.commit()
    connection.close()


class PlanFixtureTest(unittest.TestCase):
    def test_reported_plan_is_rejected_by_the_parser(self):
        with self.assertRaises(ValueError):
            parse_plan_response(json.dumps(REPORTED_PLAN_REV1, ensure_ascii=False))

    def test_good_plan_is_accepted_by_the_parser(self):
        parsed = parse_plan_response(
            json.dumps(DEMO_EXECUTION_PLAN, ensure_ascii=False)
        )
        self.assertEqual(len(parsed["steps"]), 3)

    def test_mock_fixtures_use_the_reported_bad_plan(self):
        fixtures = scenario.build_mock_fixtures()
        self.assertEqual(fixtures.bad_plan, REPORTED_PLAN_REV1)
        self.assertEqual(fixtures.good_plan, DEMO_EXECUTION_PLAN)
        self.assertTrue(fixtures.planning_markers)
        self.assertTrue(fixtures.execution_markers)
        self.assertTrue(fixtures.validation_markers)


class StepFixtureTest(unittest.TestCase):
    def _facts(self):
        return StepFacts(
            stage="execution",
            status="active",
            events=(
                StepEventFact(id=1, event_type="TASK_CREATED"),
                StepEventFact(id=2, event_type="PLAN_CREATED"),
                StepEventFact(id=3, event_type="PLAN_ACCEPTED"),
            ),
            refusals=(
                StepRefusalFact(
                    id=1, action="finish_execution", reason="expected_action_mismatch"
                ),
            ),
        )

    def test_bad_step_text_is_rejected_even_with_a_non_empty_audit(self):
        with self.assertRaises(ValueError) as context:
            validate_step_text(scenario.BAD_STEP_TEXT, self._facts())
        self.assertIn("unconfirmed_attempt", str(context.exception))

    def test_bad_step_text_names_an_action_absent_from_the_audit(self):
        # The audit names ``finish_execution``; the fixture claims ``reject_plan``.
        self.assertIn("reject_plan", scenario.BAD_STEP_TEXT)
        self.assertNotIn("finish_execution", scenario.BAD_STEP_TEXT)

    def test_bad_step_text_invents_a_journal_identifier(self):
        self.assertIn("EVT-EXEC-001", scenario.BAD_STEP_TEXT)

    def test_good_step_text_passes_validation(self):
        validate_step_text(scenario.GOOD_STEP_TEXT, self._facts())

    def test_validation_without_facts_is_disabled(self):
        validate_step_text(scenario.BAD_STEP_TEXT, None)


class VerdictTest(unittest.TestCase):
    def test_full_scenario_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db)
            result = verdict.evaluate(
                db, expect_plan_attempts=2, expect_step_attempts=2
            )
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.plan_attempts, 2)
        self.assertEqual(result.step_attempts, [2, 2, 2])
        self.assertEqual(result.request_count, 1)

    def test_api_error_fails_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, with_api_error=True)
            result = verdict.evaluate(db)
        self.assertFalse(result.ok)
        self.assertTrue(any("API_ERROR" in problem for problem in result.problems))

    def test_wrong_attempt_count_fails_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db)
            result = verdict.evaluate(
                db, expect_plan_attempts=1, expect_step_attempts=2
            )
        self.assertFalse(result.ok)
        self.assertTrue(any("planning attempts" in p for p in result.problems))

    def test_missing_event_fails_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db)
            connection = sqlite3.connect(db)
            connection.execute("DELETE FROM task_events WHERE event_type = 'PAUSE'")
            connection.commit()
            connection.close()
            result = verdict.evaluate(db)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("subsequence" in problem for problem in result.problems)
        )

    def test_attempts_are_summed_from_the_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db)
            self.assertEqual(verdict.sum_task_attempts(db), 8)

    def test_empty_database_has_no_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            connection = sqlite3.connect(db)
            connection.executescript(_SCHEMA)
            connection.commit()
            connection.close()
            result = verdict.evaluate(db)
        self.assertFalse(result.ok)
        self.assertIn("no task was stored", result.problems)

    def test_expected_events_for_matches_the_step_count(self):
        self.assertEqual(
            verdict.expected_events_for(0).count("STEP_COMPLETED"), 0
        )
        self.assertEqual(
            verdict.expected_events_for(1).count("STEP_COMPLETED"), 1
        )
        self.assertEqual(
            verdict.expected_events_for(6).count("STEP_COMPLETED"), 6
        )

    def test_mock_contract_stays_three_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db)
            result = verdict.evaluate(
                db, expect_plan_attempts=2, expect_step_attempts=2
            )
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.expected_step_count, 3)
        self.assertEqual(result.step_attempts, [2, 2, 2])


class LiveVerdictTest(unittest.TestCase):
    def test_live_verdict_accepts_every_matching_plan_size(self):
        for step_count in (4, 5, 6):
            with self.subTest(step_count=step_count):
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "app.db"
                    _build_db(db, step_count=step_count, plan_step_count=step_count)
                    result = verdict.evaluate(db, live=True)
                self.assertTrue(result.ok, result.problems)
                self.assertEqual(result.expected_step_count, step_count)
                self.assertEqual(len(result.step_attempts), step_count)

    def test_live_verdict_fails_when_a_step_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, step_count=3, plan_step_count=4)
            result = verdict.evaluate(db, live=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.expected_step_count, 4)
        self.assertTrue(
            any("expected 4 STEP_COMPLETED" in problem for problem in result.problems),
            result.problems,
        )

    def test_live_verdict_without_a_plan_fails_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, with_plan=False)
            result = verdict.evaluate(db, live=True)
        self.assertFalse(result.ok)
        self.assertIsNone(result.expected_step_count)
        self.assertTrue(
            any("no valid steps" in problem for problem in result.problems),
            result.problems,
        )

    def test_live_verdict_with_a_step_less_plan_fails_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, step_count=3, plan_step_count=0)
            result = verdict.evaluate(db, live=True)
        self.assertFalse(result.ok)
        self.assertIsNone(result.expected_step_count)
        self.assertTrue(
            any("no valid steps" in problem for problem in result.problems),
            result.problems,
        )

    def test_scenario_live_uses_the_stored_plan_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, step_count=5, plan_step_count=5)
            result = scenario.evaluate(db, live=True)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.expected_step_count, 5)


class PlanStepsReaderTest(unittest.TestCase):
    def test_recipe_reads_the_newest_plan_steps_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, step_count=4, plan_step_count=4)
            steps = recipe._plan_steps(db, 1)
        self.assertEqual(len(steps), 4)
        self.assertEqual([step["index"] for step in steps], [1, 2, 3, 4])

    def test_recipe_returns_no_steps_without_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            _build_db(db, with_plan=False)
            self.assertEqual(recipe._plan_steps(db, 1), [])


if __name__ == "__main__":
    unittest.main()
