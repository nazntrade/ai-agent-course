"""Regression tests for the canonical Day 15 demo scenario (``task_demo``).

Level U: the pure demo fixture, the workflow-step guard and the planning prompt
rule are checked without any I/O. Level F: a FakeClient orchestrator scenario
runs the canonical demo task end to end and proves that planning and
``Accept plan`` are external transitions while every plan step runs in the
execution stage. No network and no real ``.env``.
"""

import json
import unittest

from task_demo import (
    CORRECTED_PLAN_EXAMPLE,
    DEMO_EXECUTION_PLAN,
    DEMO_EXTERNAL_TRANSITIONS,
    DEMO_TASK_BRIEF,
    DEMO_TASK_GOAL,
    DEMO_TASK_TITLE,
    PHASE_AFTER_EXECUTION,
    PHASE_BEFORE_EXECUTION,
    REPORTED_LOOPBACK_PLAN,
    REPORTED_LIVE_PLAN,
    REPORTED_PLAN_REV1,
    REPORTED_TASK_GOAL,
    WORKFLOW_STEP_MARKERS,
    external_transition_actions,
    workflow_step_titles,
)
from task_orchestrator import STATUS_NOOP, STATUS_SUCCESS
from task_prompts import (
    REASON_MISSING_ENTITY,
    REASON_NONEXISTENT_TRANSITION,
    REASON_REQUIRES_OTHER_STAGE,
    TASK_PLANNING_SYSTEM_PROMPT,
    ExecutionIncompatiblePlanError,
    build_plan_messages,
    parse_plan_response,
    plan_step_violations,
    plan_violations,
)
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_FINISH_EXECUTION,
    ACTION_RUN_PLANNING,
    ACTION_RUN_VALIDATION,
    EVENT_EXECUTION_FINISHED,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_STEP_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_VALIDATION_PASSED,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
    STATUS_COMPLETED,
)
from tests.test_task_orchestrator import (
    OrchestratorTestCase,
    plan_response,
    stream_step,
    validation_response,
)

# The defect fixture: the steps that used to be generated for the demo task and
# are impossible in the execution stage after ``Accept plan``.
DEFECT_EXECUTION_PLAN = {
    "summary": "Как не надо: шаги описывают сам workflow, а не задачу.",
    "acceptance_criteria": ["Дефектный пример"],
    "steps": [
        {
            "index": 1,
            "title": "Проверка planning",
            "description": "Проверить стадию планирования перед выполнением.",
        },
        {
            "index": 2,
            "title": "Формирование плана",
            "description": "Сформировать и сохранить план задачи.",
        },
        {
            "index": 3,
            "title": "Принятие плана",
            "description": "Принять план и перейти к выполнению.",
        },
    ],
}

# Stable phrases the regression pins in the planning system prompt. They are
# the observable contract of the guidance change.
PROMPT_EXECUTION_STEPS_RULE = (
    "Шаги плана — это действия и проверки самой задачи, выполнимые в стадии "
    "execution."
)
PROMPT_NO_WORKFLOW_STEPS_RULE = "Не включай в план шаги про сам workflow и переходы"
PROMPT_PLANNING_STAGE_NOTE = (
    "Текущая стадия planning — это этап формирования самого плана"
)
PROMPT_STEPS_RUN_IN_EXECUTION_RULE = (
    "шаги плана будут выполняться в стадии execution"
)
PROMPT_PREVIOUS_STAGE_FACT_RULE = "как уже состоявшийся факт"
PROMPT_STAGE_EXCLUSIVITY_RULE = (
    "не может одновременно находиться в planning и execution"
)
PROMPT_ACTION_STAGE_REMINDER = (
    "Напоминание: шаги плана выполняются в стадии execution"
)
PROMPT_CRITERIA_RESULTS_RULE = (
    "Критерии приёмки формулируй только про результаты самой задачи"
)
PROMPT_CRITERIA_NO_FUTURE_RULE = (
    "нельзя требовать в критериях validation, done или VALIDATION_PASSED"
)
PROMPT_LOOPBACK_GUARD_RULE = (
    "Возврат в planning допустим в плане только как проверка запрета через "
    "Transition guard или аудит отказов"
)


