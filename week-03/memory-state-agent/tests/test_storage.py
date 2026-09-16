import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent import AgentConfig
from stats import TurnStats
from storage import (
    DEFAULT_CHAT_TITLE,
    BranchHasChildrenError,
    ChatStore,
    DuplicateBranchNameError,
)
from tokens import estimate_tokens

# The Day 7 schema: no ``turns`` table and no demo_context_limit /
# history_tokens_est / cost_usd columns. Used to exercise the migration.
_OLD_SCHEMA = """
CREATE TABLE chats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    system_prompt  TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    temperature    REAL    NOT NULL,
    max_tokens     INTEGER NOT NULL,
    stream         INTEGER NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_chat ON messages(chat_id, id);
CREATE TABLE app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _seed_day7_db(path):
    """Create a Day 7 database with a chat and a message, no turns table."""
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO chats (title, system_prompt, model, temperature, "
        "max_tokens, stream, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("Старый чат", "Промпт", "deepseek-v4-flash", 0.2, 1500, 1, 12, 34),
    )
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (1, 'user', ?)",
        ("Привет мир",),
    )
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (1, 'assistant', ?)",
        ("Здравствуйте",),
    )
    conn.commit()
    conn.close()


# The Day 8 schema: turns table and demo_context_limit / history_tokens_est /
# cost_usd columns exist, but no summarize / keep_recent_turns / summary_*
# columns and no summaries table. Used to exercise the Day 9 migration.
_DAY8_SCHEMA = """
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
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
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
CREATE TABLE app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _seed_day8_db(path):
    """Create a Day 8 database: chats, messages, turns and app_state tables."""
    conn = sqlite3.connect(path)
    conn.executescript(_DAY8_SCHEMA)
    conn.execute(
        "INSERT INTO chats (title, system_prompt, model, temperature, "
        "max_tokens, stream, input_tokens, output_tokens, demo_context_limit, "
        "history_tokens_est, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Чат Дня 8", "Промпт", "deepseek-v4-flash", 0.2, 1500, 1, 12, 34, 500, 100, 0.001),
    )
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (1, 'user', ?)",
        ("Привет мир",),
    )
    conn.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (1, 'assistant', ?)",
        ("Здравствуйте",),
    )
    conn.commit()
    conn.close()


def _seed_model_variants(path, models):
    """Create one chat per model value, then rewrite ``chats.model`` directly.

    Writing the raw column simulates rows persisted by an older app version,
    independent of ``AgentConfig`` normalization.
    """
    store = ChatStore(path)
    chat_ids = []
    for index, model in enumerate(models):
        chat_id = store.create_chat(AgentConfig())
        store.save_turn(
            chat_id,
            f"вопрос {index}",
            f"ответ {index}",
            stats=TurnStats(request_tokens=10 + index, response_tokens=20 + index),
        )
        chat_ids.append(chat_id)
    with closing(sqlite3.connect(path)) as conn:
        for chat_id, model in zip(chat_ids, models):
            conn.execute("UPDATE chats SET model = ? WHERE id = ?", (model, chat_id))
        conn.commit()
    return chat_ids


