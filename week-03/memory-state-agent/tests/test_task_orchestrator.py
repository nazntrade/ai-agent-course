"""Integration tests for the Day 13 task orchestrator.

Every test uses a temporary SQLite file, the ``FakeClient`` of
``tests.test_agent`` and no network or real ``.env``. The scenarios cover the
full lifecycle, the restart-safe pause/resume, the rework of defective steps,
provider and JSON failures with retry, and the guarantee that task calls never
touch the chat history, memory or statistics.
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from agent import AgentConfig
from memory import (
    INVARIANTS_BLOCK_TITLE,
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
)
from storage import ChatStore
from task_orchestrator import (
    ACTION_CREATE_TASK,
    ACTION_NONE,
    ERROR_INVALID_INPUT,
    ERROR_INVALID_TRANSITION,
    ERROR_NOT_FOUND,
    STATUS_ERROR,
    STATUS_NOOP,
    STATUS_SUCCESS,
    STEP_FACTS_EVENTS_LIMIT,
    STEP_FACTS_REFUSALS_LIMIT,
    TaskOrchestrator,
)
from task_storage import TaskRepository
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_BLOCK,
    ACTION_CANCEL,
    ACTION_FINISH_EXECUTION,
    ACTION_NEW_TASK,
    ACTION_OPEN_DIAGNOSTICS,
    ACTION_OPEN_RESULT,
    ACTION_PAUSE,
    ACTION_REJECT_PLAN,
    ACTION_RESUME,
    ACTION_RETRY,
    ACTION_RUN_PLANNING,
    ACTION_RUN_STEP,
    ACTION_RUN_VALIDATION,
    ACTION_UNBLOCK,
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_FINAL_RESULT,
    ARTIFACT_PLAN,
    ARTIFACT_SPECIFICATION,
    ARTIFACT_TASK_BRIEF,
    ARTIFACT_VALIDATION_RESULT,
    DOMAIN_ACTIONS,
    EVENT_API_ERROR,
    EVENT_BLOCK,
    EVENT_CANCEL,
    EVENT_EXECUTION_FINISHED,
    EVENT_PAUSE,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_PLAN_REJECTED,
    EVENT_RESUME,
    EVENT_RETRY,
    EVENT_STEP_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_UNBLOCK,
    EVENT_VALIDATION_FAILED,
    EVENT_VALIDATION_PASSED,
    EXPECTED_CONFIRM_PLAN,
    EXPECTED_FINISH_EXECUTION,
    EXPECTED_NONE,
    EXPECTED_REVIEW_RESULT,
    EXPECTED_RUN_PLANNING,
    EXPECTED_RUN_STEP,
    EXPECTED_RUN_VALIDATION,
    EXPECTED_USER_ACTION,
    REASON_EXPECTED_ACTION_MISMATCH,
    REASON_INVALID_PAYLOAD,
    REASON_PROGRESS_INCOMPLETE,
    REASON_RETRY_REQUIRES_API_ERROR,
    REASON_TERMINAL,
    STAGE_DONE,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    can_apply,
)
from task_context import BLOCK_INVARIANTS, BLOCK_TASK_FACTS
from task_prompts import TASK_STEP_MAX_TOKENS, TASK_STEP_RETRY_MAX_TOKENS
from task_demo import (
    CORRECTED_PLAN_EXAMPLE,
    REPORTED_LOOPBACK_PLAN,
    REPORTED_LIVE_PLAN,
)
from tests.test_agent import (
    FakeClient,
    FakeContextOverflowError,
    make_chunk,
    make_response,
    make_usage,
    raises,
)

PLAN = {
    "summary": "Two step plan",
    "acceptance_criteria": ["The first criterion"],
    "steps": [
        {"index": 1, "title": "Step one", "description": "Do the first thing"},
        {"index": 2, "title": "Step two", "description": "Do the second thing"},
    ],
}

PLAN_REVISED = {
    "summary": "Revised plan",
    "acceptance_criteria": ["The revised criterion"],
    "steps": [
        {"index": 1, "title": "Revised step", "description": "Do it differently"},
    ],
}

PLAN_FOUR = {
    "summary": "Four step plan",
    "acceptance_criteria": ["All four steps are done"],
    "steps": [
        {"index": 1, "title": "Step one", "description": "First"},
        {"index": 2, "title": "Step two", "description": "Second"},
        {"index": 3, "title": "Step three", "description": "Third"},
        {"index": 4, "title": "Step four", "description": "Fourth"},
    ],
}

# The reported defect shape (anonymized): the model invented journal ids and
# asserted a state that contradicts the stored Event timeline.
DIRTY_STEP_TEXT = (
    "Шаг выполнен: создано событие EVT-EXEC-001. "
    "Стадия: planning. Статус: completed. "
    "plan_accepted отсутствует в журнале: переход в execution ещё не выполнялся."
)

# The Day 15 fix defect: no refusal is stored, yet the step text claims an
# attempted transition that was rejected.
UNCONFIRMED_STEP_TEXT = (
    "Шаг 1: Инициирован недопустимый переход из стадии execution "
    "к завершению выполнения."
)


def plan_response(plan=None, usage=None, finish_reason="stop"):
    """A non-stream planning response carrying the plan JSON."""
    text = json.dumps(plan if plan is not None else PLAN)
    return lambda kwargs: make_response(
        text,
        usage=usage if usage is not None else make_usage(120, 60),
        finish_reason=finish_reason,
    )


def text_response(text, usage=None, finish_reason="stop"):
    """A non-stream response with arbitrary text."""
    return lambda kwargs: make_response(
        text,
        usage=usage if usage is not None else make_usage(50, 20),
        finish_reason=finish_reason,
    )


def stream_step(text, usage=None, finish_reason="stop"):
    """A streamed step response with an optional usage-only final chunk."""

    def step(kwargs):
        def generator():
            yield make_chunk(text, finish_reason=finish_reason)
            if usage is not None:
                yield SimpleNamespace(choices=[], usage=usage)

        return generator()

    return step


def failing_stream(*fragments, error=None):
    """A stream that yields some text and then breaks mid-stream."""

    def step(kwargs):
        def generator():
            for fragment in fragments:
                yield make_chunk(fragment)
            raise error if error is not None else RuntimeError("stream broke")

        return generator()

    return step


def validation_response(passed=True, defects=None, notes="", usage=None, finish_reason="stop"):
    """A non-stream validation response with a JSON verdict."""
    payload = {"passed": passed, "defects": defects or [], "notes": notes}
    text = json.dumps(payload)
    return lambda kwargs: make_response(
        text,
        usage=usage if usage is not None else make_usage(90, 30),
        finish_reason=finish_reason,
    )


def event_types(repo, task_id):
    return [event.event_type for event in repo.list_events(task_id)]


def artifacts_of(repo, task_id, kind):
    return repo.list_artifacts(task_id, kind=kind)


class OrchestratorTestCase(unittest.TestCase):
    """A temporary database with one chat and one repository."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = TaskRepository(self.store.db_path)

    def make_orchestrator(self, script=None, **kwargs):
        client = FakeClient(script=script, **kwargs)
        orchestrator = TaskOrchestrator(
            self.store, repository=self.repo, client=client
        )
        self.client = client
        return orchestrator

    def create_task(self, orchestrator, **kwargs):
        return orchestrator.create_task(
            self.chat_id,
            kwargs.pop("title", "Report task"),
            kwargs.pop("goal", "Build the report"),
            **kwargs,
        )


