"""Unit tests for the pure task-state domain and the stage prompt contracts.

No I/O, no network and no real ``.env``: the FSM, the step-progress rule, the
formatters and the strict parsers are exercised directly.
"""

import json
import unittest
from types import SimpleNamespace

from task_demo import (
    CORRECTED_PLAN_EXAMPLE,
    REPORTED_LOOPBACK_PLAN,
    REPORTED_LIVE_PLAN,
)
from task_prompts import (
    REASON_NONEXISTENT_TRANSITION,
    TASK_PLAN_MAX_TOKENS,
    TASK_PLAN_RETRY_MAX_TOKENS,
    TASK_STEP_MAX_TOKENS,
    TASK_STEP_RETRY_MAX_TOKENS,
    TASK_VALIDATION_MAX_TOKENS,
    TASK_VALIDATION_RETRY_MAX_TOKENS,
    ExecutionIncompatiblePlanError,
    PlanStepViolation,
    build_plan_messages,
    build_step_messages,
    build_validation_messages,
    format_final_result_markdown,
    format_specification_markdown,
    is_truncated,
    parse_plan_response,
    parse_validation_response,
    plan_retry_feedback,
    plan_step_violations,
    plan_violations,
)
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_BLOCK,
    ACTION_CANCEL,
    ACTION_FINISH_EXECUTION,
    ACTION_LABELS,
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
    BADGE_BLOCKED,
    BADGE_CANCELLED,
    BADGE_COMPLETED,
    BADGE_PAUSED,
    BADGE_RUNNING,
    DOMAIN_ACTIONS,
    ERROR_MESSAGE_MAX_LENGTH,
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
    InvalidTransitionError,
    REFUSAL_REASONS,
    REASON_ALLOWED,
    REASON_BLOCKED,
    REASON_CONFIRMATION_REQUIRED,
    REASON_EXPECTED_ACTION_MISMATCH,
    REASON_NOT_ALLOWED,
    REASON_PAUSED,
    REASON_PROGRESS_INCOMPLETE,
    REASON_RETRY_REQUIRES_API_ERROR,
    REASON_TASK_NOT_FOUND,
    REASON_TEXTS,
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
    StepProgress,
    Task,
    TaskArtifact,
    TaskStateMachine,
    TransitionDecision,
    TransitionPayloadError,
    UI_ACTIONS,
    apply_transition,
    badge_for,
    can_apply,
    compute_step_progress,
    creation_transition,
    explain_transition,
    format_allowed_actions,
    format_defects_block,
    format_refusal,
    format_snapshot_block,
    format_task_usage_line,
    normalize_verdict,
    ui_actions,
    validate_error_message,
)


def make_plan(count=2):
    return {
        "summary": "Краткий план",
        "acceptance_criteria": ["Первый критерий"],
        "steps": [
            {"index": index, "title": f"Step {index}", "description": f"Description {index}"}
            for index in range(1, count + 1)
        ],
    }


def make_task(**overrides):
    values = dict(
        id=7,
        chat_id=1,
        workflow_profile_id=1,
        workflow_name="default",
        title="Task title",
        goal="Task goal",
        stage=STAGE_PLANNING,
        status=STATUS_ACTIVE,
        current_step="Planning",
        current_step_index=None,
        expected_action_type=EXPECTED_RUN_PLANNING,
        expected_action_text="Run planning",
        pause_reason="",
        version=1,
    )
    values.update(overrides)
    return Task(**values)


def make_execution_task(step_index=1, version=3):
    return make_task(
        stage=STAGE_EXECUTION,
        status=STATUS_ACTIVE,
        current_step=f"Step {step_index}",
        current_step_index=step_index,
        expected_action_type=EXPECTED_RUN_STEP,
        expected_action_text="Run the current step",
        version=version,
    )


def make_validation_task(version=5):
    return make_task(
        stage=STAGE_VALIDATION,
        status=STATUS_ACTIVE,
        current_step="Validation",
        current_step_index=None,
        expected_action_type=EXPECTED_RUN_VALIDATION,
        expected_action_text="Run validation",
        version=version,
    )


def all_completed(count=2, awaiting=()):
    return [
        StepProgress(
            step_index=index,
            title=f"Step {index}",
            completed=index not in awaiting,
            awaiting_rework=index in awaiting,
        )
        for index in range(1, count + 1)
    ]


def artifacts_from(entries):
    """Build artifacts with sequential ids from ``(kind, content)`` pairs."""
    return [
        TaskArtifact(id=index, task_id=1, kind=kind, content=content)
        for index, (kind, content) in enumerate(entries, start=1)
    ]


def apply(task, event, **kwargs):
    return apply_transition(task, event, **kwargs)


class CreationTransitionTest(unittest.TestCase):
    def test_creates_planning_active_task_ready_for_planning(self):
        result = creation_transition(1, "  Task  ", "  Goal  ")

        task = result.task
        self.assertEqual(task.stage, STAGE_PLANNING)
        self.assertEqual(task.status, STATUS_ACTIVE)
        self.assertEqual(task.version, 1)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(task.expected_action_text, "Run planning")
        self.assertEqual(task.current_step, "Planning")
        self.assertIsNone(task.current_step_index)
        self.assertEqual(task.title, "Task")
        self.assertEqual(task.goal, "Goal")
        self.assertIsNone(task.id)

    def test_creation_event_has_no_idempotency_key(self):
        result = creation_transition(1, "Task", "Goal")
        self.assertEqual(result.event.event_type, EVENT_TASK_CREATED)
        self.assertIsNone(result.event.idempotency_key)
        self.assertIsNone(result.idempotency_key)
        self.assertIsNone(result.event.from_stage)
        self.assertEqual(result.event.to_stage, STAGE_PLANNING)
        self.assertEqual(result.event.to_status, STATUS_ACTIVE)

    def test_brief_artifact_only_when_non_empty(self):
        with_brief = creation_transition(1, "Task", "Goal", task_brief="  Brief  ")
        self.assertEqual([a.kind for a in with_brief.artifacts], [ARTIFACT_TASK_BRIEF])
        self.assertEqual(with_brief.artifacts[0].content, {"text": "Brief"})

        without_brief = creation_transition(1, "Task", "Goal", task_brief="   ")
        self.assertEqual(without_brief.artifacts, [])

    def test_requires_title_and_goal(self):
        with self.assertRaises(ValueError):
            creation_transition(1, "   ", "Goal")
        with self.assertRaises(ValueError):
            creation_transition(1, "Task", "")


class PlanTransitionTest(unittest.TestCase):
    def test_plan_created_moves_to_confirm_plan(self):
        task = make_task()
        result = apply(
            task,
            EVENT_PLAN_CREATED,
            payload={"plan": make_plan(3), "task_brief": "Brief"},
        )

        updated = result.task
        self.assertEqual(updated.stage, STAGE_PLANNING)
        self.assertEqual(updated.status, STATUS_ACTIVE)
        self.assertEqual(updated.version, task.version + 1)
        self.assertEqual(updated.expected_action_type, EXPECTED_CONFIRM_PLAN)
        self.assertEqual(updated.expected_action_text, "Accept or reject the plan")
        self.assertEqual(updated.current_step, "Step 1")
        self.assertEqual(updated.current_step_index, 1)

        self.assertEqual(
            [artifact.kind for artifact in result.artifacts],
            [ARTIFACT_SPECIFICATION, ARTIFACT_PLAN],
        )
        specification, plan = result.artifacts
        self.assertIn("## Goal", specification.content["markdown"])
        self.assertIn("Brief", specification.content["markdown"])
        self.assertEqual(plan.content["steps"][2]["title"], "Step 3")
        self.assertEqual(plan.content["acceptance_criteria"], ["Первый критерий"])

        self.assertEqual(result.event.event_type, EVENT_PLAN_CREATED)
        self.assertEqual(result.event.from_stage, STAGE_PLANNING)
        self.assertEqual(result.event.to_stage, STAGE_PLANNING)
        self.assertEqual(result.idempotency_key, "run_planning:7:1")
        self.assertEqual(result.event.idempotency_key, "run_planning:7:1")
        self.assertEqual(result.event.payload["step_count"], 3)

    def test_plan_created_does_not_mutate_the_input_task(self):
        task = make_task()
        apply(task, EVENT_PLAN_CREATED, payload={"plan": make_plan()})
        self.assertEqual(task.version, 1)
        self.assertEqual(task.expected_action_type, EXPECTED_RUN_PLANNING)

    def test_plan_created_uses_provided_specification_markdown(self):
        result = apply(
            make_task(),
            EVENT_PLAN_CREATED,
            payload={"plan": make_plan(), "specification_markdown": "# Custom"},
        )
        self.assertEqual(result.artifacts[0].content, {"markdown": "# Custom"})

    def test_plan_created_rejected_when_confirmation_is_expected(self):
        task = make_task(expected_action_type=EXPECTED_CONFIRM_PLAN)
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_PLAN_CREATED, payload={"plan": make_plan()})

    def test_plan_created_rejected_outside_planning(self):
        task = make_task(stage=STAGE_EXECUTION)
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_PLAN_CREATED, payload={"plan": make_plan()})

    def test_plan_created_validates_payload(self):
        task = make_task()
        invalid_plans = [
            None,
            [],
            {"summary": "s", "acceptance_criteria": ["c"], "steps": []},
            {"summary": "s", "acceptance_criteria": ["c"], "steps": [{"index": 2, "title": "t"}]},
            {"summary": "s", "acceptance_criteria": ["c"], "steps": [{"index": 1, "title": "  "}]},
            {"summary": "s", "steps": [{"index": 1, "title": "t"}]},
            {"summary": "s", "acceptance_criteria": [], "steps": [{"index": 1, "title": "t"}]},
            {"summary": "s", "acceptance_criteria": ["  "], "steps": [{"index": 1, "title": "t"}]},
        ]
        for plan in invalid_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(TransitionPayloadError):
                    apply(task, EVENT_PLAN_CREATED, payload={"plan": plan})

    def test_plan_rejected_returns_to_run_planning(self):
        task = make_task(expected_action_type=EXPECTED_CONFIRM_PLAN, version=4)
        result = apply(task, EVENT_PLAN_REJECTED, payload={})

        self.assertEqual(result.task.stage, STAGE_PLANNING)
        self.assertEqual(result.task.version, 5)
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(result.task.expected_action_text, "Run planning")
        self.assertEqual(result.task.current_step, "Planning")
        self.assertIsNone(result.task.current_step_index)
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.idempotency_key, "reject_plan:7:4")

    def test_plan_rejected_requires_confirmation_expected(self):
        with self.assertRaises(InvalidTransitionError):
            apply(make_task(), EVENT_PLAN_REJECTED, payload={})

    def test_plan_accepted_moves_to_first_step(self):
        task = make_task(expected_action_type=EXPECTED_CONFIRM_PLAN, version=2)
        result = apply(
            task,
            EVENT_PLAN_ACCEPTED,
            payload={},
            progress=[
                StepProgress(1, "Step 1", completed=False),
                StepProgress(2, "Step 2", completed=False),
            ],
        )

        self.assertEqual(result.task.stage, STAGE_EXECUTION)
        self.assertEqual(result.task.status, STATUS_ACTIVE)
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(result.task.expected_action_text, "Run the current step")
        self.assertEqual(result.task.current_step, "Step 1")
        self.assertEqual(result.task.current_step_index, 1)
        self.assertEqual(result.idempotency_key, "accept_plan:7:2")

    def test_plan_accepted_accepts_plan_from_payload(self):
        task = make_task(expected_action_type=EXPECTED_CONFIRM_PLAN)
        result = apply(task, EVENT_PLAN_ACCEPTED, payload={"plan": make_plan(4)})
        self.assertEqual(result.task.current_step_index, 1)

    def test_plan_accepted_requires_confirmation_expected(self):
        with self.assertRaises(InvalidTransitionError):
            apply(make_task(), EVENT_PLAN_ACCEPTED, payload={"plan": make_plan()})

    def test_plan_accepted_requires_progress_or_plan(self):
        task = make_task(expected_action_type=EXPECTED_CONFIRM_PLAN)
        with self.assertRaises(TransitionPayloadError):
            apply(task, EVENT_PLAN_ACCEPTED, payload={})

    def test_replan_writes_a_new_plan_revision(self):
        task = make_task()
        first = apply(task, EVENT_PLAN_CREATED, payload={"plan": make_plan()})
        rejected = apply(first.task, EVENT_PLAN_REJECTED, payload={})
        second = apply(rejected.task, EVENT_PLAN_CREATED, payload={"plan": make_plan(3)})

        self.assertEqual(
            [artifact.kind for artifact in second.artifacts],
            [ARTIFACT_SPECIFICATION, ARTIFACT_PLAN],
        )
        # Revisions are assigned by the repository, so drafts never carry one.
        self.assertTrue(all(artifact.revision is None for artifact in second.artifacts))
        self.assertEqual(second.task.current_step_index, 1)