class ReportedDefectFixtureTest(unittest.TestCase):
    """Level U: the real reported defect and its corrected plan."""

    def test_reported_plan_rev1_is_detected_by_its_three_real_titles(self):
        self.assertEqual(
            workflow_step_titles(REPORTED_PLAN_REV1),
            ("Проверка планирования", "Формирование плана", "Принятие плана"),
        )
        self.assertEqual(REPORTED_TASK_GOAL, "Day 15 — ручная приёмка переходов")

    def test_reported_plan_rev1_is_rejected_as_execution_incompatible(self):
        # The defect is about impossible steps, not about broken JSON: the
        # fixture is structurally valid but the runtime parser now rejects it.
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_PLAN_REV1))
        self.assertEqual(
            tuple(
                violation.title for violation in context.exception.violations
            ),
            ("Проверка планирования", "Формирование плана", "Принятие плана"),
        )

    def test_reported_live_plan_flags_exactly_steps_three_to_five(self):
        violations = plan_step_violations(REPORTED_LIVE_PLAN)
        self.assertEqual(
            [violation.index for violation in violations], [3, 4, 5]
        )
        self.assertEqual(
            [violation.title for violation in violations],
            [
                "Выполнить и проверить validation",
                "Проверить завершение задачи",
                "На тестовой копии состояния проверить переходы",
            ],
        )
        self.assertEqual(
            [violation.reason for violation in violations],
            [
                REASON_REQUIRES_OTHER_STAGE,
                REASON_REQUIRES_OTHER_STAGE,
                REASON_MISSING_ENTITY,
            ],
        )
        # Steps 1-2 are ordinary execution actions and stay clean.
        first_two = {"steps": REPORTED_LIVE_PLAN["steps"][:2]}
        self.assertEqual(workflow_step_titles(first_two), ())

    def test_reported_live_plan_is_rejected_with_step_and_criterion_violations(self):
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_LIVE_PLAN))
        self.assertEqual(
            [
                (violation.location, violation.index)
                for violation in context.exception.violations
            ],
            [("step", 3), ("step", 4), ("step", 5), ("criterion", 3)],
        )

    def test_reported_loopback_plan_flags_steps_and_criteria(self):
        violations = plan_violations(REPORTED_LOOPBACK_PLAN)
        self.assertEqual(
            [
                (violation.location, violation.index, violation.reason)
                for violation in violations
            ],
            [
                ("step", 3, REASON_NONEXISTENT_TRANSITION),
                ("step", 4, REASON_REQUIRES_OTHER_STAGE),
                ("step", 5, REASON_REQUIRES_OTHER_STAGE),
                ("criterion", 1, REASON_REQUIRES_OTHER_STAGE),
                ("criterion", 2, REASON_REQUIRES_OTHER_STAGE),
            ],
        )
        # Steps 1-2 are ordinary execution actions and stay clean on both
        # surfaces; the step view never reports a criterion.
        first_two = {
            "steps": REPORTED_LOOPBACK_PLAN["steps"][:2],
            "acceptance_criteria": REPORTED_LOOPBACK_PLAN["acceptance_criteria"],
        }
        self.assertEqual(
            [v.location for v in plan_violations(first_two)],
            ["criterion", "criterion"],
        )
        step_violations = plan_step_violations(REPORTED_LOOPBACK_PLAN)
        self.assertEqual([v.index for v in step_violations], [3, 4, 5])
        self.assertTrue(all(v.location == "step" for v in step_violations))

    def test_guard_frame_allows_the_loopback_prohibition_check(self):
        guarded = {
            "steps": [
                {
                    "index": 1,
                    "title": (
                        "Проверить через Transition guard/аудит отказов, что "
                        "возврат в planning отклоняется"
                    ),
                }
            ]
        }
        self.assertEqual(plan_step_violations(guarded), ())
        unguarded = {
            "steps": [
                {"index": 1, "title": "Вернуть задачу в planning и повторить переход"}
            ]
        }
        violations = plan_step_violations(unguarded)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].reason, REASON_NONEXISTENT_TRANSITION)

    def test_plan_accepted_needs_a_retrospective_frame(self):
        with_frame = {
            "steps": [
                {"index": 1, "title": "Проверить событие PLAN_ACCEPTED в Event timeline"}
            ]
        }
        without_frame = {
            "steps": [{"index": 1, "title": "Проверить событие PLAN_ACCEPTED"}]
        }
        self.assertEqual(plan_step_violations(with_frame), ())
        violations = plan_step_violations(without_frame)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].reason, REASON_REQUIRES_OTHER_STAGE)

    def test_step_completed_event_check_is_not_a_future_done_item(self):
        # ``completed`` is a substring of ``step_completed``: a retrospective
        # check of that already-recorded event is legitimate in execution
        # (DEMO_PLAN §6.2), so the event names are masked before the token test.
        for title in (
            "Проверить, что STEP_COMPLETED зафиксирован в Event timeline",
            "Проверить, что событие step_completed записано в журнал",
            "Сверить, что EXECUTION_FINISHED зафиксирован в Event timeline",
        ):
            plan = {"steps": [{"index": 1, "title": title}]}
            with self.subTest(title=title):
                self.assertEqual(plan_step_violations(plan), ())

    def test_retrospective_frame_does_not_excuse_a_future_stage_check(self):
        # A timeline frame makes a past-stage fact check valid, but it must not
        # legitimize a check of a stage that has not happened yet.
        plan = {
            "steps": [
                {
                    "index": 1,
                    "title": "Проверить стадию validation по Event timeline",
                }
            ]
        }
        violations = plan_step_violations(plan)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].reason, REASON_REQUIRES_OTHER_STAGE)

    def test_bare_refusal_word_is_not_a_guard_frame(self):
        unguarded = {
            "steps": [{"index": 1, "title": "Вернуть в planning после отказа"}]
        }
        violations = plan_step_violations(unguarded)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].reason, REASON_NONEXISTENT_TRANSITION)

        guarded = {
            "steps": [
                {
                    "index": 1,
                    "title": (
                        "Проверить через Transition guard, что возврат в "
                        "planning отклоняется"
                    ),
                }
            ]
        }
        self.assertEqual(plan_step_violations(guarded), ())

    def test_code_verb_items_naming_a_future_status_are_accepted(self):
        for title in (
            "Добавить статус completed в ответ",
            "Реализовать переход в done",
        ):
            plan = {"steps": [{"index": 1, "title": title}]}
            with self.subTest(title=title):
                self.assertEqual(plan_step_violations(plan), ())

    def test_future_status_negative_controls_stay_rejected(self):
        rejected = {
            "steps": [
                {"index": 1, "title": "Убедиться, что задача завершена (done)"},
                {"index": 2, "title": "Подтвердить событие VALIDATION_PASSED"},
            ],
            "acceptance_criteria": [
                "Событие VALIDATION_PASSED подтверждено.",
                "Задача завершена.",
            ],
        }
        self.assertEqual(
            [
                (violation.location, violation.index, violation.reason)
                for violation in plan_violations(rejected)
            ],
            [
                ("step", 1, REASON_REQUIRES_OTHER_STAGE),
                ("step", 2, REASON_REQUIRES_OTHER_STAGE),
                ("criterion", 1, REASON_REQUIRES_OTHER_STAGE),
                ("criterion", 2, REASON_REQUIRES_OTHER_STAGE),
            ],
        )

    def test_reported_loopback_plan_is_rejected_by_the_parser(self):
        with self.assertRaises(ExecutionIncompatiblePlanError) as context:
            parse_plan_response(json.dumps(REPORTED_LOOPBACK_PLAN))
        locations = {v.location for v in context.exception.violations}
        self.assertEqual(locations, {"step", "criterion"})

    def test_retrospective_framing_allows_a_past_stage_check(self):
        with_frame = {
            "steps": [
                {
                    "index": 1,
                    "title": "Проверить стадию planning по Event timeline",
                }
            ]
        }
        without_frame = {
            "steps": [{"index": 1, "title": "Проверить стадию planning"}]
        }
        self.assertEqual(plan_step_violations(with_frame), ())
        violations = plan_step_violations(without_frame)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].reason, REASON_REQUIRES_OTHER_STAGE)

    def test_neutral_plan_about_validation_and_planning_is_accepted(self):
        neutral = {
            "summary": "Реализовать функцию валидации и планировщик.",
            "acceptance_criteria": ["Функция валидации работает на корректных данных"],
            "steps": [
                {
                    "index": 1,
                    "title": "Реализовать проверку обхода validation",
                    "description": "Добавить в код проверку, обходящую validation.",
                },
                {
                    "index": 2,
                    "title": "Написать тесты для планировщика",
                    "description": "Покрыть тестами модуль планировщика.",
                },
            ],
        }
        self.assertEqual(plan_step_violations(neutral), ())
        # The criteria surface is checked too: a task that implements the
        # validation stage is not blocked by its own criterion.
        self.assertEqual(plan_violations(neutral), ())
        parsed = parse_plan_response(json.dumps(neutral))
        self.assertEqual(len(parsed["steps"]), 2)

    def test_code_verb_items_about_the_stages_are_accepted(self):
        for title in (
            "Реализовать проверку обхода validation",
            "Добавить стадию validation",
            "Написать тесты для планировщика",
        ):
            plan = {"steps": [{"index": 1, "title": title}]}
            with self.subTest(title=title):
                self.assertEqual(plan_step_violations(plan), ())

    def test_corrected_plan_example_is_clean(self):
        self.assertEqual(workflow_step_titles(CORRECTED_PLAN_EXAMPLE), ())

    def test_corrected_plan_example_passes_strict_parsing(self):
        parsed = parse_plan_response(json.dumps(CORRECTED_PLAN_EXAMPLE))
        self.assertEqual(
            [step["index"] for step in parsed["steps"]], [1, 2, 3]
        )
        self.assertTrue(parsed["acceptance_criteria"])
        self.assertEqual(
            [step["title"] for step in parsed["steps"]],
            [step["title"] for step in CORRECTED_PLAN_EXAMPLE["steps"]],
        )

    def test_corrected_plan_verifies_previous_stages_as_facts(self):
        titles = " ".join(step["title"] for step in CORRECTED_PLAN_EXAMPLE["steps"])
        self.assertIn("PLAN_ACCEPTED", titles)
        self.assertIn("plan rev 1", titles)
        self.assertIn("Transition guard", titles)
        # No corrected step asks the task to run planning again.
        for step in CORRECTED_PLAN_EXAMPLE["steps"]:
            with self.subTest(step=step["title"]):
                self.assertNotIn("принятие плана", step["title"].casefold())

    def test_corrected_plan_has_no_future_validation_or_done(self):
        blob = json.dumps(CORRECTED_PLAN_EXAMPLE).casefold()
        for forbidden in ("validation", "done", "completed"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, blob)
        self.assertEqual(plan_violations(CORRECTED_PLAN_EXAMPLE), ())

    def test_future_and_prior_stage_items_are_classified_on_both_surfaces(self):
        plan = {
            "acceptance_criteria": [
                "Переход в execution подтверждён.",
                "Результат проверен и задача завершена.",
            ],
            "steps": [
                {"index": 1, "title": "Проверить стадию validation"},
                {"index": 2, "title": "Добавить стадию validation"},
            ],
        }
        self.assertEqual(
            [
                (v.location, v.index, v.reason)
                for v in plan_violations(plan)
            ],
            [
                ("step", 1, REASON_REQUIRES_OTHER_STAGE),
                ("criterion", 2, REASON_REQUIRES_OTHER_STAGE),
            ],
        )