class LifecycleTest(OrchestratorTestCase):
    def test_full_lifecycle_reaches_done(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("Step one done", usage=make_usage(120, 60)),
                stream_step("Step two done", usage=make_usage(130, 70)),
                validation_response(passed=True, notes="Everything is fine"),
            ]
        )
        created = self.create_task(orchestrator, task_brief="Keep it short")
        self.assertEqual(created.status, STATUS_SUCCESS)
        self.assertEqual(created.action, ACTION_CREATE_TASK)
        task = created.task
        self.assertEqual(task.stage, STAGE_PLANNING)
        self.assertEqual(task.status, STATUS_ACTIVE)
        self.assertEqual(task.version, 1)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(
            self.repo.get_active_task_id(self.chat_id), task.id
        )
        self.assertEqual(event_types(self.repo, task.id), [EVENT_TASK_CREATED])
        self.assertEqual(
            [artifact.kind for artifact in self.repo.list_artifacts(task.id)],
            [ARTIFACT_TASK_BRIEF],
        )

        planning = orchestrator.run_planning(task.id)
        self.assertEqual(planning.status, STATUS_SUCCESS)
        self.assertEqual(planning.task.stage, STAGE_PLANNING)
        self.assertEqual(planning.task.expected_action_type, EXPECTED_CONFIRM_PLAN)
        self.assertEqual(planning.task.current_step, "Step one")
        self.assertEqual(planning.task.current_step_index, 1)
        self.assertEqual(planning.task.version, 2)
        plans = artifacts_of(self.repo, task.id, ARTIFACT_PLAN)
        self.assertEqual([(item.kind, item.revision) for item in plans], [(ARTIFACT_PLAN, 1)])
        self.assertEqual(plans[0].content["steps"][0]["title"], "Step one")
        specifications = artifacts_of(self.repo, task.id, ARTIFACT_SPECIFICATION)
        self.assertEqual(len(specifications), 1)
        self.assertIn("## Acceptance criteria", specifications[0].content["markdown"])

        accepted = orchestrator.accept_plan(task.id)
        self.assertEqual(accepted.status, STATUS_SUCCESS)
        self.assertEqual(accepted.task.stage, STAGE_EXECUTION)
        self.assertEqual(accepted.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(accepted.task.current_step_index, 1)

        collected = []
        first = orchestrator.run_step(task.id, on_chunk=collected.append)
        self.assertEqual(first.status, STATUS_SUCCESS)
        self.assertEqual(first.task.current_step_index, 2)
        self.assertEqual(collected, ["Step one done"])
        second = orchestrator.run_step(task.id, on_chunk=collected.append)
        self.assertEqual(second.status, STATUS_SUCCESS)
        self.assertEqual(second.task.expected_action_type, EXPECTED_FINISH_EXECUTION)
        self.assertEqual(second.task.current_step_index, None)
        self.assertEqual(collected, ["Step one done", "Step two done"])

        results = artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(
            [(item.content["step_index"], item.content["round"]) for item in results],
            [(1, 1), (2, 1)],
        )
        self.assertEqual(results[0].content["text"], "Step one done")

        finished = orchestrator.finish_execution(task.id)
        self.assertEqual(finished.status, STATUS_SUCCESS)
        self.assertEqual(finished.task.stage, STAGE_VALIDATION)
        self.assertEqual(finished.task.current_step, "Validation")
        self.assertEqual(finished.task.expected_action_type, EXPECTED_RUN_VALIDATION)

        validated = orchestrator.run_validation(task.id)
        self.assertEqual(validated.status, STATUS_SUCCESS)
        self.assertEqual(validated.task.stage, STAGE_DONE)
        self.assertEqual(validated.task.status, STATUS_COMPLETED)
        self.assertEqual(validated.task.expected_action_type, EXPECTED_REVIEW_RESULT)
        verdicts = artifacts_of(self.repo, task.id, ARTIFACT_VALIDATION_RESULT)
        self.assertTrue(verdicts[0].content["passed"])
        finals = artifacts_of(self.repo, task.id, ARTIFACT_FINAL_RESULT)
        self.assertEqual(len(finals), 1)
        self.assertIn("Step one done", finals[0].content["markdown"])

        self.assertEqual(
            event_types(self.repo, task.id),
            [
                EVENT_TASK_CREATED,
                EVENT_PLAN_CREATED,
                EVENT_PLAN_ACCEPTED,
                EVENT_STEP_COMPLETED,
                EVENT_STEP_COMPLETED,
                EVENT_EXECUTION_FINISHED,
                EVENT_VALIDATION_PASSED,
            ],
        )

    def test_planning_and_validation_are_not_streamed(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("Step one"), stream_step("Step two"),
             validation_response(passed=True)]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        self.assertFalse(self.client.payloads[0]["stream"])
        self.assertNotIn("stream_options", self.client.payloads[0])
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        streamed_step = self.client.payloads[1]
        self.assertTrue(streamed_step["stream"])
        self.assertEqual(
            streamed_step["stream_options"], {"include_usage": True}
        )
        self.assertEqual(streamed_step["max_tokens"], 2000)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.finish_execution(task.id)
        orchestrator.run_validation(task.id)
        self.assertFalse(self.client.payloads[-1]["stream"])
        self.assertEqual(self.client.payloads[-1]["max_tokens"], 1600)

    def test_run_step_without_consumer_is_not_streamed(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), text_response("Step one done"), text_response("Step two done")]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        result = orchestrator.run_step(task.id)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertFalse(self.client.payloads[-1]["stream"])
        self.assertNotIn("stream_options", self.client.payloads[-1])

    def test_reject_and_replan_produce_a_new_plan_revision(self):
        orchestrator = self.make_orchestrator(
            [plan_response(PLAN), plan_response(PLAN_REVISED)]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        rejected = orchestrator.reject_plan(task.id)
        self.assertEqual(rejected.status, STATUS_SUCCESS)
        self.assertEqual(rejected.task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(rejected.task.current_step_index, None)

        replanned = orchestrator.run_planning(task.id)
        self.assertEqual(replanned.status, STATUS_SUCCESS)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_PLAN_CREATED, EVENT_PLAN_REJECTED, EVENT_PLAN_CREATED],
        )
        plans = artifacts_of(self.repo, task.id, ARTIFACT_PLAN)
        self.assertEqual([item.revision for item in plans], [1, 2])
        self.assertEqual(plans[0].content["summary"], "Two step plan")
        self.assertEqual(plans[1].content["summary"], "Revised plan")

    def test_non_llm_actions_do_not_call_the_provider(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        calls_after_planning = self.client.calls
        orchestrator.accept_plan(task.id)
        orchestrator.pause(task.id, reason="break")
        orchestrator.resume(task.id)
        self.assertEqual(self.client.calls, calls_after_planning)


class RestartResumeTest(OrchestratorTestCase):
    def test_pause_restart_resume_continues_from_the_current_step(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("Step one done"),
                stream_step("Step two done"),
                validation_response(passed=True),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        paused = orchestrator.pause(task.id, reason="lunch")
        self.assertEqual(paused.task.status, STATUS_PAUSED)
        self.assertEqual(paused.task.stage, STAGE_EXECUTION)
        self.assertEqual(paused.task.current_step_index, 2)
        self.assertEqual(paused.task.expected_action_type, EXPECTED_RUN_STEP)
        version_before_restart = paused.task.version

        # Full restart: brand new store, repository and orchestrator on the
        # same database file, with a fresh client.
        store = ChatStore(self.path)
        repo = TaskRepository(store.db_path)
        client = FakeClient(
            script=[stream_step("Step two done"), validation_response(passed=True)]
        )
        restarted = TaskOrchestrator(store, repository=repo, client=client)

        reloaded = repo.get_task(task.id)
        self.assertEqual(reloaded.status, STATUS_PAUSED)
        self.assertEqual(reloaded.current_step_index, 2)
        self.assertEqual(reloaded.stage, STAGE_EXECUTION)

        resumed = restarted.resume(task.id)
        self.assertEqual(resumed.status, STATUS_SUCCESS)
        self.assertEqual(resumed.task.status, STATUS_ACTIVE)
        self.assertEqual(resumed.task.stage, STAGE_EXECUTION)
        self.assertEqual(resumed.task.current_step_index, 2)
        self.assertEqual(resumed.task.version, version_before_restart + 1)
        self.assertEqual(resumed.task.pause_reason, "")

        second = restarted.run_step(task.id, on_chunk=lambda text: None)
        self.assertEqual(second.status, STATUS_SUCCESS)
        self.assertEqual(second.task.expected_action_type, EXPECTED_FINISH_EXECUTION)
        self.assertEqual(client.calls, 1)

        restarted.finish_execution(task.id)
        done = restarted.run_validation(task.id)
        self.assertEqual(done.status, STATUS_SUCCESS)
        self.assertEqual(done.task.stage, STAGE_DONE)

        results = artifacts_of(repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(
            [(item.content["step_index"], item.content["round"]) for item in results],
            [(1, 1), (2, 1)],
        )
        self.assertEqual(
            event_types(repo, task.id),
            [
                EVENT_TASK_CREATED,
                EVENT_PLAN_CREATED,
                EVENT_PLAN_ACCEPTED,
                EVENT_STEP_COMPLETED,
                EVENT_PAUSE,
                EVENT_RESUME,
                EVENT_STEP_COMPLETED,
                EVENT_EXECUTION_FINISHED,
                EVENT_VALIDATION_PASSED,
            ],
        )


class ValidationReworkTest(OrchestratorTestCase):
    def test_failed_validation_reworks_only_the_defective_steps(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(PLAN_FOUR),
                stream_step("one"),
                stream_step("two"),
                stream_step("three"),
                stream_step("four"),
                validation_response(
                    passed=False,
                    defects=[
                        {"step_index": 4, "description": "The fourth is wrong"},
                        {"step_index": 2, "description": "The second is wrong"},
                    ],
                    notes="Rework two steps",
                ),
                stream_step("two again"),
                stream_step("four again"),
                validation_response(passed=True, notes="Now it passes"),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        for _ in range(4):
            self.assertEqual(
                orchestrator.run_step(task.id, on_chunk=lambda text: None).status,
                STATUS_SUCCESS,
            )
        orchestrator.finish_execution(task.id)

        failed = orchestrator.run_validation(task.id)
        self.assertEqual(failed.status, STATUS_SUCCESS)
        self.assertEqual(failed.task.stage, STAGE_EXECUTION)
        self.assertEqual(failed.task.current_step_index, 2)
        self.assertEqual(failed.task.current_step, "Step two")
        self.assertEqual(failed.task.expected_action_type, EXPECTED_RUN_STEP)

        # Finishing before the rework is rejected and changes nothing.
        version = failed.task.version
        premature = orchestrator.finish_execution(task.id)
        self.assertEqual(premature.status, STATUS_NOOP)
        self.assertEqual(self.repo.get_task(task.id).version, version)

        rework_two = orchestrator.run_step(task.id, on_chunk=lambda text: None)
        self.assertEqual(rework_two.status, STATUS_SUCCESS)
        self.assertEqual(rework_two.task.current_step_index, 4)
        rework_four = orchestrator.run_step(task.id, on_chunk=lambda text: None)
        self.assertEqual(rework_four.status, STATUS_SUCCESS)
        self.assertEqual(rework_four.task.expected_action_type, EXPECTED_FINISH_EXECUTION)

        orchestrator.finish_execution(task.id)
        done = orchestrator.run_validation(task.id)
        self.assertEqual(done.status, STATUS_SUCCESS)
        self.assertEqual(done.task.stage, STAGE_DONE)

        results = artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(
            [(item.content["step_index"], item.content["round"]) for item in results],
            [(1, 1), (2, 1), (3, 1), (4, 1), (2, 2), (4, 2)],
        )
        verdicts = artifacts_of(self.repo, task.id, ARTIFACT_VALIDATION_RESULT)
        self.assertEqual([item.revision for item in verdicts], [1, 2])
        self.assertFalse(verdicts[0].content["passed"])
        self.assertTrue(verdicts[1].content["passed"])
        events = event_types(self.repo, task.id)
        self.assertIn(EVENT_VALIDATION_FAILED, events)
        self.assertIn(EVENT_VALIDATION_PASSED, events)
        self.assertEqual(events.count(EVENT_EXECUTION_FINISHED), 2)
        self.assertEqual(events.count(EVENT_STEP_COMPLETED), 6)


class ProviderErrorTest(OrchestratorTestCase):
    def test_error_before_the_first_token_records_api_error(self):
        orchestrator = self.make_orchestrator([raises(RuntimeError("provider down"))])
        task = self.create_task(orchestrator).task
        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "provider_error")
        self.assertIn("provider down", result.error_message)
        unchanged = self.repo.get_task(task.id)
        self.assertEqual(unchanged.version, 1)
        self.assertEqual(unchanged.stage, STAGE_PLANNING)
        self.assertEqual(unchanged.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_PLAN), [])
        self.assertEqual(
            event_types(self.repo, task.id), [EVENT_TASK_CREATED, EVENT_API_ERROR]
        )
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "provider_error")
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["message"], "provider down")
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(payload["usage"], {"model": "deepseek-flash"})

    def test_long_error_message_is_truncated(self):
        orchestrator = self.make_orchestrator([raises(RuntimeError("x" * 500))])
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(len(payload["message"]), 200)

    def test_stream_error_discards_the_partial_text(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), failing_stream("Partial text")]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        collected = []

        result = orchestrator.run_step(task.id, on_chunk=collected.append)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "stream_error")
        self.assertEqual(collected, ["Partial text"])
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT), [])
        unchanged = self.repo.get_task(task.id)
        self.assertEqual(unchanged.stage, STAGE_EXECUTION)
        self.assertEqual(unchanged.current_step_index, 1)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_PLAN_CREATED, EVENT_PLAN_ACCEPTED, EVENT_API_ERROR],
        )

    def test_retry_repeats_the_failed_action_and_adds_one_revision(self):
        orchestrator = self.make_orchestrator(
            [raises(RuntimeError("provider down")), plan_response()]
        )
        task = self.create_task(orchestrator).task
        failed = orchestrator.run_planning(task.id)
        self.assertEqual(failed.status, STATUS_ERROR)
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

        retried = orchestrator.retry(task.id)

        self.assertEqual(retried.status, STATUS_SUCCESS)
        self.assertEqual(retried.action, ACTION_RETRY)
        self.assertEqual(retried.task.expected_action_type, EXPECTED_CONFIRM_PLAN)
        self.assertEqual(len(artifacts_of(self.repo, task.id, ARTIFACT_PLAN)), 1)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_API_ERROR, EVENT_RETRY, EVENT_PLAN_CREATED],
        )

    def test_retry_without_api_error_is_rejected(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self.create_task(orchestrator).task
        result = orchestrator.retry(task.id)
        self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(event_types(self.repo, task.id), [EVENT_TASK_CREATED])

    def test_retry_while_paused_or_blocked_is_a_noop_without_an_event(self):
        for state in ("paused", "blocked"):
            with self.subTest(state=state):
                orchestrator = self.make_orchestrator([plan_response()])
                task = self.create_task(orchestrator).task
                if state == "paused":
                    orchestrator.pause(task.id, reason="lunch")
                else:
                    orchestrator.block(task.id, "Need data", "Provide the data")
                self.repo.append_event(
                    task.id,
                    EVENT_API_ERROR,
                    payload={"kind": "stream_error", "message": "connection dropped"},
                )
                before = event_types(self.repo, task.id)
                self.assertNotIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

                result = orchestrator.retry(task.id)

                self.assertEqual(result.status, STATUS_NOOP)
                self.assertEqual(event_types(self.repo, task.id), before)
                self.assertNotIn(EVENT_RETRY, before)

    def test_context_overflow_is_classified_and_retryable(self):
        orchestrator = self.make_orchestrator(
            [raises(FakeContextOverflowError()), plan_response()]
        )
        task = self.create_task(orchestrator).task
        failed = orchestrator.run_planning(task.id)
        self.assertEqual(failed.status, STATUS_ERROR)
        self.assertEqual(failed.error_kind, "context_overflow")
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "context_overflow")
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

        retried = orchestrator.retry(task.id)
        self.assertEqual(retried.status, STATUS_SUCCESS)
        self.assertEqual(len(artifacts_of(self.repo, task.id, ARTIFACT_PLAN)), 1)