class StepTransitionTest(unittest.TestCase):
    def test_step_completed_advances_to_the_next_step(self):
        task = make_execution_task(1)
        progress = [
            StepProgress(1, "Step 1"),
            StepProgress(2, "Step 2"),
            StepProgress(3, "Step 3"),
        ]
        result = apply(
            task,
            EVENT_STEP_COMPLETED,
            payload={"text": "Result one"},
            progress=progress,
        )

        updated = result.task
        self.assertEqual(updated.stage, STAGE_EXECUTION)
        self.assertEqual(updated.version, task.version + 1)
        self.assertEqual(updated.current_step_index, 2)
        self.assertEqual(updated.current_step, "Step 2")
        self.assertEqual(updated.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(result.artifacts[0].kind, ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(
            result.artifacts[0].content,
            {"step_index": 1, "round": 1, "text": "Result one"},
        )
        self.assertEqual(result.event.payload["step_index"], 1)
        self.assertEqual(result.event.payload["round"], 1)
        self.assertEqual(result.idempotency_key, "run_step:7:3")

    def test_step_completed_expects_finish_execution_after_the_last_step(self):
        task = make_execution_task(3)
        progress = [
            StepProgress(1, "Step 1", completed=True),
            StepProgress(2, "Step 2", completed=True),
            StepProgress(3, "Step 3", completed=False),
        ]
        result = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "Result"}, progress=progress
        )

        self.assertEqual(result.task.expected_action_type, EXPECTED_FINISH_EXECUTION)
        self.assertEqual(result.task.expected_action_text, "Finish execution")
        self.assertEqual(result.task.current_step, "Execution")
        self.assertIsNone(result.task.current_step_index)

    def test_step_completed_serves_rework_first(self):
        task = make_execution_task(2)
        progress = [
            StepProgress(1, "Step 1", completed=True),
            StepProgress(2, "Step 2", awaiting_rework=True, defects=("d2",)),
            StepProgress(3, "Step 3"),
            StepProgress(4, "Step 4", awaiting_rework=True, defects=("d4",)),
        ]
        result = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "Result"}, progress=progress
        )
        self.assertEqual(result.task.current_step_index, 4)
        self.assertEqual(result.task.current_step, "Step 4")

    def test_step_completed_uses_the_round_of_the_step(self):
        task = make_execution_task(2)
        progress = [
            StepProgress(1, "Step 1", completed=True, round=1),
            StepProgress(2, "Step 2", awaiting_rework=True, round=2),
        ]
        result = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "Result"}, progress=progress
        )
        self.assertEqual(result.artifacts[0].content["round"], 3)

    def test_step_completed_rejected_when_step_is_already_completed(self):
        task = make_execution_task(1)
        progress = [StepProgress(1, "Step 1", completed=True), StepProgress(2, "Step 2")]
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_STEP_COMPLETED, payload={"text": "r"}, progress=progress)

    def test_step_completed_rejected_without_current_index(self):
        task = make_execution_task(1)
        task = task.evolve(current_step_index=None, current_step="Execution")
        with self.assertRaises(InvalidTransitionError):
            apply(
                task,
                EVENT_STEP_COMPLETED,
                payload={"text": "r"},
                progress=[StepProgress(1, "Step 1")],
            )

    def test_step_completed_rejected_on_payload_step_mismatch(self):
        task = make_execution_task(1)
        with self.assertRaises(InvalidTransitionError):
            apply(
                task,
                EVENT_STEP_COMPLETED,
                payload={"text": "r", "step_index": 2},
                progress=[StepProgress(1, "Step 1"), StepProgress(2, "Step 2")],
            )

    def test_step_completed_rejects_unknown_step(self):
        task = make_execution_task(5)
        with self.assertRaises(InvalidTransitionError):
            apply(
                task,
                EVENT_STEP_COMPLETED,
                payload={"text": "r"},
                progress=[StepProgress(1, "Step 1")],
            )

    def test_step_completed_requires_the_result_text(self):
        task = make_execution_task(1)
        progress = [StepProgress(1, "Step 1")]
        for payload in ({}, {"text": ""}, {"text": "   "}, {"text": None}):
            with self.subTest(payload=payload):
                with self.assertRaises(TransitionPayloadError):
                    apply(task, EVENT_STEP_COMPLETED, payload=payload, progress=progress)

    def test_step_completed_requires_run_step_expectation(self):
        task = make_execution_task(1).evolve(
            expected_action_type=EXPECTED_FINISH_EXECUTION
        )
        with self.assertRaises(InvalidTransitionError):
            apply(
                task,
                EVENT_STEP_COMPLETED,
                payload={"text": "r"},
                progress=[StepProgress(1, "Step 1")],
            )

    def test_step_completed_requires_progress(self):
        with self.assertRaises(TransitionPayloadError):
            apply(make_execution_task(1), EVENT_STEP_COMPLETED, payload={"text": "r"})


class ExecutionFinishedTest(unittest.TestCase):
    def test_moves_to_validation_when_every_step_is_completed(self):
        task = make_execution_task(2).evolve(
            expected_action_type=EXPECTED_FINISH_EXECUTION,
            expected_action_text="Finish execution",
            current_step="Execution",
            current_step_index=None,
            version=6,
        )
        result = apply(
            task,
            EVENT_EXECUTION_FINISHED,
            payload={},
            progress=all_completed(3),
        )

        self.assertEqual(result.task.stage, STAGE_VALIDATION)
        self.assertEqual(result.task.status, STATUS_ACTIVE)
        self.assertEqual(result.task.version, 7)
        self.assertEqual(result.task.current_step, "Validation")
        self.assertIsNone(result.task.current_step_index)
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_VALIDATION)
        self.assertEqual(result.task.expected_action_text, "Run validation")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.idempotency_key, "finish_execution:7:6")

    def test_rejected_when_a_step_is_incomplete(self):
        task = make_execution_task(2).evolve(
            expected_action_type=EXPECTED_FINISH_EXECUTION
        )
        progress = all_completed(3) + [StepProgress(4, "Step 4")]
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_EXECUTION_FINISHED, payload={}, progress=progress)

    def test_rejected_without_finish_expectation(self):
        task = make_execution_task(1)
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_EXECUTION_FINISHED, payload={}, progress=all_completed(1))