class DemoFixtureTest(unittest.TestCase):
    """Level U: the canonical plan and the workflow-step guard."""

    def test_demo_execution_plan_is_clean_and_strictly_valid(self):
        self.assertEqual(workflow_step_titles(DEMO_EXECUTION_PLAN), ())
        parsed = parse_plan_response(json.dumps(DEMO_EXECUTION_PLAN))
        self.assertEqual(
            [step["index"] for step in parsed["steps"]], [1, 2, 3]
        )
        self.assertTrue(parsed["acceptance_criteria"])
        self.assertEqual(
            [step["title"] for step in parsed["steps"]],
            [step["title"] for step in DEMO_EXECUTION_PLAN["steps"]],
        )

    def test_demo_execution_plan_has_continuous_indexes_and_no_empty_step(self):
        steps = DEMO_EXECUTION_PLAN["steps"]
        self.assertEqual(
            [step["index"] for step in steps], list(range(1, len(steps) + 1))
        )
        for step in steps:
            self.assertTrue(str(step["title"]).strip())
            self.assertTrue(str(step["description"]).strip())

    def test_defect_fixture_is_detected(self):
        detected = workflow_step_titles(DEFECT_EXECUTION_PLAN)
        self.assertEqual(
            detected,
            ("Проверка planning", "Формирование плана", "Принятие плана"),
        )
        self.assertEqual(
            external_transition_actions(PHASE_BEFORE_EXECUTION),
            ("create_task", ACTION_RUN_PLANNING, ACTION_ACCEPT_PLAN),
        )

    def test_detection_is_case_insensitive_and_scans_descriptions(self):
        plan = {
            "summary": "mixed",
            "acceptance_criteria": ["c"],
            "steps": [
                {
                    "index": 1,
                    "title": "Deploy the service",
                    "description": "Then run VALIDATION on the result.",
                },
                {
                    "index": 2,
                    "title": "Collect the metrics",
                    "description": "Read the exported counters.",
                },
            ],
        }
        self.assertEqual(workflow_step_titles(plan), ("Deploy the service",))

    def test_guard_ignores_malformed_plans(self):
        for plan in (None, {}, {"steps": "nope"}, {"steps": [None, 42]}):
            with self.subTest(plan=plan):
                self.assertEqual(workflow_step_titles(plan), ())

    def test_markers_cover_the_defect_fixture_phrases(self):
        markers = tuple(marker.casefold() for marker in WORKFLOW_STEP_MARKERS)
        for phrase in ("Проверка planning", "Формирование плана", "Принятие плана"):
            with self.subTest(phrase=phrase):
                self.assertTrue(any(marker in phrase.casefold() for marker in markers))