class StructuredResponseErrorTest(OrchestratorTestCase):
    def test_invalid_plan_json_retries_once_then_records_invalid_response(self):
        orchestrator = self.make_orchestrator(
            [text_response("not a json"), text_response("{broken")]
        )
        task = self.create_task(orchestrator).task
        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 2)
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "invalid_response")
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_PLAN), [])

    def test_invalid_plan_json_then_a_valid_retry_succeeds(self):
        orchestrator = self.make_orchestrator(
            [text_response("not a json"), plan_response()]
        )
        task = self.create_task(orchestrator).task
        result = orchestrator.run_planning(task.id)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(len(artifacts_of(self.repo, task.id, ARTIFACT_PLAN)), 1)

    def test_truncated_plan_is_retried_then_recorded_as_truncated(self):
        orchestrator = self.make_orchestrator(
            [
                text_response('{"summary": "cut', finish_reason="length"),
                text_response('{"summary": "still cut', finish_reason="max_tokens"),
            ]
        )
        task = self.create_task(orchestrator).task
        result = orchestrator.run_planning(task.id)
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "truncated")
        self.assertEqual(len(result.stage_result.attempts), 2)
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "truncated")
        self.assertEqual(payload["finish_reason"], "max_tokens")

    def test_truncated_plan_retry_reuses_the_original_messages(self):
        """A truncation retry must not append corrective parser feedback."""
        orchestrator = self.make_orchestrator(
            [
                text_response('{"summary": "cut', finish_reason="length"),
                plan_response(),
            ]
        )
        task = self.create_task(orchestrator).task

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(self.client.calls, 2)
        first_messages = self.client.payloads[0]["messages"]
        second_messages = self.client.payloads[1]["messages"]
        self.assertEqual(second_messages, first_messages)
        self.assertEqual(len(second_messages), len(first_messages))
        joined = "\n".join(message["content"] for message in second_messages)
        self.assertNotIn("Предыдущий план отклонён", joined)
        self.assertNotIn("Недопустимые шаги", joined)
        self.assertGreater(
            self.client.payloads[1]["max_tokens"],
            self.client.payloads[0]["max_tokens"],
        )

    def test_truncated_step_is_retried_and_the_retry_wins(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("Cut off", finish_reason="length"),
                stream_step("The complete step text"),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)

        result = orchestrator.run_step(task.id, on_chunk=lambda text: None)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(result.stage_result.attempts), 2)
        results = artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(results[0].content["text"], "The complete step text")

    def test_twice_truncated_step_is_recorded_without_an_artifact(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("Cut off", finish_reason="length"),
                stream_step("Still cut", finish_reason="length"),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)

        result = orchestrator.run_step(task.id, on_chunk=lambda text: None)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "truncated")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT), [])
        self.assertEqual(self.repo.get_task(task.id).current_step_index, 1)
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

    def test_empty_step_text_is_invalid_response_without_a_retry(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), text_response("   ")]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)

        result = orchestrator.run_step(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT), [])

    def test_invalid_validation_json_is_retried_once(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("one"),
                stream_step("two"),
                text_response("not a verdict"),
                text_response("still not a verdict"),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.finish_execution(task.id)

        result = orchestrator.run_validation(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(
            artifacts_of(self.repo, task.id, ARTIFACT_VALIDATION_RESULT), []
        )
        self.assertEqual(self.repo.get_task(task.id).stage, STAGE_VALIDATION)


class StepFactsGuardTest(OrchestratorTestCase):
    """The execution reply may not invent or contradict stored journal facts."""

    def _execution_task(self, orchestrator):
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        return self.repo.get_task(task.id)

    def test_dirty_step_text_is_retried_then_the_clean_text_is_stored(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                text_response("Step one done"),
                text_response(DIRTY_STEP_TEXT),
                text_response("Step two done"),
            ]
        )
        task = self._execution_task(orchestrator)

        first = orchestrator.run_step(task.id)
        self.assertEqual(first.status, STATUS_SUCCESS)

        second = orchestrator.run_step(task.id)

        self.assertEqual(second.status, STATUS_SUCCESS)
        self.assertEqual(len(second.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 4)

        # The retry keeps the original packet and appends one corrective user
        # message that points at the attached facts.
        dirty_messages = self.client.payloads[2]["messages"]
        retry_messages = self.client.payloads[3]["messages"]
        self.assertEqual(retry_messages[: len(dirty_messages)], dirty_messages)
        feedback = retry_messages[-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("task_facts", feedback["content"])
        self.assertIn("не выдумывай", feedback["content"])

        results = artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(
            [item.content["step_index"] for item in results], [1, 2]
        )
        stored_text = results[1].content["text"]
        self.assertEqual(stored_text, "Step two done")
        self.assertNotIn("EVT-", stored_text)
        self.assertNotIn("ещё не выполнялся", stored_text)

        # Regression: the timeline the dirty text denied is present and the
        # stored result does not contradict it.
        events = event_types(self.repo, task.id)
        self.assertIn(EVENT_PLAN_ACCEPTED, events)
        self.assertEqual(events.count(EVENT_STEP_COMPLETED), 2)

    def test_twice_dirty_step_text_records_invalid_response(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                text_response("Step one done"),
                text_response(DIRTY_STEP_TEXT),
                text_response(DIRTY_STEP_TEXT),
            ]
        )
        task = self._execution_task(orchestrator)
        orchestrator.run_step(task.id)
        before = self.repo.get_task(task.id)

        result = orchestrator.run_step(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 4)
        results = artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual([item.content["step_index"] for item in results], [1])

        after = self.repo.get_task(task.id)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.current_step, before.current_step)
        self.assertEqual(after.current_step_index, before.current_step_index)
        self.assertEqual(after.version, before.version)
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "invalid_response")
        self.assertEqual(payload["attempts"], 2)
        self.assertLessEqual(len(payload["message"]), 200)

    def test_clean_step_text_is_not_retried(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                text_response("Step one done"),
            ]
        )
        task = self._execution_task(orchestrator)

        result = orchestrator.run_step(task.id)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(result.stage_result.attempts), 1)
        self.assertEqual(self.client.calls, 2)

    def test_facts_guard_retry_uses_the_larger_budget(self):
        """The retry of a rejected step must use the stage retry budget."""
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                text_response("Step one done"),
                text_response(DIRTY_STEP_TEXT),
                text_response("Step two done"),
            ]
        )
        task = self._execution_task(orchestrator)
        orchestrator.run_step(task.id)

        second = orchestrator.run_step(task.id)

        self.assertEqual(second.status, STATUS_SUCCESS)
        self.assertEqual(len(second.stage_result.attempts), 2)
        dirty_call = self.client.payloads[2]
        retry_call = self.client.payloads[3]
        self.assertEqual(dirty_call["max_tokens"], TASK_STEP_MAX_TOKENS)
        self.assertEqual(retry_call["max_tokens"], TASK_STEP_RETRY_MAX_TOKENS)
        self.assertGreater(retry_call["max_tokens"], dirty_call["max_tokens"])
        # The corrective feedback is appended to the retried messages only.
        feedback = retry_call["messages"][-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("task_facts", feedback["content"])
        self.assertEqual(retry_call["messages"][:-1], dirty_call["messages"])

    def test_unconfirmed_attempt_is_retried_then_the_clean_text_is_stored(self):
        """No stored refusal: the model may not claim a rejected transition."""
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                text_response(UNCONFIRMED_STEP_TEXT),
                text_response("Step one done"),
            ]
        )
        task = self._execution_task(orchestrator)

        result = orchestrator.run_step(task.id)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 3)
        feedback = self.client.payloads[2]["messages"][-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("task_facts", feedback["content"])
        self.assertIn("был бы отклонён", feedback["content"])

        results = artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual([item.content["step_index"] for item in results], [1])
        self.assertEqual(results[0].content["text"], "Step one done")
        self.assertEqual(self.repo.list_transition_attempts(task.id), [])

    def test_twice_unconfirmed_attempt_records_invalid_response(self):
        """The unconfirmed attempt follows the same retry contract as a denial."""
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                text_response(UNCONFIRMED_STEP_TEXT),
                text_response(UNCONFIRMED_STEP_TEXT),
            ]
        )
        task = self._execution_task(orchestrator)
        before = self.repo.get_task(task.id)

        result = orchestrator.run_step(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 3)
        self.assertEqual(
            artifacts_of(self.repo, task.id, ARTIFACT_EXECUTION_RESULT), []
        )

        after = self.repo.get_task(task.id)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.current_step, before.current_step)
        self.assertEqual(after.current_step_index, before.current_step_index)
        self.assertEqual(after.version, before.version)
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "invalid_response")
        self.assertEqual(payload["attempts"], 2)


