"""UI tests for the Day 13 task state machine (``task_ui`` and ``app.py``).

The pure formatters are checked without Streamlit. The app tests run through
``streamlit.testing.v1.AppTest`` with a temporary SQLite database and a bare
``object()`` client: rendering and every non-model action must work without a
provider call, and any accidental API call would raise and fail the test. Task
states are prepared directly through the FSM and the repository, so the tests
never depend on a fake provider script.
"""

import os
import tempfile
import threading
import unittest
from types import SimpleNamespace

from streamlit.proto.RootContainer_pb2 import RootContainer as RootContainerProto
from streamlit.testing.v1 import AppTest

import task_runner
from agent import AgentConfig
from storage import ChatStore
from task_orchestrator import TaskOrchestrator
from task_storage import TaskRepository
from task_ui import (
    STEP_RUN_TOKEN_KEY,
    badge_label,
    build_step_results,
    event_summary,
    format_next_action,
    format_plan,
    format_stage_indicator,
    format_task_compact,
    format_task_details,
    running_step_label,
    shorten,
    step_result_label,
)
from tasks import (
    ARTIFACT_TASK_BRIEF,
    ARTIFACT_EXECUTION_RESULT,
    EVENT_API_ERROR,
    EVENT_BLOCK,
    EVENT_CANCEL,
    EVENT_EXECUTION_FINISHED,
    EVENT_PAUSE,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_STEP_COMPLETED,
    EVENT_VALIDATION_PASSED,
    EXPECTED_CONFIRM_PLAN,
    EXPECTED_RUN_PLANNING,
    EXPECTED_RUN_STEP,
    EXPECTED_RUN_VALIDATION,
    STAGE_EXECUTION,
    apply_transition,
)
from tests.test_agent import FakeClient
from tests.test_task_orchestrator import stream_step

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
)

MODE_KEY = "ui_mode"
MODE_CHAT = "Chat"
MODE_DIAGNOSTICS = "Diagnostics / Memory"
MODE_TASK = "Diagnostics / Task"

PLAN_TWO_STEPS = {
    "summary": "Two step plan",
    "acceptance_criteria": ["First criterion"],
    "steps": [
        {"index": 1, "title": "Step one", "description": "Do the first thing"},
        {"index": 2, "title": "Step two", "description": "Do the second thing"},
    ],
}

DIAGNOSTIC_LABELS = {
    "Short-term memory · current line",
    "Working memory · this chat & line",
    "Long-term memory · shared across chats",
    "System prompt & invariants (not memory)",
    "User profiles",
    "History compression",
    "Sticky Facts",
    "Branches",
    "Current chat statistics",
    "Conversation comparison",
}

TASK_LABELS = {
    "Create task",
    "Event timeline",
    "Artifacts",
    "Workflow",
    "Context packet preview",
    "Task usage",
    "Plan",
    "Execution results",
}


def stage_row(indicator, label):
    """Return the indicator ``div`` that carries one stage label."""
    for chunk in indicator.split("</div>"):
        if label in chunk:
            return chunk
    raise AssertionError(f"stage {label!r} is missing from the indicator")