class DemoExternalTransitionsTest(unittest.TestCase):
    """Level U: the ordered external transitions around the execution stage."""

    def test_before_execution_has_run_planning_and_accept_plan(self):
        actions = external_transition_actions(PHASE_BEFORE_EXECUTION)
        self.assertEqual(
            actions, ("create_task", ACTION_RUN_PLANNING, ACTION_ACCEPT_PLAN)
        )
        self.assertLess(actions.index(ACTION_RUN_PLANNING), actions.index(ACTION_ACCEPT_PLAN))

    def test_after_execution_has_finish_execution_and_run_validation(self):
        actions = external_transition_actions(PHASE_AFTER_EXECUTION)
        self.assertEqual(
            actions, (ACTION_FINISH_EXECUTION, ACTION_RUN_VALIDATION)
        )

    def test_ordered_set_places_planning_and_accept_before_execution(self):
        ordered = external_transition_actions()
        self.assertEqual(
            ordered,
            (
                "create_task",
                ACTION_RUN_PLANNING,
                ACTION_ACCEPT_PLAN,
                ACTION_FINISH_EXECUTION,
                ACTION_RUN_VALIDATION,
            ),
        )
        last_before = max(
            ordered.index(action)
            for action in external_transition_actions(PHASE_BEFORE_EXECUTION)
        )
        first_after = min(
            ordered.index(action)
            for action in external_transition_actions(PHASE_AFTER_EXECUTION)
        )
        self.assertLess(last_before, first_after)

    def test_every_transition_uses_a_domain_stage_and_a_unique_action(self):
        stages = (STAGE_PLANNING, STAGE_EXECUTION, STAGE_VALIDATION)
        actions = [transition.action for transition in DEMO_EXTERNAL_TRANSITIONS]
        self.assertEqual(len(actions), len(set(actions)))
        for transition in DEMO_EXTERNAL_TRANSITIONS:
            with self.subTest(action=transition.action):
                self.assertIn(transition.stage, stages)
                self.assertIn(
                    transition.phase,
                    (PHASE_BEFORE_EXECUTION, PHASE_AFTER_EXECUTION),
                )