class StepFactsWiringTest(OrchestratorTestCase):
    """The execution snapshot is copied from storage, never assembled by hand."""

    def test_step_packet_facts_come_from_the_repository(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("Step one done"),
                stream_step("Step two done"),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        first = orchestrator.run_step(task.id, on_chunk=lambda text: None)
        self.assertEqual(first.status, STATUS_SUCCESS)

        # A disallowed action is refused and audited without a provider call.
        refused = orchestrator.run_validation(task.id)
        self.assertEqual(refused.status, STATUS_NOOP)
        refusals = self.repo.list_transition_attempts(task.id)
        self.assertTrue(refusals)

        before = self.repo.get_task(task.id)
        events = self.repo.list_events(task.id)
        second = orchestrator.run_step(task.id, on_chunk=lambda text: None)
        self.assertEqual(second.status, STATUS_SUCCESS)

        packet = second.packet
        self.assertEqual(packet.stage, STAGE_EXECUTION)
        self.assertEqual(packet.action, ACTION_RUN_STEP)
        facts = packet.step_facts
        self.assertIsNotNone(facts)

        self.assertEqual(facts.stage, before.stage)
        self.assertEqual(facts.status, before.status)
        self.assertEqual(facts.current_step, before.current_step)
        self.assertEqual(facts.current_step_index, before.current_step_index)
        self.assertEqual(facts.expected_action_type, before.expected_action_type)
        self.assertEqual(facts.version, before.version)

        # The journal snapshot carries exactly the stored rows, in order.
        self.assertEqual(
            [(event.id, event.event_type) for event in facts.events],
            [(event.id, event.event_type) for event in events],
        )
        stored_types = [event.event_type for event in facts.events]
        for expected in (
            EVENT_TASK_CREATED,
            EVENT_PLAN_CREATED,
            EVENT_PLAN_ACCEPTED,
            EVENT_STEP_COMPLETED,
        ):
            self.assertIn(expected, stored_types)

        # The refusal audit snapshot carries the audited refusal.
        self.assertEqual(
            [(item.id, item.action, item.reason) for item in facts.refusals],
            [(item.id, item.action, item.reason) for item in refusals],
        )

        # The rendered task_facts block reflects the same storage rows.
        content = packet.block(BLOCK_TASK_FACTS).content
        for event in events:
            self.assertIn(f"id {event.id}: {event.event_type}", content)
        for item in refusals:
            self.assertIn(f"id {item.id}: {item.action}", content)


class StepFactsLimitTest(OrchestratorTestCase):
    """The execution snapshot stays bounded and chronological."""

    def _execution_task(self, orchestrator):
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        return self.repo.get_task(task.id)

    def test_step_packet_keeps_the_last_events_in_chronological_order(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("Step one done"), stream_step("Step two done")]
        )
        task = self._execution_task(orchestrator)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)

        # Grow the append-only journal past the snapshot limit through the
        # repository's own API (no manual packet assembly).
        for _ in range(STEP_FACTS_EVENTS_LIMIT * 2):
            self.repo.append_event(
                task.id,
                EVENT_API_ERROR,
                payload={"kind": "provider_error", "message": "journal growth"},
            )
        all_events = self.repo.list_events(task.id)
        self.assertGreater(len(all_events), STEP_FACTS_EVENTS_LIMIT)

        second = orchestrator.run_step(task.id, on_chunk=lambda text: None)

        facts = second.packet.step_facts
        expected = all_events[-STEP_FACTS_EVENTS_LIMIT:]
        self.assertEqual(len(facts.events), STEP_FACTS_EVENTS_LIMIT)
        self.assertEqual(
            [event.id for event in facts.events], [event.id for event in expected]
        )
        # Chronological (ascending id) order, the newest last.
        ids = [event.id for event in facts.events]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids[-1], all_events[-1].id)
        self.assertNotEqual(ids[0], all_events[0].id)

    def test_step_packet_keeps_the_last_refusals(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("Step one done"), stream_step("Step two done")]
        )
        task = self._execution_task(orchestrator)

        for _ in range(STEP_FACTS_REFUSALS_LIMIT + 2):
            refused = orchestrator.run_validation(task.id)
            self.assertEqual(refused.status, STATUS_NOOP)
        all_attempts = self.repo.list_transition_attempts(task.id)
        self.assertGreater(len(all_attempts), STEP_FACTS_REFUSALS_LIMIT)

        second = orchestrator.run_step(task.id, on_chunk=lambda text: None)

        facts = second.packet.step_facts
        expected = all_attempts[-STEP_FACTS_REFUSALS_LIMIT:]
        self.assertEqual(len(facts.refusals), STEP_FACTS_REFUSALS_LIMIT)
        self.assertEqual(
            [(item.id, item.action, item.reason) for item in facts.refusals],
            [(item.id, item.action, item.reason) for item in expected],
        )
        ids = [item.id for item in facts.refusals]
        self.assertEqual(ids, sorted(ids))


