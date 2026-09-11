import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from agent import AgentConfig
from stats import TurnStats
from storage import ChatStore
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

    def test_title_truncated_at_40_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(os.path.join(tmp, "test.db"))
            chat_id = store.create_chat(AgentConfig())
            long_text = "x" * 50
            store.save_turn(chat_id, long_text, "Ответ")
            self.assertEqual(store.list_chats()[0].title, "x" * 40 + "…")

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


if __name__ == "__main__":
    unittest.main()