class ValidationTransitionTest(unittest.TestCase):
    def test_validation_passed_completes_the_task(self):
        task = make_validation_task()
        result = apply(
            task,
            EVENT_VALIDATION_PASSED,
            payload={"notes": "Все критерии выполнены"},
            progress=all_completed(1),
        )

        updated = result.task
        self.assertEqual(updated.stage, STAGE_DONE)
        self.assertEqual(updated.status, STATUS_COMPLETED)
        self.assertEqual(updated.version, task.version + 1)
        self.assertEqual(updated.current_step, "Done")
        self.assertIsNone(updated.current_step_index)
        self.assertEqual(updated.expected_action_type, EXPECTED_REVIEW_RESULT)
        self.assertEqual(updated.expected_action_text, "Review the result")

        self.assertEqual(
            [artifact.kind for artifact in result.artifacts],
            [ARTIFACT_VALIDATION_RESULT, ARTIFACT_FINAL_RESULT],
        )
        self.assertEqual(
            result.artifacts[0].content,
            {"passed": True, "defects": [], "notes": "Все критерии выполнены"},
        )
        self.assertIn("Task title", result.artifacts[1].content["markdown"])
        self.assertEqual(result.event.to_stage, STAGE_DONE)
        self.assertEqual(result.event.to_status, STATUS_COMPLETED)
        self.assertEqual(result.idempotency_key, "run_validation:7:5")

    def test_validation_passed_rejects_defects(self):
        payload = {
            "passed": True,
            "defects": [{"step_index": 1, "description": "broken"}],
        }
        with self.assertRaises(TransitionPayloadError):
            apply(make_validation_task(), EVENT_VALIDATION_PASSED, payload=payload)

    def test_validation_passed_rejects_failed_verdict(self):
        with self.assertRaises(TransitionPayloadError):
            apply(
                make_validation_task(),
                EVENT_VALIDATION_PASSED,
                payload={"passed": False, "defects": [{"step_index": 1, "description": "d"}]},
            )

    def test_validation_failed_returns_to_the_first_defect_step(self):
        task = make_validation_task()
        payload = {
            "passed": False,
            "defects": [
                {"step_index": 3, "description": "third is broken"},
                {"step_index": 2, "description": "second is broken"},
            ],
        }
        result = apply(
            task,
            EVENT_VALIDATION_FAILED,
            payload=payload,
            progress=[
                StepProgress(1, "Step 1", completed=True),
                StepProgress(2, "Step 2"),
                StepProgress(3, "Step 3"),
            ],
        )

        self.assertEqual(result.task.stage, STAGE_EXECUTION)
        self.assertEqual(result.task.status, STATUS_ACTIVE)
        self.assertEqual(result.task.current_step_index, 2)
        self.assertEqual(result.task.current_step, "Step 2")
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(
            result.task.expected_action_text, "Fix the defects and run the step"
        )
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(result.artifacts[0].kind, ARTIFACT_VALIDATION_RESULT)
        self.assertFalse(result.artifacts[0].content["passed"])
        self.assertEqual(len(result.artifacts[0].content["defects"]), 2)

    def test_validation_failed_requires_defects(self):
        for payload in (
            {"passed": False, "defects": []},
            {"passed": False},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(TransitionPayloadError):
                    apply(
                        make_validation_task(),
                        EVENT_VALIDATION_FAILED,
                        payload=payload,
                        progress=all_completed(1),
                    )

    def test_validation_failed_rejects_out_of_range_step(self):
        payload = {"passed": False, "defects": [{"step_index": 4, "description": "d"}]}
        with self.assertRaises(TransitionPayloadError):
            apply(
                make_validation_task(),
                EVENT_VALIDATION_FAILED,
                payload=payload,
                progress=all_completed(3),
            )

    def test_validation_failed_requires_known_step_count(self):
        payload = {"passed": False, "defects": [{"step_index": 1, "description": "d"}]}
        with self.assertRaises(TransitionPayloadError):
            apply(make_validation_task(), EVENT_VALIDATION_FAILED, payload=payload)

    def test_validation_requires_run_validation_expectation(self):
        task = make_validation_task().evolve(expected_action_type=EXPECTED_NONE)
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_VALIDATION_PASSED, payload={})

    def test_done_is_terminal(self):
        completed = apply(
            make_validation_task(), EVENT_VALIDATION_PASSED, payload={}
        ).task
        for event in (
            EVENT_PAUSE,
            EVENT_RESUME,
            EVENT_BLOCK,
            EVENT_UNBLOCK,
            EVENT_CANCEL,
            EVENT_PLAN_CREATED,
            EVENT_VALIDATION_PASSED,
            EVENT_API_ERROR,
            EVENT_RETRY,
        ):
            with self.subTest(event=event):
                with self.assertRaises(InvalidTransitionError):
                    apply(completed, event, payload={"confirmed": True})


class PauseResumeBlockCancelTest(unittest.TestCase):
    def test_pause_keeps_stage_step_and_expected_action(self):
        task = make_execution_task(2)
        result = apply(task, EVENT_PAUSE, payload={"reason": "interrupted"})

        self.assertEqual(result.task.status, STATUS_PAUSED)
        self.assertEqual(result.task.stage, STAGE_EXECUTION)
        self.assertEqual(result.task.current_step, "Step 2")
        self.assertEqual(result.task.current_step_index, 2)
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(result.task.pause_reason, "interrupted")
        self.assertEqual(result.task.version, task.version + 1)
        self.assertEqual(result.event.to_status, STATUS_PAUSED)
        self.assertEqual(result.event.to_stage, STAGE_EXECUTION)

    def test_pause_without_reason_clears_it(self):
        task = make_task(pause_reason="old")
        result = apply(task, EVENT_PAUSE, payload={})
        self.assertEqual(result.task.pause_reason, "")

    def test_pause_rejected_from_paused_or_blocked(self):
        for status in (STATUS_PAUSED, STATUS_BLOCKED):
            with self.subTest(status=status):
                with self.assertRaises(InvalidTransitionError):
                    apply(make_task(status=status), EVENT_PAUSE, payload={})

    def test_resume_restores_active_and_clears_reason(self):
        task = make_validation_task().evolve(
            status=STATUS_PAUSED, pause_reason="wait"
        )
        result = apply(task, EVENT_RESUME, payload={})

        self.assertEqual(result.task.status, STATUS_ACTIVE)
        self.assertEqual(result.task.stage, STAGE_VALIDATION)
        self.assertEqual(result.task.current_step, "Validation")
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_VALIDATION)
        self.assertEqual(result.task.pause_reason, "")
        self.assertEqual(result.task.version, task.version + 1)
        self.assertEqual(result.event.to_status, STATUS_ACTIVE)

    def test_resume_rejected_when_not_paused(self):
        with self.assertRaises(InvalidTransitionError):
            apply(make_task(), EVENT_RESUME, payload={})

    def test_block_requires_reason_and_expected_action(self):
        task = make_task()
        for payload in ({}, {"reason": "why"}, {"expected_action_text": "do it"}, {"reason": "  ", "expected_action_text": "do it"}):
            with self.subTest(payload=payload):
                with self.assertRaises(TransitionPayloadError):
                    apply(task, EVENT_BLOCK, payload=payload)

    def test_block_keeps_stage_and_asks_for_user_action(self):
        task = make_execution_task(2)
        result = apply(
            task,
            EVENT_BLOCK,
            payload={"reason": "need input", "expected_action_text": "Provide the key"},
        )

        self.assertEqual(result.task.status, STATUS_BLOCKED)
        self.assertEqual(result.task.stage, STAGE_EXECUTION)
        self.assertEqual(result.task.current_step_index, 2)
        self.assertEqual(result.task.expected_action_type, EXPECTED_USER_ACTION)
        self.assertEqual(result.task.expected_action_text, "Provide the key")
        self.assertEqual(result.task.pause_reason, "need input")
        self.assertEqual(
            result.event.payload["previous_expected_action_type"], EXPECTED_RUN_STEP
        )
        self.assertEqual(result.event.to_status, STATUS_BLOCKED)

    def test_block_allowed_from_paused(self):
        task = make_task(status=STATUS_PAUSED, stage=STAGE_PLANNING)
        result = apply(
            task,
            EVENT_BLOCK,
            payload={"reason": "r", "expected_action_text": "a"},
        )
        self.assertEqual(result.task.status, STATUS_BLOCKED)

    def test_unblock_recomputes_the_expected_action_from_the_step_pointer(self):
        blocked = make_execution_task(2).evolve(
            status=STATUS_BLOCKED,
            expected_action_type=EXPECTED_USER_ACTION,
            expected_action_text="Provide the key",
            pause_reason="need input",
        )
        progress = [
            StepProgress(1, "Step 1", completed=True),
            StepProgress(2, "Step 2", awaiting_rework=True),
            StepProgress(3, "Step 3"),
        ]
        result = apply(blocked, EVENT_UNBLOCK, payload={}, progress=progress)

        self.assertEqual(result.task.status, STATUS_ACTIVE)
        self.assertEqual(result.task.pause_reason, "")
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(result.task.current_step_index, 2)
        self.assertEqual(result.task.current_step, "Step 2")
        self.assertEqual(result.event.to_status, STATUS_ACTIVE)

    def test_unblock_expects_finish_execution_when_all_steps_are_done(self):
        blocked = make_execution_task(3).evolve(
            status=STATUS_BLOCKED,
            expected_action_type=EXPECTED_USER_ACTION,
        )
        result = apply(
            blocked, EVENT_UNBLOCK, payload={}, progress=all_completed(3)
        )
        self.assertEqual(result.task.expected_action_type, EXPECTED_FINISH_EXECUTION)
        self.assertIsNone(result.task.current_step_index)

    def test_unblock_planning_without_plan_expects_planning(self):
        blocked = make_task(status=STATUS_BLOCKED, expected_action_type=EXPECTED_USER_ACTION)
        result = apply(blocked, EVENT_UNBLOCK, payload={}, progress=[])
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertEqual(result.task.expected_action_text, "Run planning")

    def test_unblock_planning_with_plan_expects_confirmation(self):
        blocked = make_task(status=STATUS_BLOCKED, expected_action_type=EXPECTED_USER_ACTION)
        result = apply(
            blocked,
            EVENT_UNBLOCK,
            payload={},
            progress=[StepProgress(1, "Step 1")],
        )
        self.assertEqual(result.task.expected_action_type, EXPECTED_CONFIRM_PLAN)

    def test_unblock_uses_the_previous_expected_action_when_given(self):
        blocked = make_task(status=STATUS_BLOCKED, expected_action_type=EXPECTED_USER_ACTION)
        result = apply(
            blocked,
            EVENT_UNBLOCK,
            payload={"previous_expected_action_type": EXPECTED_CONFIRM_PLAN},
            progress=[StepProgress(1, "Step 1")],
        )
        self.assertEqual(result.task.expected_action_type, EXPECTED_CONFIRM_PLAN)

    def test_unblock_validation_stage(self):
        blocked = make_validation_task().evolve(
            status=STATUS_BLOCKED, expected_action_type=EXPECTED_USER_ACTION
        )
        result = apply(blocked, EVENT_UNBLOCK, payload={}, progress=all_completed(1))
        self.assertEqual(result.task.expected_action_type, EXPECTED_RUN_VALIDATION)
        self.assertEqual(result.task.current_step, "Validation")

    def test_unblock_rejected_when_not_blocked(self):
        with self.assertRaises(InvalidTransitionError):
            apply(make_task(), EVENT_UNBLOCK, payload={})

    def test_cancel_requires_confirmation(self):
        task = make_task()
        for payload in ({}, {"confirmed": False}):
            with self.subTest(payload=payload):
                with self.assertRaises(TransitionPayloadError):
                    apply(task, EVENT_CANCEL, payload=payload)

    def test_cancel_keeps_stage_and_clears_expected_action(self):
        task = make_validation_task()
        result = apply(task, EVENT_CANCEL, payload={"confirmed": True})

        self.assertEqual(result.task.status, STATUS_CANCELLED)
        self.assertEqual(result.task.stage, STAGE_VALIDATION)
        self.assertEqual(result.task.expected_action_type, EXPECTED_NONE)
        self.assertEqual(result.task.expected_action_text, "")
        self.assertEqual(result.task.version, task.version + 1)
        self.assertEqual(result.event.to_status, STATUS_CANCELLED)
        self.assertEqual(result.artifacts, [])

    def test_cancel_allowed_from_active_paused_and_blocked(self):
        for status in (STATUS_ACTIVE, STATUS_PAUSED, STATUS_BLOCKED):
            with self.subTest(status=status):
                task = make_task(status=status)
                result = apply(task, EVENT_CANCEL, payload={"confirmed": True})
                self.assertEqual(result.task.status, STATUS_CANCELLED)

    def test_cancelled_is_terminal(self):
        cancelled = apply(
            make_task(), EVENT_CANCEL, payload={"confirmed": True}
        ).task
        for event in (EVENT_PAUSE, EVENT_RESUME, EVENT_CANCEL, EVENT_RETRY, EVENT_API_ERROR):
            with self.subTest(event=event):
                with self.assertRaises(InvalidTransitionError):
                    apply(cancelled, event, payload={"confirmed": True})


