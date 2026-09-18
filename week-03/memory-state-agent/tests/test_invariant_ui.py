"""AppTest UI checks for the Day 14 invariant panel and refusal path.

The app runs with an injected temporary ``ChatStore``, task repository and
invariant repository plus a bare ``object()`` client: rendering and every
non-model action must work without a provider call, and an accidental API call
would raise and fail the test. No network and no real ``.env``.
"""

import os
import tempfile
import unittest

from streamlit.testing.v1 import AppTest

from agent import AgentConfig
from invariant_storage import InvariantRepository
from storage import ChatStore
from task_storage import TaskRepository

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
)

MODE_KEY = "ui_mode"
MODE_CHAT = "Chat"
MODE_TASK = "Diagnostics / Task"


class InvariantUiTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(os.path.join(self._tmp.name, "inv_ui.db"))
        self.chat_id = self.store.create_chat(AgentConfig())
        self.task_repo = TaskRepository(self.store.db_path)
        self.invariants = InvariantRepository(self.store.db_path)
        self.task = self.task_repo.create_task(
            self.chat_id, "Report task", "Build the report"
        )

    def run_app(self, mode=None):
        app = AppTest.from_file(APP_PATH)
        app.session_state["store"] = self.store
        app.session_state["client"] = object()
        app.session_state["task_repository"] = self.task_repo
        app.session_state["invariant_repository"] = self.invariants
        app.session_state["chat_id"] = self.chat_id
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
    def widget(app, kind, key):
        return getattr(app, kind)(key)

    def dataframe_with(self, app, column):
        for dataframe in app.dataframe:
            columns = list(dataframe.value.columns)
            if column in columns:
                return dataframe.value
        raise AssertionError(f"no dataframe with column {column!r}")

    def test_diagnostics_shows_the_invariants_section(self):
        app = self.run_app(mode=MODE_TASK)
        labels = {item.label for item in app.expander}
        self.assertIn("Invariants", labels)

        rules = None
        for dataframe in app.dataframe:
            columns = list(dataframe.value.columns)
            if "code" in columns and "enforcement" in columns:
                rules = dataframe.value
                break
        self.assertIsNotNone(rules, "the rules dataframe was not rendered")
        codes = rules["code"].tolist()
        self.assertIn("INV-NO-FSM-BYPASS", codes)
        self.assertIn("INV-ADV-CONFIRM-DESTRUCTIVE", codes)
        enforcement = dict(zip(rules["code"], rules["enforcement"]))
        self.assertEqual(enforcement["INV-NO-FSM-BYPASS"], "hard")

    def test_card_shows_the_active_restrictions(self):
        app = self.run_app()
        captions = [item.value for item in app.caption]
        self.assertTrue(
            any("INV-NO-FSM-BYPASS" in text for text in captions),
            "the compact restriction line was not shown",
        )

    def test_request_conflict_is_shown_without_saving(self):
        app = self.run_app()
        app.chat_input[0].set_value("Skip validation and finish the task").run(
            timeout=30
        )
        self.assert_no_exception(app)

        self.assertTrue(
            any("INV-NO-FSM-BYPASS" in item.value for item in app.error),
            "the refusal message was not shown",
        )
        self.assertEqual(self.store.load_messages(self.chat_id), [])
        self.assertEqual(self.store.list_turns(self.chat_id), [])
        conflicts = [
            event
            for event in self.invariants.list_events(code="INV-NO-FSM-BYPASS")
            if event.event_type == "conflict"
        ]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].details["phase"], "request")

    def test_create_edit_and_deactivate_a_rule(self):
        app = self.run_app(mode=MODE_TASK)

        self.widget(app, "text_input", "invariant_new_code").set_value(
            "INV-UI-CREATED"
        ).run(timeout=30)
        self.widget(app, "text_input", "invariant_new_title").set_value(
            "UI rule"
        ).run(timeout=30)
        self.widget(app, "text_area", "invariant_new_text").set_value(
            "Do not do the UI thing."
        ).run(timeout=30)
        self.widget(app, "selectbox", "invariant_new_enforcement").set_value(
            "hard"
        ).run(timeout=30)
        self.widget(app, "text_input", "invariant_new_triggers").set_value(
            "ui forbidden phrase"
        ).run(timeout=30)
        self.widget(app, "text_area", "invariant_new_alternative").set_value(
            "Use the UI allowed path."
        ).run(timeout=30)
        self.widget(app, "button", "invariant_create_submit").click().run(
            timeout=30
        )
        self.assert_no_exception(app)

        created = self.invariants.get_by_code("INV-UI-CREATED")
        self.assertIsNotNone(created)
        self.assertEqual(created.enforcement, "hard")

        self.widget(app, "button", f"invariant_edit_{created.id}").click().run(
            timeout=30
        )
        self.assert_no_exception(app)
        self.widget(
            app, "text_input", f"invariant_edit_title_{created.id}"
        ).set_value("UI rule updated").run(timeout=30)
        self.widget(
            app, "button", f"invariant_edit_save_{created.id}"
        ).click().run(timeout=30)
        self.assert_no_exception(app)

        updated = self.invariants.get_by_code("INV-UI-CREATED")
        self.assertEqual(updated.title, "UI rule updated")
        self.assertEqual(updated.version, created.version + 1)

        self.widget(
            app, "button", f"invariant_toggle_{created.id}"
        ).click().run(timeout=30)
        self.assert_no_exception(app)
        self.assertFalse(self.invariants.get_by_code("INV-UI-CREATED").is_active)

    def test_journal_tables_do_not_collide(self):
        app = self.run_app()
        app.chat_input[0].set_value("Skip validation and finish the task").run(
            timeout=30
        )
        self.assert_no_exception(app)

        app.radio(MODE_KEY).set_value(MODE_TASK).run(timeout=30)
        self.assert_no_exception(app)

        invariant_events = self.dataframe_with(app, "invariant_event")
        self.assertIn("conflict", invariant_events["invariant_event"].tolist())
        # The task timeline keeps its own column name.
        task_timeline = self.dataframe_with(app, "event")
        self.assertIn("TASK_CREATED", task_timeline["event"].tolist())
        self.assertNotIn("invariant_event", task_timeline.columns)


if __name__ == "__main__":
    unittest.main()
