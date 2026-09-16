"""Integration tests for explicit memory persistence in ``ChatStore``.

Each test runs against a real temporary SQLite file (no mocks, no network).
The Day 11 migration test seeds a synthetic final Day 10 database so the new
tables and the ``invariants`` column are exercised on legacy data.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent import AgentConfig
from memory import MEMORY_SCOPE_LONG_TERM, MEMORY_SCOPE_WORKING
from stats import TurnStats
from storage import ChatStore, DuplicateMemoryKeyError


# The final Day 10 schema: chats/messages/turns/summaries/facts/branches all in
# their Day 10 shape, but no memory tables and no ``invariants`` column.
_DAY10_SCHEMA = """
CREATE TABLE chats (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT    NOT NULL,
    system_prompt       TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    temperature         REAL    NOT NULL,
    max_tokens          INTEGER NOT NULL,
    stream              INTEGER NOT NULL,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    demo_context_limit  INTEGER,
    history_tokens_est  INTEGER,
    cost_usd            REAL,
    summarize           INTEGER NOT NULL DEFAULT 1,
    keep_recent_turns   INTEGER NOT NULL DEFAULT 3,
    summary_input_tokens  INTEGER,
    summary_output_tokens INTEGER,
    summary_cost_usd      REAL,
    summary_cache_hit_tokens  INTEGER,
    summary_cache_miss_tokens INTEGER,
    context_strategy    TEXT,
    sliding_window_messages INTEGER NOT NULL DEFAULT 6,
    facts_window_messages   INTEGER NOT NULL DEFAULT 6,
    facts_anchor_message_id INTEGER,
    active_branch_id        INTEGER,
    facts_ok_input_tokens   INTEGER,
    facts_ok_output_tokens  INTEGER,
    facts_ok_cache_hit_tokens  INTEGER,
    facts_ok_cache_miss_tokens INTEGER,
    facts_ok_cost_usd          REAL,
    facts_fail_input_tokens    INTEGER,
    facts_fail_output_tokens   INTEGER,
    facts_fail_cache_hit_tokens  INTEGER,
    facts_fail_cache_miss_tokens INTEGER,
    facts_fail_cost_usd          REAL,
    last_context_tokens         INTEGER,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    branch_id  INTEGER,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_chat ON messages(chat_id, id);
CREATE TABLE turns (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id                 INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_message_id         INTEGER NOT NULL,
    assistant_message_id    INTEGER NOT NULL,
    user_message_tokens_est INTEGER,
    request_tokens          INTEGER,
    response_tokens         INTEGER,
    total_tokens            INTEGER,
    prompt_cache_hit_tokens INTEGER,
    prompt_cache_miss_tokens INTEGER,
    finish_reason           TEXT,
    cost_usd                REAL,
    cost_assumption         TEXT,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_turns_chat ON turns(chat_id, id);
CREATE TABLE summaries (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id                INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    content                TEXT    NOT NULL,
    covered_messages_count INTEGER NOT NULL,
    format_version         INTEGER NOT NULL DEFAULT 1,
    prompt_tokens          INTEGER,
    response_tokens        INTEGER,
    total_tokens           INTEGER,
    cost_usd               REAL,
    cost_assumption        TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    branch_id              INTEGER
);
CREATE UNIQUE INDEX idx_summaries_main ON summaries(chat_id) WHERE branch_id IS NULL;
CREATE UNIQUE INDEX idx_summaries_branch
    ON summaries(chat_id, branch_id) WHERE branch_id IS NOT NULL;
CREATE TABLE facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    category   TEXT    NOT NULL DEFAULT 'other',
    fact_key   TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active',
    reason     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_facts_chat ON facts(chat_id, status, id);
CREATE UNIQUE INDEX idx_facts_active_key
    ON facts(chat_id, fact_key) WHERE status = 'active';
CREATE TABLE branches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    name             TEXT    NOT NULL,
    parent_branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
    fork_message_id  INTEGER NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_branches_chat ON branches(chat_id, id);
CREATE TABLE app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _seed_day10_db(path):
    """Create a final-Day-10 database with one chat, turn and summary."""
    conn = sqlite3.connect(path)
    conn.executescript(_DAY10_SCHEMA)
    conn.execute(
        "INSERT INTO chats (title, system_prompt, model, temperature, max_tokens, "
        "stream, input_tokens, output_tokens, demo_context_limit, "
        "history_tokens_est, cost_usd, summarize, keep_recent_turns, "
        "context_strategy, sliding_window_messages, facts_window_messages) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Чат Дня 10", "Промпт", "deepseek-flash", 0.2, 1500, 1, 12, 34, 500,
         100, 0.001, 1, 3, "summary", 6, 6),
    )
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (1, 'user', ?)",
        ("Привет мир",),
    )
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (1, 'assistant', ?)",
        ("Здравствуйте",),
    )
    conn.execute(
        "INSERT INTO turns (chat_id, user_message_id, assistant_message_id, "
        "request_tokens, response_tokens) VALUES (1, 1, 2, 5, 8)",
    )
    conn.execute(
        "INSERT INTO summaries (chat_id, content, covered_messages_count) "
        "VALUES (1, 'Сводка Дня 10', 2)",
    )
    conn.commit()
    conn.close()


class MemoryStorageTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        # A throwaway chat keeps the first real chat id different from 1, so a
        # swapped chat id cannot pass by coincidence.
        self.store.create_chat(AgentConfig())
        self.chat_id = self.store.create_chat(AgentConfig())

    def tearDown(self):
        self._tmp.cleanup()

    def _add_turn(self, chat_id=None):
        chat_id = self.chat_id if chat_id is None else chat_id
        self.store.save_turn(chat_id, "вопрос", "ответ")
        return [
            message.id
            for message in self.store.load_branch_history(chat_id)
            if message.role == "assistant"
        ][-1]

    def _count(self, table, where="", params=()):
        with closing(sqlite3.connect(self.path)) as conn:
            sql = f"SELECT COUNT(*) FROM {table}"
            if where:
                sql += f" WHERE {where}"
            return conn.execute(sql, params).fetchone()[0]

    # --- CRUD -----------------------------------------------------------------

    def test_working_memory_crud_roundtrip(self):
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "  city  ", "  Москва  ", chat_id=self.chat_id
        )
        items = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, item_id)
        self.assertEqual(items[0].key, "city")
        self.assertEqual(items[0].value, "Москва")
        self.assertTrue(items[0].included)
        self.assertIsNotNone(items[0].updated_at)

        self.store.edit_memory_item(
            MEMORY_SCOPE_WORKING, item_id, "city", "Санкт-Петербург",
            chat_id=self.chat_id,
        )
        edited = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )[0]
        self.assertEqual(edited.value, "Санкт-Петербург")

        self.store.set_memory_item_included(
            MEMORY_SCOPE_WORKING, item_id, False, chat_id=self.chat_id
        )
        toggled = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )[0]
        self.assertFalse(toggled.included)
        self.assertEqual(toggled.value, "Санкт-Петербург")

        self.store.forget_memory_item(
            MEMORY_SCOPE_WORKING, item_id, chat_id=self.chat_id
        )
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id
            ),
            [],
        )
        with self.assertRaises(KeyError):
            self.store.forget_memory_item(
                MEMORY_SCOPE_WORKING, item_id, chat_id=self.chat_id
            )

    def test_long_term_memory_crud_roundtrip(self):
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_LONG_TERM, "name", "Алексей"
        )
        items = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        self.assertEqual([(item.id, item.key, item.value) for item in items],
                         [(item_id, "name", "Алексей")])

        self.store.edit_memory_item(
            MEMORY_SCOPE_LONG_TERM, item_id, "name", "Александр"
        )
        self.assertEqual(
            self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)[0].value,
            "Александр",
        )

        self.store.set_memory_item_included(
            MEMORY_SCOPE_LONG_TERM, item_id, 0
        )
        self.assertFalse(
            self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)[0].included
        )

        self.store.forget_memory_item(MEMORY_SCOPE_LONG_TERM, item_id)
        self.assertEqual(self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM), [])

    def test_edit_keeps_included_flag(self):
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_LONG_TERM, "city", "Москва"
        )
        self.store.set_memory_item_included(MEMORY_SCOPE_LONG_TERM, item_id, False)
        self.store.edit_memory_item(MEMORY_SCOPE_LONG_TERM, item_id, "city", "Казань")
        item = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)[0]
        self.assertEqual(item.value, "Казань")
        self.assertFalse(item.included)

    # --- Validation -----------------------------------------------------------

    def test_duplicate_key_on_main_line(self):
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "city", "Москва", chat_id=self.chat_id
        )
        with self.assertRaises(DuplicateMemoryKeyError) as ctx:
            self.store.add_memory_item(
                MEMORY_SCOPE_WORKING, "city", "Казань", chat_id=self.chat_id
            )
        self.assertEqual(ctx.exception.key, "city")
        items = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )
        self.assertEqual([item.value for item in items], ["Москва"])

    def test_duplicate_key_on_branch(self):
        branch = self.store.create_branch(
            self.chat_id, "B", None, self._add_turn()
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "city", "Москва",
            chat_id=self.chat_id, branch_id=branch,
        )
        with self.assertRaises(DuplicateMemoryKeyError):
            self.store.add_memory_item(
                MEMORY_SCOPE_WORKING, "city", "Казань",
                chat_id=self.chat_id, branch_id=branch,
            )
        # The same key is allowed in a different scope.
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "city", "Тверь", chat_id=self.chat_id
        )

    def test_duplicate_long_term_key(self):
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "k", "1")
        with self.assertRaises(DuplicateMemoryKeyError):
            self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "k", "2")

    def test_edit_to_existing_key_rejected(self):
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "a", "1")
        second = self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "b", "2")
        with self.assertRaises(DuplicateMemoryKeyError):
            self.store.edit_memory_item(MEMORY_SCOPE_LONG_TERM, second, "a", "9")
        self.assertEqual(
            [(item.key, item.value)
             for item in self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)],
            [("a", "1"), ("b", "2")],
        )

    def test_empty_key_or_value_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "  ", "v")
        with self.assertRaises(ValueError):
            self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "k", "  ")

    def test_nonexistent_item_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.edit_memory_item(
                MEMORY_SCOPE_LONG_TERM, 999, "k", "v"
            )
        with self.assertRaises(KeyError):
            self.store.forget_memory_item(MEMORY_SCOPE_LONG_TERM, 999)
        with self.assertRaises(KeyError):
            self.store.set_memory_item_included(
                MEMORY_SCOPE_LONG_TERM, 999, False
            )
        with self.assertRaises(KeyError):
            self.store.edit_memory_item(
                MEMORY_SCOPE_WORKING, 999, "k", "v", chat_id=self.chat_id
            )

    def test_unknown_chat_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.list_memory_items(MEMORY_SCOPE_WORKING, chat_id=99999)
        with self.assertRaises(KeyError):
            self.store.add_memory_item(
                MEMORY_SCOPE_WORKING, "k", "v", chat_id=99999
            )

    def test_foreign_branch_raises_valueerror(self):
        other_chat = self.store.create_chat(AgentConfig())
        other_branch = self.store.create_branch(
            other_chat, "B", None, self._add_turn(other_chat)
        )
        with self.assertRaises(ValueError):
            self.store.add_memory_item(
                MEMORY_SCOPE_WORKING, "k", "v",
                chat_id=self.chat_id, branch_id=other_branch,
            )
        with self.assertRaises(ValueError):
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING,
                chat_id=self.chat_id,
                branch_id=other_branch,
            )

    def test_long_term_with_chat_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_memory_item(
                MEMORY_SCOPE_LONG_TERM, "k", "v", chat_id=self.chat_id
            )
        with self.assertRaises(ValueError):
            self.store.list_memory_items(
                MEMORY_SCOPE_LONG_TERM, chat_id=self.chat_id
            )
        with self.assertRaises(ValueError):
            self.store.add_memory_item(
                MEMORY_SCOPE_LONG_TERM, "k", "v", branch_id=1
            )

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.store.list_memory_items("episodic")
        with self.assertRaises(ValueError):
            self.store.add_memory_item("episodic", "k", "v", chat_id=self.chat_id)

    # --- Isolation ------------------------------------------------------------

    def test_chat_isolation(self):
        other = self.store.create_chat(AgentConfig())
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v", chat_id=self.chat_id
        )
        self.assertEqual(
            self.store.list_memory_items(MEMORY_SCOPE_WORKING, chat_id=other), []
        )

    def test_branch_does_not_inherit_main_and_vice_versa(self):
        branch = self.store.create_branch(
            self.chat_id, "B", None, self._add_turn()
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "where", "main", chat_id=self.chat_id
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "where", "branch",
            chat_id=self.chat_id, branch_id=branch,
        )

        main_keys = [
            item.value
            for item in self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id
            )
        ]
        branch_keys = [
            item.value
            for item in self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=branch
            )
        ]
        self.assertEqual(main_keys, ["main"])
        self.assertEqual(branch_keys, ["branch"])

    def test_sibling_branches_are_isolated(self):
        checkpoint = self._add_turn()
        first = self.store.create_branch(self.chat_id, "B1", None, checkpoint)
        second = self.store.create_branch(self.chat_id, "B2", None, checkpoint)
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "only-first",
            chat_id=self.chat_id, branch_id=first,
        )
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=second
            ),
            [],
        )

    def test_long_term_visible_across_chats_and_branches(self):
        other = self.store.create_chat(AgentConfig())
        branch = self.store.create_branch(
            self.chat_id, "B", None, self._add_turn()
        )
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "shared", "value")

        # Long-term memory is not scoped: every listing returns the same entry.
        for _ in range(2):
            self.assertEqual(
                [item.value
                 for item in self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)],
                ["value"],
            )
        # Working listings never leak the long-term entry.
        self.assertEqual(
            self.store.list_memory_items(MEMORY_SCOPE_WORKING, chat_id=other), []
        )
        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=branch
            ),
            [],
        )

    # --- Promote (move) -------------------------------------------------------

    def test_promote_moves_item_to_long_term(self):
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "city", "Москва", chat_id=self.chat_id
        )
        long_term_id = self.store.promote_memory_item(
            self.chat_id, None, item_id
        )

        self.assertEqual(
            self.store.list_memory_items(
                MEMORY_SCOPE_WORKING, chat_id=self.chat_id
            ),
            [],
        )
        promoted = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0].id, long_term_id)
        self.assertEqual((promoted[0].key, promoted[0].value), ("city", "Москва"))

    def test_promote_duplicate_keeps_working_item(self):
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "city", "Тверь")
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "city", "Москва", chat_id=self.chat_id
        )
        with self.assertRaises(DuplicateMemoryKeyError) as ctx:
            self.store.promote_memory_item(self.chat_id, None, item_id)
        self.assertEqual(ctx.exception.key, "city")

        working = self.store.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )
        self.assertEqual([item.value for item in working], ["Москва"])
        long_term = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        self.assertEqual([item.value for item in long_term], ["Тверь"])

    def test_promote_missing_item_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.promote_memory_item(self.chat_id, None, 99999)
        self.assertEqual(self._count("long_term_memory"), 0)

    def test_promote_keeps_included_flag(self):
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v", chat_id=self.chat_id
        )
        self.store.set_memory_item_included(
            MEMORY_SCOPE_WORKING, item_id, False, chat_id=self.chat_id
        )
        self.store.promote_memory_item(self.chat_id, None, item_id)
        self.assertFalse(
            self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)[0].included
        )

    def test_promoted_item_survives_source_branch_deletion(self):
        branch = self.store.create_branch(
            self.chat_id, "B", None, self._add_turn()
        )
        item_id = self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "v",
            chat_id=self.chat_id, branch_id=branch,
        )
        self.store.promote_memory_item(self.chat_id, branch, item_id)

        self.store.delete_branch(self.chat_id, branch)

        long_term = self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        self.assertEqual([item.value for item in long_term], ["v"])
        # The deleted branch no longer exists, so its working rows are gone.
        self.assertEqual(
            self._count("working_memory", "branch_id = ?", (branch,)), 0
        )

    # --- Deletion and restart -------------------------------------------------

    def test_delete_chat_removes_working_but_keeps_long_term(self):
        branch = self.store.create_branch(
            self.chat_id, "B", None, self._add_turn()
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "main", "v", chat_id=self.chat_id
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "branch", "v",
            chat_id=self.chat_id, branch_id=branch,
        )
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "keep", "v")

        self.store.delete_chat(self.chat_id)

        self.assertEqual(
            self._count("working_memory", "chat_id = ?", (self.chat_id,)), 0
        )
        self.assertEqual(
            [item.value
             for item in self.store.list_memory_items(MEMORY_SCOPE_LONG_TERM)],
            ["v"],
        )

    def test_delete_branch_removes_only_its_working_rows(self):
        checkpoint = self._add_turn()
        first = self.store.create_branch(self.chat_id, "B1", None, checkpoint)
        second = self.store.create_branch(self.chat_id, "B2", None, checkpoint)
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "main", chat_id=self.chat_id
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "first",
            chat_id=self.chat_id, branch_id=first,
        )
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "k", "second",
            chat_id=self.chat_id, branch_id=second,
        )

        self.store.delete_branch(self.chat_id, first)

        self.assertEqual(
            self._count("working_memory", "branch_id = ?", (first,)), 0
        )
        self.assertEqual(
            [item.value
             for item in self.store.list_memory_items(
                 MEMORY_SCOPE_WORKING, chat_id=self.chat_id
             )],
            ["main"],
        )
        self.assertEqual(
            [item.value
             for item in self.store.list_memory_items(
                 MEMORY_SCOPE_WORKING, chat_id=self.chat_id, branch_id=second
             )],
            ["second"],
        )

    def test_restart_sees_same_memory(self):
        self.store.add_memory_item(
            MEMORY_SCOPE_WORKING, "w", "1", chat_id=self.chat_id
        )
        self.store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "l", "2")

        reopened = ChatStore(self.path)

        working = reopened.list_memory_items(
            MEMORY_SCOPE_WORKING, chat_id=self.chat_id
        )
        long_term = reopened.list_memory_items(MEMORY_SCOPE_LONG_TERM)
        self.assertEqual([(item.key, item.value) for item in working], [("w", "1")])
        self.assertEqual([(item.key, item.value) for item in long_term], [("l", "2")])


