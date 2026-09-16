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
from memory import (
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
    WORKING_MEMORY_BLOCK_TITLE,
    format_memory_block,
)
from storage import ChatStore
from tokens import estimate_tokens

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
)

WINDOW_FIELDS = {
    "summary": "Recent turns without compression",
    "sliding": "Sliding window size, messages",
    "sticky_facts": "Sticky Facts window size, messages",
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
                if item.label == "Context strategy"
            )
            box.select(strategy).run(timeout=30)
            self.assertEqual(len(app.exception), 0)
        return app

    def test_app_renders_without_network_or_real_db(self):
        app = self._run()
        self.assertTrue(
            any(
                item.label == "Context strategy"
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
                    self.assertNotIn(
                        "Recent turns without compression", labels
                    )
                    self.assertNotIn(
                        "Sliding window size, messages", labels
                    )
                    self.assertNotIn(
                        "Sticky Facts window size, messages", labels
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
        self.assertNotIn("Delete chat", [button.label for button in app.button])

        # The controls are icon-only buttons: an empty label plus a material
        # icon. AppTest cannot measure the rendered 4px padding or the
        # content-sized width, but it can pin the icon and the empty label.
        rename_button = self._button(app, f"rename_{self.chat_id}")
        self.assertEqual(rename_button.label, "")
        self.assertEqual(rename_button.icon, ":material/edit:")
        delete_button = self._button(app, f"delete_{self.chat_id}")
        self.assertEqual(delete_button.label, "")
        self.assertEqual(delete_button.icon, ":material/delete:")

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
            any("irreversible" in item.value for item in app.warning)
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
                "parent: main line" in text
                and "ответ 1" in text
                and "checkpoint" in text
                for text in captions
            )
        )
        self.assertTrue(
            any(
                'parent: "Родительская"' in text
                and "родительский ответ" in text
                for text in captions
            )
        )
        markdown_texts = [item.value for item in app.markdown]
        self.assertTrue(
            any(
                'Active branch: "Дочерняя"' in text
                and 'parent: "Родительская"' in text
                for text in markdown_texts
            )
        )

    def test_changing_parent_updates_checkpoint_options(self):
        app = self._run()
        parent_box = self._selectbox(app, f"branch_parent_{self.chat_id}")
        self.assertEqual(
            parent_box.value, f'"Дочерняя" (id {self.child_id})'
        )
        child_options = list(
            self._selectbox(
                app, f"branch_checkpoint_{self.chat_id}_{self.child_id}"
            ).options
        )

        parent_box.select("Main line").run(timeout=30)

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
                "A branch will be created from line" in text
                and "turn:" in text
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
            any("Enter a branch name" in item.value for item in app.error)
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
            any("already" in item.value and "exists" in item.value for item in app.error)
        )

    def test_delete_requires_confirmation_cancel_and_confirm(self):
        app = self._run()
        branch_delete_button = self._button(
            app, f"branch_delete_{self.child_id}"
        )
        self.assertEqual(branch_delete_button.label, "")
        self.assertEqual(branch_delete_button.icon, ":material/delete:")
        branch_delete_button.click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                '"Дочерняя"' in item.value and "irreversible" in item.value
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
                "Delete child branches first" in item.value
                and '"Дочерняя"' in item.value
                for item in app.error
            )
        )
        self.assertFalse(
            "pending_branch_delete_id" in app.session_state
            and app.session_state["pending_branch_delete_id"] is not None
        )
        self.assertFalse(
            any("irreversible" in item.value for item in app.warning)
        )
        ids = {
            branch.id for branch in self.store.list_branches(self.chat_id)
        }
        self.assertIn(self.parent_id, ids)
        self.assertIn(self.child_id, ids)