class FormatterTestCase(unittest.TestCase):
    """Pure formatter checks: no Streamlit, no database, no provider."""

    def task(self, **changes):
        from tasks import Task

        base = Task(
            id=1,
            chat_id=1,
            title="Report task",
            goal="Build the report",
            stage="planning",
            status="active",
            current_step="Step one",
            current_step_index=1,
            expected_action_type="confirm_plan",
            expected_action_text="Accept or reject the plan",
            version=3,
        )
        return base.evolve(**changes) if changes else base

    def test_indicator_orders_stages_top_to_bottom(self):
        indicator = format_stage_indicator(self.task(stage="execution"))
        positions = [
            indicator.index(label)
            for label in ("Planning", "Execution", "Validation", "Done")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertGreaterEqual(indicator.count('class="task-stage-link"'), 3)
        self.assertIn("border-left:1px solid", indicator)

    def test_indicator_styles_passed_current_and_upcoming(self):
        indicator = format_stage_indicator(self.task(stage="validation"))

        passed = stage_row(indicator, "Planning")
        self.assertIn("opacity:0.45", passed)
        self.assertIn("font-size:0.92rem", passed)
        self.assertIn("text-decoration:line-through", passed)
        self.assertIn("✓", passed)

        current = stage_row(indicator, "Validation")
        self.assertIn("opacity:1", current)
        self.assertIn("font-size:1.10rem", current)
        self.assertIn("font-weight:700", current)
        self.assertIn("●", current)

        upcoming = stage_row(indicator, "Done")
        self.assertIn("opacity:0.5", upcoming)
        self.assertIn("font-size:0.90rem", upcoming)
        self.assertIn("text-decoration:none", upcoming)
        self.assertIn("○", upcoming)

    def test_indicator_shows_step_and_expected_action_under_current_stage(self):
        indicator = format_stage_indicator(self.task())

        planning = stage_row(indicator, "Planning")
        self.assertIn("Step: Step one", planning)
        self.assertIn("Next: Accept or reject the plan", planning)
        self.assertIn("RUNNING", planning)

    def test_indicator_blocked_shows_reason_and_expected_user_action(self):
        task = self.task(
            status="blocked",
            pause_reason="The API spec is missing",
            expected_action_text="Upload the API spec",
        )
        indicator = format_stage_indicator(task)

        current = stage_row(indicator, "Planning")
        self.assertIn("BLOCKED", current)
        self.assertIn("Blocked: The API spec is missing", current)
        self.assertIn("Expected from you: Upload the API spec", current)
        self.assertNotIn("Next:", current)

    def test_indicator_escapes_html_from_user_text(self):
        task = self.task(
            status="blocked",
            pause_reason="<script>alert(1)</script>",
            expected_action_text="<img src=x onerror=alert(2)>",
        )
        indicator = format_stage_indicator(task)

        self.assertNotIn("<script>", indicator)
        self.assertNotIn("<img", indicator)
        self.assertIn("&lt;script&gt;", indicator)
        self.assertIn("&lt;img", indicator)

    def test_indicator_is_empty_without_task(self):
        self.assertEqual(format_stage_indicator(None), "")

    def test_badge_label_covers_every_status(self):
        self.assertEqual(badge_label(self.task()), "RUNNING")
        self.assertEqual(badge_label(self.task(status="paused")), "PAUSED")
        self.assertEqual(badge_label(self.task(status="blocked")), "BLOCKED")
        self.assertEqual(
            badge_label(self.task(status="completed")), "COMPLETED"
        )
        self.assertEqual(
            badge_label(self.task(status="cancelled")), "CANCELLED"
        )

    def test_shorten_collapses_and_truncates(self):
        self.assertEqual(shorten("  a   b\n c "), "a b c")
        self.assertEqual(shorten("abcdefghij", 5), "abcd…")
        self.assertLessEqual(len(shorten("x" * 100, 10)), 10)
        self.assertEqual(shorten(None), "")
        self.assertEqual(shorten("short", 50), "short")

    def test_format_task_compact_summarizes_the_result(self):
        task = self.task(stage="done", status="completed", version=7)
        text = format_task_compact(task, PLAN_TWO_STEPS)
        self.assertIn("Report task", text)
        self.assertIn("done/completed", text)
        self.assertIn("2 steps", text)
        self.assertIn("v7", text)

    def test_format_task_details_keeps_the_full_text(self):
        long_title = "T" * 150
        long_goal = "G" * 200
        long_reason = "R" * 200
        long_expected = "E" * 200
        task = self.task(
            title=long_title,
            goal=long_goal,
            status="blocked",
            pause_reason=long_reason,
            expected_action_text=long_expected,
        )
        event = SimpleNamespace(
            event_type="BLOCK",
            from_stage="execution",
            to_stage="execution",
            from_status="active",
            to_status="blocked",
            created_at="2026-09-17 12:00:00",
        )

        details = format_task_details(task, event)

        for fragment in (long_title, long_goal, long_reason, long_expected):
            with self.subTest(fragment=fragment[:8]):
                self.assertIn(fragment, details)
        self.assertNotIn("…", details)
        self.assertIn("Blocked:", details)
        self.assertIn("Expected from you:", details)
        self.assertIn("Last event: BLOCK", details)

    def test_format_task_details_without_task_or_event(self):
        self.assertEqual(format_task_details(None), "")
        self.assertEqual(event_summary(None), "no events yet")

        task = self.task(status="paused", pause_reason="waiting")
        details = format_task_details(task)
        self.assertIn("Pause reason: waiting", details)
        self.assertIn("Expected action: Accept or reject the plan", details)
        self.assertIn("Last event: no events yet", details)

    def test_format_plan_renders_summary_criteria_and_steps(self):
        text = format_plan(PLAN_TWO_STEPS)

        self.assertIn("**Summary:** Two step plan", text)
        self.assertIn("**Acceptance criteria**", text)
        self.assertIn("- First criterion", text)
        self.assertIn("**1. Step one**", text)
        self.assertIn("Do the first thing", text)
        self.assertIn("**2. Step two**", text)
        self.assertIn("Do the second thing", text)
        self.assertNotIn("…", text)

    def test_format_plan_handles_missing_or_malformed_values(self):
        self.assertEqual(format_plan(None), "No plan yet.")
        self.assertEqual(format_plan("not a plan"), "No plan yet.")
        self.assertEqual(format_plan([1, 2]), "No plan yet.")

        empty = format_plan({})
        self.assertIn("**Summary:** —", empty)
        self.assertIn("**Acceptance criteria:** —", empty)
        self.assertIn("**Steps:** —", empty)

        partial = format_plan({"summary": "Only a summary"})
        self.assertIn("Only a summary", partial)
        self.assertIn("**Acceptance criteria:** —", partial)
        self.assertIn("**Steps:** —", partial)

    def test_format_plan_lists_unknown_fields(self):
        text = format_plan(
            {
                "summary": "Plan",
                "acceptance_criteria": ["Hold"],
                "steps": [{"index": 1, "title": "Do it"}],
                "owner": "team",
                "budget": 3,
            }
        )

        self.assertIn("**Other fields**", text)
        self.assertIn('- owner: "team"', text)
        self.assertIn("- budget: 3", text)
        # Known fields are not repeated in the technical list.
        self.assertNotIn("- summary:", text)
        self.assertNotIn("- steps:", text)

    def test_format_plan_does_not_truncate_a_long_description(self):
        description = "D" * 400
        text = format_plan(
            {
                "summary": "S",
                "acceptance_criteria": ["C"],
                "steps": [{"index": 1, "title": "T", "description": description}],
            }
        )

        self.assertIn(description, text)
        self.assertNotIn("…", text)

    def test_format_plan_skips_invalid_steps_without_raising(self):
        text = format_plan(
            {
                "summary": "S",
                "acceptance_criteria": ["C", "", None],
                "steps": [
                    "not a mapping",
                    {"index": "one", "title": "Bad index"},
                    {"index": 2, "title": ""},
                    {"index": 3, "title": "Kept"},
                ],
            }
        )

        self.assertIn("- C", text)
        self.assertIn("**3. Kept**", text)
        self.assertNotIn("Bad index", text)

    # --- Step results -----------------------------------------------------

    def test_running_step_label_names_the_step_and_the_plan_size(self):
        task = self.task(
            stage="execution",
            current_step="Step two",
            current_step_index=2,
            expected_action_type=EXPECTED_RUN_STEP,
        )
        self.assertEqual(
            running_step_label(task, PLAN_TWO_STEPS),
            "Running step 2 of 2: Step two",
        )

    def test_running_step_label_falls_back_without_a_plan_or_index(self):
        self.assertEqual(
            running_step_label(
                self.task(current_step_index=None), PLAN_TWO_STEPS
            ),
            "Running the task step...",
        )
        self.assertEqual(
            running_step_label(self.task(), None),
            "Running the task step...",
        )
        self.assertEqual(
            running_step_label(None, PLAN_TWO_STEPS),
            "Running the task step...",
        )

    def _execution_artifact(self, artifact_id, revision, content):
        return SimpleNamespace(
            kind=ARTIFACT_EXECUTION_RESULT,
            id=artifact_id,
            revision=revision,
            content=content,
        )

    def test_build_step_results_keeps_the_latest_revision_per_step(self):
        artifacts = [
            self._execution_artifact(
                1, 1, {"step_index": 1, "round": 1, "text": "first"}
            ),
            self._execution_artifact(
                2, 2, {"step_index": 2, "round": 1, "text": "second"}
            ),
            self._execution_artifact(
                3, 3, {"step_index": 1, "round": 2, "text": "reworked"}
            ),
        ]

        views = build_step_results(PLAN_TWO_STEPS, artifacts)

        self.assertEqual([view["step_index"] for view in views], [1, 2])
        self.assertEqual(views[0]["total"], 2)
        self.assertEqual(views[0]["text"], "reworked")
        self.assertEqual(views[0]["round"], 2)
        self.assertEqual(views[1]["text"], "second")
        self.assertEqual(views[1]["round"], 1)
        # The most recent revision (step 1 rev 3) is the expanded one.
        self.assertTrue(views[0]["is_latest"])
        self.assertFalse(views[1]["is_latest"])

    def test_build_step_results_skips_missing_results_and_empty_text(self):
        empty = [
            self._execution_artifact(
                1, 1, {"step_index": 1, "round": 1, "text": "   "}
            )
        ]
        self.assertEqual(build_step_results(PLAN_TWO_STEPS, empty), [])
        self.assertEqual(build_step_results(PLAN_TWO_STEPS, []), [])
        self.assertEqual(build_step_results(None, []), [])
        self.assertEqual(build_step_results({}, []), [])

    def test_build_step_results_uses_progress_round_when_absent(self):
        artifacts = [
            self._execution_artifact(
                1, 1, {"step_index": 1, "text": "done"}
            )
        ]

        views = build_step_results(PLAN_TWO_STEPS, artifacts)

        self.assertEqual(views[0]["round"], 1)

    def test_step_result_label_adds_revision_and_rework(self):
        view = {
            "step_index": 2,
            "total": 3,
            "title": "Step two",
            "round": 1,
            "awaiting_rework": False,
        }
        self.assertEqual(step_result_label(view), "Step 2 of 3: Step two")

        view["round"] = 2
        self.assertEqual(
            step_result_label(view), "Step 2 of 3: Step two · revision 2"
        )

        view["awaiting_rework"] = True
        self.assertEqual(
            step_result_label(view),
            "Step 2 of 3: Step two · revision 2 · awaiting rework",
        )

    def test_format_next_action_covers_the_task_states(self):
        running = self.task(
            stage="execution",
            current_step="Step two",
            current_step_index=2,
            expected_action_type=EXPECTED_RUN_STEP,
            expected_action_text="Run the current step",
        )
        self.assertEqual(
            format_next_action(running, PLAN_TWO_STEPS),
            "Next: Run step 2 of 2: Step two",
        )

        blocked = self.task(
            status="blocked", expected_action_text="Upload the API spec"
        )
        self.assertEqual(
            format_next_action(blocked, PLAN_TWO_STEPS),
            "Expected from you: Upload the API spec",
        )

        paused = self.task(
            status="paused", expected_action_text="Run the current step"
        )
        self.assertEqual(
            format_next_action(paused, PLAN_TWO_STEPS),
            "Paused. Next action: Run the current step",
        )

        validation = self.task(
            stage="validation",
            expected_action_type=EXPECTED_RUN_VALIDATION,
            current_step_index=None,
        )
        self.assertEqual(
            format_next_action(validation, PLAN_TWO_STEPS),
            "Next: Run validation",
        )

        planning = self.task(
            stage="planning",
            expected_action_type=EXPECTED_RUN_PLANNING,
            current_step_index=None,
        )
        self.assertEqual(
            format_next_action(planning, PLAN_TWO_STEPS), "Next: Run planning"
        )

        confirm = self.task(expected_action_type=EXPECTED_CONFIRM_PLAN)
        self.assertEqual(
            format_next_action(confirm, PLAN_TWO_STEPS),
            "Next: Accept or reject the plan",
        )

        done = self.task(stage="done", status="completed")
        self.assertEqual(
            format_next_action(done, PLAN_TWO_STEPS), "Task completed."
        )

        cancelled = self.task(status="cancelled")
        self.assertEqual(
            format_next_action(cancelled, PLAN_TWO_STEPS), "Task cancelled."
        )

        self.assertEqual(format_next_action(None, None), "No active task.")


class TaskUiTestCase(unittest.TestCase):
    """A temporary database with one chat plus the UI helpers."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(os.path.join(self._tmp.name, "task_ui.db"))
        self.chat_id = self.store.create_chat(AgentConfig())
        self.repo = TaskRepository(self.store.db_path)
        self.orchestrator = TaskOrchestrator(
            self.store, repository=self.repo, client=object()
        )

    def tearDown(self):
        task_runner.get_registry().clear()

    # --- state builders ---------------------------------------------------

    def apply(self, task, event, payload=None):
        progress = self.repo.step_progress(task.id)
        transition = apply_transition(
            task,
            event,
            payload=payload,
            progress=progress,
            last_event_type=self.repo.latest_event_type(task.id),
        )
        return self.repo.commit_transition(task.id, task.version, transition)

    def new_task(self, **kwargs):
        return self.repo.create_task(
            self.chat_id,
            kwargs.pop("title", "Report task"),
            kwargs.pop("goal", "Build the report"),
            **kwargs,
        )

    def planning_task(self, **kwargs):
        return self.new_task(**kwargs)

    def confirm_plan_task(self):
        return self.apply(
            self.planning_task(), EVENT_PLAN_CREATED, {"plan": PLAN_TWO_STEPS}
        )

    def execution_task(self):
        return self.apply(self.confirm_plan_task(), EVENT_PLAN_ACCEPTED)

    def finish_execution_task(self):
        task = self.execution_task()
        task = self.apply(task, EVENT_STEP_COMPLETED, {"text": "step one done"})
        return self.apply(task, EVENT_STEP_COMPLETED, {"text": "step two done"})

    def validation_task(self):
        return self.apply(self.finish_execution_task(), EVENT_EXECUTION_FINISHED)

    def done_task(self):
        return self.apply(
            self.validation_task(),
            EVENT_VALIDATION_PASSED,
            {"passed": True, "defects": [], "notes": "Looks good"},
        )

    def paused_task(self):
        return self.apply(self.execution_task(), EVENT_PAUSE)

    def blocked_task(self):
        return self.apply(
            self.execution_task(),
            EVENT_BLOCK,
            {
                "reason": "Need the API spec",
                "expected_action_text": "Upload the API spec",
            },
        )

    def blocked_long_task(self):
        """A planning task whose every long field exceeds the card limits."""
        task = self.new_task(
            title="Report task " + "with a deliberately long title " * 6,
            goal="Build the report " + "covering every requested section " * 8,
        )
        return self.apply(
            task,
            EVENT_BLOCK,
            {
                "reason": "Need the API spec " + "and the access list " * 8,
                "expected_action_text": (
                    "Upload the API spec " + "and confirm the access " * 8
                ),
            },
        )

    def cancelled_task(self):
        return self.apply(self.execution_task(), EVENT_CANCEL, {"confirmed": True})

    def api_error_task(self):
        task = self.execution_task()
        self.repo.append_event(
            task.id,
            EVENT_API_ERROR,
            payload={"kind": "stream_error", "message": "connection dropped"},
        )
        return self.repo.get_task(task.id)

    def last_task(self):
        return self.repo.list_tasks(self.chat_id)[-1]

    # --- AppTest helpers --------------------------------------------------

    def run_app(self, mode=None, chat_id=None, inject_task_state=True):
        app = AppTest.from_file(APP_PATH)
        app.session_state["store"] = self.store
        app.session_state["client"] = object()
        if inject_task_state:
            app.session_state["task_repository"] = self.repo
            app.session_state["task_orchestrator"] = self.orchestrator
        if chat_id is not None:
            app.session_state["chat_id"] = chat_id
        app.run(timeout=30)
        self.assert_no_exception(app)
        if mode is not None:
            app.radio(MODE_KEY).set_value(mode).run(timeout=30)
            self.assert_no_exception(app)
        return app

    @staticmethod
    def assert_no_exception(app):
        if len(app.exception) != 0:
            raise AssertionError(
                f"AppTest raised: {[item.value for item in app.exception]}"
            )

    @staticmethod
    def button(app, key):
        return next(item for item in app.button if item.key == key)

    @staticmethod
    def keys(app):
        return {node.key for node in app if getattr(node, "key", None)}

    @staticmethod
    def action_keys(app):
        return {
            item.key
            for item in app.button
            if item.key and item.key.startswith("task_action_")
        }

    @staticmethod
    def column_containing(app, key):
        return next(
            column
            for column in app.columns
            if any(getattr(node, "key", None) == key for node in column)
        )

    @staticmethod
    def parent_block(root, target):
        for node in root:
            children = getattr(node, "children", None)
            if children and any(child is target for child in children.values()):
                return node
        raise AssertionError("the element has no parent block")

    @staticmethod
    def expander_with_label(app, label):
        return next(item for item in app.expander if item.label == label)

    @staticmethod
    def block_markdown(block):
        return [item.value for item in block.markdown]

    def assert_no_nested_expanders(self, node, inside=False):
        """Fail when any expander is nested inside another expander."""
        is_expander = getattr(node, "type", None) == "expander"
        if inside and is_expander:
            self.fail("an expander is nested inside another expander")
        for child in getattr(node, "children", {}).values():
            self.assert_no_nested_expanders(child, inside or is_expander)

    def assert_app_has_no_nested_expanders(self, app):
        # The AppTest root keeps main, sidebar and event blocks: walk each one
        # because the root itself does not expose its children directly.
        for index in range(len(app)):
            self.assert_no_nested_expanders(app[index])

    # --- Chat regression --------------------------------------------------

    def test_chat_without_task_shows_invitation_and_keeps_chat_clean(self):
        app = self.run_app()

        self.assertTrue(
            any("No active task" in item.value for item in app.markdown)
        )
        self.assertIn("task_new_task", {item.key for item in app.button})
        self.assertEqual(len(app.chat_input), 1)
        self.assertFalse(
            TASK_LABELS & {item.label for item in app.expander}
        )
        self.assertFalse(
            DIAGNOSTIC_LABELS & {item.label for item in app.expander}
        )
        self.assertFalse(
            any(
                key.startswith(("profile_", "mem_", "branch_"))
                for key in self.keys(app)
            )
        )
        self.assertFalse(
            any(node.type == "expander" for node in app.main)
        )

    def test_card_is_first_in_bottom_and_profile_row_stays_below_it(self):
        self.execution_task()
        app = self.run_app()

        indicator = next(
            item
            for item in app.markdown
            if "task-stage-indicator" in item.value
        )
        selector_column = self.column_containing(
            app, f"active_profile_{self.chat_id}"
        )
        profile_row = self.parent_block(app, selector_column)
        bottom = app[RootContainerProto.BOTTOM]
        children = list(bottom.children.values())

        self.assertIs(children[0], indicator)
        self.assertIs(self.parent_block(app, profile_row), bottom)
        self.assertLess(children.index(indicator), children.index(profile_row))
        self.assertLess(
            children.index(profile_row), children.index(app.chat_input[0])
        )
        self.assertFalse(
            any(node.type == "chat_message" for node in bottom)
        )

    # --- Actions per state ------------------------------------------------

    def test_card_shows_only_allowed_actions_per_state(self):
        cases = [
            (
                "planning",
                self.planning_task,
                {
                    "run_planning",
                    "pause",
                    "block",
                    "cancel",
                    "new_task",
                    "open_diagnostics",
                },
            ),
            (
                "confirm_plan",
                self.confirm_plan_task,
                {
                    "accept_plan",
                    "reject_plan",
                    "pause",
                    "block",
                    "cancel",
                    "new_task",
                    "open_diagnostics",
                },
            ),
            (
                "execution",
                self.execution_task,
                {
                    "run_step",
                    "pause",
                    "block",
                    "cancel",
                    "new_task",
                    "open_diagnostics",
                },
            ),
            (
                "finish_execution",
                self.finish_execution_task,
                {
                    "finish_execution",
                    "pause",
                    "block",
                    "cancel",
                    "new_task",
                    "open_diagnostics",
                },
            ),
            (
                "validation",
                self.validation_task,
                {
                    "run_validation",
                    "pause",
                    "block",
                    "cancel",
                    "new_task",
                    "open_diagnostics",
                },
            ),
            (
                "paused",
                self.paused_task,
                {"resume", "block", "cancel", "new_task", "open_diagnostics"},
            ),
            (
                "blocked",
                self.blocked_task,
                {"unblock", "cancel", "new_task", "open_diagnostics"},
            ),
            (
                "completed",
                self.done_task,
                {"open_result", "new_task"},
            ),
            (
                "cancelled",
                self.cancelled_task,
                {"new_task"},
            ),
        ]
        for name, build, expected in cases:
            with self.subTest(state=name):
                build()
                app = self.run_app()
                self.assertEqual(
                    self.action_keys(app),
                    {f"task_action_{action}" for action in expected},
                )

    def test_retry_is_shown_only_after_an_api_error(self):
        self.execution_task()
        app = self.run_app()
        self.assertNotIn("task_action_retry", self.action_keys(app))

        self.api_error_task()
        app = self.run_app()
        self.assertIn("task_action_retry", self.action_keys(app))

    def test_blocked_card_shows_reason_and_expected_action(self):
        self.blocked_task()
        app = self.run_app()

        indicator = next(
            item
            for item in app.markdown
            if "task-stage-indicator" in item.value
        )
        self.assertIn("Blocked: Need the API spec", indicator.value)
        self.assertIn("Expected from you: Upload the API spec", indicator.value)
        self.assertIn("BLOCKED", indicator.value)

    def test_cancel_requires_confirmation(self):
        self.execution_task()
        app = self.run_app()

        self.button(app, "task_action_cancel").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(self.last_task().status, "active")
        self.assertTrue(
            any("irreversible" in item.value for item in app.warning)
        )
        self.assertIn("task_cancel_confirm", {item.key for item in app.button})

        self.button(app, "task_cancel_confirm").click().run(timeout=30)
        self.assert_no_exception(app)
        self.assertEqual(self.last_task().status, "cancelled")

    def test_cancel_can_be_kept(self):
        self.execution_task()
        app = self.run_app()

        self.button(app, "task_action_cancel").click().run(timeout=30)
        self.button(app, "task_cancel_keep").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(self.last_task().status, "active")
        self.assertIn("task_action_cancel", self.action_keys(app))

    def test_pause_resume_round_trip_through_the_card(self):
        self.execution_task()
        app = self.run_app()

        self.button(app, "task_action_pause").click().run(timeout=30)
        self.assert_no_exception(app)
        self.assertEqual(self.last_task().status, "paused")
        self.assertIn("task_action_resume", self.action_keys(app))

        self.button(app, "task_action_resume").click().run(timeout=30)
        self.assert_no_exception(app)
        self.assertEqual(self.last_task().status, "active")

    def test_block_form_requires_reason_and_expected_action(self):
        self.execution_task()
        app = self.run_app()

        self.button(app, "task_action_block").click().run(timeout=30)
        self.assert_no_exception(app)
        submit = self.button(app, "task_block_submit")
        submit.click().run(timeout=30)
        self.assert_no_exception(app)
        self.assertEqual(self.last_task().status, "active")
        self.assertTrue(any(item.value for item in app.error))

        next(
            item for item in app.text_input if item.key == "task_block_reason"
        ).set_value("Need the API spec")
        next(
            item
            for item in app.text_input
            if item.key == "task_block_expected_action"
        ).set_value("Upload the API spec")
        app.run(timeout=30)
        self.button(app, "task_block_submit").click().run(timeout=30)
        self.assert_no_exception(app)

        task = self.last_task()
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.pause_reason, "Need the API spec")

    def test_unblock_restores_the_expected_action(self):
        self.blocked_task()
        app = self.run_app()

        self.button(app, "task_action_unblock").click().run(timeout=30)
        self.assert_no_exception(app)

        task = self.last_task()
        self.assertEqual(task.status, "active")
        self.assertEqual(task.expected_action_type, "run_step")

    def test_done_card_shows_result_and_new_task(self):
        self.done_task()
        app = self.run_app()

        self.assertIn("task_action_open_result", self.action_keys(app))
        self.assertIn("task_action_new_task", self.action_keys(app))
        self.assertNotIn("task_action_cancel", self.action_keys(app))
        self.assertTrue(
            any("COMPLETED" in item.value for item in app.markdown)
        )

    def test_open_full_result_dialog_shows_the_final_result(self):
        self.done_task()
        app = self.run_app()

        self.button(app, "task_action_open_result").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertIn(
            "task_result_diagnostics", {item.key for item in app.button}
        )
        self.assertTrue(
            any(
                "Task result" in item.value for item in app.code
            )
        )

    def test_open_diagnostics_card_button_switches_the_mode(self):
        self.execution_task()
        app = self.run_app()

        self.button(app, "task_action_open_diagnostics").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(app.radio(MODE_KEY).value, MODE_TASK)
        self.assertIn("Task state", [item.value for item in app.subheader])

    def test_result_dialog_diagnostics_button_switches_the_mode(self):
        self.done_task()
        app = self.run_app()

        self.button(app, "task_action_open_result").click().run(timeout=30)
        self.assert_no_exception(app)
        self.button(app, "task_result_diagnostics").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(app.radio(MODE_KEY).value, MODE_TASK)
        self.assertIn("Task state", [item.value for item in app.subheader])

    def test_details_button_opens_the_full_text_dialog(self):
        task = self.blocked_long_task()
        app = self.run_app()

        self.button(app, "task_details").click().run(timeout=30)
        self.assert_no_exception(app)

        details = [
            item.value
            for item in app.markdown
            if "Goal:" in item.value and "Last event:" in item.value
        ]
        self.assertTrue(details, "the details dialog was not rendered")
        text = details[0]
        self.assertIn(task.title, text)
        self.assertIn(task.goal, text)
        self.assertIn(task.pause_reason, text)
        self.assertIn(task.expected_action_text, text)
        self.assertNotIn("…", text)

    def test_diagnostics_task_fields_are_not_shortened(self):
        task = self.blocked_long_task()
        app = self.run_app(mode=MODE_TASK)

        fields = [
            item.value
            for item in app.markdown
            if "Pause/block reason:" in item.value
        ]
        self.assertTrue(fields, "the task fields were not rendered")
        self.assertIn(task.pause_reason, fields[0])
        self.assertIn(task.expected_action_text, fields[0])
        self.assertNotIn("…", fields[0])

    def test_new_task_button_opens_the_create_dialog(self):
        app = self.run_app()

        self.button(app, "task_new_task").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertTrue(
            any(
                item.key and item.key.startswith("task_dialog_create_title_")
                for item in app.text_input
            )
        )

    def test_create_task_through_the_modal_dialog(self):
        app = self.run_app()
        self.button(app, "task_new_task").click().run(timeout=30)
        self.assert_no_exception(app)

        next(
            item
            for item in app.text_input
            if item.key == "task_dialog_create_title_0"
        ).set_value("Dialog task")
        next(
            item
            for item in app.text_input
            if item.key == "task_dialog_create_goal_0"
        ).set_value("Write it")
        app.run(timeout=30)
        self.button(app, "task_dialog_create_submit_0").click().run(timeout=30)
        self.assert_no_exception(app)

        tasks = self.repo.list_tasks(self.chat_id)
        self.assertEqual([task.title for task in tasks], ["Dialog task"])
        self.assertEqual(self.repo.get_active_task_id(self.chat_id), tasks[0].id)

    def test_run_step_error_keeps_the_task_and_writes_no_artifact(self):
        task = self.execution_task()
        app = self.run_app()

        # The bare object client cannot answer, so the background run fails:
        # the follower reports the error, the session token is released and the
        # repository records only API_ERROR.
        self.button(app, "task_action_run_step").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertNotIn(STEP_RUN_TOKEN_KEY, app.session_state)
        self.assertFalse(task_runner.step_run_busy(task.id))
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_API_ERROR)
        self.assertEqual(
            self.repo.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT), []
        )
        self.assertEqual(self.repo.get_task(task.id).stage, STAGE_EXECUTION)
        self.assertTrue(app.error)
        self.assertIn("task_action_retry", self.action_keys(app))

    # --- Create task ------------------------------------------------------

    def test_create_task_form_in_diagnostics_creates_the_card(self):
        app = self.run_app(mode=MODE_TASK)

        title_key = next(
            item.key
            for item in app.text_input
            if item.key.startswith("task_diag_create_title_")
        )
        goal_key = next(
            item.key
            for item in app.text_input
            if item.key.startswith("task_diag_create_goal_")
        )
        next(item for item in app.text_input if item.key == title_key).set_value(
            "Release notes"
        )
        next(item for item in app.text_input if item.key == goal_key).set_value(
            "Write the release notes"
        )
        app.run(timeout=30)

        submit = next(
            item
            for item in app.button
            if item.key and item.key.startswith("task_diag_create_submit_")
        )
        submit.click().run(timeout=30)
        self.assert_no_exception(app)

        tasks = self.repo.list_tasks(self.chat_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Release notes")
        self.assertEqual(self.repo.get_active_task_id(self.chat_id), tasks[0].id)

        app.radio(MODE_KEY).set_value(MODE_CHAT).run(timeout=30)
        self.assert_no_exception(app)
        self.assertTrue(
            any(
                "task-stage-indicator" in item.value for item in app.markdown
            )
        )

    def test_empty_create_form_shows_validation_errors(self):
        app = self.run_app(mode=MODE_TASK)
        before = len(self.repo.list_tasks(self.chat_id))

        submit = next(
            item
            for item in app.button
            if item.key and item.key.startswith("task_diag_create_submit_")
        )
        submit.click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(len(self.repo.list_tasks(self.chat_id)), before)
        self.assertTrue(
            any("Title must not be empty." in item.value for item in app.error)
        )

    # --- Diagnostics / Task -----------------------------------------------

    def test_diagnostics_task_renders_every_section(self):
        self.execution_task()
        app = self.run_app(mode=MODE_TASK)

        self.assertIn("Task state", [item.value for item in app.subheader])
        labels = {item.label for item in app.expander}
        for label in TASK_LABELS:
            with self.subTest(panel=label):
                self.assertIn(label, labels)

        markdown_values = [item.value for item in app.markdown]
        self.assertTrue(
            any("specification rev 1" in value for value in markdown_values)
        )
        self.assertTrue(
            any("plan rev 1" in value for value in markdown_values)
        )
        # The artifact contents follow their headers as markdown JSON blocks
        # instead of the previous nested expander + st.json pair.
        self.assertTrue(
            any("# Task specification" in value for value in markdown_values),
            "the specification artifact content was not rendered",
        )
        self.assertTrue(
            any('"acceptance_criteria"' in value for value in markdown_values),
            "the plan artifact content was not rendered",
        )
        # No expander may sit inside another expander; st.json is not used.
        self.assert_app_has_no_nested_expanders(app)
        self.assertEqual(len(app.json), 0, "st.json must not be used anymore")

        # User profiles stay in Diagnostics / Memory, never in the task panel.
        self.assertNotIn("User profiles", labels)

        list_rows = next(
            df.value
            for df in app.dataframe
            if "stage" in df.value.columns and "title" in df.value.columns
        )
        self.assertEqual(list_rows["title"].tolist(), ["Report task"])
        self.assertEqual(list_rows["stage"].tolist(), ["execution"])

        timelines = [
            df.value
            for df in app.dataframe
            if "event" in df.value.columns
        ]
        self.assertTrue(timelines)
        events = timelines[0]["event"].tolist()
        self.assertIn("TASK_CREATED", events)
        self.assertIn("PLAN_CREATED", events)
        self.assertIn("PLAN_ACCEPTED", events)

        previews = [
            df.value
            for df in app.dataframe
            if "Block" in df.value.columns
        ]
        self.assertTrue(previews)
        blocks = previews[0]["Block"].tolist()
        self.assertIn("system_prompt", blocks)
        self.assertIn("task_snapshot", blocks)
        self.assertIn("plan", blocks)

        captions = [item.value for item in app.caption]
        self.assertTrue(
            any(
                "estimate, not billing" in text and "0 API calls" in text
                for text in captions
            )
        )
        self.assertTrue(
            any("counted separately from the chat statistics" in text for text in captions)
        )

    def test_diagnostics_plan_is_readable_and_raw_json_is_hidden(self):
        task = self.confirm_plan_task()
        app = self.run_app(mode=MODE_TASK)

        plan_expander = self.expander_with_label(app, "Plan")
        readable = "\n".join(self.block_markdown(plan_expander))
        self.assertIn("Two step plan", readable)
        self.assertIn("First criterion", readable)
        self.assertIn("Step one", readable)
        self.assertIn("Step two", readable)
        # The formatted plan does not expose the raw JSON field names.
        self.assertNotIn('"acceptance_criteria"', readable)

        raw_key = f"task_plan_raw_{task.id}"
        raw_checkbox = next(
            item for item in app.checkbox if item.key == raw_key
        )
        self.assertFalse(raw_checkbox.value)
        raw_checkbox.set_value(True).run(timeout=30)
        self.assert_no_exception(app)

        plan_expander = self.expander_with_label(app, "Plan")
        readable = "\n".join(self.block_markdown(plan_expander))
        self.assertIn('"acceptance_criteria"', readable)

    def test_diagnostics_long_artifact_is_rendered_in_full(self):
        task = self.confirm_plan_task()
        marker = "END-OF-ARTIFACT-MARKER"
        self.repo.add_artifact(
            task.id,
            "planning",
            ARTIFACT_TASK_BRIEF,
            {"text": ("A long artifact line. " * 400) + marker},
        )

        app = self.run_app(mode=MODE_TASK)

        # The unique tail marker proves the artifact is rendered to its end.
        self.assertTrue(
            any(
                marker in item.value and "A long artifact line." in item.value
                for item in app.markdown
            ),
            "the full artifact content was not rendered",
        )

    def test_review_plan_dialog_accepts_the_plan(self):
        task = self.confirm_plan_task()
        app = self.run_app()
        self.assertIn("task_review_plan", {item.key for item in app.button})

        self.button(app, "task_review_plan").click().run(timeout=30)
        self.assert_no_exception(app)

        buttons = {item.key for item in app.button}
        self.assertIn("task_plan_dialog_accept", buttons)
        self.assertIn("task_plan_dialog_reject", buttons)
        dialog_text = "\n".join(item.value for item in app.markdown)
        self.assertIn("Two step plan", dialog_text)
        self.assertIn("**1. Step one**", dialog_text)

        self.button(app, "task_plan_dialog_accept").click().run(timeout=30)
        self.assert_no_exception(app)
        self.assertEqual(self.repo.get_task(task.id).stage, STAGE_EXECUTION)

    def test_review_plan_dialog_rejects_the_plan(self):
        task = self.confirm_plan_task()
        app = self.run_app()
        self.button(app, "task_review_plan").click().run(timeout=30)
        self.assert_no_exception(app)

        self.button(app, "task_plan_dialog_reject").click().run(timeout=30)
        self.assert_no_exception(app)

        updated = self.repo.get_task(task.id)
        self.assertEqual(updated.expected_action_type, EXPECTED_RUN_PLANNING)
        self.assertIn("task_action_run_planning", self.action_keys(app))

    def test_card_accept_plan_action_still_changes_the_state(self):
        task = self.confirm_plan_task()
        app = self.run_app()

        self.button(app, "task_action_accept_plan").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(self.repo.get_task(task.id).stage, STAGE_EXECUTION)

    def test_planning_without_plan_shows_caption_without_review(self):
        self.planning_task()
        app = self.run_app(mode=MODE_TASK)

        plan_expander = self.expander_with_label(app, "Plan")
        captions = [item.value for item in plan_expander.caption]
        self.assertIn("No plan yet.", captions)
        # The no-plan placeholder must be shown exactly once, not twice.
        texts = self.block_markdown(plan_expander) + captions
        self.assertEqual(
            sum(text.count("No plan yet.") for text in texts),
            1,
            "the no-plan placeholder must be shown exactly once",
        )
        self.assertFalse(
            any(
                item.key and item.key.startswith("task_plan_raw_")
                for item in app.checkbox
            )
        )

        app.radio(MODE_KEY).set_value(MODE_CHAT).run(timeout=30)
        self.assert_no_exception(app)
        self.assertNotIn("task_review_plan", {item.key for item in app.button})

    def test_done_card_has_no_review_plan_button(self):
        self.done_task()
        app = self.run_app()
        self.assertNotIn("task_review_plan", {item.key for item in app.button})

    def test_review_plan_dialog_without_plan_shows_one_caption(self):
        task = self.planning_task()
        app = self.run_app()
        app.session_state["task_plan_dialog_task"] = task.id
        app.run(timeout=30)
        self.assert_no_exception(app)

        # The dialog shows the no-plan caption once and offers no actions.
        placeholders = [
            item.value for item in app.caption if item.value == "No plan yet."
        ]
        self.assertEqual(len(placeholders), 1)
        self.assertNotIn(
            "task_plan_dialog_accept", {item.key for item in app.button}
        )
        self.assertNotIn(
            "task_plan_dialog_reject", {item.key for item in app.button}
        )

    def test_diagnostics_memory_keeps_user_profiles_and_hides_tasks(self):
        app = self.run_app(mode=MODE_DIAGNOSTICS)

        labels = {item.label for item in app.expander}
        self.assertIn("User profiles", labels)
        for label in TASK_LABELS:
            self.assertNotIn(label, labels)
        self.assertFalse(
            any(
                item.key and item.key.startswith("task_")
                for item in app.button
            )
        )

    def test_open_selects_the_task_and_switches_the_card(self):
        first = self.planning_task(title="First task")
        second = self.planning_task(title="Second task")
        # The chat remembers the first task; the card must show it.
        self.repo.set_active_task_id(self.chat_id, first.id)

        app = self.run_app(mode=MODE_TASK)
        self.button(app, f"task_open_{second.id}").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(self.repo.get_active_task_id(self.chat_id), second.id)

        app.radio(MODE_KEY).set_value(MODE_CHAT).run(timeout=30)
        self.assert_no_exception(app)
        indicator = next(
            item
            for item in app.markdown
            if "task-stage-indicator" in item.value
        )
        self.assertIn("Second task", " ".join(
            item.value for item in app.caption
        ))
        self.assertIn("Planning", indicator.value)

    # --- Execution results panel and background step runs ----------------

    def test_execution_results_panel_shows_step_texts_in_chat(self):
        self.finish_execution_task()
        app = self.run_app()

        self.assertIn(
            "Execution results", [item.value for item in app.subheader]
        )
        labels = {item.label for item in app.expander}
        self.assertIn("Step 1 of 2: Step one", labels)
        self.assertIn("Step 2 of 2: Step two", labels)
        texts = [item.value for item in app.markdown]
        self.assertTrue(any("step one done" in text for text in texts))
        self.assertTrue(any("step two done" in text for text in texts))

    def test_execution_results_panel_survives_rerun_and_new_session(self):
        self.finish_execution_task()
        app = self.run_app()
        self.assertIn(
            "Execution results", [item.value for item in app.subheader]
        )

        app.run(timeout=30)
        self.assert_no_exception(app)
        self.assertIn(
            "Execution results", [item.value for item in app.subheader]
        )

        # A new AppTest session on the same database reads the stored results.
        other_session = self.run_app()
        self.assertIn(
            "Execution results",
            [item.value for item in other_session.subheader],
        )

    def test_execution_results_panel_hidden_without_results(self):
        self.execution_task()
        app = self.run_app()

        self.assertNotIn(
            "Execution results", [item.value for item in app.subheader]
        )
        self.assertNotIn(
            "Execution results", {item.label for item in app.expander}
        )

    def test_run_step_success_streams_and_stores_the_result(self):
        task = self.execution_task()
        # A real client streams one step; the background run must store the
        # artifact and the card must show its text in the results panel.
        client = FakeClient(script=[stream_step("Step one done")])
        self.orchestrator = TaskOrchestrator(
            self.store, repository=self.repo, client=client
        )
        app = self.run_app()

        self.button(app, "task_action_run_step").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertFalse(app.error)
        self.assertNotIn(STEP_RUN_TOKEN_KEY, app.session_state)
        self.assertFalse(task_runner.step_run_busy(task.id))
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_STEP_COMPLETED)
        results = self.repo.list_artifacts(task.id, kind=ARTIFACT_EXECUTION_RESULT)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content["text"], "Step one done")
        self.assertIn(
            "Execution results", [item.value for item in app.subheader]
        )
        self.assertTrue(
            any("Step one done" in item.value for item in app.markdown)
        )
        self.assertFalse(
            self.button(app, "task_action_run_step").proto.disabled
        )

    def test_orphan_step_run_is_cleared_with_a_notice(self):
        task = self.execution_task()
        app = self.run_app()
        token = "orphan-token"
        record = task_runner.StepRunRecord(
            token=token,
            task_id=task.id,
            chat_id=self.chat_id,
            action="run_step",
            status=task_runner.STEP_RUN_RUNNING,
            thread=None,
        )
        task_runner.get_registry()._records[token] = record
        task_runner.get_registry()._by_task[task.id] = token
        app.session_state[STEP_RUN_TOKEN_KEY] = token

        app.run(timeout=30)
        self.assert_no_exception(app)

        # The orphan is discarded, its token is released and the step button
        # becomes available again with an explanatory message.
        self.assertNotIn(STEP_RUN_TOKEN_KEY, app.session_state)
        self.assertIsNone(task_runner.step_run_record(token))
        self.assertTrue(
            any(
                "stopped unexpectedly" in item.value for item in app.error
            )
        )
        self.assertFalse(
            self.button(app, "task_action_run_step").proto.disabled
        )

    def test_foreign_live_run_keeps_the_button_disabled(self):
        task = self.execution_task()
        started = threading.Event()
        release = threading.Event()

        def worker(on_chunk):
            started.set()
            release.wait(10)

        record = task_runner.get_registry().start(
            task.id, self.chat_id, "run_step", worker
        )
        self.assertTrue(started.wait(5))
        try:
            # The token is deliberately not in this session: the app only sees
            # the process-level record and must not block on someone else's run.
            app = self.run_app()
            self.assert_no_exception(app)
            self.assertTrue(
                self.button(app, "task_action_run_step").proto.disabled
            )
            self.assertTrue(
                any("Running step" in item.value for item in app.caption)
            )
        finally:
            release.set()
            record.thread.join(10)
            task_runner.discard_step_run(record.token)

    def test_new_session_after_a_failed_run_step_has_no_busy_state(self):
        task = self.execution_task()
        app = self.run_app()
        self.button(app, "task_action_run_step").click().run(timeout=30)
        self.assert_no_exception(app)

        # A fresh session starts with empty session state and the process
        # registry holds no live run for the task, so the button reaches the
        # provider on the first click.
        fresh = self.run_app()
        self.assertNotIn(STEP_RUN_TOKEN_KEY, fresh.session_state)
        self.assertFalse(task_runner.step_run_busy(task.id))

        self.button(fresh, "task_action_run_step").click().run(timeout=30)
        self.assert_no_exception(fresh)
        self.assertEqual(self.repo.latest_event_type(task.id), EVENT_API_ERROR)
        self.assertFalse(
            any("already running" in item.value for item in fresh.error)
        )

    def test_diagnostics_task_shows_readable_execution_results(self):
        self.finish_execution_task()
        app = self.run_app(mode=MODE_TASK)

        labels = {item.label for item in app.expander}
        self.assertIn("Execution results", labels)
        expander = self.expander_with_label(app, "Execution results")
        texts = "\n".join(self.block_markdown(expander))
        self.assertIn("Step 1 of 2: Step one", texts)
        self.assertIn("step one done", texts)
        self.assertIn("step two done", texts)
        self.assertIn("Next: Finish execution", texts)
        self.assert_app_has_no_nested_expanders(app)

    # --- Empty database ---------------------------------------------------

    def test_empty_database_renders_in_all_three_modes(self):
        empty_store = ChatStore(os.path.join(self._tmp.name, "empty_tasks.db"))
        app = AppTest.from_file(APP_PATH)
        app.session_state["store"] = empty_store
        app.session_state["client"] = object()
        app.run(timeout=30)
        self.assert_no_exception(app)
        self.assertTrue(
            any("No conversations yet" in item.value for item in app.info)
        )

        for mode in (MODE_DIAGNOSTICS, MODE_TASK, MODE_CHAT):
            with self.subTest(mode=mode):
                app.radio(MODE_KEY).set_value(mode).run(timeout=30)
                self.assert_no_exception(app)
                self.assertTrue(
                    any(
                        "No conversations yet" in item.value
                        for item in app.info
                    )
                )

    def test_mode_radio_order_and_default(self):
        app = self.run_app()
        radio = app.radio(MODE_KEY)
        self.assertEqual(
            list(radio.options), [MODE_CHAT, MODE_DIAGNOSTICS, MODE_TASK]
        )
        self.assertEqual(radio.value, MODE_CHAT)

    def test_deleting_a_chat_clears_the_task_selection(self):
        task = self.planning_task()
        self.repo.set_active_task_id(self.chat_id, task.id)
        self.assertEqual(self.repo.get_active_task_id(self.chat_id), task.id)

        app = self.run_app()
        self.button(app, f"delete_{self.chat_id}").click().run(timeout=30)
        self.button(app, "delete_confirm").click().run(timeout=30)
        self.assert_no_exception(app)

        self.assertEqual(self.repo.list_tasks(self.chat_id), [])
        self.assertIsNone(self.repo.get_active_task_id(self.chat_id))


if __name__ == "__main__":
    unittest.main()