class PlanningPromptRuleTest(unittest.TestCase):
    """Level U: the planning prompt forbids workflow steps."""

    def test_prompt_requires_execution_stage_steps(self):
        self.assertIn(PROMPT_EXECUTION_STEPS_RULE, TASK_PLANNING_SYSTEM_PROMPT)

    def test_prompt_forbids_workflow_steps(self):
        self.assertIn(PROMPT_NO_WORKFLOW_STEPS_RULE, TASK_PLANNING_SYSTEM_PROMPT)
        self.assertIn("внешние переходы", TASK_PLANNING_SYSTEM_PROMPT)

    def test_prompt_marks_planning_as_plan_formation_only(self):
        self.assertIn(PROMPT_PLANNING_STAGE_NOTE, TASK_PLANNING_SYSTEM_PROMPT)
        self.assertIn(
            PROMPT_STEPS_RUN_IN_EXECUTION_RULE, TASK_PLANNING_SYSTEM_PROMPT
        )

    def test_prompt_checks_previous_stages_as_facts(self):
        self.assertIn(PROMPT_PREVIOUS_STAGE_FACT_RULE, TASK_PLANNING_SYSTEM_PROMPT)
        self.assertIn("Event timeline", TASK_PLANNING_SYSTEM_PROMPT)

    def test_prompt_forbids_planning_and_execution_at_once(self):
        self.assertIn(PROMPT_STAGE_EXCLUSIVITY_RULE, TASK_PLANNING_SYSTEM_PROMPT)

    def test_prompt_forbids_future_validation_and_done_in_criteria(self):
        self.assertIn(PROMPT_CRITERIA_RESULTS_RULE, TASK_PLANNING_SYSTEM_PROMPT)
        self.assertIn(PROMPT_CRITERIA_NO_FUTURE_RULE, TASK_PLANNING_SYSTEM_PROMPT)

    def test_prompt_restricts_the_planning_loopback(self):
        self.assertIn(PROMPT_LOOPBACK_GUARD_RULE, TASK_PLANNING_SYSTEM_PROMPT)
        self.assertIn("Transition guard", TASK_PLANNING_SYSTEM_PROMPT)

    def test_action_message_repeats_the_stage_reminder(self):
        messages = build_plan_messages(REPORTED_TASK_GOAL, DEMO_TASK_BRIEF)
        action = messages[1]["content"]
        self.assertIn("run_planning", action)
        self.assertIn(PROMPT_ACTION_STAGE_REMINDER, action)
        self.assertIn(PROMPT_PREVIOUS_STAGE_FACT_RULE, action)
        self.assertIn("Transition guard", action)
        self.assertIn("Критерии приёмки", action)


