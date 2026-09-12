"""Deterministic Streamlit UI checks for the Day 10 strategy selector.

The app is executed with an injected temporary ``ChatStore`` and a fake client
through ``st.session_state``, so neither the real database nor ``.env`` nor the
provider is touched. Because the injected client is a bare object, any attempt
to call the API during rendering would raise and fail these tests.
"""

import os
import tempfile
import unittest

from streamlit.testing.v1 import AppTest

from agent import AgentConfig
from storage import ChatStore

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
)

WINDOW_FIELDS = {
    "summary": "Последних ходов без сжатия",
    "sliding": "Размер скользящего окна, сообщения",
    "sticky_facts": "Размер окна Sticky Facts, сообщения",
    "full": None,
    "branching": None,
}


class AppUiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChatStore(os.path.join(self._tmp.name, "ui.db"))
        self.chat_id = self.store.create_chat(
            AgentConfig(context_strategy="summary")
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, strategy=None):
        app = AppTest.from_file(APP_PATH)
        app.session_state["store"] = self.store
        app.session_state["client"] = object()
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        if strategy is not None:
            box = next(
                item
                for item in app.selectbox
                if item.label == "Стратегия контекста"
            )
            box.select(strategy).run(timeout=30)
            self.assertEqual(len(app.exception), 0)
        return app

    def test_app_renders_without_network_or_real_db(self):
        app = self._run()
        self.assertTrue(
            any(
                item.label == "Стратегия контекста"
                for item in app.selectbox
            )
        )

    def test_strategy_field_switches_with_selection(self):
        for strategy, expected_field in WINDOW_FIELDS.items():
            with self.subTest(strategy=strategy):
                app = self._run(strategy)
                labels = [item.label for item in app.number_input]
                if expected_field is not None:
                    self.assertIn(expected_field, labels)
                else:
                    self.assertNotIn("Последних ходов без сжатия", labels)
                    self.assertNotIn(
                        "Размер скользящего окна, сообщения", labels
                    )
                    self.assertNotIn(
                        "Размер окна Sticky Facts, сообщения", labels
                    )
                # The switch is persisted in the temporary store.
                self.assertEqual(
                    self.store.load_config(self.chat_id)["context_strategy"],
                    strategy,
                )

    @staticmethod
    def _button(app, key):
        return next(button for button in app.button if button.key == key)

    def test_sidebar_has_icons_and_no_big_delete_button(self):
        app = self._run()
        keys = {button.key for button in app.button}
        self.assertIn(f"chat_{self.chat_id}", keys)
        self.assertIn(f"rename_{self.chat_id}", keys)
        self.assertIn(f"delete_{self.chat_id}", keys)
        self.assertNotIn("Удалить чат", [button.label for button in app.button])

    def test_long_title_keeps_rename_and_delete_controls(self):
        # The narrow-sidebar bug is visual, so AppTest cannot measure overflow.
        # This pins that a long title still renders all three controls and that
        # the rename icon stays usable for that chat.
        self.store.rename_chat(
            self.chat_id, "Очень длинное название чата " * 5
        )
        app = self._run()
        keys = {button.key for button in app.button}
        self.assertIn(f"chat_{self.chat_id}", keys)
        self.assertIn(f"rename_{self.chat_id}", keys)
        self.assertIn(f"delete_{self.chat_id}", keys)

        self._button(app, f"rename_{self.chat_id}").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                item.key == f"rename_input_{self.chat_id}"
                for item in app.text_input
            )
        )

    def test_rename_icon_opens_editor_and_saves(self):
        app = self._run()
        self._button(app, f"rename_{self.chat_id}").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                item.key == f"rename_input_{self.chat_id}"
                for item in app.text_input
            )
        )

        field = next(
            item
            for item in app.text_input
            if item.key == f"rename_input_{self.chat_id}"
        )
        field.set_value("Моё новое имя").run(timeout=30)
        self._button(app, f"rename_save_{self.chat_id}").click().run(timeout=30)

        # The save path and rerender must not touch the provider (bare client).
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.store.list_chats()[0].title, "Моё новое имя")
        self.assertFalse(
            any(
                item.key == f"rename_input_{self.chat_id}"
                for item in app.text_input
            )
        )

    def test_rename_blank_keeps_title_and_warns(self):
        app = self._run()
        original = self.store.list_chats()[0].title
        self._button(app, f"rename_{self.chat_id}").click().run(timeout=30)
        field = next(
            item
            for item in app.text_input
            if item.key == f"rename_input_{self.chat_id}"
        )
        field.set_value("   ").run(timeout=30)
        self._button(app, f"rename_save_{self.chat_id}").click().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.store.list_chats()[0].title, original)
        self.assertTrue(app.warning)
        # The editor stays open so the user can fix the name.
        self.assertTrue(
            any(
                item.key == f"rename_input_{self.chat_id}"
                for item in app.text_input
            )
        )

    def test_rename_cancel_closes_editor_without_change(self):
        app = self._run()
        original = self.store.list_chats()[0].title
        self._button(app, f"rename_{self.chat_id}").click().run(timeout=30)
        self._button(app, f"rename_cancel_{self.chat_id}").click().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.store.list_chats()[0].title, original)
        self.assertFalse(
            any(
                item.key == f"rename_input_{self.chat_id}"
                for item in app.text_input
            )
        )

    def test_rename_editor_moves_to_last_clicked_chat(self):
        other_id = self.store.create_chat(AgentConfig())
        app = self._run()
        self._button(app, f"rename_{self.chat_id}").click().run(timeout=30)
        # Opening the editor for another chat closes the first one.
        self._button(app, f"rename_{other_id}").click().run(timeout=30)

        inputs = {item.key for item in app.text_input}
        self.assertNotIn(f"rename_input_{self.chat_id}", inputs)
        self.assertIn(f"rename_input_{other_id}", inputs)

    def test_delete_icon_requires_confirmation_then_removes_chat(self):
        app = self._run()
        self._button(app, f"delete_{self.chat_id}").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any("необратимо" in item.value for item in app.warning)
        )

        self._button(app, "delete_confirm").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.store.list_chats(), [])
        self.assertFalse(
            any(button.key == f"delete_{self.chat_id}" for button in app.button)
        )

    def test_delete_cancel_keeps_chat(self):
        app = self._run()
        self._button(app, f"delete_{self.chat_id}").click().run(timeout=30)
        self._button(app, "delete_cancel").click().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(self.store.list_chats()), 1)
        self.assertFalse(app.warning)

    def test_delete_non_active_chat_keeps_active_selection(self):
        other_id = self.store.create_chat(AgentConfig())
        app = self._run()
        # Startup selects the newest chat; the original chat is not active.
        self.assertEqual(app.session_state["chat_id"], other_id)

        self._button(app, f"delete_{self.chat_id}").click().run(timeout=30)
        self._button(app, "delete_confirm").click().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual([chat.id for chat in self.store.list_chats()], [other_id])
        self.assertEqual(app.session_state["chat_id"], other_id)