class ApiErrorAndRetryTest(unittest.TestCase):
    def test_api_error_keeps_the_task_unchanged(self):
        task = make_execution_task(2)
        result = apply(
            task,
            EVENT_API_ERROR,
            payload={"kind": "provider_error", "message": "connection reset"},
        )

        self.assertEqual(result.task.version, task.version)
        self.assertEqual(result.task.stage, task.stage)
        self.assertEqual(result.task.status, task.status)
        self.assertEqual(result.task.current_step, task.current_step)
        self.assertEqual(result.task.current_step_index, task.current_step_index)
        self.assertEqual(result.task.updated_at, task.updated_at)
        self.assertEqual(result.artifacts, [])
        self.assertIsNone(result.idempotency_key)
        self.assertIsNone(result.event.idempotency_key)
        self.assertEqual(result.event.event_type, EVENT_API_ERROR)
        self.assertEqual(result.event.from_stage, STAGE_EXECUTION)
        self.assertEqual(result.event.to_stage, STAGE_EXECUTION)
        self.assertEqual(result.event.payload["kind"], "provider_error")
        self.assertEqual(result.event.payload["message"], "connection reset")

    def test_api_error_truncates_the_message(self):
        result = apply(
            make_task(),
            EVENT_API_ERROR,
            payload={"kind": "invalid_response", "message": "x" * 500},
        )
        self.assertEqual(
            len(result.event.payload["message"]), ERROR_MESSAGE_MAX_LENGTH
        )
        self.assertEqual(
            result.event.payload["message"], "x" * ERROR_MESSAGE_MAX_LENGTH
        )

    def test_api_error_keeps_usage_fields(self):
        result = apply(
            make_task(),
            EVENT_API_ERROR,
            payload={
                "kind": "truncated",
                "message": "cut",
                "finish_reason": "length",
                "attempts": 2,
                "usage": {"prompt_tokens": 10},
            },
        )
        self.assertEqual(result.event.payload["finish_reason"], "length")
        self.assertEqual(result.event.payload["attempts"], 2)
        self.assertEqual(result.event.payload["usage"], {"prompt_tokens": 10})

    def test_api_error_rejects_unknown_kind(self):
        with self.assertRaises(TransitionPayloadError):
            apply(make_task(), EVENT_API_ERROR, payload={"kind": "nope"})

    def test_api_error_allowed_on_paused_and_blocked(self):
        for status in (STATUS_PAUSED, STATUS_BLOCKED):
            with self.subTest(status=status):
                result = apply(
                    make_task(status=status),
                    EVENT_API_ERROR,
                    payload={"kind": "stream_error"},
                )
                self.assertEqual(result.task.status, status)

    def test_retry_requires_a_previous_api_error(self):
        task = make_task()
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_RETRY, payload={}, last_event_type=None)
        with self.assertRaises(InvalidTransitionError):
            apply(task, EVENT_RETRY, payload={}, last_event_type=EVENT_PAUSE)

    def test_retry_is_an_event_without_version_change(self):
        task = make_task()
        result = apply(
            task,
            EVENT_RETRY,
            payload={"attempts": 1},
            last_event_type=EVENT_API_ERROR,
        )
        self.assertEqual(result.task.version, task.version)
        self.assertEqual(result.event.event_type, EVENT_RETRY)
        self.assertIsNone(result.event.idempotency_key)
        self.assertIsNone(result.idempotency_key)
        self.assertEqual(result.event.payload["attempts"], 1)

    def test_retry_after_api_error_then_successful_step_changes_version_once(self):
        task = make_execution_task(1)
        error = apply(task, EVENT_API_ERROR, payload={"kind": "provider_error"})
        retry = apply(
            error.task,
            EVENT_RETRY,
            payload={},
            last_event_type=EVENT_API_ERROR,
        )
        finished = apply(
            retry.task,
            EVENT_STEP_COMPLETED,
            payload={"text": "done"},
            progress=[StepProgress(1, "Step 1")],
            last_event_type=EVENT_RETRY,
        )
        self.assertEqual(retry.task.version, task.version)
        self.assertEqual(finished.task.version, task.version + 1)


class VersionRuleTest(unittest.TestCase):
    def test_state_changing_events_bump_version_once(self):
        cases = [
            (make_task(), EVENT_PLAN_CREATED, {"plan": make_plan()}, None),
            (
                make_task(expected_action_type=EXPECTED_CONFIRM_PLAN),
                EVENT_PLAN_REJECTED,
                {},
                None,
            ),
            (
                make_task(expected_action_type=EXPECTED_CONFIRM_PLAN),
                EVENT_PLAN_ACCEPTED,
                {"plan": make_plan()},
                None,
            ),
            (
                make_execution_task(1),
                EVENT_STEP_COMPLETED,
                {"text": "r"},
                [StepProgress(1, "Step 1")],
            ),
            (
                make_execution_task(1).evolve(
                    expected_action_type=EXPECTED_FINISH_EXECUTION
                ),
                EVENT_EXECUTION_FINISHED,
                {},
                all_completed(1),
            ),
            (make_validation_task(), EVENT_VALIDATION_PASSED, {}, all_completed(1)),
            (
                make_validation_task(),
                EVENT_VALIDATION_FAILED,
                {"passed": False, "defects": [{"step_index": 1, "description": "d"}]},
                all_completed(1),
            ),
            (make_task(), EVENT_PAUSE, {}, None),
            (make_task(status=STATUS_PAUSED), EVENT_RESUME, {}, None),
            (
                make_task(),
                EVENT_BLOCK,
                {"reason": "r", "expected_action_text": "a"},
                None,
            ),
            (make_task(status=STATUS_BLOCKED), EVENT_UNBLOCK, {}, []),
            (make_task(), EVENT_CANCEL, {"confirmed": True}, None),
        ]
        for task, event, payload, progress in cases:
            with self.subTest(event=event):
                result = apply(task, event, payload=payload, progress=progress)
                self.assertEqual(result.task.version, task.version + 1)
                self.assertIsNotNone(result.task.updated_at)

    def test_api_error_and_retry_do_not_bump_version(self):
        task = make_task(version=9)
        error = apply(task, EVENT_API_ERROR, payload={"kind": "provider_error"})
        self.assertEqual(error.task.version, 9)
        retry = apply(
            error.task, EVENT_RETRY, payload={}, last_event_type=EVENT_API_ERROR
        )
        self.assertEqual(retry.task.version, 9)


