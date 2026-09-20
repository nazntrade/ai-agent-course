"""Offline tests of the recipe's live-timeout propagation and screenshot listing.

No browser is launched: the UI helpers and the SQLite reads of the recipe are
replaced with recording fakes, so the recipe can be driven through its full flow
and every timeout it passes can be inspected.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_local_e2e
from integrations.memory_state_agent import recipe
from lib import browser
from lib.isolation import RunDir

TIMEOUT_MS = 123456


class RecordingUI:
    """Records every UI call and writes fake screenshots."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def click_button(self, page, text, **kwargs):
        self._record("click_button", page, text, **kwargs)

    def select_radio(self, page, group, option, **kwargs):
        self._record("select_radio", page, group, option, **kwargs)

    def open_expander(self, page, title, **kwargs):
        self._record("open_expander", page, title, **kwargs)

    def fill_key(self, page, key, value, **kwargs):
        self._record("fill_key", page, key, value, **kwargs)

    def click_key(self, page, key, **kwargs):
        self._record("click_key", page, key, **kwargs)

    def wait_for_key(self, page, key, **kwargs):
        self._record("wait_for_key", page, key, **kwargs)

    def button_enabled(self, page, key):
        return True

    def text_visible(self, page, text):
        return True

    def screenshot(self, page, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-png")
        return target


class FakePage:
    def __init__(self):
        self.gotos = []

    def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))


class FakeApp:
    app_url = "http://127.0.0.1:1"

    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1
        return True


class RecipeTimeoutTest(unittest.TestCase):
    def _run(self, *, step_count=4):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = RunDir("MOCK", root=tmp).create()
            ui = RecordingUI()
            page = FakePage()
            app = FakeApp()
            steps = [
                {"index": index, "title": f"step {index}", "description": ""}
                for index in range(1, step_count + 1)
            ]
            with patch.object(recipe, "ui", ui), patch.object(
                recipe, "wait_until", lambda predicate, **kwargs: True
            ), patch.object(recipe, "_scalar", lambda *a, **k: 1), patch.object(
                recipe, "_plan_steps", lambda db, task: steps
            ), patch.object(
                recipe, "_event_attempts", lambda *a, **k: 2
            ), patch.object(
                recipe, "_event_count", lambda *a, **k: 0
            ), patch.object(
                recipe, "_probe_checks", lambda *a, **k: ([], 1)
            ), patch.object(
                recipe, "_event_rows", lambda *a, **k: []
            ), patch.object(
                recipe,
                "_snapshot",
                lambda *a, **k: {"task": {}, "events": [], "artifacts": [], "attempts": []},
            ):
                result = recipe.run_recipe(
                    page,
                    db_path=run_dir.db_path,
                    run_dir=run_dir,
                    app=app,
                    title="t",
                    goal="g",
                    brief="b",
                    timeout_ms=TIMEOUT_MS,
                )
        return result, ui, page, app

    def test_every_ui_call_uses_the_run_timeout(self):
        result, ui, page, app = self._run(step_count=4)
        with self.subTest("timeouts"):
            for name, _args, kwargs in ui.calls:
                self.assertIn("timeout", kwargs, f"{name} lost its timeout")
                self.assertEqual(kwargs["timeout"], TIMEOUT_MS, name)
        # The browser navigation and the app restart also keep the budget.
        self.assertEqual(page.gotos[0][1]["timeout"], TIMEOUT_MS)
        self.assertEqual(app.restarts, 1)

    def test_run_step_click_count_follows_the_plan(self):
        result, ui, _page, _app = self._run(step_count=6)
        run_step_clicks = [
            call
            for call in ui.calls
            if call[0] == "click_key" and call[1][1] == "task_action_run_step"
        ]
        self.assertEqual(len(run_step_clicks), 6)
        self.assertEqual(result.step_total, 6)

    def test_finish_execution_wait_uses_the_run_timeout(self):
        _result, ui, _page, _app = self._run(step_count=4)
        waits = [
            call
            for call in ui.calls
            if call[0] == "wait_for_key" and call[1][1] == "task_action_finish_execution"
        ]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][2]["timeout"], TIMEOUT_MS)


class FillKeyTimeoutTest(unittest.TestCase):
    class FakeField:
        def __init__(self):
            self.waits = []
            self.values = []

        def wait_for(self, *, state, timeout):
            self.waits.append((state, timeout))

        def fill(self, value):
            self.values.append(value)

    class FakeLocator:
        def __init__(self, field):
            self._field = field

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self._field

    class FakePage:
        def __init__(self):
            self.field = FillKeyTimeoutTest.FakeField()

        def locator(self, selector):
            return FillKeyTimeoutTest.FakeLocator(self.field)

    def test_fill_key_passes_the_timeout_to_the_wait(self):
        page = self.FakePage()
        browser.fill_key(page, "task_diag_create_title_0", "hello", timeout=4321)
        self.assertEqual(page.field.waits, [("visible", 4321)])
        self.assertEqual(page.field.values, ["hello"])

    def test_fill_key_keeps_the_previous_default(self):
        page = self.FakePage()
        browser.fill_key(page, "task_diag_create_title_0", "hello")
        self.assertEqual(
            page.field.waits, [("visible", browser.DEFAULT_TIMEOUT_MS)]
        )


class UiTimeoutSignatureTest(unittest.TestCase):
    def test_every_ui_helper_accepts_a_timeout(self):
        for name in (
            "wait_for_key",
            "click_key",
            "select_radio",
            "open_expander",
            "click_button",
            "fill_key",
            "wait_for_text",
        ):
            with self.subTest(helper=name):
                signature = inspect.signature(getattr(browser, name))
                self.assertIn("timeout", signature.parameters)


class ScreenshotListingTest(unittest.TestCase):
    def test_screenshots_are_listed_even_when_the_recipe_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = RunDir("MOCK", root=tmp).create()
            (run_dir.screenshot("01_chat_created.png")).write_bytes(b"x")
            (run_dir.screenshot("03_plan_dialog.png")).write_bytes(b"x")
            paths = run_local_e2e._screenshot_paths(run_dir)
        self.assertEqual(
            [Path(path).name for path in paths],
            ["01_chat_created.png", "03_plan_dialog.png"],
        )

    def test_no_screenshots_is_an_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = RunDir("MOCK", root=tmp).create()
            self.assertEqual(run_local_e2e._screenshot_paths(run_dir), [])


if __name__ == "__main__":
    unittest.main()