# The Day 9 schema snapshot: summarize/keep_recent_turns/summary_* columns and
# the summaries table exist, but no Day 10 strategy columns, facts/branches
# tables or branch_id columns.
_DAY9_SCHEMA = """
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
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
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
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_summaries_chat ON summaries(chat_id);
CREATE TABLE app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _seed_day9_db(path):
    """Create a Day 9 database: one compressing chat and one plain chat.

    Chat 1 has ``summarize = 1`` and a stored summary with counters; chat 2 has
    ``summarize = 0``. The Day 10 migration must derive the strategy from that
    flag without touching any other row.
    """
    conn = sqlite3.connect(path)
    conn.executescript(_DAY9_SCHEMA)
    conn.execute(
        "INSERT INTO chats (title, system_prompt, model, temperature, max_tokens, "
        "stream, input_tokens, output_tokens, demo_context_limit, "
        "history_tokens_est, cost_usd, summarize, keep_recent_turns, "
        "summary_input_tokens, summary_output_tokens, summary_cost_usd, "
        "summary_cache_hit_tokens, summary_cache_miss_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Чат со сжатием", "Промпт", "deepseek-flash", 0.2, 1500, 1, 12, 34,
         500, 100, 0.001, 1, 3, 100, 50, 0.002, 10, 90),
    )
    conn.execute(
        "INSERT INTO chats (title, system_prompt, model, temperature, max_tokens, "
        "stream, summarize, keep_recent_turns) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("Чат без сжатия", "Промпт", "deepseek-flash", 0.2, 1500, 1, 0, 5),
    )
    for chat_id in (1, 2):
        conn.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, 'user', ?)",
            (chat_id, f"вопрос {chat_id}"),
        )
        conn.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, 'assistant', ?)",
            (chat_id, f"ответ {chat_id}"),
        )
        conn.execute(
            "INSERT INTO turns (chat_id, user_message_id, assistant_message_id, "
            "request_tokens, response_tokens) VALUES (?, ?, ?, ?, ?)",
            (chat_id, chat_id * 2 - 1, chat_id * 2, 5 + chat_id, 8 + chat_id),
        )
    conn.execute(
        "INSERT INTO summaries (chat_id, content, covered_messages_count, "
        "prompt_tokens, response_tokens) VALUES (1, 'Сводка Дня 9', 2, 100, 50)",
    )
    conn.commit()
    conn.close()


class ChatStoreTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_schema_init_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            ChatStore(path)
            ChatStore(path)  # second init on the same path must not fail

    def test_create_and_list_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            self.assertEqual(store.list_chats(), [])
            first = store.create_chat(AgentConfig())
            second = store.create_chat(AgentConfig())
            self.assertEqual([chat.id for chat in store.list_chats()], [second, first])

    def test_save_turn_writes_messages_and_derives_title_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "Первое сообщение", "Ответ 1")
            store.save_turn(chat_id, "Второе сообщение", "Ответ 2")

            messages = store.load_messages(chat_id)
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual(messages[0]["content"], "Первое сообщение")
            self.assertEqual(messages[1]["content"], "Ответ 1")
            # Title is derived from the first message and never overwritten.
            self.assertEqual(store.list_chats()[0].title, "Первое сообщение")

    def test_save_turn_derives_title_for_legacy_default_title(self):
        # A database migrated from Day 10 still carries the old "Новый чат"
        # default; the first turn must rename it like a fresh "New chat".
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            self.assertTrue(store.rename_chat(chat_id, "Новый чат"))

            store.save_turn(chat_id, "Первая реплика", "Ответ 1")
            self.assertEqual(store.list_chats()[0].title, "Первая реплика")

            # The derived title is not overwritten by later turns.
            store.save_turn(chat_id, "Вторая реплика", "Ответ 2")
            self.assertEqual(store.list_chats()[0].title, "Первая реплика")

    def test_save_turn_keeps_manual_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            self.assertTrue(store.rename_chat(chat_id, "Моё имя"))

            store.save_turn(chat_id, "Первое сообщение", "Ответ 1")
            self.assertEqual(store.list_chats()[0].title, "Моё имя")

    def test_title_truncated_at_40_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            long_text = "x" * 50
            store.save_turn(chat_id, long_text, "Ответ")
            self.assertEqual(store.list_chats()[0].title, "x" * 40 + "…")

    def test_rename_chat_strips_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            self.assertTrue(store.rename_chat(chat_id, "  Моё имя  "))
            self.assertEqual(store.list_chats()[0].title, "Моё имя")
            # The manual title survives reopening the database.
            reopened = ChatStore(path)
            self.assertEqual(reopened.list_chats()[0].title, "Моё имя")

    def test_rename_chat_keeps_updated_at_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            first = store.create_chat(AgentConfig())
            second = store.create_chat(AgentConfig())
            with closing(sqlite3.connect(path)) as conn:
                updated_before = conn.execute(
                    "SELECT updated_at FROM chats WHERE id = ?", (first,)
                ).fetchone()[0]

            self.assertTrue(store.rename_chat(first, "Переименован"))

            with closing(sqlite3.connect(path)) as conn:
                updated_after = conn.execute(
                    "SELECT updated_at FROM chats WHERE id = ?", (first,)
                ).fetchone()[0]
            self.assertEqual(updated_after, updated_before)
            self.assertEqual(
                [chat.id for chat in store.list_chats()], [second, first]
            )

    def test_rename_chat_blank_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            self.assertFalse(store.rename_chat(chat_id, "   "))
            self.assertFalse(store.rename_chat(chat_id, ""))
            self.assertEqual(store.list_chats()[0].title, DEFAULT_CHAT_TITLE)

    def test_rename_chat_unknown_id_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            self.assertFalse(store.rename_chat(99999, "Любое имя"))

    def test_rename_chat_special_characters_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            title = "O'Brien \"quoted\" 🚀; DROP TABLE chats; --"
            self.assertTrue(store.rename_chat(chat_id, title))
            self.assertTrue(store.rename_chat(chat_id, title))
            self.assertEqual(store.list_chats()[0].title, title)
            # The table is intact after the special-character title.
            self.assertEqual(len(store.list_chats()), 1)

    def test_rename_chat_keeps_long_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            long_name = "И" * 300
            self.assertTrue(store.rename_chat(chat_id, long_name))
            self.assertEqual(store.list_chats()[0].title, long_name)

    def test_manual_rename_is_not_overwritten_by_first_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.rename_chat(chat_id, "Ручное имя")
            store.save_turn(chat_id, "Первое сообщение", "Ответ")
            self.assertEqual(store.list_chats()[0].title, "Ручное имя")

    def test_rename_after_first_message_survives_later_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "Первое сообщение", "Ответ")
            store.rename_chat(chat_id, "Ручное имя")
            store.save_turn(chat_id, "Второе сообщение", "Ответ 2")
            self.assertEqual(store.list_chats()[0].title, "Ручное имя")

    def test_multi_chat_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            a = store.create_chat(AgentConfig(system_prompt="Промпт A"))
            b = store.create_chat(AgentConfig(system_prompt="Промпт B"))
            store.save_turn(a, "Вопрос A", "Ответ A")
            store.save_turn(b, "Вопрос B", "Ответ B")

            self.assertEqual([m["content"] for m in store.load_messages(a)], ["Вопрос A", "Ответ A"])
            self.assertEqual([m["content"] for m in store.load_messages(b)], ["Вопрос B", "Ответ B"])
            self.assertEqual(store.load_config(a)["system_prompt"], "Промпт A")
            self.assertEqual(store.load_config(b)["system_prompt"], "Промпт B")

    def test_save_load_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())

            config = AgentConfig(
                system_prompt="Промпт",
                model="custom",
                temperature=0.9,
                max_tokens=256,
                stream=False,
                demo_context_limit=500,
            )
            store.save_config(chat_id, config)
            loaded = store.load_config(chat_id)
            self.assertEqual(loaded["system_prompt"], "Промпт")
            self.assertEqual(loaded["model"], "custom")
            self.assertAlmostEqual(loaded["temperature"], 0.9)
            self.assertEqual(loaded["max_tokens"], 256)
            self.assertFalse(loaded["stream"])
            self.assertEqual(loaded["demo_context_limit"], 500)

            config.stream = True
            store.save_config(chat_id, config)
            self.assertTrue(store.load_config(chat_id)["stream"])

    def test_demo_context_limit_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(demo_context_limit=123))
            self.assertEqual(store.load_config(chat_id)["demo_context_limit"], 123)

            store.save_config(chat_id, AgentConfig(demo_context_limit=None))
            self.assertIsNone(store.load_config(chat_id)["demo_context_limit"])

    def test_delete_chat_removes_messages_and_last_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "Вопрос", "Ответ")
            store.set_last_selected(chat_id)
            self.assertEqual(store.get_last_selected_id(), chat_id)

            store.delete_chat(chat_id)
            self.assertEqual(store.load_messages(chat_id), [])
            self.assertIsNone(store.get_last_selected_id())
            with closing(sqlite3.connect(path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM turns WHERE chat_id = ?", (chat_id,)
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_last_selected_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            self.assertIsNone(store.get_last_selected_id())
            store.set_last_selected(42)
            self.assertEqual(store.get_last_selected_id(), 42)
            store.set_last_selected(43)
            self.assertEqual(store.get_last_selected_id(), 43)

    def test_usage_accumulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

            store.save_turn(chat_id, "Вопрос", "Ответ", stats=TurnStats(request_tokens=5, response_tokens=10))
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 5, "output_tokens": 10})

            # Partial update: only input reported.
            store.save_turn(chat_id, "Вопрос 2", "Ответ 2", stats=TurnStats(request_tokens=3))
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 8, "output_tokens": 10})

            store.save_turn(chat_id, "Вопрос 3", "Ответ 3", stats=TurnStats(request_tokens=2, response_tokens=4))
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": 10, "output_tokens": 14})

    def test_save_turn_records_turn_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            stats = TurnStats(
                user_message_tokens_est=7,
                request_tokens=10,
                response_tokens=20,
                total_tokens=30,
                prompt_cache_hit_tokens=4,
                prompt_cache_miss_tokens=6,
                finish_reason="stop",
                cost_usd=0.0001,
                cost_assumption="учтена разбивка cache hit/miss из usage",
            )
            store.save_turn(chat_id, "Вопрос", "Ответ", stats=stats)

            history = store.load_history(chat_id)
            self.assertEqual([m.role for m in history], ["user", "assistant"])
            user_msg, assistant_msg = history
            self.assertIsNotNone(user_msg.turn)
            self.assertEqual(user_msg.turn.user_message_tokens_est, 7)
            self.assertEqual(user_msg.turn.request_tokens, 10)
            self.assertEqual(user_msg.turn.response_tokens, 20)
            self.assertEqual(user_msg.turn.total_tokens, 30)
            self.assertEqual(user_msg.turn.prompt_cache_hit_tokens, 4)
            self.assertEqual(user_msg.turn.prompt_cache_miss_tokens, 6)
            self.assertEqual(user_msg.turn.finish_reason, "stop")
            self.assertAlmostEqual(user_msg.turn.cost_usd, 0.0001)
            self.assertIsNotNone(assistant_msg.turn)

            turn_record = store.list_turns(chat_id)[0]
            self.assertEqual(turn_record.stats.finish_reason, "stop")
            self.assertIsNotNone(turn_record.created_at)

    def test_load_history_old_messages_have_no_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            # Insert a message directly, without a turn row (simulates old data).
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "INSERT INTO messages (chat_id, role, content) VALUES (?, 'user', ?)",
                    (chat_id, "Старое сообщение"),
                )
                conn.commit()

            history = store.load_history(chat_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].role, "user")
            self.assertEqual(history[0].content, "Старое сообщение")
            self.assertIsNone(history[0].turn)

    def test_list_chats_summary_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(
                chat_id, "Вопрос", "Ответ",
                stats=TurnStats(request_tokens=5, response_tokens=10, cost_usd=0.001),
            )
            chat = store.list_chats()[0]
            self.assertEqual(chat.input_tokens, 5)
            self.assertEqual(chat.output_tokens, 10)
            self.assertIsNotNone(chat.cost_usd)
            self.assertEqual(chat.turns_count, 1)
            self.assertIsNotNone(chat.history_tokens_est)

    def test_get_chat_stats_with_nulls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            stats = store.get_chat_stats(chat_id)
            self.assertIsNone(stats.input_tokens)
            self.assertIsNone(stats.output_tokens)
            self.assertIsNone(stats.history_tokens_est)
            self.assertIsNone(stats.cost_usd)
            self.assertEqual(stats.turns_count, 0)

    def test_history_tokens_est_accumulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(
                chat_id, "Вопрос", "Ответ",
                stats=TurnStats(
                    user_message_tokens_est=7, response_tokens=20, assistant_tokens_est=21
                ),
            )
            # Exact response size is preferred over the local estimate.
            self.assertEqual(
                store.get_chat_stats(chat_id).history_tokens_est,
                7 + 20,
            )

            # When the exact response size is missing, fall back to the estimate.
            store.save_turn(
                chat_id, "Вопрос 2", "Ответ 2",
                stats=TurnStats(user_message_tokens_est=3, assistant_tokens_est=9),
            )
            self.assertEqual(
                store.get_chat_stats(chat_id).history_tokens_est,
                7 + 20 + 3 + 9,
            )

    def test_save_turn_atomic_on_db_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            # A trigger makes the turns insert fail, proving the earlier message
            # inserts are rolled back too.
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "CREATE TRIGGER fail_turn BEFORE INSERT ON turns "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
                )
                conn.commit()

            with self.assertRaises(sqlite3.Error):
                store.save_turn(
                    chat_id, "Вопрос", "Ответ",
                    stats=TurnStats(request_tokens=1, response_tokens=2),
                )

            self.assertEqual(store.load_messages(chat_id), [])
            self.assertEqual(store.get_chat_stats(chat_id).turns_count, 0)
            self.assertEqual(store.get_usage(chat_id), {"input_tokens": None, "output_tokens": None})

    def test_migration_from_day7_schema_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day7_db(path)

            store = ChatStore(path)

            self.assertEqual(store.load_messages(1)[0]["content"], "Привет мир")
            self.assertEqual(store.list_chats()[0].title, "Старый чат")
            self.assertIsNone(store.load_config(1)["demo_context_limit"])

            stats = store.get_chat_stats(1)
            self.assertEqual(stats.input_tokens, 12)
            self.assertEqual(stats.output_tokens, 34)
            self.assertEqual(stats.turns_count, 0)
            # history_tokens_est is backfilled from the existing messages.
            self.assertIsNotNone(stats.history_tokens_est)
            self.assertGreater(stats.history_tokens_est, 0)

            # Old messages have no turn rows, so turn is None.
            history = store.load_history(1)
            self.assertEqual([m.role for m in history], ["user", "assistant"])
            self.assertTrue(all(m.turn is None for m in history))

    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day7_db(path)
            ChatStore(path)
            ChatStore(path)  # second open must not fail
            store = ChatStore(path)
            self.assertEqual(store.load_messages(1)[0]["content"], "Привет мир")

    def test_backfill_only_fills_chats_with_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day7_db(path)
            # Add a second chat with no messages: it must stay NULL.
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "INSERT INTO chats (title, system_prompt, model, temperature, "
                    "max_tokens, stream) VALUES (?, ?, ?, ?, ?, ?)",
                    ("Пустой чат", "Промпт", "deepseek-v4-flash", 0.2, 1500, 1),
                )
                conn.commit()

            store = ChatStore(path)
            self.assertIsNotNone(store.get_chat_stats(1).history_tokens_est)
            self.assertIsNone(store.get_chat_stats(2).history_tokens_est)

    def test_backfill_matches_estimate_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day7_db(path)

            store = ChatStore(path)
            expected = estimate_tokens("Привет мир") + estimate_tokens("Здравствуйте")
            self.assertEqual(store.get_chat_stats(1).history_tokens_est, expected)

    def test_save_summary_upserts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            self.assertIsNone(store.load_summary(chat_id))

            store.save_summary(
                chat_id, "Сводка 1", 4,
                stats=TurnStats(request_tokens=10, response_tokens=20, total_tokens=30, cost_usd=0.001),
            )
            s = store.load_summary(chat_id)
            self.assertEqual(s.content, "Сводка 1")
            self.assertEqual(s.covered_messages_count, 4)
            self.assertEqual(s.prompt_tokens, 10)
            self.assertEqual(s.response_tokens, 20)
            self.assertEqual(s.total_tokens, 30)
            self.assertAlmostEqual(s.cost_usd, 0.001)
            self.assertIsNotNone(s.updated_at)

            # A second call replaces the single row instead of inserting another.
            store.save_summary(
                chat_id, "Сводка 2", 8,
                stats=TurnStats(request_tokens=15, response_tokens=25),
            )
            s = store.load_summary(chat_id)
            self.assertEqual(s.content, "Сводка 2")
            self.assertEqual(s.covered_messages_count, 8)
            self.assertEqual(s.prompt_tokens, 15)
            self.assertEqual(s.response_tokens, 25)
            self.assertIsNone(s.total_tokens)
            self.assertIsNone(s.cost_usd)

            with closing(sqlite3.connect(path)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            self.assertEqual(count, 1)

    def test_summary_counters_accumulate_over_two_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_summary(
                chat_id, "Сводка 1", 2,
                stats=TurnStats(
                    request_tokens=100,
                    response_tokens=50,
                    cost_usd=0.001,
                    prompt_cache_hit_tokens=10,
                    prompt_cache_miss_tokens=90,
                ),
            )
            store.save_summary(
                chat_id, "Сводка 2", 4,
                stats=TurnStats(
                    request_tokens=60,
                    response_tokens=30,
                    cost_usd=0.002,
                    prompt_cache_hit_tokens=5,
                    prompt_cache_miss_tokens=55,
                ),
            )
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 160)
            self.assertEqual(stats.summary_output_tokens, 80)
            self.assertAlmostEqual(stats.summary_cost_usd, 0.003)
            self.assertEqual(stats.summary_cache_hit_tokens, 15)
            self.assertEqual(stats.summary_cache_miss_tokens, 145)
            # Turn counters stay untouched by summary accumulation.
            self.assertIsNone(stats.input_tokens)
            self.assertIsNone(stats.output_tokens)

    def test_delete_chat_cascades_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            store.save_summary(
                chat_id, "Сводка", 2,
                stats=TurnStats(request_tokens=10, response_tokens=20),
            )
            self.assertIsNotNone(store.load_summary(chat_id))

            store.delete_chat(chat_id)
            with closing(sqlite3.connect(path)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            self.assertEqual(count, 0)

    def test_summarize_and_keep_recent_turns_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(summarize=False, keep_recent_turns=7))
            loaded = store.load_config(chat_id)
            self.assertFalse(loaded["summarize"])
            self.assertEqual(loaded["keep_recent_turns"], 7)

            store.save_config(chat_id, AgentConfig(summarize=True, keep_recent_turns=0))
            loaded = store.load_config(chat_id)
            self.assertTrue(loaded["summarize"])
            self.assertEqual(loaded["keep_recent_turns"], 0)

    def test_migration_from_day8_adds_summary_columns_and_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day8_db(path)

            store = ChatStore(path)

            # Old data is preserved.
            self.assertEqual(store.load_messages(1)[0]["content"], "Привет мир")
            self.assertEqual(store.list_chats()[0].title, "Чат Дня 8")

            # New config columns carry their defaults.
            config = store.load_config(1)
            self.assertTrue(config["summarize"])
            self.assertEqual(config["keep_recent_turns"], 3)
            self.assertEqual(config["demo_context_limit"], 500)

            # New counter columns are NULL until a summary is saved.
            stats = store.get_chat_stats(1)
            self.assertEqual(stats.input_tokens, 12)
            self.assertEqual(stats.output_tokens, 34)
            self.assertIsNone(stats.summary_input_tokens)
            self.assertIsNone(stats.summary_output_tokens)
            self.assertIsNone(stats.summary_cost_usd)
            self.assertIsNone(stats.summary_cache_hit_tokens)
            self.assertIsNone(stats.summary_cache_miss_tokens)

            # The summaries table exists and is empty for this chat.
            self.assertIsNone(store.load_summary(1))

            # Idempotent: reopening must not fail or corrupt data.
            ChatStore(path)
            self.assertEqual(store.load_messages(1)[0]["content"], "Привет мир")

    def test_record_summary_attempt_accumulates_without_summary_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            self.assertIsNone(store.load_summary(chat_id))
            with closing(sqlite3.connect(path)) as conn:
                updated_before = conn.execute(
                    "SELECT updated_at FROM chats WHERE id = ?", (chat_id,)
                ).fetchone()[0]

            store.record_summary_attempt(
                chat_id,
                stats=TurnStats(
                    request_tokens=100,
                    response_tokens=50,
                    prompt_cache_hit_tokens=10,
                    prompt_cache_miss_tokens=90,
                    cost_usd=0.001,
                ),
            )
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 100)
            self.assertEqual(stats.summary_output_tokens, 50)
            self.assertEqual(stats.summary_cache_hit_tokens, 10)
            self.assertEqual(stats.summary_cache_miss_tokens, 90)
            self.assertAlmostEqual(stats.summary_cost_usd, 0.001)
            # No summaries row is created and the chat timestamp is untouched.
            self.assertIsNone(store.load_summary(chat_id))
            with closing(sqlite3.connect(path)) as conn:
                updated_after = conn.execute(
                    "SELECT updated_at FROM chats WHERE id = ?", (chat_id,)
                ).fetchone()[0]
            self.assertEqual(updated_after, updated_before)

            # None values are skipped rather than coerced to 0.
            store.record_summary_attempt(
                chat_id, stats=TurnStats(request_tokens=None, cost_usd=None)
            )
            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.summary_input_tokens, 100)
            self.assertEqual(stats.summary_output_tokens, 50)
            self.assertAlmostEqual(stats.summary_cost_usd, 0.001)

    def test_save_summary_db_error_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            store.save_summary(
                chat_id, "S1", 2,
                stats=TurnStats(request_tokens=100, response_tokens=50, cost_usd=0.001),
            )
            before = store.load_summary(chat_id)
            stats_before = store.get_chat_stats(chat_id)

            # A trigger makes the counter update fail; the whole transaction,
            # including the summaries upsert, must roll back.
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "CREATE TRIGGER fail_chat_update BEFORE UPDATE ON chats "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
                )
                conn.commit()

            with self.assertRaises(sqlite3.Error):
                store.save_summary(
                    chat_id, "S2", 8,
                    stats=TurnStats(
                        request_tokens=999, response_tokens=999, cost_usd=9.0
                    ),
                )

            after = store.load_summary(chat_id)
            self.assertEqual(after.content, "S1")
            self.assertEqual(after.covered_messages_count, 2)
            self.assertEqual(after.prompt_tokens, before.prompt_tokens)
            self.assertEqual(after.updated_at, before.updated_at)
            stats_after = store.get_chat_stats(chat_id)
            self.assertEqual(
                stats_after.summary_input_tokens, stats_before.summary_input_tokens
            )
            self.assertEqual(
                stats_after.summary_output_tokens, stats_before.summary_output_tokens
            )
            self.assertAlmostEqual(
                stats_after.summary_cost_usd, stats_before.summary_cost_usd
            )


class LegacyModelMigrationTest(unittest.TestCase):
    """Migration of the legacy model ID stored in ``chats.model``."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def _raw_models(self, path):
        with closing(sqlite3.connect(path)) as conn:
            return dict(conn.execute("SELECT id, model FROM chats").fetchall())

    def test_exact_legacy_is_migrated_others_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            ids = _seed_model_variants(
                path,
                [
                    "deepseek-v4-flash",
                    "custom",
                    "deepseek-v4-flash-extra",
                    "DeepSeek-V4-Flash",
                    "deepseek-flash",
                ],
            )

            ChatStore(path)

            raw = self._raw_models(path)
            self.assertEqual(raw[ids[0]], "deepseek-flash")
            self.assertEqual(raw[ids[1]], "custom")
            self.assertEqual(raw[ids[2]], "deepseek-v4-flash-extra")
            self.assertEqual(raw[ids[3]], "DeepSeek-V4-Flash")
            self.assertEqual(raw[ids[4]], "deepseek-flash")

    def test_migration_idempotent_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            (chat_id,) = _seed_model_variants(path, ["deepseek-v4-flash"])

            ChatStore(path)
            ChatStore(path)
            store = ChatStore(path)  # third open must not fail or change data

            self.assertEqual(self._raw_models(path)[chat_id], "deepseek-flash")
            self.assertEqual(
                store.load_messages(chat_id),
                [
                    {"role": "user", "content": "вопрос 0"},
                    {"role": "assistant", "content": "ответ 0"},
                ],
            )
            self.assertEqual(
                store.get_usage(chat_id),
                {"input_tokens": 10, "output_tokens": 20},
            )
            self.assertEqual(store.get_chat_stats(chat_id).turns_count, 1)

    def test_fresh_db_stores_canonical_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            self.assertEqual(self._raw_models(path)[chat_id], "deepseek-flash")

    def test_migrated_row_loaded_as_canonical_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            (chat_id,) = _seed_model_variants(path, ["deepseek-v4-flash"])
            store = ChatStore(path)
            self.assertEqual(store.load_config(chat_id)["model"], "deepseek-flash")

    def test_day7_legacy_seed_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day7_db(path)
            ChatStore(path)
            self.assertEqual(self._raw_models(path)[1], "deepseek-flash")