class TransitionProbeTest(OrchestratorTestCase):
    """The guard probe runs the production handler without changing the task."""

    def _execution_task(self, orchestrator):
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        return self.repo.get_task(task.id)

    def test_probe_of_a_disallowed_action_writes_only_the_audit(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._execution_task(orchestrator)
        before = self.repo.get_task(task.id)
        events_before = event_types(self.repo, task.id)
        artifacts_before = self.repo.list_artifacts(task.id)
        attempts_before = self.repo.list_transition_attempts(task.id)
        calls = self.client.calls

        probe = orchestrator.probe_refused_transition(task.id)

        self.assertFalse(probe.allowed)
        self.assertEqual(probe.reason, REASON_PROGRESS_INCOMPLETE)
        self.assertIsNotNone(probe.audit_id)
        self.assertEqual(self.client.calls, calls)

        attempts = self.repo.list_transition_attempts(task.id)
        self.assertEqual(len(attempts), len(attempts_before) + 1)
        newest = attempts[-1]
        self.assertEqual(newest.id, probe.audit_id)
        self.assertEqual(newest.action, ACTION_FINISH_EXECUTION)
        self.assertEqual(newest.reason, REASON_PROGRESS_INCOMPLETE)

        after = self.repo.get_task(task.id)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.current_step, before.current_step)
        self.assertEqual(after.current_step_index, before.current_step_index)
        self.assertEqual(after.expected_action_type, before.expected_action_type)
        self.assertEqual(event_types(self.repo, task.id), events_before)
        self.assertEqual(self.repo.list_artifacts(task.id), artifacts_before)

    def test_probe_of_an_allowed_action_writes_nothing(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("one"),
                stream_step("two"),
            ]
        )
        task = self._execution_task(orchestrator)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        before = self.repo.get_task(task.id)
        events_before = event_types(self.repo, task.id)
        artifacts_before = self.repo.list_artifacts(task.id)
        attempts_before = self.repo.list_transition_attempts(task.id)
        calls = self.client.calls

        probe = orchestrator.probe_refused_transition(task.id)

        self.assertTrue(probe.allowed)
        self.assertIsNone(probe.audit_id)
        self.assertEqual(probe.reason, "")
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(
            self.repo.list_transition_attempts(task.id), attempts_before
        )
        self.assertEqual(event_types(self.repo, task.id), events_before)
        self.assertEqual(self.repo.list_artifacts(task.id), artifacts_before)
        after = self.repo.get_task(task.id)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.current_step_index, before.current_step_index)

    def test_probe_of_a_missing_task_returns_an_error(self):
        orchestrator = self.make_orchestrator()

        probe = orchestrator.probe_refused_transition(99999)

        self.assertFalse(probe.allowed)
        self.assertIsNotNone(probe.error)
        self.assertIsNone(probe.audit_id)
        self.assertEqual(self.client.calls, 0)


