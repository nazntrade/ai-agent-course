"""Orchestrator-level tests for the Day 14 hard invariants.

Every test uses a temporary SQLite file, the ``FakeClient`` of
``tests.test_agent`` and no network or real ``.env``. A hard conflict must not
call the provider, must not change the task state and must not write a task
event or an artifact; only the invariant journal records the refusal.
"""

import os
import tempfile
import unittest

from agent import AgentConfig
from invariant_storage import InvariantRepository
from invariants import (
    ENFORCEMENT_HARD,
    Invariant,
    SCOPE_GLOBAL,
)
from storage import ChatStore
from task_orchestrator import (
    ERROR_INVARIANT_CONFLICT,
    STATUS_REFUSED,
    STATUS_SUCCESS,
    TaskOrchestrator,
)
from task_storage import TaskRepository
from tasks import (
    ACTION_PAUSE,
    ACTION_RUN_PLANNING,
    ACTION_RUN_STEP,
    ACTION_RUN_VALIDATION,
    ARTIFACT_PLAN,
    EVENT_API_ERROR,
    EVENT_PAUSE,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_TASK_CREATED,
    STAGE_DONE,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    apply_transition,
)
from tests.test_agent import FakeClient
from tests.test_task_orchestrator import PLAN, plan_response, stream_step, validation_response


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "invariants.db")
        self.store = ChatStore(self.path)
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = TaskRepository(self.store.db_path)
        self.invariants = InvariantRepository(self.store.db_path)

    def orchestrator(self, script=None):
        client = FakeClient(script=script)
        instance = TaskOrchestrator(
            self.store,
            repository=self.repo,
            client=client,
            invariants=self.invariants,
        )
        self.client = client
        return instance

    def add_rule(self, **overrides) -> Invariant:
        fields = dict(
            code="INV-BLOCK",
            title="Blocking rule",
            text="This is forbidden.",
            scope=SCOPE_GLOBAL,
            enforcement=ENFORCEMENT_HARD,
            triggers=("blocking phrase",),
        )
        fields.update(overrides)
        return self.invariants.create_invariant(Invariant(**fields))

    def new_task(self, **kwargs):
        return self.repo.create_task(
            self.chat_id,
            kwargs.pop("title", "Report task"),
            kwargs.pop("goal", "Build the report"),
            **kwargs,
        )

    def commit(self, task, event, payload=None):
        transition = apply_transition(
            task, event, payload=payload, progress=self.repo.step_progress(task.id)
        )
        return self.repo.commit_transition(task.id, task.version, transition)

    def execution_task(self):
        task = self.commit(self.new_task(), EVENT_PLAN_CREATED, {"plan": PLAN})
        return self.commit(task, EVENT_PLAN_ACCEPTED)

    def task_event_types(self, task_id):
        return [event.event_type for event in self.repo.list_events(task_id)]

    def conflict_events(self, code="INV-BLOCK"):
        return [
            event
            for event in self.invariants.list_events(code=code)
            if event.event_type == "conflict"
        ]


class ActionConflictTest(OrchestratorTestCase):
    def test_hard_action_conflict_blocks_before_the_provider(self):
        self.add_rule(guard_actions=(ACTION_RUN_PLANNING,))
        orchestrator = self.orchestrator([plan_response()])
        task = self.new_task()

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_REFUSED)
        self.assertEqual(result.error_kind, ERROR_INVARIANT_CONFLICT)
        self.assertEqual(self.client.payloads, [])
        self.assertEqual(self.task_event_types(task.id), [EVENT_TASK_CREATED])
        unchanged = self.repo.get_task(task.id)
        self.assertEqual(unchanged.version, task.version)
        self.assertEqual(unchanged.status, STATUS_ACTIVE)
        conflicts = self.conflict_events()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].details["phase"], "action")
        self.assertEqual(conflicts[0].details["action"], ACTION_RUN_PLANNING)

    def test_retry_does_not_write_retry_when_the_action_is_forbidden(self):
        self.add_rule(guard_actions=(ACTION_RUN_STEP,))
        orchestrator = self.orchestrator()
        task = self.execution_task()
        self.repo.append_event(
            task.id, EVENT_API_ERROR, payload={"kind": "stream_error", "message": "x"}
        )

        result = orchestrator.retry(task.id)

        self.assertEqual(result.status, STATUS_REFUSED)
        self.assertEqual(self.client.payloads, [])
        types = self.task_event_types(task.id)
        self.assertEqual(types[-1], EVENT_API_ERROR)
        self.assertNotIn("RETRY", types)

    def test_deactivation_makes_the_action_allowed_again(self):
        rule = self.add_rule(guard_actions=(ACTION_RUN_PLANNING,))
        orchestrator = self.orchestrator([plan_response()])
        task = self.new_task()

        refused = orchestrator.run_planning(task.id)
        self.assertEqual(refused.status, STATUS_REFUSED)

        self.invariants.set_active(rule.id, False)
        allowed = orchestrator.run_planning(task.id)

        self.assertEqual(allowed.status, STATUS_SUCCESS)
        self.assertEqual(allowed.task.expected_action_type, "confirm_plan")
        self.assertEqual(len(self.client.payloads), 1)