class CanApplyTest(unittest.TestCase):
    CASES = (
        (
            dict(stage=STAGE_PLANNING, status=STATUS_ACTIVE, expected_action_type=EXPECTED_RUN_PLANNING),
            (ACTION_RUN_PLANNING, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_PLANNING, status=STATUS_ACTIVE, expected_action_type=EXPECTED_CONFIRM_PLAN),
            (ACTION_ACCEPT_PLAN, ACTION_REJECT_PLAN, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_EXECUTION, status=STATUS_ACTIVE, expected_action_type=EXPECTED_RUN_STEP),
            (ACTION_RUN_STEP, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(
                stage=STAGE_EXECUTION,
                status=STATUS_ACTIVE,
                expected_action_type=EXPECTED_FINISH_EXECUTION,
            ),
            (ACTION_FINISH_EXECUTION, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_VALIDATION, status=STATUS_ACTIVE, expected_action_type=EXPECTED_RUN_VALIDATION),
            (ACTION_RUN_VALIDATION, ACTION_PAUSE, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_EXECUTION, status=STATUS_PAUSED, expected_action_type=EXPECTED_RUN_STEP),
            (ACTION_RESUME, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_PLANNING, status=STATUS_PAUSED, expected_action_type=EXPECTED_RUN_PLANNING),
            (ACTION_RESUME, ACTION_BLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_EXECUTION, status=STATUS_BLOCKED, expected_action_type=EXPECTED_USER_ACTION),
            (ACTION_UNBLOCK, ACTION_CANCEL),
        ),
        (
            dict(stage=STAGE_DONE, status=STATUS_COMPLETED, expected_action_type=EXPECTED_REVIEW_RESULT),
            (),
        ),
        (
            dict(stage=STAGE_VALIDATION, status=STATUS_CANCELLED, expected_action_type=EXPECTED_NONE),
            (),
        ),
    )

    def test_matrix_matches_the_specification(self):
        for state, expected in self.CASES:
            with self.subTest(state=state):
                task = make_task(**state)
                self.assertEqual(can_apply(task), expected)
                self.assertEqual(TaskStateMachine.can_apply(task), expected)

    def test_retry_is_added_after_api_error_for_active_tasks_only(self):
        for state, expected in self.CASES:
            with self.subTest(state=state):
                task = make_task(**state)
                actions = can_apply(task, last_event_type=EVENT_API_ERROR)
                if task.status == STATUS_ACTIVE:
                    self.assertEqual(actions, expected + (ACTION_RETRY,))
                else:
                    self.assertEqual(actions, expected)

    def test_retry_is_not_offered_while_paused_or_blocked(self):
        task = make_execution_task(1)
        for status in (STATUS_PAUSED, STATUS_BLOCKED):
            with self.subTest(status=status):
                paused = task.evolve(status=status)
                actions = can_apply(paused, last_event_type=EVENT_API_ERROR)
                self.assertNotIn(ACTION_RETRY, actions)
                self.assertEqual(actions, can_apply(paused))

    def test_unknown_state_yields_no_domain_actions(self):
        task = make_task(stage=STAGE_EXECUTION, expected_action_type=EXPECTED_NONE)
        self.assertEqual(can_apply(task), ())

    def test_ui_actions_per_state(self):
        self.assertEqual(
            ui_actions(make_task(stage=STAGE_DONE, status=STATUS_COMPLETED)),
            (ACTION_OPEN_RESULT, ACTION_NEW_TASK),
        )
        self.assertEqual(
            ui_actions(make_task(status=STATUS_CANCELLED)),
            (ACTION_NEW_TASK,),
        )
        self.assertEqual(
            ui_actions(make_task()),
            (ACTION_NEW_TASK, ACTION_OPEN_DIAGNOSTICS),
        )
        self.assertEqual(ui_actions(None), ())

    def test_stage_progress_does_not_change_the_action_set(self):
        task = make_task()
        self.assertEqual(
            can_apply(task, progress=[StepProgress(1, "Step 1")]),
            can_apply(task),
        )


class BadgeTest(unittest.TestCase):
    def test_badges_match_the_status(self):
        cases = (
            (make_task(status=STATUS_ACTIVE, stage=STAGE_PLANNING), BADGE_RUNNING),
            (make_task(status=STATUS_ACTIVE, stage=STAGE_EXECUTION), BADGE_RUNNING),
            (make_task(status=STATUS_ACTIVE, stage=STAGE_VALIDATION), BADGE_RUNNING),
            (make_task(status=STATUS_PAUSED), BADGE_PAUSED),
            (make_task(status=STATUS_BLOCKED), BADGE_BLOCKED),
            (make_task(status=STATUS_COMPLETED, stage=STAGE_DONE), BADGE_COMPLETED),
            (make_task(status=STATUS_CANCELLED), BADGE_CANCELLED),
        )
        for task, expected in cases:
            with self.subTest(status=task.status):
                self.assertEqual(badge_for(task), expected)


class UnknownEventTest(unittest.TestCase):
    def test_unknown_event_is_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            apply(make_task(), "NOT_AN_EVENT", payload={})

    def test_missing_task_is_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            apply(None, EVENT_PAUSE, payload={})

    def test_task_created_is_not_applied_to_an_existing_task(self):
        with self.assertRaises(InvalidTransitionError):
            apply(make_task(), EVENT_TASK_CREATED, payload={})

    def test_rejected_transition_produces_nothing(self):
        try:
            apply(make_task(), EVENT_VALIDATION_PASSED, payload={})
        except InvalidTransitionError as exc:
            self.assertIn("stage", str(exc))
        else:  # pragma: no cover - defensive
            self.fail("InvalidTransitionError was not raised")


class StepProgressRuleTest(unittest.TestCase):
    def test_step_without_execution_is_not_completed(self):
        progress = compute_step_progress(make_plan(2), [])
        self.assertEqual([step.completed for step in progress], [False, False])
        self.assertEqual([step.awaiting_rework for step in progress], [False, False])
        self.assertEqual([step.round for step in progress], [0, 0])
        self.assertEqual(progress[0].title, "Step 1")

    def test_multi_defect_validation_marks_only_defective_steps(self):
        artifacts = artifacts_from(
            [
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 1, "text": "r1"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 2, "round": 1, "text": "r2"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 3, "round": 1, "text": "r3"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 4, "round": 1, "text": "r4"}),
                (
                    ARTIFACT_VALIDATION_RESULT,
                    {
                        "passed": False,
                        "defects": [
                            {"step_index": 2, "description": "d2"},
                            {"step_index": 4, "description": "d4"},
                        ],
                    },
                ),
            ]
        )
        progress = compute_step_progress(make_plan(4), artifacts)

        self.assertEqual([step.completed for step in progress], [True, False, True, False])
        self.assertEqual(
            [step.awaiting_rework for step in progress], [False, True, False, True]
        )
        self.assertEqual(progress[1].defects, ("d2",))
        self.assertEqual(progress[3].defects, ("d4",))
        self.assertEqual(progress[0].defects, ())

    def test_next_pointer_serves_the_minimum_rework_step(self):
        artifacts = artifacts_from(
            [
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 1, "text": "r1"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 2, "round": 1, "text": "r2"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 3, "round": 1, "text": "r3"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 4, "round": 1, "text": "r4"}),
                (
                    ARTIFACT_VALIDATION_RESULT,
                    {
                        "passed": False,
                        "defects": [
                            {"step_index": 2, "description": "d2"},
                            {"step_index": 4, "description": "d4"},
                        ],
                    },
                ),
            ]
        )
        plan = make_plan(4)
        progress = compute_step_progress(plan, artifacts)
        task = make_execution_task(2)

        completed = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "reworked"}, progress=progress
        )
        self.assertEqual(completed.task.current_step_index, 4)

    def test_new_successful_revision_clears_the_rework_expectation(self):
        artifacts = artifacts_from(
            [
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 2, "round": 1, "text": "r1"}),
                (
                    ARTIFACT_VALIDATION_RESULT,
                    {"passed": False, "defects": [{"step_index": 2, "description": "d2"}]},
                ),
            ]
        )
        plan = make_plan(2)
        awaiting = compute_step_progress(plan, artifacts)[1]
        self.assertTrue(awaiting.awaiting_rework)
        self.assertFalse(awaiting.completed)

        artifacts.append(
            TaskArtifact(
                id=3,
                task_id=1,
                kind=ARTIFACT_EXECUTION_RESULT,
                content={"step_index": 2, "round": 2, "text": "r2"},
            )
        )
        redone = compute_step_progress(plan, artifacts)[1]
        self.assertTrue(redone.completed)
        self.assertFalse(redone.awaiting_rework)
        self.assertEqual(redone.round, 2)
        self.assertEqual(redone.defects, ())

    def test_round_counts_successful_revisions_only(self):
        artifacts = artifacts_from(
            [
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 1, "text": "a"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 2, "text": "b"}),
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 3, "text": "c"}),
            ]
        )
        progress = compute_step_progress(make_plan(1), artifacts)
        self.assertEqual(progress[0].round, 3)
        self.assertTrue(progress[0].completed)

    def test_failed_validation_without_the_step_does_not_affect_it(self):
        artifacts = artifacts_from(
            [
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 1, "text": "a"}),
                (
                    ARTIFACT_VALIDATION_RESULT,
                    {"passed": False, "defects": [{"step_index": 2, "description": "d"}]},
                ),
            ]
        )
        progress = compute_step_progress(make_plan(2), artifacts)
        self.assertTrue(progress[0].completed)
        self.assertFalse(progress[0].awaiting_rework)

    def test_passed_validation_does_not_block(self):
        artifacts = artifacts_from(
            [
                (ARTIFACT_EXECUTION_RESULT, {"step_index": 1, "round": 1, "text": "a"}),
                (ARTIFACT_VALIDATION_RESULT, {"passed": True, "defects": []}),
            ]
        )
        progress = compute_step_progress(make_plan(1), artifacts)
        self.assertTrue(progress[0].completed)

    def test_no_plan_yields_no_progress(self):
        self.assertEqual(compute_step_progress(None, []), [])
        self.assertEqual(compute_step_progress({}, []), [])
        self.assertEqual(compute_step_progress({"steps": []}, []), [])

    def test_artifact_without_id_does_not_crash(self):
        artifact = TaskArtifact(
            kind=ARTIFACT_EXECUTION_RESULT,
            content={"step_index": 1, "round": 1, "text": "a"},
        )
        progress = compute_step_progress(make_plan(1), [artifact])
        self.assertTrue(progress[0].completed)