class MemoryMigrationTest(unittest.TestCase):
    """Opening a Day 10 database adds memory tables and ``invariants``."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_day10_migration_adds_memory_without_losing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day10_db(path)

            store = ChatStore(path)

            # Existing data is intact.
            self.assertEqual(
                [m["content"] for m in store.load_messages(1)],
                ["Привет мир", "Здравствуйте"],
            )
            self.assertEqual(store.get_chat_stats(1).turns_count, 1)
            summary = store.load_summary(1)
            self.assertEqual(summary.content, "Сводка Дня 10")
            # The new column reads as an empty string for legacy rows.
            self.assertEqual(store.load_config(1)["invariants"], "")
            self.assertEqual(AgentConfig(**store.load_config(1)).invariants, "")

            # Memory tables exist but start empty.
            self.assertEqual(
                store.list_memory_items(MEMORY_SCOPE_WORKING, chat_id=1), []
            )
            self.assertEqual(store.list_memory_items(MEMORY_SCOPE_LONG_TERM), [])

            # CRUD works on the migrated database.
            item_id = store.add_memory_item(
                MEMORY_SCOPE_WORKING, "k", "v", chat_id=1
            )
            store.add_memory_item(MEMORY_SCOPE_LONG_TERM, "l", "v")
            self.assertEqual(
                [item.id
                 for item in store.list_memory_items(
                     MEMORY_SCOPE_WORKING, chat_id=1
                 )],
                [item_id],
            )

            # Reopening is idempotent and keeps the new data.
            reopened = ChatStore(path)
            self.assertEqual(
                [m["content"] for m in reopened.load_messages(1)],
                ["Привет мир", "Здравствуйте"],
            )
            self.assertEqual(
                [item.value
                 for item in reopened.list_memory_items(
                     MEMORY_SCOPE_WORKING, chat_id=1
                 )],
                ["v"],
            )
            self.assertEqual(reopened.load_config(1)["invariants"], "")


if __name__ == "__main__":
    unittest.main()