class MemoryAppUiTest(unittest.TestCase):
    """Day 11 explicit memory layers rendered and mutated through the UI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChatStore(os.path.join(self._tmp.name, "memory_ui.db"))
        self.chat_id = self.store.create_chat(
            AgentConfig(context_strategy="full")
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, chat_id=None):
        app = AppTest.from_file(APP_PATH)
        app.session_state["store"] = self.store
        app.session_state["client"] = object()
        if chat_id is not None:
            app.session_state["chat_id"] = chat_id
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        return app

    @staticmethod
    def _button(app, key):
        return next(button for button in app.button if button.key == key)

    @staticmethod
    def _checkbox(app, key):
        return next(item for item in app.checkbox if item.key == key)

    @staticmethod
    def _text_input(app, key):
        return next(item for item in app.text_input if item.key == key)

    def _add_item(self, app, scope, scope_token, key, value):
        self._text_input(app, f"mem_add_key_{scope}_{scope_token}").set_value(key)
        self._text_input(app, f"mem_add_value_{scope}_{scope_token}").set_value(
            value
        )
        app.run(timeout=30)
        self._button(app, f"mem_add_{scope}_{scope_token}").click().run(timeout=30)
        self.assertEqual(len(app.exception), 0)

    def _create_branch(self, name="Ветка"):
        self.store.save_turn(self.chat_id, "вопрос", "ответ")
        checkpoint = self.store.load_line_checkpoints(self.chat_id, None)[-1].id
        branch_id = self.store.create_branch(self.chat_id, name, None, checkpoint)
        self.store.set_active_branch(self.chat_id, branch_id)
        return branch_id

    def test_memory_section_renders_four_english_panels(self):
        app = self._run()

        self.assertIn("Memory layers", [item.value for item in app.subheader])
        labels = [item.label for item in app.expander]
        for label in (
            "Short-term memory · current line",
            "Working memory · this chat & line",
            "Long-term memory · shared across chats",
            "System prompt & invariants (not memory)",
        ):
            self.assertIn(label, labels)

        captions = [item.value for item in app.caption]
        self.assertTrue(
            any(
                "The full conversation is shown in the chat" in text
                for text in captions
            )
        )
        self.assertTrue(
            any(
                "No working memory items for this line yet." in text
                for text in captions
            )
        )
        self.assertTrue(
            any(
                "Shared by all chats; survives chat and branch deletion." in text
                for text in captions
            )
        )
        self.assertTrue(
            any(
                "These are not memory layers and are never stored as memory items."
                in text
                for text in captions
            )
        )

    def test_add_working_item_writes_to_real_temp_db(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")

        items = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )
        self.assertEqual(
            [(item.key, item.value) for item in items], [("city", "Москва")]
        )
        # A successful add clears the form for the next entry.
        self.assertEqual(
            self._text_input(app, "mem_add_key_working_main").value, ""
        )

    def test_working_memory_is_isolated_between_main_and_branch(self):
        self._add_item(
            self._run(), MEMORY_SCOPE_WORKING, "main", "line", "main"
        )
        branch_id = self._create_branch()

        app = self._run()
        self._add_item(
            app, MEMORY_SCOPE_WORKING, str(branch_id), "line", "branch"
        )

        main_items = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )
        branch_items = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=branch_id
        )
        self.assertEqual([item.value for item in main_items], ["main"])
        self.assertEqual([item.value for item in branch_items], ["branch"])

    def test_include_checkbox_toggles_included_in_db(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]

        include_key = f"mem_include_working_main_{item.id}"
        self.assertTrue(self._checkbox(app, include_key).value)
        self._checkbox(app, include_key).uncheck().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertFalse(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
            )[0].included
        )

    def test_edit_updates_value_in_db(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]

        self._button(app, f"mem_edit_working_main_{item.id}").click().run(
            timeout=30
        )
        self._text_input(
            app, f"mem_edit_value_working_main_{item.id}"
        ).set_value("Казань").run(timeout=30)
        self._button(
            app, f"mem_edit_save_working_main_{item.id}"
        ).click().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
            )[0].value,
            "Казань",
        )

    def test_edit_cancel_keeps_value(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]

        self._button(app, f"mem_edit_working_main_{item.id}").click().run(
            timeout=30
        )
        self._text_input(
            app, f"mem_edit_value_working_main_{item.id}"
        ).set_value("Казань").run(timeout=30)
        self._button(
            app, f"mem_edit_cancel_working_main_{item.id}"
        ).click().run(timeout=30)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
            )[0].value,
            "Москва",
        )

    def test_forget_deletes_item(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]

        self._button(app, f"mem_forget_working_main_{item.id}").click().run(
            timeout=30
        )

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
            ),
            [],
        )

    def test_promote_moves_item_into_long_term(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]

        self._button(app, f"mem_promote_working_main_{item.id}").click().run(
            timeout=30
        )

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
            ),
            [],
        )
        self.assertEqual(
            [
                (entry.key, entry.value)
                for entry in self.store.list_memory_items(
                    MEMORY_SCOPE_LONG_TERM
                )
            ],
            [("city", "Москва")],
        )

    def test_promote_duplicate_key_shows_error_and_keeps_working(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_LONG_TERM, "shared", "city", "Москва")
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Казань")
        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]

        self._button(app, f"mem_promote_working_main_{item.id}").click().run(
            timeout=30
        )

        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                "Long-term memory already contains an item with this key."
                in error.value
                for error in app.error
            )
        )
        self.assertEqual(
            [
                entry.value
                for entry in self.store.list_memory_items(
                    MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
                )
            ],
            ["Казань"],
        )

    def test_empty_add_shows_error_without_db_change(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "", "value")

        self.assertTrue(
            any(
                "Key and value must not be empty." in error.value
                for error in app.error
            )
        )
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
            ),
            [],
        )

    def test_duplicate_add_shows_error_without_db_change(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Казань")

        self.assertTrue(
            any(
                "A memory item with this key already exists in this layer."
                in error.value
                for error in app.error
            )
        )
        self.assertEqual(
            [
                entry.value
                for entry in self.store.list_memory_items(
                    MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
                )
            ],
            ["Москва"],
        )

    def test_long_term_is_shared_when_another_chat_is_active(self):
        app = self._run(chat_id=self.chat_id)
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "local", "chat a")
        self._add_item(app, MEMORY_SCOPE_LONG_TERM, "shared", "shared", "global")
        long_item = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)[0]

        other_id = self.store.create_chat(AgentConfig(context_strategy="full"))
        other_app = self._run(chat_id=other_id)

        self.assertTrue(
            any(
                item.key == f"mem_include_long_term_shared_{long_item.id}"
                for item in other_app.checkbox
            )
        )
        self.assertFalse(
            any(
                item.key.startswith("mem_include_working_")
                for item in other_app.checkbox
            )
        )
        self.assertTrue(
            any("**shared**: global" in item.value for item in other_app.markdown)
        )

    def test_token_caption_changes_when_item_is_excluded(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")

        def working_caption(tree):
            return next(
                item.value
                for item in tree.caption
                if "tokens in the prompt" in item.value
            )

        before = working_caption(app)
        self.assertIn("1 of 1 included", before)
        self.assertNotIn("0 tokens in the prompt", before)

        item = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )[0]
        self._checkbox(
            app, f"mem_include_working_main_{item.id}"
        ).uncheck().run(timeout=30)

        after = working_caption(app)
        self.assertIn("0 tokens in the prompt", after)
        self.assertIn("0 of 1 included", after)
        self.assertNotEqual(before, after)

    def test_token_caption_counts_the_payload_block_header(self):
        app = self._run()
        self._add_item(app, MEMORY_SCOPE_WORKING, "main", "city", "Москва")
        items = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=None
        )

        # The reported value must match the exact block the agent sends,
        # header included; the previous headerless estimate was too small.
        expected = estimate_tokens(
            format_memory_block(items, WORKING_MEMORY_BLOCK_TITLE)
        )
        headerless = estimate_tokens(format_memory_block(items, ""))
        self.assertGreater(expected, headerless)

        caption = next(
            item.value
            for item in app.caption
            if "tokens in the prompt" in item.value
        )
        self.assertIn(f"≈ {expected} tokens in the prompt", caption)

    def test_system_prompt_and_invariants_panel_and_config_save(self):
        app = self._run()
        invariants_field = next(
            item
            for item in app.text_area
            if item.key == f"invariants_{self.chat_id}"
        )
        invariants_field.set_value("Always answer in English.").run(timeout=30)

        self.assertEqual(
            self.store.load_config(self.chat_id)["invariants"],
            "Always answer in English.",
        )
        self.assertTrue(
            any(
                "Always answer in English." in item.value for item in app.code
            )
        )
        self.assertTrue(
            any("Invariants · ≈" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("System prompt · ≈" in item.value for item in app.markdown)
        )

    def test_working_panel_shows_active_line_main_and_branch(self):
        main_app = self._run()
        self.assertTrue(
            any(
                "Scope: chat «New chat» · line: main" in item.value
                for item in main_app.caption
            )
        )
        self.assertTrue(
            any(
                "Active line: **main line**" in item.value
                for item in main_app.markdown
            )
        )

        branch_id = self._create_branch()
        branch_app = self._run()
        self.assertTrue(
            any(
                f"branch «Ветка» (id {branch_id})" in item.value
                for item in branch_app.caption
            )
        )
        self.assertTrue(
            any(
                f"Active line: **branch «Ветка» (id {branch_id})**"
                in item.value
                for item in branch_app.markdown
            )
        )


if __name__ == "__main__":
    unittest.main()