class FormatterTest(unittest.TestCase):
    def test_snapshot_block_lists_the_state(self):
        task = make_execution_task(2).evolve(pause_reason="waiting", version=6)
        block = format_snapshot_block(task)
        for fragment in (
            "Снимок задачи",
            "Task title",
            "Task goal",
            "execution",
            "active",
            "6",
            "Step 2",
            "шаг 2",
            "Run the current step",
            "waiting",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, block)

    def test_snapshot_block_without_current_step(self):
        block = format_snapshot_block(make_task(current_step=""))
        self.assertIn("не определён", block)
        self.assertNotIn("Причина", block)

    def test_defects_block_lists_awaiting_steps(self):
        progress = [
            StepProgress(1, "Step 1", completed=True),
            StepProgress(2, "Step 2", awaiting_rework=True, defects=("d2", "d2b")),
            StepProgress(3, "Step 3", awaiting_rework=True),
        ]
        block = format_defects_block(progress)
        self.assertIn("Шаг 2 «Step 2»: d2", block)
        self.assertIn("Шаг 2 «Step 2»: d2b", block)
        self.assertIn("Шаг 3 «Step 3»: требуется переделка", block)
        self.assertNotIn("Step 1", block)

    def test_defects_block_is_empty_without_rework(self):
        self.assertEqual(format_defects_block([]), "")
        self.assertEqual(format_defects_block(None), "")
        self.assertEqual(
            format_defects_block([StepProgress(1, "Step 1", completed=True)]), ""
        )

    def test_task_usage_line(self):
        usage = SimpleNamespace(
            calls=3,
            attempts=4,
            input_tokens=1200,
            output_tokens=800,
            cost_usd=0.0012,
        )
        line = format_task_usage_line(usage)
        self.assertIn("Calls: 3", line)
        self.assertIn("Attempts: 4", line)
        self.assertIn("1200 in / 800 out", line)
        self.assertIn("$0.0012", line)
        self.assertEqual(format_task_usage_line(None), "No task usage recorded")

    def test_task_usage_line_with_missing_values(self):
        usage = SimpleNamespace(calls=1, attempts=1, input_tokens=None, output_tokens=None, cost_usd=None)
        line = format_task_usage_line(usage)
        self.assertIn("n/a", line)

    def test_validate_error_message_truncates(self):
        self.assertEqual(validate_error_message(None), "")
        self.assertEqual(validate_error_message("  short  "), "short")
        long_message = "x" * (ERROR_MESSAGE_MAX_LENGTH + 50)
        truncated = validate_error_message(long_message)
        self.assertEqual(len(truncated), ERROR_MESSAGE_MAX_LENGTH)
        self.assertEqual(truncated, long_message[:ERROR_MESSAGE_MAX_LENGTH])

    def test_specification_markdown_sections(self):
        markdown = format_specification_markdown("Goal text", "Brief text", make_plan(2))
        self.assertIn("# Task specification", markdown)
        self.assertIn("Goal text", markdown)
        self.assertIn("Brief text", markdown)
        self.assertIn("Краткий план", markdown)
        self.assertIn("1. **Step 1**", markdown)
        self.assertIn("- Первый критерий", markdown)

    def test_final_result_markdown_with_all_parts(self):
        task = make_task(stage=STAGE_DONE, status=STATUS_COMPLETED)
        markdown = format_final_result_markdown(
            task,
            make_plan(2),
            [
                {"step_index": 1, "round": 1, "text": "first result"},
                {"step_index": 2, "round": 2, "text": "second result"},
            ],
            {"passed": True, "defects": [], "notes": "all good"},
        )
        self.assertIn("# Task result: Task title", markdown)
        self.assertIn("first result", markdown)
        self.assertIn("second result", markdown)
        self.assertIn("Verdict: passed", markdown)
        self.assertIn("all good", markdown)

    def test_final_result_markdown_with_failed_verdict_and_missing_executions(self):
        task = make_task(stage=STAGE_DONE, status=STATUS_COMPLETED)
        markdown = format_final_result_markdown(
            task,
            make_plan(1),
            [],
            {"passed": False, "defects": [{"step_index": 1, "description": "broken"}]},
        )
        self.assertIn("Verdict: failed", markdown)
        self.assertIn("Step 1: broken", markdown)
        self.assertIn("_No result recorded._", markdown)

    def test_final_result_markdown_tolerates_missing_plan(self):
        markdown = format_final_result_markdown(make_task(), None, None, None)
        self.assertIn("# Task result", markdown)