class Day10StorageTest(unittest.TestCase):
    """Day 10 migration, strategy config and per-line summary behaviour."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_day9_migration_backfills_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day9_db(path)
            store = ChatStore(path)
            self.assertEqual(store.load_config(1)["context_strategy"], "summary")
            self.assertEqual(store.load_config(2)["context_strategy"], "full")
            self.assertTrue(store.load_config(1)["summarize"])
            self.assertFalse(store.load_config(2)["summarize"])
            self.assertEqual(store.get_chat_stats(1).context_strategy, "summary")
            self.assertEqual(store.get_chat_stats(2).context_strategy, "full")

    def test_day9_migration_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day9_db(path)
            store = ChatStore(path)
            self.assertEqual(
                [m["content"] for m in store.load_messages(1)],
                ["вопрос 1", "ответ 1"],
            )
            self.assertEqual(store.get_chat_stats(1).turns_count, 1)
            self.assertEqual(store.get_chat_stats(1).input_tokens, 12)
            self.assertEqual(store.get_chat_stats(1).output_tokens, 34)
            self.assertEqual(store.get_chat_stats(1).summary_input_tokens, 100)
            self.assertEqual(store.get_chat_stats(1).summary_output_tokens, 50)
            summary = store.load_summary(1)
            self.assertEqual(summary.content, "Сводка Дня 9")
            self.assertIsNone(summary.branch_id)

    def test_day7_and_day8_migrations_backfill_summary(self):
        for seed in (_seed_day7_db, _seed_day8_db):
            with self.subTest(seed=seed.__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "test.db")
                    seed(path)
                    store = ChatStore(path)
                    # Both legacy schemas default summarize to 1.
                    self.assertEqual(
                        store.load_config(1)["context_strategy"], "summary"
                    )

    def test_repeated_opens_do_not_change_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day9_db(path)
            ChatStore(path)
            with closing(sqlite3.connect(path)) as conn:
                before = conn.execute(
                    "SELECT * FROM chats ORDER BY id"
                ).fetchall()
            for _ in range(3):
                ChatStore(path)
            with closing(sqlite3.connect(path)) as conn:
                after = conn.execute("SELECT * FROM chats ORDER BY id").fetchall()
            self.assertEqual(before, after)

    def test_manual_strategy_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            chat_id = store.create_chat(AgentConfig())
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "UPDATE chats SET context_strategy = 'sliding' WHERE id = ?",
                    (chat_id,),
                )
                conn.commit()
            reopened = ChatStore(path)
            self.assertEqual(reopened.load_config(chat_id)["context_strategy"], "sliding")
            with closing(sqlite3.connect(path)) as conn:
                raw = conn.execute(
                    "SELECT context_strategy FROM chats WHERE id = ?", (chat_id,)
                ).fetchone()[0]
            self.assertEqual(raw, "sliding")

    def test_strategy_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(
                AgentConfig(
                    context_strategy="sliding",
                    sliding_window_messages=4,
                    facts_window_messages=8,
                )
            )
            loaded = store.load_config(chat_id)
            self.assertEqual(loaded["context_strategy"], "sliding")
            self.assertEqual(loaded["sliding_window_messages"], 4)
            self.assertEqual(loaded["facts_window_messages"], 8)
            self.assertFalse(loaded["summarize"])
            # load_config returns exactly the AgentConfig fields.
            config = AgentConfig(**loaded)
            self.assertEqual(config.context_strategy, "sliding")

            store.save_config(
                chat_id,
                AgentConfig(context_strategy="sticky_facts", summarize=False),
            )
            loaded = store.load_config(chat_id)
            self.assertEqual(loaded["context_strategy"], "sticky_facts")
            self.assertEqual(loaded["sliding_window_messages"], 6)
            self.assertEqual(loaded["facts_window_messages"], 6)

    def test_create_chat_with_duck_typed_config(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            store = ChatStore(path)
            config = SimpleNamespace(
                system_prompt="p",
                model="deepseek-flash",
                temperature=0.2,
                max_tokens=100,
                stream=True,
                demo_context_limit=None,
                summarize=False,
                keep_recent_turns=3,
            )
            chat_id = store.create_chat(config)
            self.assertEqual(store.load_config(chat_id)["context_strategy"], "full")

    def test_list_chats_reports_strategy_context_and_facts_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig(context_strategy="sticky_facts"))
            store.set_last_context_tokens(chat_id, 123)
            store.record_facts_attempt(
                chat_id, TurnStats(cost_usd=0.001), failed=False
            )
            store.record_facts_attempt(
                chat_id, TurnStats(cost_usd=0.002), failed=True
            )
            chat = store.list_chats()[0]
            self.assertEqual(chat.context_strategy, "sticky_facts")
            self.assertEqual(chat.last_context_tokens, 123)
            self.assertAlmostEqual(chat.facts_cost_usd, 0.003)

            stats = store.get_chat_stats(chat_id)
            self.assertEqual(stats.last_context_tokens, 123)
            self.assertEqual(stats.context_strategy, "sticky_facts")
            self.assertAlmostEqual(stats.facts_ok_cost_usd, 0.001)
            self.assertAlmostEqual(stats.facts_fail_cost_usd, 0.002)

    def test_summary_isolated_per_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            store.save_turn(chat_id, "вопрос", "ответ")
            assistant_id = store.load_branch_history(chat_id)[1].id
            branch_id = store.create_branch(chat_id, "B", None, assistant_id)

            store.save_summary(
                chat_id, "Основная", 2, stats=TurnStats(request_tokens=1)
            )
            store.save_summary(
                chat_id,
                "Ветка",
                2,
                stats=TurnStats(request_tokens=2),
                branch_id=branch_id,
            )
            main = store.load_summary(chat_id)
            self.assertEqual(main.content, "Основная")
            self.assertIsNone(main.branch_id)
            branch = store.load_summary(chat_id, branch_id=branch_id)
            self.assertEqual(branch.content, "Ветка")
            self.assertEqual(branch.branch_id, branch_id)


class BranchStorageTest(unittest.TestCase):
    """Branch creation, lineage reconstruction and switching."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        self.chat_id = self.store.create_chat(AgentConfig(context_strategy="branching"))

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_turns(self, count):
        for index in range(1, count + 1):
            self.store.save_turn(
                self.chat_id,
                f"вопрос {index}",
                f"ответ {index}",
                stats=TurnStats(request_tokens=index, response_tokens=index),
            )

    def _line(self, chat_id=None):
        return self.store.load_branch_history(chat_id or self.chat_id)

    def _assistant_ids(self, chat_id=None):
        return [
            message.id
            for message in self._line(chat_id)
            if message.role == "assistant"
        ]

    def test_create_branch_from_completed_turn(self):
        self._seed_turns(3)
        checkpoint = self._assistant_ids()[1]
        branch_id = self.store.create_branch(self.chat_id, "B1", None, checkpoint)
        branches = self.store.list_branches(self.chat_id)
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].id, branch_id)
        self.assertEqual(branches[0].fork_message_id, checkpoint)
        self.assertEqual(branches[0].parent_branch_id, None)

    def test_create_branch_rejects_invalid_checkpoints(self):
        self._seed_turns(2)
        history = self._line()
        user_id = history[0].id
        with self.assertRaises(ValueError):
            self.store.create_branch(self.chat_id, "B", None, user_id)
        with self.assertRaises(ValueError):
            self.store.create_branch(self.chat_id, "B", None, None)
        with self.assertRaises(ValueError):
            self.store.create_branch(self.chat_id, "   ", None, self._assistant_ids()[0])

        # A message without a turn row is not a valid checkpoint either.
        with closing(sqlite3.connect(self.path)) as conn:
            cursor = conn.execute(
                "INSERT INTO messages (chat_id, role, content) VALUES (?, 'assistant', ?)",
                (self.chat_id, "без хода"),
            )
            orphan_id = cursor.lastrowid
            conn.commit()
        with self.assertRaises(ValueError):
            self.store.create_branch(self.chat_id, "B", None, orphan_id)

        # A checkpoint of another chat is rejected.
        other_chat = self.store.create_chat(AgentConfig())
        self.store.save_turn(other_chat, "чужой вопрос", "чужой ответ")
        foreign_id = self.store.load_branch_history(other_chat)[1].id
        with self.assertRaises(ValueError):
            self.store.create_branch(self.chat_id, "B", None, foreign_id)

    def test_two_branches_from_same_checkpoint(self):
        self._seed_turns(3)
        checkpoint = self._assistant_ids()[1]
        first = self.store.create_branch(self.chat_id, "B1", None, checkpoint)
        second = self.store.create_branch(self.chat_id, "B2", None, checkpoint)
        ids = [branch.id for branch in self.store.list_branches(self.chat_id)]
        self.assertEqual(ids, [first, second])

    def test_line_is_prefix_plus_active_and_siblings_excluded(self):
        self._seed_turns(3)
        checkpoint = self._assistant_ids()[1]
        first = self.store.create_branch(self.chat_id, "B1", None, checkpoint)
        second = self.store.create_branch(self.chat_id, "B2", None, checkpoint)
        self.store.save_turn(
            self.chat_id, "b1 q", "b1 a", branch_id=first
        )
        self.store.save_turn(
            self.chat_id, "b2 q", "b2 a", branch_id=second
        )

        self.store.set_active_branch(self.chat_id, first)
        contents = [m.content for m in self._line()]
        self.assertEqual(
            contents, ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2", "b1 q", "b1 a"]
        )
        self.assertNotIn("b2 q", contents)
        self.assertNotIn("вопрос 3", contents)

        self.store.set_active_branch(self.chat_id, second)
        contents = [m.content for m in self._line()]
        self.assertEqual(
            contents, ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2", "b2 q", "b2 a"]
        )

        self.store.set_active_branch(self.chat_id, None)
        contents = [m.content for m in self._line()]
        self.assertEqual(
            contents,
            ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2", "вопрос 3", "ответ 3"],
        )

    def test_nested_branch(self):
        self._seed_turns(2)
        root_checkpoint = self._assistant_ids()[1]
        first = self.store.create_branch(self.chat_id, "B1", None, root_checkpoint)
        self.store.save_turn(self.chat_id, "b1 q", "b1 a", branch_id=first)
        self.store.set_active_branch(self.chat_id, first)
        child_checkpoint = self._assistant_ids()[-1]
        second = self.store.create_branch(
            self.chat_id, "B2", parent_branch_id=first, fork_message_id=child_checkpoint
        )
        self.store.save_turn(self.chat_id, "b2 q", "b2 a", branch_id=second)
        self.store.set_active_branch(self.chat_id, second)
        contents = [m.content for m in self._line()]
        self.assertEqual(
            contents,
            ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2", "b1 q", "b1 a", "b2 q", "b2 a"],
        )

    def test_switch_and_validate_active_branch(self):
        self._seed_turns(1)
        checkpoint = self._assistant_ids()[0]
        branch_id = self.store.create_branch(self.chat_id, "B", None, checkpoint)
        self.store.set_active_branch(self.chat_id, branch_id)
        self.assertEqual(self.store.get_active_branch(self.chat_id).id, branch_id)
        self.store.set_active_branch(self.chat_id, None)
        self.assertIsNone(self.store.get_active_branch(self.chat_id))

        other_chat = self.store.create_chat(AgentConfig())
        with self.assertRaises(ValueError):
            self.store.set_active_branch(other_chat, branch_id)

    def test_active_branch_survives_reopen(self):
        self._seed_turns(1)
        checkpoint = self._assistant_ids()[0]
        branch_id = self.store.create_branch(self.chat_id, "B", None, checkpoint)
        self.store.save_turn(self.chat_id, "b q", "b a", branch_id=branch_id)
        self.store.set_active_branch(self.chat_id, branch_id)

        reopened = ChatStore(self.path)
        self.assertEqual(reopened.get_active_branch(self.chat_id).id, branch_id)
        contents = [m.content for m in reopened.load_branch_line(self.chat_id)]
        self.assertEqual(contents, ["вопрос 1", "ответ 1", "b q", "b a"])

    def test_load_line_messages_after(self):
        self._seed_turns(3)
        history = self._line()
        anchor = history[1].id  # assistant of turn 1
        after = self.store.load_line_messages_after(self.chat_id, anchor)
        self.assertEqual(
            [m.content for m in after],
            ["вопрос 2", "ответ 2", "вопрос 3", "ответ 3"],
        )
        self.assertEqual(after[0].id, history[2].id)

    def test_branch_history_has_turn_stats(self):
        self._seed_turns(1)
        history = self._line()
        self.assertEqual([m.role for m in history], ["user", "assistant"])
        self.assertIsNotNone(history[0].turn)
        self.assertIsNotNone(history[1].turn)
        self.assertIsNotNone(history[0].id)

    def test_delete_chat_cascades_branches(self):
        self._seed_turns(1)
        checkpoint = self._assistant_ids()[0]
        branch_id = self.store.create_branch(self.chat_id, "B", None, checkpoint)
        self.store.save_turn(self.chat_id, "b q", "b a", branch_id=branch_id)
        self.store.delete_chat(self.chat_id)
        with closing(sqlite3.connect(self.path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM branches WHERE chat_id = ?", (self.chat_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_corrupt_active_branch_falls_back_to_main(self):
        self._seed_turns(1)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE chats SET active_branch_id = 999 WHERE id = ?",
                (self.chat_id,),
            )
            conn.commit()
        contents = [m.content for m in self.store.load_branch_line(self.chat_id)]
        self.assertEqual(contents, ["вопрос 1", "ответ 1"])


class BranchManagementTest(unittest.TestCase):
    """Line checkpoints, duplicate-name rejection and branch deletion."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.db")
        self.store = ChatStore(self.path)
        # A throwaway chat keeps the chat id different from the branch ids, so
        # a swapped delete key cannot pass by coincidence.
        self.store.create_chat(AgentConfig())
        self.chat_id = self.store.create_chat(
            AgentConfig(context_strategy="branching")
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_main(self, count):
        for index in range(1, count + 1):
            self.store.save_turn(
                self.chat_id,
                f"вопрос {index}",
                f"ответ {index}",
                stats=TurnStats(request_tokens=index, response_tokens=index),
            )

    def _main_assistant_ids(self):
        return [
            message.id
            for message in self.store.load_branch_history(self.chat_id)
            if message.role == "assistant"
        ]

    def _two_sibling_branches(self):
        self._seed_main(2)
        checkpoint = self._main_assistant_ids()[0]
        first = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        second = self.store.create_branch(self.chat_id, "B", None, checkpoint)
        self.store.save_turn(self.chat_id, "a-q", "a-a", branch_id=first)
        self.store.save_turn(self.chat_id, "b-q", "b-a", branch_id=second)
        return first, second, checkpoint

    def test_load_line_checkpoints_main_and_branch(self):
        self._seed_main(3)
        main_checkpoints = self.store.load_line_checkpoints(self.chat_id)
        self.assertEqual(
            [message.content for message in main_checkpoints],
            ["ответ 1", "ответ 2", "ответ 3"],
        )
        main_ids = [message.id for message in main_checkpoints]
        self.assertEqual(main_ids, sorted(main_ids))

        checkpoint = self._main_assistant_ids()[0]
        branch = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        self.store.save_turn(self.chat_id, "a-q", "a-a", branch_id=branch)

        branch_checkpoints = self.store.load_line_checkpoints(
            self.chat_id, branch
        )
        self.assertEqual(
            [message.content for message in branch_checkpoints],
            ["ответ 1", "a-a"],
        )
        # The main line still lists only its own completed turns.
        self.assertEqual(
            [m.id for m in self.store.load_line_checkpoints(self.chat_id)],
            main_ids,
        )

    def test_load_line_checkpoints_rejects_foreign_and_unknown(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        branch = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        other_chat = self.store.create_chat(AgentConfig())

        self.assertEqual(self.store.load_line_checkpoints(99999), [])
        with self.assertRaises(ValueError):
            self.store.load_line_checkpoints(other_chat, branch)
        with self.assertRaises(ValueError):
            self.store.load_line_checkpoints(self.chat_id, 99999)

    def test_duplicate_names_are_rejected_and_close_name_allowed(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        first = self.store.create_branch(
            self.chat_id, "Вариант А", None, checkpoint
        )
        for duplicate in ("Вариант А", "вариант а", "  Вариант   А  "):
            with self.subTest(duplicate=duplicate):
                with self.assertRaises(DuplicateBranchNameError) as ctx:
                    self.store.create_branch(
                        self.chat_id, duplicate, None, checkpoint
                    )
                self.assertEqual(ctx.exception.name, duplicate.strip())
                self.assertEqual(
                    len(self.store.list_branches(self.chat_id)), 1
                )

        second = self.store.create_branch(
            self.chat_id, "Вариант Б", None, checkpoint
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.list_branches(self.chat_id)), 2)

    def test_delete_branch_keeps_sibling_prefix_and_main_summary(self):
        first, second, _ = self._two_sibling_branches()
        self.store.save_summary(
            self.chat_id, "Основная", 4, stats=TurnStats(request_tokens=1)
        )

        self.store.delete_branch(self.chat_id, first)

        self.assertEqual(
            [branch.id for branch in self.store.list_branches(self.chat_id)],
            [second],
        )
        self.store.set_active_branch(self.chat_id, None)
        self.assertEqual(
            [m.content for m in self.store.load_branch_line(self.chat_id)],
            ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2"],
        )
        summary = self.store.load_summary(self.chat_id)
        self.assertEqual(summary.content, "Основная")

        self.store.set_active_branch(self.chat_id, second)
        contents = [m.content for m in self.store.load_branch_line(self.chat_id)]
        self.assertIn("b-q", contents)
        self.assertNotIn("a-q", contents)

    def test_delete_branch_with_children_is_rejected(self):
        first, _, _ = self._two_sibling_branches()
        self.store.save_turn(self.chat_id, "a2-q", "a2-a", branch_id=first)
        child_checkpoint = self.store.load_line_checkpoints(
            self.chat_id, first
        )[-1].id
        child = self.store.create_branch(
            self.chat_id,
            "A-child",
            parent_branch_id=first,
            fork_message_id=child_checkpoint,
        )
        self.store.set_active_branch(self.chat_id, child)
        before = self.store.list_branches(self.chat_id)

        with self.assertRaises(BranchHasChildrenError) as ctx:
            self.store.delete_branch(self.chat_id, first)

        self.assertEqual(ctx.exception.branch.id, first)
        self.assertEqual([item.id for item in ctx.exception.children], [child])
        self.assertEqual(self.store.list_branches(self.chat_id), before)
        self.assertEqual(self.store.get_active_branch(self.chat_id).id, child)

    def test_delete_active_root_branch_switches_to_main(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        branch = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        self.store.save_turn(self.chat_id, "a-q", "a-a", branch_id=branch)
        self.store.set_active_branch(self.chat_id, branch)

        self.store.delete_branch(self.chat_id, branch)

        self.assertEqual(self.store.list_branches(self.chat_id), [])
        self.assertIsNone(self.store.get_active_branch(self.chat_id))
        self.assertEqual(
            [m.content for m in self.store.load_branch_line(self.chat_id)],
            ["вопрос 1", "ответ 1"],
        )

    def test_delete_active_child_branch_switches_to_parent(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        parent = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        self.store.save_turn(self.chat_id, "a-q", "a-a", branch_id=parent)
        child_checkpoint = self.store.load_line_checkpoints(
            self.chat_id, parent
        )[-1].id
        child = self.store.create_branch(
            self.chat_id,
            "B",
            parent_branch_id=parent,
            fork_message_id=child_checkpoint,
        )
        self.store.save_turn(self.chat_id, "b-q", "b-a", branch_id=child)
        self.store.set_active_branch(self.chat_id, child)

        self.store.delete_branch(self.chat_id, child)

        self.assertEqual(self.store.get_active_branch(self.chat_id).id, parent)
        self.assertEqual(
            [item.id for item in self.store.list_branches(self.chat_id)],
            [parent],
        )

    def test_delete_last_branch_returns_to_main(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        only = self.store.create_branch(self.chat_id, "Only", None, checkpoint)
        self.store.set_active_branch(self.chat_id, only)

        self.store.delete_branch(self.chat_id, only)

        self.assertIsNone(self.store.get_active_branch(self.chat_id))
        self.assertEqual(self.store.list_branches(self.chat_id), [])

    def test_delete_removes_only_own_messages_turns_and_summary(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        branch = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        self.store.save_turn(self.chat_id, "a-q", "a-a", branch_id=branch)
        self.store.save_summary(
            self.chat_id,
            "Ветка",
            2,
            stats=TurnStats(request_tokens=2),
            branch_id=branch,
        )
        self.store.save_summary(
            self.chat_id, "Основная", 2, stats=TurnStats(request_tokens=1)
        )

        self.store.delete_branch(self.chat_id, branch)

        with closing(sqlite3.connect(self.path)) as conn:
            branch_messages = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE branch_id = ?", (branch,)
            ).fetchone()[0]
            branch_summaries = conn.execute(
                "SELECT COUNT(*) FROM summaries WHERE branch_id = ?", (branch,)
            ).fetchone()[0]
            chat_turns = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE chat_id = ?", (self.chat_id,)
            ).fetchone()[0]
        self.assertEqual(branch_messages, 0)
        self.assertEqual(branch_summaries, 0)
        self.assertEqual(chat_turns, 1)
        self.assertEqual(self.store.load_summary(self.chat_id).content, "Основная")

    def test_delete_branch_rejects_foreign_and_unknown(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        branch = self.store.create_branch(self.chat_id, "A", None, checkpoint)
        other_chat = self.store.create_chat(AgentConfig())

        with self.assertRaises(ValueError):
            self.store.delete_branch(other_chat, branch)
        with self.assertRaises(ValueError):
            self.store.delete_branch(self.chat_id, 99999)
        self.assertEqual(len(self.store.list_branches(self.chat_id)), 1)

    def test_delete_branch_survives_reopen(self):
        first, second, _ = self._two_sibling_branches()
        self.store.delete_branch(self.chat_id, first)

        reopened = ChatStore(self.path)
        self.assertEqual(
            [branch.id for branch in reopened.list_branches(self.chat_id)],
            [second],
        )
        reopened.set_active_branch(self.chat_id, second)
        self.assertEqual(
            [m.content for m in reopened.load_branch_line(self.chat_id)][-2:],
            ["b-q", "b-a"],
        )

    def test_delete_branch_atomic_on_db_error(self):
        first, second, _ = self._two_sibling_branches()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "CREATE TRIGGER fail_branch_delete BEFORE DELETE ON branches "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
            )
            conn.commit()

        with self.assertRaises(sqlite3.Error):
            self.store.delete_branch(self.chat_id, first)

        self.assertEqual(
            sorted(branch.id for branch in self.store.list_branches(self.chat_id)),
            sorted([first, second]),
        )
        with closing(sqlite3.connect(self.path)) as conn:
            branch_messages = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE branch_id = ?", (first,)
            ).fetchone()[0]
        self.assertEqual(branch_messages, 2)

    def test_delete_branch_with_corrupt_active_id_does_not_crash(self):
        first, second, _ = self._two_sibling_branches()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE chats SET active_branch_id = 999 WHERE id = ?",
                (self.chat_id,),
            )
            conn.commit()

        self.store.delete_branch(self.chat_id, first)

        self.assertEqual(
            [branch.id for branch in self.store.list_branches(self.chat_id)],
            [second],
        )

    def test_existing_duplicates_do_not_break_open_list_delete(self):
        self._seed_main(1)
        checkpoint = self._main_assistant_ids()[0]
        with closing(sqlite3.connect(self.path)) as conn:
            for _ in range(2):
                conn.execute(
                    "INSERT INTO branches (chat_id, name, parent_branch_id, "
                    "fork_message_id) VALUES (?, ?, NULL, ?)",
                    (self.chat_id, "Вариант А", checkpoint),
                )
            conn.commit()

        reopened = ChatStore(self.path)
        branches = reopened.list_branches(self.chat_id)
        self.assertEqual(
            [branch.name for branch in branches], ["Вариант А", "Вариант А"]
        )

        reopened.delete_branch(self.chat_id, branches[0].id)

        remaining = reopened.list_branches(self.chat_id)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].name, "Вариант А")


class BranchDuplicateLegacyMigrationTest(unittest.TestCase):
    """A migrated Day 9 database can already contain same-named branches."""

    def setUp(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_day9_migration_opens_with_duplicate_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            _seed_day9_db(path)
            ChatStore(path)  # migration creates the branches table
            with closing(sqlite3.connect(path)) as conn:
                for _ in range(2):
                    conn.execute(
                        "INSERT INTO branches (chat_id, name, parent_branch_id, "
                        "fork_message_id) VALUES (1, ?, NULL, ?)",
                        ("Вариант А", 2),
                    )
                conn.commit()

            reopened = ChatStore(path)
            branches = reopened.list_branches(1)
            self.assertEqual(len(branches), 2)
            self.assertEqual(
                [branch.name for branch in branches], ["Вариант А", "Вариант А"]
            )

            reopened.delete_branch(1, branches[0].id)

            self.assertEqual(len(reopened.list_branches(1)), 1)
            self.assertEqual(
                [m["content"] for m in reopened.load_messages(1)],
                ["вопрос 1", "ответ 1"],
            )


if __name__ == "__main__":
    unittest.main()
