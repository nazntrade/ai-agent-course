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
import unittest
from types import SimpleNamespace

from streamlit.proto.RootContainer_pb2 import RootContainer as RootContainerProto
from streamlit.testing.v1 import AppTest

from agent import AgentConfig
from storage import ChatStore
from task_orchestrator import TaskOrchestrator
from task_storage import TaskRepository
from task_ui import (
    badge_label,
    event_summary,
    format_stage_indicator,
    format_task_compact,
    format_task_details,
    shorten,
)
from tasks import (
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
    STAGE_EXECUTION,
    apply_transition,
)

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

        # The bare object client cannot answer, so the action fails: the card
        # reports the error, the placeholder is cleared by the app callback and
        # the repository records only API_ERROR.
        self.button(app, "task_action_run_step").click().run(timeout=30)
        self.assert_no_exception(app)

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
        self.assertIn("specification rev 1", labels)
        self.assertIn("plan rev 1", labels)

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