class PromptParserTest(unittest.TestCase):
    def test_parse_plan_response_valid(self):
        parsed = parse_plan_response(json.dumps(make_plan(2)))
        self.assertEqual([step["index"] for step in parsed["steps"]], [1, 2])
        self.assertEqual(parsed["acceptance_criteria"], ["Первый критерий"])

    def test_parse_plan_response_accepts_a_code_fence(self):
        text = "```json\n" + json.dumps(make_plan(1)) + "\n```"
        parsed = parse_plan_response(text)
        self.assertEqual(parsed["steps"][0]["title"], "Step 1")

    def test_parse_plan_response_rejects_bad_payloads(self):
        bad = [
            "",
            "   ",
            "not json",
            "[]",
            json.dumps({"summary": "s", "acceptance_criteria": ["c"], "steps": []}),
            json.dumps(
                {
                    "summary": "s",
                    "acceptance_criteria": ["c"],
                    "steps": [{"index": 1, "title": "t"}, {"index": 3, "title": "t"}],
                }
            ),
            json.dumps(
                {
                    "summary": "s",
                    "acceptance_criteria": ["c"],
                    "steps": [{"index": 1, "title": "   "}],
                }
            ),
            json.dumps({"summary": "s", "steps": [{"index": 1, "title": "t"}]}),
            json.dumps({"summary": "s", "acceptance_criteria": [], "steps": [{"index": 1, "title": "t"}]}),
        ]
        for text in bad:
            with self.subTest(text=text[:40]):
                with self.assertRaises(ValueError):
                    parse_plan_response(text)

    def test_parse_plan_response_reports_a_readable_error(self):
        with self.assertRaises(ValueError) as context:
            parse_plan_response("{}")
        self.assertIn("step", str(context.exception).lower())

    def test_parse_plan_response_rejects_the_reported_live_plan(self):
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_LIVE_PLAN))
        violations = context.exception.violations
        self.assertEqual(
            [
                (violation.location, violation.index)
                for violation in violations
            ],
            [("step", 3), ("step", 4), ("step", 5), ("criterion", 3)],
        )
        self.assertIsInstance(context.exception, ValueError)

    def test_parse_plan_response_rejects_the_reported_loopback_plan(self):
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_LOOPBACK_PLAN))
        violations = context.exception.violations
        self.assertEqual(
            {violation.location for violation in violations},
            {"step", "criterion"},
        )
        self.assertEqual(
            [violation.index for violation in violations if violation.location == "step"],
            [3, 4, 5],
        )
        self.assertEqual(
            [
                violation.reason
                for violation in violations
                if violation.location == "criterion"
            ],
            ["requires_other_stage", "requires_other_stage"],
        )
        self.assertEqual(
            violations[0].reason, REASON_NONEXISTENT_TRANSITION
        )
        message = str(context.exception)
        self.assertIn("step 3", message)
        self.assertIn("criterion 1", message)
        self.assertLessEqual(len(message), ERROR_MESSAGE_MAX_LENGTH)
        for title in ("Вернуть задачу в planning", "VALIDATION_PASSED", "Задача завершена"):
            with self.subTest(title=title):
                self.assertNotIn(title, message)

    def test_plan_retry_feedback_names_steps_and_criteria(self):
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_LOOPBACK_PLAN))
        content = plan_retry_feedback(context.exception)[0]["content"]
        self.assertIn("невыполнимые в стадии execution", content)
        self.assertIn("Недопустимые шаги", content)
        self.assertIn("Недопустимые критерии приёмки", content)
        self.assertIn("шаг 3", content)
        self.assertIn("критерий 1", content)
        self.assertIn("Transition guard", content)

    def test_parse_plan_response_accepts_the_corrected_plan(self):
        parsed = parse_plan_response(json.dumps(CORRECTED_PLAN_EXAMPLE))
        self.assertEqual([step["index"] for step in parsed["steps"]], [1, 2, 3])
        self.assertEqual(plan_step_violations(parsed), ())
        self.assertEqual(plan_violations(parsed), ())

    def test_parse_plan_response_error_message_stays_compact(self):
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_LIVE_PLAN))
        message = str(context.exception)
        self.assertNotIn("Выполнить и проверить validation", message)
        self.assertNotIn("тестовой копии", message)
        self.assertIn("step 3", message)
        self.assertLessEqual(len(message), ERROR_MESSAGE_MAX_LENGTH)

    def test_plan_retry_feedback_keeps_the_impossible_step_wording(self):
        exc = ExecutionIncompatiblePlanError(
            [
                PlanStepViolation(
                    index=3, title="Finish the task", reason="requires_other_stage"
                ),
                PlanStepViolation(
                    index=5, title="Test copy", reason="missing_entity"
                ),
            ]
        )
        message = plan_retry_feedback(exc)[0]
        self.assertEqual(message["role"], "user")
        self.assertIn("невыполнимые в стадии execution", message["content"])
        self.assertIn("Недопустимые шаги", message["content"])
        self.assertIn("шаг 3", message["content"])
        self.assertIn("шаг 5", message["content"])

    def test_plan_retry_feedback_describes_a_structural_error_as_a_format_issue(self):
        message = plan_retry_feedback(
            ValueError("Plan must contain at least one step")
        )[0]
        self.assertEqual(message["role"], "user")
        # A syntax/structural error is not an execution-incompatible step list.
        self.assertNotIn("невыполнимые в стадии execution", message["content"])
        self.assertNotIn("Недопустимые шаги", message["content"])
        self.assertIn("формат", message["content"])
        self.assertIn("Верни исправленный план", message["content"])

    def test_parse_plan_response_accepts_a_neutral_validation_task(self):
        neutral = {
            "summary": "Реализовать функцию валидации.",
            "acceptance_criteria": ["Валидация отклоняет мусор"],
            "steps": [
                {
                    "index": 1,
                    "title": "Реализовать проверку обхода validation",
                    "description": "Добавить проверку, обходящую validation.",
                },
                {
                    "index": 2,
                    "title": "Написать тесты для планировщика",
                    "description": "Покрыть тестами модуль планировщика.",
                },
            ],
        }
        parsed = parse_plan_response(json.dumps(neutral))
        self.assertEqual(len(parsed["steps"]), 2)
        self.assertEqual(plan_step_violations(neutral), ())
        self.assertEqual(plan_violations(neutral), ())

    def test_parse_validation_response_passed(self):
        verdict = parse_validation_response(
            json.dumps({"passed": True, "defects": [], "notes": "ok"}), steps_count=2
        )
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["defects"], [])
        self.assertEqual(verdict["notes"], "ok")

    def test_parse_validation_response_failed(self):
        text = json.dumps(
            {"passed": False, "defects": [{"step_index": 2, "description": "d"}]}
        )
        verdict = parse_validation_response(text, steps_count=2)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["defects"][0]["step_index"], 2)

    def test_parse_validation_response_rejects_bad_payloads(self):
        bad = [
            "",
            "nope",
            "[]",
            json.dumps({"defects": []}),
            json.dumps({"passed": "yes"}),
            json.dumps({"passed": True, "defects": [{"step_index": 1, "description": "d"}]}),
            json.dumps({"passed": False, "defects": []}),
            json.dumps({"passed": False, "defects": [{"step_index": 4, "description": "d"}]}),
            json.dumps({"passed": False, "defects": [{"step_index": 1, "description": " "}]}),
        ]
        for text in bad:
            with self.subTest(text=text[:40]):
                with self.assertRaises(ValueError):
                    parse_validation_response(text, steps_count=3)

    def test_normalize_verdict_accepts_either_verdict_without_expectation(self):
        self.assertTrue(normalize_verdict({"passed": True})["passed"])
        self.assertFalse(
            normalize_verdict(
                {"passed": False, "defects": [{"step_index": 1, "description": "d"}]}
            )["passed"]
        )

    def test_is_truncated(self):
        self.assertTrue(is_truncated("length"))
        self.assertTrue(is_truncated("max_tokens"))
        self.assertFalse(is_truncated("stop"))
        self.assertFalse(is_truncated(None))

    def test_budgets_grow_on_retry(self):
        self.assertGreater(TASK_PLAN_RETRY_MAX_TOKENS, TASK_PLAN_MAX_TOKENS)
        self.assertGreater(TASK_STEP_RETRY_MAX_TOKENS, TASK_STEP_MAX_TOKENS)
        self.assertGreater(TASK_VALIDATION_RETRY_MAX_TOKENS, TASK_VALIDATION_MAX_TOKENS)

    def test_build_plan_messages(self):
        messages = build_plan_messages("Do the thing", "Some brief")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("JSON", messages[0]["content"])
        self.assertIn("Do the thing", messages[1]["content"])
        self.assertIn("Some brief", messages[1]["content"])
        self.assertIn("run_planning", messages[1]["content"])

    def test_build_plan_messages_without_brief(self):
        messages = build_plan_messages("Goal")
        self.assertNotIn("Бриф", messages[1]["content"])

    def test_build_step_messages_with_defects(self):
        step = {"index": 2, "title": "Step 2", "description": "Do it"}
        messages = build_step_messages(step, ["first defect", "second defect"])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Step 2", messages[1]["content"])
        self.assertIn("Do it", messages[1]["content"])
        self.assertIn("first defect", messages[1]["content"])
        self.assertIn("second defect", messages[1]["content"])

    def test_build_step_messages_without_defects(self):
        messages = build_step_messages({"index": 1, "title": "Step 1"}, [])
        self.assertNotIn("замечания", messages[1]["content"])

    def test_build_validation_messages(self):
        messages = build_validation_messages(
            "Goal",
            ["criterion one"],
            make_plan(2)["steps"],
            [{"step_index": 1, "round": 1, "text": "result one"}],
        )
        self.assertEqual(messages[0]["role"], "system")
        content = messages[1]["content"]
        self.assertIn("run_validation", content)
        self.assertIn("criterion one", content)
        self.assertIn("критериям приёмки", content)
        self.assertIn("JSON", content)
        # The goal, the plan steps and the step results are carried by the
        # artifact blocks of the packet, so the action message no longer
        # duplicates them.
        self.assertNotIn("result one", content)
        self.assertNotIn("1. Step 1", content)

    def test_build_validation_messages_without_results(self):
        messages = build_validation_messages("Goal", ["c"], [], [])
        content = messages[1]["content"]
        self.assertIn("c", content)
        self.assertNotIn("Результаты шагов", content)
        self.assertNotIn("План:", content)


class LifecycleIntegrationTest(unittest.TestCase):
    """Drive the FSM through the full scenario with artifacts as the source of truth."""

    def test_plan_execute_validate_rework_and_complete(self):
        plan = make_plan(2)
        created = creation_transition(1, "Task", "Goal", task_brief="Brief")
        task = created.task

        plan_created = apply(
            task,
            EVENT_PLAN_CREATED,
            payload={"plan": plan, "task_brief": "Brief"},
        )
        self.assertEqual(plan_created.task.expected_action_type, EXPECTED_CONFIRM_PLAN)

        artifacts = artifacts_from(
            [
                (ARTIFACT_TASK_BRIEF, {"text": "Brief"}),
                (ARTIFACT_SPECIFICATION, plan_created.artifacts[0].content),
                (ARTIFACT_PLAN, plan_created.artifacts[1].content),
            ]
        )
        accepted = apply(
            plan_created.task,
            EVENT_PLAN_ACCEPTED,
            payload={},
            progress=compute_step_progress(plan, artifacts),
        )
        task = accepted.task
        self.assertEqual(task.current_step_index, 1)

        # Step 1
        progress = compute_step_progress(plan, artifacts)
        step_one = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "one"}, progress=progress
        )
        artifacts.append(
            TaskArtifact(
                id=len(artifacts) + 1,
                task_id=1,
                kind=ARTIFACT_EXECUTION_RESULT,
                content=step_one.artifacts[0].content,
            )
        )
        task = step_one.task
        self.assertEqual(task.current_step_index, 2)

        # Step 2
        progress = compute_step_progress(plan, artifacts)
        step_two = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "two"}, progress=progress
        )
        artifacts.append(
            TaskArtifact(
                id=len(artifacts) + 1,
                task_id=1,
                kind=ARTIFACT_EXECUTION_RESULT,
                content=step_two.artifacts[0].content,
            )
        )
        task = step_two.task
        self.assertEqual(task.expected_action_type, EXPECTED_FINISH_EXECUTION)

        finished = apply(
            task,
            EVENT_EXECUTION_FINISHED,
            payload={},
            progress=compute_step_progress(plan, artifacts),
        )
        task = finished.task
        self.assertEqual(task.stage, STAGE_VALIDATION)

        # Validation fails on step 2 only.
        failed = apply(
            task,
            EVENT_VALIDATION_FAILED,
            payload={
                "passed": False,
                "defects": [{"step_index": 2, "description": "step two is wrong"}],
            },
            progress=compute_step_progress(plan, artifacts),
        )
        artifacts.append(
            TaskArtifact(
                id=len(artifacts) + 1,
                task_id=1,
                kind=ARTIFACT_VALIDATION_RESULT,
                content=failed.artifacts[0].content,
            )
        )
        task = failed.task
        self.assertEqual(task.stage, STAGE_EXECUTION)
        self.assertEqual(task.current_step_index, 2)

        progress = compute_step_progress(plan, artifacts)
        self.assertTrue(progress[0].completed)
        self.assertTrue(progress[1].awaiting_rework)
        self.assertEqual(progress[1].defects, ("step two is wrong",))

        # Rework step 2; only the defective step is redone.
        redone = apply(
            task, EVENT_STEP_COMPLETED, payload={"text": "two again"}, progress=progress
        )
        artifacts.append(
            TaskArtifact(
                id=len(artifacts) + 1,
                task_id=1,
                kind=ARTIFACT_EXECUTION_RESULT,
                content=redone.artifacts[0].content,
            )
        )
        task = redone.task
        progress = compute_step_progress(plan, artifacts)
        self.assertTrue(progress[1].completed)
        self.assertFalse(progress[1].awaiting_rework)
        self.assertEqual(progress[1].round, 2)
        self.assertEqual(task.expected_action_type, EXPECTED_FINISH_EXECUTION)

        finished = apply(
            task,
            EVENT_EXECUTION_FINISHED,
            payload={},
            progress=progress,
        )
        done = apply(
            finished.task,
            EVENT_VALIDATION_PASSED,
            payload={"notes": "ok"},
            progress=progress,
        )
        self.assertEqual(done.task.stage, STAGE_DONE)
        self.assertEqual(done.task.status, STATUS_COMPLETED)
        self.assertEqual(
            [artifact.kind for artifact in done.artifacts],
            [ARTIFACT_VALIDATION_RESULT, ARTIFACT_FINAL_RESULT],
        )
        self.assertEqual(done.task.expected_action_type, EXPECTED_REVIEW_RESULT)

    def test_pause_then_resume_keeps_the_same_task_and_step(self):
        task = make_execution_task(2, version=4)
        paused = apply(task, EVENT_PAUSE, payload={"reason": "break"})
        self.assertEqual(paused.task.id, task.id)
        self.assertEqual(paused.task.stage, STAGE_EXECUTION)
        self.assertEqual(paused.task.current_step_index, 2)

        resumed = apply(paused.task, EVENT_RESUME, payload={})
        self.assertEqual(resumed.task.id, task.id)
        self.assertEqual(resumed.task.stage, STAGE_EXECUTION)
        self.assertEqual(resumed.task.current_step_index, 2)
        self.assertEqual(resumed.task.expected_action_type, EXPECTED_RUN_STEP)
        self.assertEqual(resumed.task.pause_reason, "")
        self.assertEqual(resumed.task.version, 6)