class ExecutionIncompatiblePlanTest(OrchestratorTestCase):
    """Day 15 iteration 2: impossible live plans are rejected at runtime."""

    def test_incompatible_plan_retries_with_feedback_then_succeeds(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(REPORTED_LIVE_PLAN),
                plan_response(CORRECTED_PLAN_EXAMPLE),
            ]
        )
        task = self.create_task(orchestrator).task

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(result.task.expected_action_type, EXPECTED_CONFIRM_PLAN)

        first_messages = self.client.payloads[0]["messages"]
        second_messages = self.client.payloads[1]["messages"]
        self.assertEqual(second_messages[: len(first_messages)], first_messages)
        self.assertEqual(len(second_messages), len(first_messages) + 1)
        feedback = second_messages[-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("execution", feedback["content"])
        self.assertIn("Event timeline", feedback["content"])
        self.assertIn("шаг 3", feedback["content"])
        # The first request never received the corrective feedback.
        self.assertNotIn(
            "Недопустимые шаги",
            "\n".join(message["content"] for message in first_messages),
        )

        plans = artifacts_of(self.repo, task.id, ARTIFACT_PLAN)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].content, CORRECTED_PLAN_EXAMPLE)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_PLAN_CREATED],
        )

    def test_incompatible_plan_twice_records_invalid_response(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(REPORTED_LIVE_PLAN),
                plan_response(REPORTED_LIVE_PLAN),
            ]
        )
        task = self.create_task(orchestrator).task
        before = self.repo.get_task(task.id)

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_PLAN), [])

        after = self.repo.get_task(task.id)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.expected_action_type, before.expected_action_type)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_API_ERROR],
        )
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["kind"], "invalid_response")
        self.assertEqual(payload["attempts"], 2)
        # The failed action stays retryable.
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))

    def test_loopback_plan_retries_with_feedback_then_succeeds(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(REPORTED_LOOPBACK_PLAN),
                plan_response(CORRECTED_PLAN_EXAMPLE),
            ]
        )
        task = self.create_task(orchestrator).task

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 2)

        feedback = self.client.payloads[1]["messages"][-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("Недопустимые шаги", feedback["content"])
        self.assertIn("Недопустимые критерии приёмки", feedback["content"])
        self.assertIn("шаг 3", feedback["content"])
        self.assertIn("критерий 1", feedback["content"])

        plans = artifacts_of(self.repo, task.id, ARTIFACT_PLAN)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].content, CORRECTED_PLAN_EXAMPLE)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_PLAN_CREATED],
        )

    def test_loopback_plan_twice_records_invalid_response(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(REPORTED_LOOPBACK_PLAN),
                plan_response(REPORTED_LOOPBACK_PLAN),
            ]
        )
        task = self.create_task(orchestrator).task
        before = self.repo.get_task(task.id)

        result = orchestrator.run_planning(task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "invalid_response")
        self.assertEqual(len(result.stage_result.attempts), 2)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(artifacts_of(self.repo, task.id, ARTIFACT_PLAN), [])

        after = self.repo.get_task(task.id)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.expected_action_type, before.expected_action_type)
        self.assertEqual(
            event_types(self.repo, task.id),
            [EVENT_TASK_CREATED, EVENT_API_ERROR],
        )
        self.assertIn(ACTION_RETRY, orchestrator.allowed_actions(task.id))


class ChatIsolationTest(OrchestratorTestCase):
    def test_task_calls_do_not_touch_history_memory_or_stats(self):
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "focus", "reporting", chat_id=self.chat_id
        )
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "language", "Russian")
        working_before = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )
        long_term_before = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        stats_before = self.store.get_chat_stats(self.chat_id)

        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("one"),
                stream_step("two"),
                validation_response(passed=True),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.finish_execution(task.id)
        orchestrator.run_validation(task.id)

        self.assertEqual(self.store.load_messages(self.chat_id), [])
        self.assertEqual(self.store.get_chat_stats(self.chat_id), stats_before)
        self.assertEqual(stats_before.turns_count, 0)
        self.assertEqual(
            self.store.get_usage(self.chat_id),
            {"input_tokens": None, "output_tokens": None},
        )
        self.assertEqual(
            self.store.list_memory_items(MEMORY_SCOPE_WORKING, chat_id=self.chat_id),
            working_before,
        )
        self.assertEqual(
            self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM), long_term_before
        )
        self.assertEqual(self.store.load_facts(self.chat_id), [])
        self.assertIsNone(self.store.load_summary(self.chat_id))