class TransitionConflictTest(OrchestratorTestCase):
    def test_hard_transition_conflict_blocks_the_commit(self):
        self.add_rule(code="INV-BLOCK-PAUSE", guard_events=(EVENT_PAUSE,))
        orchestrator = self.orchestrator()
        task = self.new_task()
        artifacts_before = self.repo.list_artifacts(task.id)

        result = orchestrator.pause(task.id, reason="stop")

        self.assertEqual(result.status, STATUS_REFUSED)
        self.assertEqual(self.client.payloads, [])
        unchanged = self.repo.get_task(task.id)
        self.assertEqual(unchanged.status, STATUS_ACTIVE)
        self.assertEqual(unchanged.version, task.version)
        self.assertEqual(self.repo.list_artifacts(task.id), artifacts_before)
        self.assertEqual(self.task_event_types(task.id), [EVENT_TASK_CREATED])
        conflicts = self.conflict_events("INV-BLOCK-PAUSE")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].details["phase"], "commit")
        self.assertEqual(conflicts[0].details["event"], EVENT_PAUSE)

    def test_hard_transition_conflict_writes_no_transition_attempt(self):
        # A hard invariant refusal is not a transition refusal: it never reaches
        # the FSM audit, so task_transition_attempts stays empty.
        self.add_rule(code="INV-BLOCK-PAUSE-AUDIT", guard_events=(EVENT_PAUSE,))
        orchestrator = self.orchestrator()
        task = self.new_task()

        result = orchestrator.pause(task.id, reason="stop")

        self.assertEqual(result.status, STATUS_REFUSED)
        self.assertEqual(self.repo.list_transition_attempts(task.id), [])

    def test_hard_action_conflict_writes_no_transition_attempt(self):
        self.add_rule(code="INV-BLOCK-PLAN-AUDIT", guard_actions=(ACTION_RUN_PLANNING,))
        orchestrator = self.orchestrator([plan_response()])
        task = self.new_task()

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_REFUSED)
        self.assertEqual(self.repo.list_transition_attempts(task.id), [])

    def test_probe_of_a_hard_invariant_refusal_has_no_audit_row(self):
        # A hard invariant refusal is journaled by the invariant store, not by
        # the FSM audit: the probe must report the refusal message with no
        # audit id, and ``task_transition_attempts`` must not grow.
        self.add_rule(code="INV-BLOCK-PROBE", guard_actions=(ACTION_PAUSE,))
        orchestrator = self.orchestrator()
        task = self.new_task()
        attempts_before = self.repo.list_transition_attempts(task.id)

        probe = orchestrator.probe_refused_transition(task.id, ACTION_PAUSE)

        self.assertFalse(probe.allowed)
        self.assertIsNone(probe.audit_id)
        self.assertIsNone(probe.error)
        self.assertTrue(probe.message)
        self.assertEqual(
            self.repo.list_transition_attempts(task.id), attempts_before
        )

    def test_legitimate_validation_is_not_blocked_by_the_seed(self):
        orchestrator = self.orchestrator(
            [
                plan_response(),
                stream_step("Step one done"),
                stream_step("Step two done"),
                validation_response(passed=True, notes="All good"),
            ]
        )
        task = self.new_task()
        task = orchestrator.run_planning(task.id).task
        task = orchestrator.accept_plan(task.id).task
        task = orchestrator.run_step(task.id, on_chunk=lambda text: None).task
        task = orchestrator.run_step(task.id, on_chunk=lambda text: None).task
        task = orchestrator.finish_execution(task.id).task
        result = orchestrator.run_validation(task.id)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.task.stage, STAGE_DONE)
        self.assertEqual(result.task.status, STATUS_COMPLETED)
        # No validation conflict was recorded for the legitimate run.
        self.assertEqual(self.conflict_events("INV-NO-FSM-BYPASS"), [])

    def test_terminal_task_is_a_noop_not_a_refusal(self):
        orchestrator = self.orchestrator(
            [
                plan_response(),
                stream_step("Step one done"),
                stream_step("Step two done"),
                validation_response(passed=True),
            ]
        )
        task = self.new_task()
        task = orchestrator.run_planning(task.id).task
        task = orchestrator.accept_plan(task.id).task
        task = orchestrator.run_step(task.id, on_chunk=lambda text: None).task
        task = orchestrator.run_step(task.id, on_chunk=lambda text: None).task
        task = orchestrator.finish_execution(task.id).task
        task = orchestrator.run_validation(task.id).task

        # A rule that would block planning is ignored on a terminal task: the
        # FSM guard runs first and returns a noop, never a refusal.
        self.add_rule(guard_actions=(ACTION_RUN_PLANNING,))
        result = orchestrator.run_planning(task.id)
        self.assertEqual(result.status, "noop")
        self.assertEqual(result.error_kind, "invalid_transition")


if __name__ == "__main__":
    unittest.main()