class TransitionExplanationTest(unittest.TestCase):
    """Unit coverage of the Day 15 refusal explanation (FR-10, FR-11)."""

    STATES = (
        dict(
            stage=STAGE_PLANNING,
            status=STATUS_ACTIVE,
            expected_action_type=EXPECTED_RUN_PLANNING,
        ),
        dict(
            stage=STAGE_PLANNING,
            status=STATUS_ACTIVE,
            expected_action_type=EXPECTED_CONFIRM_PLAN,
        ),
        dict(
            stage=STAGE_EXECUTION,
            status=STATUS_ACTIVE,
            expected_action_type=EXPECTED_RUN_STEP,
        ),
        dict(
            stage=STAGE_EXECUTION,
            status=STATUS_ACTIVE,
            expected_action_type=EXPECTED_FINISH_EXECUTION,
        ),
        dict(
            stage=STAGE_VALIDATION,
            status=STATUS_ACTIVE,
            expected_action_type=EXPECTED_RUN_VALIDATION,
        ),
        dict(
            stage=STAGE_EXECUTION,
            status=STATUS_PAUSED,
            expected_action_type=EXPECTED_RUN_STEP,
        ),
        dict(
            stage=STAGE_EXECUTION,
            status=STATUS_BLOCKED,
            expected_action_type=EXPECTED_USER_ACTION,
        ),
        dict(
            stage=STAGE_DONE,
            status=STATUS_COMPLETED,
            expected_action_type=EXPECTED_REVIEW_RESULT,
        ),
        dict(
            stage=STAGE_VALIDATION,
            status=STATUS_CANCELLED,
            expected_action_type=EXPECTED_NONE,
        ),
    )

    def test_explain_transition_never_drifts_from_can_apply(self):
        for state in self.STATES:
            task = make_task(**state)
            for last_event in (None, EVENT_API_ERROR, EVENT_PAUSE):
                allowed = tuple(can_apply(task, last_event_type=last_event))
                for action in DOMAIN_ACTIONS:
                    with self.subTest(
                        state=state, last_event=last_event, action=action
                    ):
                        decision = explain_transition(
                            task, action, last_event_type=last_event
                        )
                        self.assertEqual(decision.action, action)
                        self.assertEqual(decision.allowed, action in allowed)
                        self.assertEqual(decision.allowed_actions, allowed)

    def test_allowed_action_keeps_the_empty_reason(self):
        decision = explain_transition(make_task(), ACTION_RUN_PLANNING)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, REASON_ALLOWED)
        self.assertIn("Run planning", decision.message)

    def test_allowed_action_with_empty_reason_keeps_the_positive_message(self):
        decision = explain_transition(make_task(), ACTION_RUN_PLANNING)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, REASON_ALLOWED)
        self.assertEqual(
            format_refusal(decision),
            "Run planning is allowed in the current state.",
        )

    def test_allowed_action_with_a_reason_is_rendered_as_a_refusal(self):
        # The orchestrator can allow an action while overriding its reason: the
        # cancel of an unconfirmed request is refused by the caller, even though
        # ACTION_CANCEL is in the allowed set.
        decision = TransitionDecision(
            action=ACTION_CANCEL,
            allowed=True,
            reason=REASON_CONFIRMATION_REQUIRED,
            allowed_actions=(
                ACTION_RUN_PLANNING,
                ACTION_PAUSE,
                ACTION_BLOCK,
                ACTION_CANCEL,
            ),
        )

        message = format_refusal(decision)

        self.assertNotIn("is allowed in the current state", message)
        self.assertIn(REASON_TEXTS[REASON_CONFIRMATION_REQUIRED], message)
        self.assertIn("Cancel", message)
        self.assertIn("Allowed now:", message)
        self.assertIn("Run planning", message)

    def test_missing_task_reason(self):
        decision = explain_transition(None, ACTION_RUN_PLANNING)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_TASK_NOT_FOUND)
        self.assertEqual(decision.allowed_actions, ())

    def test_terminal_reason_for_done_and_cancelled(self):
        for task in (
            make_task(
                stage=STAGE_DONE,
                status=STATUS_COMPLETED,
                expected_action_type=EXPECTED_REVIEW_RESULT,
            ),
            make_task(
                status=STATUS_CANCELLED, expected_action_type=EXPECTED_NONE
            ),
        ):
            for action in (ACTION_CANCEL, ACTION_RUN_STEP, ACTION_PAUSE):
                with self.subTest(status=task.status, action=action):
                    decision = explain_transition(task, action)
                    self.assertFalse(decision.allowed)
                    self.assertEqual(decision.reason, REASON_TERMINAL)

    def test_paused_and_blocked_reasons(self):
        paused = make_task(status=STATUS_PAUSED)
        blocked = make_task(
            status=STATUS_BLOCKED, expected_action_type=EXPECTED_USER_ACTION
        )
        self.assertEqual(
            explain_transition(paused, ACTION_RUN_PLANNING).reason, REASON_PAUSED
        )
        self.assertEqual(
            explain_transition(blocked, ACTION_RUN_STEP).reason, REASON_BLOCKED
        )

    def test_stage_bound_actions_before_approval_are_a_mismatch(self):
        task = make_task()
        for action in (ACTION_RUN_STEP, ACTION_RUN_VALIDATION, ACTION_ACCEPT_PLAN):
            with self.subTest(action=action):
                decision = explain_transition(task, action)
                self.assertFalse(decision.allowed)
                self.assertEqual(
                    decision.reason, REASON_EXPECTED_ACTION_MISMATCH
                )

    def test_status_only_action_is_not_allowed(self):
        task = make_task()
        for action in (ACTION_UNBLOCK, ACTION_RESUME):
            with self.subTest(action=action):
                decision = explain_transition(task, action)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, REASON_NOT_ALLOWED)

    def test_finish_execution_with_incomplete_steps(self):
        task = make_task(
            stage=STAGE_EXECUTION,
            status=STATUS_ACTIVE,
            current_step_index=1,
            expected_action_type=EXPECTED_RUN_STEP,
        )
        progress = [StepProgress(1, "Step 1"), StepProgress(2, "Step 2")]
        decision = explain_transition(task, ACTION_FINISH_EXECUTION, progress=progress)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_PROGRESS_INCOMPLETE)

    def test_finish_execution_with_complete_progress_but_wrong_expectation(self):
        task = make_task(
            stage=STAGE_EXECUTION,
            status=STATUS_ACTIVE,
            current_step_index=1,
            expected_action_type=EXPECTED_RUN_STEP,
        )
        progress = [StepProgress(1, "Step 1", completed=True)]
        decision = explain_transition(task, ACTION_FINISH_EXECUTION, progress=progress)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_EXPECTED_ACTION_MISMATCH)

    def test_retry_requires_an_api_error(self):
        task = make_task()
        refused = explain_transition(task, ACTION_RETRY, last_event_type=EVENT_RETRY)
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.reason, REASON_RETRY_REQUIRES_API_ERROR)

        allowed = explain_transition(
            task, ACTION_RETRY, last_event_type=EVENT_API_ERROR
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.reason, REASON_ALLOWED)

    def test_format_refusal_names_the_action_and_the_reason(self):
        done = make_task(
            stage=STAGE_DONE,
            status=STATUS_COMPLETED,
            expected_action_type=EXPECTED_REVIEW_RESULT,
        )
        message = format_refusal(explain_transition(done, ACTION_PAUSE))
        self.assertIn("Pause", message)
        self.assertIn(REASON_TEXTS[REASON_TERMINAL], message)
        self.assertIn("No action is allowed", message)

    def test_format_refusal_lists_the_allowed_actions(self):
        message = format_refusal(explain_transition(make_task(), ACTION_RUN_STEP))
        self.assertIn(REASON_TEXTS[REASON_EXPECTED_ACTION_MISMATCH], message)
        self.assertIn("Allowed now:", message)
        self.assertIn("Run planning", message)

    def test_format_allowed_actions(self):
        self.assertEqual(format_allowed_actions(()), "none")
        self.assertEqual(
            format_allowed_actions((ACTION_RUN_PLANNING,)), "Run planning"
        )
        rendered = format_allowed_actions((ACTION_RUN_PLANNING, ACTION_PAUSE))
        self.assertIn("Run planning", rendered)
        self.assertIn("Pause", rendered)

    def test_action_labels_cover_every_domain_and_ui_action(self):
        for action in tuple(DOMAIN_ACTIONS) + tuple(UI_ACTIONS):
            with self.subTest(action=action):
                self.assertIn(action, ACTION_LABELS)
                self.assertTrue(ACTION_LABELS[action])

    def test_refusal_reasons_are_documented_and_exclude_allowed(self):
        for code in REFUSAL_REASONS:
            with self.subTest(code=code):
                self.assertIn(code, REASON_TEXTS)
                self.assertNotEqual(code, REASON_ALLOWED)
        self.assertNotIn(REASON_ALLOWED, REFUSAL_REASONS)
        self.assertEqual(REASON_TEXTS[REASON_ALLOWED], "")


if __name__ == "__main__":
    unittest.main()