class DemoScenarioTest(OrchestratorTestCase):
    """Level F: the canonical demo task runs end to end on a FakeClient."""

    def _demo_orchestrator(self):
        steps = DEMO_EXECUTION_PLAN["steps"]
        script = [plan_response(DEMO_EXECUTION_PLAN)]
        script.extend(
            stream_step(f"Result of {step['title']}") for step in steps
        )
        script.append(validation_response(passed=True))
        return self.make_orchestrator(script), steps

    def test_demo_scenario_runs_every_step_in_execution(self):
        orchestrator, steps = self._demo_orchestrator()
        created = self.create_task(
            orchestrator,
            title=DEMO_TASK_TITLE,
            goal=DEMO_TASK_GOAL,
            task_brief=DEMO_TASK_BRIEF,
        )
        task = created.task

        planning = orchestrator.run_planning(task.id)
        self.assertEqual(planning.status, STATUS_SUCCESS)
        stored_plan = self.repo.load_plan(task.id)
        self.assertEqual(stored_plan, DEMO_EXECUTION_PLAN)
        self.assertEqual(workflow_step_titles(stored_plan), ())

        # Planning and Accept plan are external transitions: execution actions
        # are refused until the plan is accepted and never reach the provider.
        refused = orchestrator.run_step(task.id)
        self.assertEqual(refused.status, STATUS_NOOP)
        self.assertEqual(self.client.calls, 1)

        accepted = orchestrator.accept_plan(task.id)
        self.assertEqual(accepted.status, STATUS_SUCCESS)
        self.assertEqual(accepted.task.stage, STAGE_EXECUTION)

        for step in steps:
            current = self.repo.get_task(task.id)
            self.assertEqual(current.stage, STAGE_EXECUTION)
            self.assertEqual(current.current_step, step["title"])
            result = orchestrator.run_step(task.id, on_chunk=lambda text: None)
            self.assertEqual(result.status, STATUS_SUCCESS)

        finished = orchestrator.finish_execution(task.id)
        self.assertEqual(finished.status, STATUS_SUCCESS)
        self.assertEqual(finished.task.stage, STAGE_VALIDATION)

        validated = orchestrator.run_validation(task.id)
        self.assertEqual(validated.status, STATUS_SUCCESS)
        self.assertEqual(validated.task.status, STATUS_COMPLETED)

        # One planning call, one call per step and one validation call: the
        # refused execution action did not call the provider.
        self.assertEqual(self.client.calls, 1 + len(steps) + 1)

        events = [event.event_type for event in self.repo.list_events(task.id)]
        self.assertEqual(
            events,
            [
                EVENT_TASK_CREATED,
                EVENT_PLAN_CREATED,
                EVENT_PLAN_ACCEPTED,
                *[EVENT_STEP_COMPLETED] * len(steps),
                EVENT_EXECUTION_FINISHED,
                EVENT_VALIDATION_PASSED,
            ],
        )

    def test_planning_call_sends_the_workflow_rule_to_the_provider(self):
        # Level F: the rule must survive the StageContextBuilder packet, i.e.
        # the provider actually receives it in the planning request.
        orchestrator = self.make_orchestrator([plan_response(DEMO_EXECUTION_PLAN)])
        created = self.create_task(
            orchestrator,
            title=DEMO_TASK_TITLE,
            goal=DEMO_TASK_GOAL,
            task_brief=DEMO_TASK_BRIEF,
        )

        planning = orchestrator.run_planning(created.task.id)
        self.assertEqual(planning.status, STATUS_SUCCESS)

        payload = self.client.payloads[0]
        system_text = "\n".join(
            message["content"]
            for message in payload["messages"]
            if message["role"] == "system"
        )
        self.assertIn(PROMPT_EXECUTION_STEPS_RULE, system_text)
        self.assertIn(PROMPT_PREVIOUS_STAGE_FACT_RULE, system_text)
        self.assertIn(PROMPT_STAGE_EXCLUSIVITY_RULE, system_text)

        action = payload["messages"][-1]["content"]
        self.assertIn(PROMPT_ACTION_STAGE_REMINDER, action)

    def test_demo_plan_step_count_matches_the_execution_transitions(self):
        # The execution plan is consistent with the external transition set:
        # planning and Accept plan happen outside it, finish/validation after it.
        before = external_transition_actions(PHASE_BEFORE_EXECUTION)
        after = external_transition_actions(PHASE_AFTER_EXECUTION)
        self.assertIn(ACTION_RUN_PLANNING, before)
        self.assertIn(ACTION_ACCEPT_PLAN, before)
        self.assertIn(ACTION_FINISH_EXECUTION, after)
        self.assertIn(ACTION_RUN_VALIDATION, after)
        self.assertEqual(workflow_step_titles(DEMO_EXECUTION_PLAN), ())


if __name__ == "__main__":
    unittest.main()