class BranchAppUiTest(unittest.TestCase):
    """Branch tree, creation and deletion through the Streamlit UI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChatStore(os.path.join(self._tmp.name, "branch_ui.db"))
        self.chat_id = self.store.create_chat(
            AgentConfig(context_strategy="branching")
        )
        for index in range(1, 4):
            self.store.save_turn(
                self.chat_id, f"вопрос {index}", f"ответ {index}"
            )
        checkpoint = self.store.load_branch_history(self.chat_id)[1].id
        self.parent_id = self.store.create_branch(
            self.chat_id, "Родительская", None, checkpoint
        )
        self.store.save_turn(
            self.chat_id,
            "родительский вопрос",
            "родительский ответ",
            branch_id=self.parent_id,
        )
        parent_checkpoint = self.store.load_line_checkpoints(
            self.chat_id, self.parent_id
        )[-1].id
        self.child_id = self.store.create_branch(
            self.chat_id,
            "Дочерняя",
            parent_branch_id=self.parent_id,
            fork_message_id=parent_checkpoint,
        )
        self.store.set_active_branch(self.chat_id, self.child_id)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        app = AppTest.from_file(APP_PATH)
        app.session_state["store"] = self.store
        app.session_state["client"] = object()
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        return app

    @staticmethod
    def _button(app, key):
        return next(button for button in app.button if button.key == key)

    @staticmethod
    def _selectbox(app, key):
        return next(item for item in app.selectbox if item.key == key)

    def _name_key(self, app):
        prefix = f"branch_name_{self.chat_id}_"
        return next(
            item.key for item in app.text_input if item.key.startswith(prefix)
        )

    def _indent(self, label):
        return len(label) - len(label.lstrip("\u00a0"))

    def test_tree_shows_parent_kind_snippet_marker_and_indent(self):
        app = self._run()
        parent_button = self._button(app, f"branch_{self.parent_id}")
        child_button = self._button(app, f"branch_{self.child_id}")

        self.assertNotIn("▶", parent_button.label)
        self.assertIn("▶", child_button.label)
        self.assertGreater(
            self._indent(child_button.label), self._indent(parent_button.label)
        )

        captions = [caption.value for caption in app.caption]
        self.assertTrue(
            any(
                "родитель: основная линия" in text
                and "ответ 1" in text
                and "checkpoint" in text
                for text in captions
            )
        )
        self.assertTrue(
            any(
                "родитель: «Родительская»" in text
                and "родительский ответ" in text
                for text in captions
            )
        )
        markdown_texts = [item.value for item in app.markdown]
        self.assertTrue(
            any(
                "Активная ветка: «Дочерняя»" in text
                and "родитель: «Родительская»" in text
                for text in markdown_texts
            )
        )

    def test_changing_parent_updates_checkpoint_options(self):
        app = self._run()
        parent_box = self._selectbox(app, f"branch_parent_{self.chat_id}")
        self.assertEqual(parent_box.value, f"«Дочерняя» (id {self.child_id})")
        child_options = list(
            self._selectbox(
                app, f"branch_checkpoint_{self.chat_id}_{self.child_id}"
            ).options
        )

        parent_box.select("Основная линия").run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        main_options = list(
            self._selectbox(
                app, f"branch_checkpoint_{self.chat_id}_main"
            ).options
        )
        self.assertEqual(len(main_options), 3)
        self.assertNotEqual(child_options, main_options)

    def test_creation_preview_and_success(self):
        app = self._run()
        captions = [caption.value for caption in app.caption]
        self.assertTrue(
            any(
                "Будет создана ветка от линии" in text
                and "ход:" in text
                and "id " in text
                for text in captions
            )
        )

        name_field = next(
            item for item in app.text_input if item.key == self._name_key(app)
        )
        name_field.set_value("Новая ветка").run(timeout=30)
        self._button(app, f"branch_create_{self.chat_id}").click().run(
            timeout=30
        )

        self.assertEqual(len(app.exception), 0)
        created = next(
            branch
            for branch in self.store.list_branches(self.chat_id)
            if branch.name == "Новая ветка"
        )
        self.assertEqual(
            self.store.get_active_branch(self.chat_id).id, created.id
        )
        # A fresh nonce clears the name field after a successful creation.
        name_fields = [
            item
            for item in app.text_input
            if item.key.startswith(f"branch_name_{self.chat_id}_")
        ]
        self.assertEqual(name_fields[0].value, "")

    def test_empty_and_duplicate_names_show_error_without_creating(self):
        app = self._run()
        before = len(self.store.list_branches(self.chat_id))

        next(
            item for item in app.text_input if item.key == self._name_key(app)
        ).set_value("   ").run(timeout=30)
        self._button(app, f"branch_create_{self.chat_id}").click().run(
            timeout=30
        )
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(self.store.list_branches(self.chat_id)), before)
        self.assertTrue(
            any("Введите имя ветки" in item.value for item in app.error)
        )

        next(
            item for item in app.text_input if item.key == self._name_key(app)
        ).set_value("Родительская").run(timeout=30)
        self._button(app, f"branch_create_{self.chat_id}").click().run(
            timeout=30
        )
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(self.store.list_branches(self.chat_id)), before)
        self.assertTrue(
            any("уже существует" in item.value for item in app.error)
        )

    def test_delete_requires_confirmation_cancel_and_confirm(self):
        app = self._run()
        self._button(app, f"branch_delete_{self.child_id}").click().run(
            timeout=30
        )
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                "«Дочерняя»" in item.value and "необратимо" in item.value
                for item in app.warning
            )
        )

        self._button(app, "branch_delete_cancel").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                branch.id == self.child_id
                for branch in self.store.list_branches(self.chat_id)
            )
        )

        self._button(app, f"branch_delete_{self.child_id}").click().run(
            timeout=30
        )
        self._button(app, "branch_delete_confirm").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(
            any(
                branch.id == self.child_id
                for branch in self.store.list_branches(self.chat_id)
            )
        )
        # The parent line becomes active after the active branch is deleted.
        self.assertEqual(
            self.store.get_active_branch(self.chat_id).id, self.parent_id
        )

    def test_delete_parent_with_children_is_blocked(self):
        app = self._run()
        self._button(app, f"branch_delete_{self.parent_id}").click().run(
            timeout=30
        )

        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                "Сначала удалите дочерние ветки" in item.value
                and "«Дочерняя»" in item.value
                for item in app.error
            )
        )
        self.assertFalse(
            "pending_branch_delete_id" in app.session_state
            and app.session_state["pending_branch_delete_id"] is not None
        )
        self.assertFalse(
            any("необратимо" in item.value for item in app.warning)
        )
        ids = {
            branch.id for branch in self.store.list_branches(self.chat_id)
        }
        self.assertIn(self.parent_id, ids)
        self.assertIn(self.child_id, ids)


if __name__ == "__main__":
    unittest.main()