class UsageAccountingTest(OrchestratorTestCase):
    def test_usage_is_stored_in_events_and_aggregated(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(usage=make_usage(100, 40)),
                stream_step("one", usage=make_usage(120, 30)),
                stream_step("two", usage=make_usage(130, 35)),
                validation_response(passed=True, usage=make_usage(90, 25)),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.finish_execution(task.id)
        orchestrator.run_validation(task.id)

        usage = self.repo.get_task_usage(task.id)
        self.assertEqual(usage.calls, 4)
        self.assertEqual(usage.attempts, 4)
        self.assertEqual(usage.input_tokens, 100 + 120 + 130 + 90)
        self.assertEqual(usage.output_tokens, 40 + 30 + 35 + 25)
        self.assertIsNotNone(usage.cost_usd)
        self.assertEqual(usage.models, ("deepseek-flash",))
        self.assertIn("stop", usage.finish_reasons)

    def test_usage_is_present_in_the_plan_event_payload(self):
        orchestrator = self.make_orchestrator(
            [plan_response(usage=make_usage(100, 40))]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["usage"]["prompt_tokens"], 100)
        self.assertEqual(payload["usage"]["completion_tokens"], 40)
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(payload["finish_reason"], "stop")
        self.assertEqual(payload["model"], "deepseek-flash")

    def test_error_payload_has_usage_but_no_prompt_or_invariants(self):
        orchestrator = self.make_orchestrator(
            [
                text_response("not json", usage=make_usage(70, 12)),
                text_response("still not json", usage=make_usage(80, 15)),
            ]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        payload = self.repo.list_events(task.id)[-1].payload
        self.assertEqual(payload["usage"]["prompt_tokens"], 150)
        self.assertEqual(payload["usage"]["completion_tokens"], 27)
        self.assertEqual(payload["attempts"], 2)
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("You are a helpful assistant.", dumped)
        self.assertNotIn("Ты планируешь выполнение задачи", dumped)


class CanApplyTest(OrchestratorTestCase):
    def test_allowed_actions_match_can_apply_plus_ui_actions(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("one"), stream_step("two"),
             validation_response(passed=True)]
        )
        task = self.create_task(orchestrator).task
        self.assertEqual(
            orchestrator.allowed_actions(task.id),
            (
                ACTION_RUN_PLANNING,
                ACTION_PAUSE,
                ACTION_BLOCK,
                ACTION_CANCEL,
                ACTION_NEW_TASK,
                ACTION_OPEN_DIAGNOSTICS,
            ),
        )

        orchestrator.run_planning(task.id)
        self.assertEqual(
            orchestrator.allowed_actions(task.id),
            (
                ACTION_ACCEPT_PLAN,
                ACTION_REJECT_PLAN,
                ACTION_PAUSE,
                ACTION_BLOCK,
                ACTION_CANCEL,
                ACTION_NEW_TASK,
                ACTION_OPEN_DIAGNOSTICS,
            ),
        )

        orchestrator.accept_plan(task.id)
        orchestrator.pause(task.id)
        self.assertEqual(
            orchestrator.allowed_actions(task.id),
            (
                ACTION_RESUME,
                ACTION_BLOCK,
                ACTION_CANCEL,
                ACTION_NEW_TASK,
                ACTION_OPEN_DIAGNOSTICS,
            ),
        )
        orchestrator.resume(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        self.assertIn(ACTION_FINISH_EXECUTION, orchestrator.allowed_actions(task.id))
        orchestrator.finish_execution(task.id)
        self.assertIn(ACTION_RUN_VALIDATION, orchestrator.allowed_actions(task.id))
        orchestrator.run_validation(task.id)
        self.assertEqual(
            orchestrator.allowed_actions(task.id), (ACTION_OPEN_RESULT, ACTION_NEW_TASK)
        )


class BlockUnblockCancelTest(OrchestratorTestCase):
    def test_block_unblock_keeps_the_stage_and_recomputes_the_action(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)

        blocked = orchestrator.block(task.id, "Need the finance data", "Provide the data")
        self.assertEqual(blocked.status, STATUS_SUCCESS)
        self.assertEqual(blocked.task.status, STATUS_BLOCKED)
        self.assertEqual(blocked.task.stage, STAGE_EXECUTION)
        self.assertEqual(blocked.task.expected_action_type, EXPECTED_USER_ACTION)
        self.assertEqual(blocked.task.pause_reason, "Need the finance data")
        self.assertEqual(
            orchestrator.allowed_actions(task.id),
            (ACTION_UNBLOCK, ACTION_CANCEL, ACTION_NEW_TASK, ACTION_OPEN_DIAGNOSTICS),
        )
        self.assertEqual(
            event_types(self.repo, task.id)[-1:], [EVENT_BLOCK]
        )

        unblocked = orchestrator.unblock(task.id)
        self.assertEqual(unblocked.status, STATUS_SUCCESS)
        self.assertEqual(unblocked.task.status, STATUS_ACTIVE)
        self.assertEqual(unblocked.task.stage, STAGE_EXECUTION)
        self.assertEqual(unblocked.task.pause_reason, "")
        self.assertEqual(unblocked.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(unblocked.task.current_step_index, 1)
        self.assertEqual(event_types(self.repo, task.id)[-1], EVENT_UNBLOCK)

    def test_cancel_requires_confirmation_and_keeps_the_journal(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)

        rejected = orchestrator.cancel(task.id, confirmed=False)
        self.assertEqual(rejected.status, STATUS_NOOP)
        self.assertNotIn(EVENT_CANCEL, event_types(self.repo, task.id))

        cancelled = orchestrator.cancel(task.id, confirmed=True)
        self.assertEqual(cancelled.status, STATUS_SUCCESS)
        self.assertEqual(cancelled.task.status, STATUS_CANCELLED)
        self.assertEqual(cancelled.task.stage, STAGE_PLANNING)
        self.assertEqual(cancelled.task.expected_action_type, EXPECTED_NONE)
        self.assertIn(EVENT_CANCEL, event_types(self.repo, task.id))
        self.assertEqual(len(artifacts_of(self.repo, task.id, ARTIFACT_PLAN)), 1)
        self.assertEqual(
            orchestrator.allowed_actions(task.id), (ACTION_NEW_TASK,)
        )
        self.assertEqual(orchestrator.cancel(task.id, confirmed=True).status, STATUS_NOOP)


class SelectionTest(OrchestratorTestCase):
    def test_select_task_and_current_task(self):
        orchestrator = self.make_orchestrator()
        first = self.create_task(orchestrator, title="First").task
        second = self.create_task(orchestrator, title="Second").task
        self.assertEqual(orchestrator.current_task(self.chat_id).id, second.id)

        selected = orchestrator.select_task(self.chat_id, first.id)
        self.assertEqual(selected.status, STATUS_SUCCESS)
        self.assertEqual(orchestrator.current_task(self.chat_id).id, first.id)

    def test_select_task_of_another_chat_is_rejected(self):
        other_chat = self.store.create_chat(AgentConfig())
        orchestrator = self.make_orchestrator()
        task = self.create_task(orchestrator).task

        result = orchestrator.select_task(other_chat, task.id)

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, ERROR_NOT_FOUND)

    def test_create_task_validates_title_and_goal(self):
        orchestrator = self.make_orchestrator()
        empty_title = orchestrator.create_task(self.chat_id, "   ", "Goal")
        self.assertEqual(empty_title.status, STATUS_ERROR)
        self.assertEqual(empty_title.error_kind, ERROR_INVALID_INPUT)
        empty_goal = orchestrator.create_task(self.chat_id, "Title", "")
        self.assertEqual(empty_goal.status, STATUS_ERROR)
        self.assertEqual(empty_goal.error_kind, ERROR_INVALID_INPUT)

    def test_create_task_for_a_missing_chat_is_rejected(self):
        orchestrator = self.make_orchestrator()
        result = orchestrator.create_task(999, "Title", "Goal")
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, ERROR_NOT_FOUND)


class PreviewTest(OrchestratorTestCase):
    def test_preview_builds_the_packet_without_any_provider_call(self):
        orchestrator = self.make_orchestrator(
            [text_response("should never be called")]
        )
        task = self.create_task(orchestrator).task

        packet = orchestrator.preview_packet(task.id)

        self.assertEqual(self.client.calls, 0)
        self.assertEqual(packet.stage, STAGE_PLANNING)
        self.assertEqual(packet.action, ACTION_RUN_PLANNING)
        self.assertTrue(packet.messages)
        names = [block.name for block in packet.blocks]
        self.assertIn("task_snapshot", names)
        self.assertIn("action", names)
        self.assertEqual(packet.messages[-1]["role"], "user")
        self.assertGreater(packet.total_tokens, 0)
        self.assertIsNotNone(packet.total_cost_usd)

    def test_preview_for_a_prepared_task_targets_validation(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("one"), stream_step("two")]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        calls = self.client.calls

        packet = orchestrator.preview_packet(task.id)

        self.assertEqual(self.client.calls, calls)
        self.assertEqual(packet.stage, STAGE_VALIDATION)
        self.assertEqual(packet.action, ACTION_RUN_VALIDATION)

    def test_preview_of_a_terminal_task_has_no_model_action(self):
        orchestrator = self.make_orchestrator(
            [plan_response(), stream_step("one"), stream_step("two"),
             validation_response(passed=True)]
        )
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.finish_execution(task.id)
        orchestrator.run_validation(task.id)

        packet = orchestrator.preview_packet(task.id)

        self.assertEqual(packet.action, ACTION_NONE)
        self.assertEqual(packet.stage, STAGE_DONE)
        self.assertEqual(
            [block.name for block in packet.blocks], ["task_snapshot"]
        )
        self.assertEqual(self.client.calls, 4)

    def test_preview_of_a_missing_task_is_empty(self):
        orchestrator = self.make_orchestrator()
        packet = orchestrator.preview_packet(4242)
        self.assertEqual(packet.blocks, [])
        self.assertEqual(packet.messages, [])
        self.assertEqual(packet.total_tokens, 0)


class SettingsChangeTest(OrchestratorTestCase):
    def test_chat_settings_changes_affect_only_the_next_call(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self.create_task(orchestrator).task
        orchestrator.run_planning(task.id)
        version = self.repo.get_task(task.id).version
        artifact_count = len(self.repo.list_artifacts(task.id))

        self.store.save_config(
            self.chat_id,
            AgentConfig(
                system_prompt="Brand new system prompt",
                invariants="Never use jargon",
                context_strategy="sliding",
            ),
        )

        packet = orchestrator.preview_packet(task.id)

        self.assertIn(
            "Never use jargon", packet.block(BLOCK_INVARIANTS).content
        )
        self.assertIn(INVARIANTS_BLOCK_TITLE, packet.block(BLOCK_INVARIANTS).content)
        self.assertEqual(packet.block("system_prompt").content, "Brand new system prompt")
        # The already stored task and artifacts are untouched.
        self.assertEqual(self.repo.get_task(task.id).version, version)
        self.assertEqual(len(self.repo.list_artifacts(task.id)), artifact_count)


class TransitionControlTest(OrchestratorTestCase):
    """Day 15: refusals are explained, audited and never call the provider."""

    def _planning_task(self, orchestrator):
        return self.create_task(orchestrator).task

    def _execution_task(self, orchestrator):
        task = self._planning_task(orchestrator)
        orchestrator.run_planning(task.id)
        orchestrator.accept_plan(task.id)
        return self.repo.get_task(task.id)

    def _done_task(self, orchestrator):
        task = self._execution_task(orchestrator)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        orchestrator.finish_execution(task.id)
        orchestrator.run_validation(task.id)
        return self.repo.get_task(task.id)

    def _cancelled_task(self, orchestrator):
        task = self._planning_task(orchestrator)
        orchestrator.run_planning(task.id)
        orchestrator.cancel(task.id, confirmed=True)
        return self.repo.get_task(task.id)

    def _invoke_domain_actions(self, orchestrator, task):
        return [
            orchestrator.run_planning(task.id),
            orchestrator.run_step(task.id),
            orchestrator.run_validation(task.id),
            orchestrator.accept_plan(task.id),
            orchestrator.reject_plan(task.id),
            orchestrator.finish_execution(task.id),
            orchestrator.pause(task.id),
            orchestrator.resume(task.id),
            orchestrator.block(task.id, "reason", "provide"),
            orchestrator.unblock(task.id),
            orchestrator.cancel(task.id, confirmed=True),
            orchestrator.retry(task.id),
        ]

    def test_run_step_and_validation_before_approval_are_refused(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._planning_task(orchestrator)
        before = self.repo.get_task(task.id)
        artifacts_before = self.repo.list_artifacts(task.id)

        step = orchestrator.run_step(task.id)
        validation = orchestrator.run_validation(task.id)

        for result in (step, validation):
            self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, 0)
        after = self.repo.get_task(task.id)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.version, before.version)
        self.assertEqual(self.repo.list_artifacts(task.id), artifacts_before)
        attempts = self.repo.list_transition_attempts(task.id)
        self.assertEqual(
            [item.action for item in attempts],
            [ACTION_RUN_STEP, ACTION_RUN_VALIDATION],
        )
        self.assertEqual(
            [item.reason for item in attempts],
            [REASON_EXPECTED_ACTION_MISMATCH, REASON_EXPECTED_ACTION_MISMATCH],
        )
        self.assertEqual(attempts[0].from_stage, STAGE_PLANNING)
        self.assertEqual(attempts[0].from_status, STATUS_ACTIVE)

    def test_validation_outside_the_validation_stage_is_refused(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._planning_task(orchestrator)

        result = orchestrator.run_validation(task.id)

        self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.repo.get_task(task.id).stage, STAGE_PLANNING)
        attempt = self.repo.list_transition_attempts(task.id)[-1]
        self.assertEqual(attempt.action, ACTION_RUN_VALIDATION)
        self.assertEqual(attempt.reason, REASON_EXPECTED_ACTION_MISMATCH)

    def test_finish_execution_with_incomplete_steps_is_refused(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._execution_task(orchestrator)
        calls = self.client.calls
        version = task.version

        result = orchestrator.finish_execution(task.id)

        self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.repo.get_task(task.id).version, version)
        attempt = self.repo.list_transition_attempts(task.id)[-1]
        self.assertEqual(attempt.action, ACTION_FINISH_EXECUTION)
        self.assertEqual(attempt.reason, REASON_PROGRESS_INCOMPLETE)

    def test_commit_payload_refusal_is_audited_without_touching_the_task(self):
        # An otherwise allowed action whose payload the FSM rejects on commit:
        # BLOCK requires a reason, so an empty one reaches _commit, fails there
        # and must leave the task and the journal untouched.
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._planning_task(orchestrator)
        before = self.repo.get_task(task.id)
        events_before = event_types(self.repo, task.id)
        artifacts_before = self.repo.list_artifacts(task.id)

        result = orchestrator.block(task.id, "", "Provide the data")

        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(result.error_kind, ERROR_INVALID_TRANSITION)
        after = self.repo.get_task(task.id)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.stage, before.stage)
        self.assertEqual(after.status, before.status)
        self.assertEqual(event_types(self.repo, task.id), events_before)
        self.assertEqual(self.repo.list_artifacts(task.id), artifacts_before)
        attempt = self.repo.list_transition_attempts(task.id)[-1]
        self.assertEqual(attempt.action, ACTION_BLOCK)
        self.assertEqual(attempt.reason, REASON_INVALID_PAYLOAD)

    def test_retry_without_api_error_records_the_reason(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._planning_task(orchestrator)

        result = orchestrator.retry(task.id)

        self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, 0)
        attempt = self.repo.list_transition_attempts(task.id)[-1]
        self.assertEqual(attempt.action, ACTION_RETRY)
        self.assertEqual(attempt.reason, REASON_RETRY_REQUIRES_API_ERROR)

    def test_done_task_refuses_every_domain_action(self):
        orchestrator = self.make_orchestrator(
            [
                plan_response(),
                stream_step("one"),
                stream_step("two"),
                validation_response(passed=True),
            ]
        )
        task = self._done_task(orchestrator)
        calls = self.client.calls
        version = task.version

        results = self._invoke_domain_actions(orchestrator, task)

        for result in results:
            self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.repo.get_task(task.id).version, version)
        attempts = self.repo.list_transition_attempts(task.id)
        self.assertEqual(len(attempts), len(results))
        self.assertIn(REASON_TERMINAL, {item.reason for item in attempts})

    def test_cancelled_task_refuses_every_domain_action(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._cancelled_task(orchestrator)
        calls = self.client.calls
        version = task.version

        results = self._invoke_domain_actions(orchestrator, task)

        for result in results:
            self.assertEqual(result.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.repo.get_task(task.id).version, version)
        attempts = self.repo.list_transition_attempts(task.id)
        self.assertEqual(len(attempts), len(results))
        self.assertIn(REASON_TERMINAL, {item.reason for item in attempts})

    def test_pause_restart_resume_with_fresh_instances_has_no_duplicates(self):
        orchestrator = self.make_orchestrator([plan_response(), stream_step("one")])
        task = self._execution_task(orchestrator)
        orchestrator.run_step(task.id, on_chunk=lambda text: None)
        paused = orchestrator.pause(task.id, reason="restart")
        self.assertEqual(paused.status, STATUS_SUCCESS)
        version_before = paused.task.version

        store = ChatStore(self.path)
        repo = TaskRepository(store.db_path)
        client = FakeClient(script=[stream_step("two")])
        restarted = TaskOrchestrator(store, repository=repo, client=client)

        resumed = restarted.resume(task.id)

        self.assertEqual(resumed.status, STATUS_SUCCESS)
        self.assertEqual(resumed.task.id, task.id)
        self.assertEqual(resumed.task.stage, STAGE_EXECUTION)
        self.assertEqual(resumed.task.current_step_index, 2)
        self.assertEqual(resumed.task.version, version_before + 1)
        self.assertEqual(resumed.task.pause_reason, "")
        events = [event.event_type for event in repo.list_events(task.id)]
        self.assertEqual(events.count(EVENT_PAUSE), 1)
        self.assertEqual(events.count(EVENT_RESUME), 1)

    def test_transition_guard_report_is_read_only(self):
        orchestrator = self.make_orchestrator([plan_response()])
        task = self._planning_task(orchestrator)
        calls = self.client.calls

        report = orchestrator.transition_guard_report(task.id)

        self.assertEqual(self.client.calls, calls)
        self.assertEqual(report.task.id, task.id)
        self.assertEqual(report.allowed, can_apply(task))
        self.assertEqual(
            [decision.action for decision in report.decisions],
            list(DOMAIN_ACTIONS),
        )
        self.assertEqual(report.refusals, [])

        orchestrator.run_step(task.id)
        report = orchestrator.transition_guard_report(task.id)

        self.assertEqual(self.client.calls, calls)
        self.assertEqual(len(report.refusals), 1)
        self.assertEqual(
            report.refusals[0].reason, REASON_EXPECTED_ACTION_MISMATCH
        )

    def test_transition_guard_report_for_a_missing_task(self):
        orchestrator = self.make_orchestrator()

        report = orchestrator.transition_guard_report(99999)

        self.assertIsNone(report.task)
        self.assertEqual(report.allowed, ())
        self.assertEqual(report.decisions, [])
        self.assertEqual(report.refusals, [])


if __name__ == "__main__":
    unittest.main()
