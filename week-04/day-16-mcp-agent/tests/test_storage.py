"""Unit tests of the shared SQLite layer (schema, pragmas, CAS, cascade)."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from storage.chats import ChatRepository
from storage.db import BUSY_TIMEOUT_MS, SCHEMA_VERSION, Database, SchemaVersionError
from storage.reports import ReportRepository
from storage.tasks import MAX_RUNS_PER_TASK, TaskRepository


class _TempDbTestCase(unittest.TestCase):
    """Every test gets its own database file in a temporary directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "storage.sqlite3"
        self.db = Database(self.path)


class LazyOpenTest(_TempDbTestCase):
    """Building does not create the file; the first operation does."""

    def test_construction_does_not_create_the_file(self):
        self.assertFalse(self.path.exists())

    def test_connection_creates_the_file_and_schema(self):
        with self.db.connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        # ``sqlite_sequence`` is an internal table created by AUTOINCREMENT.
        self.assertTrue(
            {"meta", "chats", "messages", "tasks", "runs", "reports"} <= tables,
            tables,
        )

    def test_schema_version_is_written_once(self):
        with self.db.connection() as connection:
            first = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        # A second initialization must not change the stored version.
        with self.db.connection() as connection:
            second = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(first, str(SCHEMA_VERSION))
        self.assertEqual(second, str(SCHEMA_VERSION))

    def test_fresh_schema_is_v3_with_max_results_and_reports(self):
        with self.db.connection() as connection:
            version = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            columns = {
                row["name"]: row
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            report_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(reports)").fetchall()
            }
        self.assertEqual(version, "3")
        self.assertIn("max_results", columns)
        self.assertEqual(int(columns["max_results"]["notnull"]), 1)
        self.assertEqual(str(columns["max_results"]["dflt_value"]).strip(), "0")
        self.assertEqual(
            report_columns,
            {
                "id",
                "chat_id",
                "topic",
                "summary",
                "sources_json",
                "digest_id",
                "source_count",
                "created_at",
            },
        )


class PragmaTest(_TempDbTestCase):
    """The connection pragmas the two processes depend on are applied."""

    def test_wal_foreign_keys_and_busy_timeout(self):
        with self.db.connection() as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0],
                BUSY_TIMEOUT_MS,
            )


class SchemaVersionGateTest(_TempDbTestCase):
    """A newer schema version is refused without writing to the file."""

    def test_newer_schema_version_raises(self):
        connection = sqlite3.connect(str(self.path))
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '99')")
        connection.commit()
        connection.close()

        with self.assertRaises(SchemaVersionError):
            self.db.initialize()


V1_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, "
    "role TEXT NOT NULL CHECK (role IN ('user','assistant')), "
    "content TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, "
    "chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, "
    "query TEXT NOT NULL, interval_seconds INTEGER NOT NULL, "
    "status TEXT NOT NULL CHECK (status IN ('active','stopped')), "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "next_run_at REAL NOT NULL, stopped_at REAL, last_run_at REAL, "
    "last_status TEXT, last_error TEXT, run_count INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX idx_tasks_due ON tasks(status, next_run_at)",
    "CREATE INDEX idx_tasks_chat ON tasks(chat_id, created_at)",
    "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
    "started_at REAL NOT NULL, finished_at REAL NOT NULL, "
    "status TEXT NOT NULL CHECK (status IN ('ok','empty','error')), "
    "result_json TEXT, result_count INTEGER NOT NULL DEFAULT 0, error TEXT)",
    "CREATE INDEX idx_runs_task ON runs(task_id, id)",
)


class SchemaMigrationTest(_TempDbTestCase):
    """A v1 database with data is upgraded to v2 in place, without data loss."""

    def _seed_v1(self):
        connection = sqlite3.connect(str(self.path))
        try:
            for statement in V1_SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', '1')"
            )
            connection.execute(
                "INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES ('c1', 'Old chat', 10.0, 20.0)"
            )
            connection.execute(
                "INSERT INTO tasks(id, chat_id, query, interval_seconds, status, "
                "created_at, updated_at, next_run_at) "
                "VALUES ('t1', 'c1', 'old news', 60, 'active', 10.0, 20.0, 30.0)"
            )
            connection.execute(
                "INSERT INTO runs(task_id, started_at, finished_at, status, "
                "result_json, result_count) VALUES ('t1', 10.0, 11.0, 'ok', "
                "'{\"count\": 2}', 2)"
            )
        finally:
            connection.commit()
            connection.close()

    def test_v1_database_is_upgraded_without_losing_data(self):
        self._seed_v1()

        self.db.initialize()

        with self.db.connection() as connection:
            version = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            task = connection.execute(
                "SELECT * FROM tasks WHERE id='t1'"
            ).fetchone()
            chat = connection.execute(
                "SELECT * FROM chats WHERE id='c1'"
            ).fetchone()
            runs = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE task_id='t1'"
            ).fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

        self.assertEqual(version, "3")
        self.assertEqual(int(task["max_results"]), 0)
        self.assertEqual(task["query"], "old news")
        self.assertEqual(chat["title"], "Old chat")
        self.assertEqual(runs, 1)
        self.assertEqual(foreign_keys, 1)

    def test_upgraded_database_cascades_and_is_idempotent(self):
        self._seed_v1()
        self.db.initialize()
        # A second run of the migration must not raise or repeat the ALTER.
        self.db.initialize()
        Database(self.path).initialize()

        with self.db.connection() as connection:
            connection.execute("DELETE FROM chats WHERE id='c1'")
            tasks = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(tasks, 0)
        self.assertEqual(runs, 0)


V2_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, "
    "role TEXT NOT NULL CHECK (role IN ('user','assistant')), "
    "content TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE INDEX idx_messages_chat ON messages(chat_id, id)",
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, "
    "chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, "
    "query TEXT NOT NULL, interval_seconds INTEGER NOT NULL, "
    "max_results INTEGER NOT NULL DEFAULT 0, "
    "status TEXT NOT NULL CHECK (status IN ('active','stopped')), "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "next_run_at REAL NOT NULL, stopped_at REAL, last_run_at REAL, "
    "last_status TEXT, last_error TEXT, run_count INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX idx_tasks_due ON tasks(status, next_run_at)",
    "CREATE INDEX idx_tasks_chat ON tasks(chat_id, created_at)",
    "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
    "started_at REAL NOT NULL, finished_at REAL NOT NULL, "
    "status TEXT NOT NULL CHECK (status IN ('ok','empty','error')), "
    "result_json TEXT, result_count INTEGER NOT NULL DEFAULT 0, error TEXT)",
    "CREATE INDEX idx_runs_task ON runs(task_id, id)",
)


class SchemaV2MigrationTest(_TempDbTestCase):
    """A v2 database with data is upgraded to v3, adding only the reports table."""

    def _seed_v2(self):
        connection = sqlite3.connect(str(self.path))
        try:
            for statement in V2_SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', '2')"
            )
            connection.execute(
                "INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES ('c1', 'Chat v2', 10.0, 20.0)"
            )
            connection.execute(
                "INSERT INTO tasks(id, chat_id, query, interval_seconds, max_results, "
                "status, created_at, updated_at, next_run_at) "
                "VALUES ('t1', 'c1', 'v2 news', 60, 2, 'active', 10.0, 20.0, 30.0)"
            )
        finally:
            connection.commit()
            connection.close()

    def test_v2_database_gains_reports_without_losing_data(self):
        self._seed_v2()
        self.db.initialize()
        # A second run must be idempotent.
        self.db.initialize()

        with self.db.connection() as connection:
            version = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            task = connection.execute("SELECT * FROM tasks WHERE id='t1'").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            count = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(version, "3")
        self.assertIn("reports", tables)
        self.assertEqual(count, 0)
        self.assertEqual(task["query"], "v2 news")
        self.assertEqual(int(task["max_results"]), 2)


class ConcurrencyTest(_TempDbTestCase):
    """Short-lived connections can write from several threads."""

    def test_parallel_inserts_succeed(self):
        errors: list = []

        def worker(index):
            try:
                ChatRepository(self.db).create_chat(f"chat-{index}")
            except Exception as exc:  # noqa: BLE001 - reported through the list
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(ChatRepository(self.db).count_chats(), 5)


class CascadeTest(_TempDbTestCase):
    """Deleting a chat cascades to messages, tasks and runs."""

    def test_delete_chat_removes_children(self):
        chats = ChatRepository(self.db)
        tasks = TaskRepository(self.db)
        reports = ReportRepository(self.db)
        chats.create_chat("Chat", chat_id="c1")
        chats.append_message("c1", "user", "hello")
        task = tasks.create_task("c1", "news", 60, now=100.0)
        tasks.record_run(
            task["id"], started_at=100.0, finished_at=101.0, status="ok",
            result_count=2, result={"query": "news", "count": 2, "results": []},
        )
        reports.insert_report(
            "c1",
            topic="news",
            summary="1. t\n   Source: https://a.test/1",
            sources=[{"title": "t", "url": "https://a.test/1", "description": ""}],
            digest_id="d19-x",
        )

        self.assertTrue(chats.delete_chat("c1"))

        with self.db.connection() as connection:
            for table in ("messages", "tasks", "runs", "reports"):
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0, table)


class RunRetentionTest(_TempDbTestCase):
    """Only the newest runs of a task are kept."""

    def test_runs_are_pruned(self):
        chats = ChatRepository(self.db)
        tasks = TaskRepository(self.db)
        chats.create_chat("Chat", chat_id="c1")
        task = tasks.create_task("c1", "news", 60, now=100.0)
        for index in range(MAX_RUNS_PER_TASK + 7):
            tasks.record_run(
                task["id"],
                started_at=100.0 + index,
                finished_at=101.0 + index,
                status="empty",
                result_count=0,
            )
        self.assertEqual(tasks.run_count(task["id"]), MAX_RUNS_PER_TASK)


class RunWithoutTaskTest(_TempDbTestCase):
    """A run for a missing task is dropped and creates no row."""

    def test_record_run_returns_none_for_a_deleted_task(self):
        result = TaskRepository(self.db).record_run(
            "missing", started_at=1.0, finished_at=2.0, status="ok"
        )
        self.assertIsNone(result)


class ClaimCasTest(_TempDbTestCase):
    """The slot claim is idempotent: exactly one caller wins."""

    def setUp(self):
        super().setUp()
        self.chats = ChatRepository(self.db)
        self.tasks = TaskRepository(self.db)
        self.chats.create_chat("Chat", chat_id="c1")
        self.task = self.tasks.create_task(
            "c1", "news", 60, now=100.0, next_run_at=100.0
        )

    def test_first_claim_wins_and_second_loses(self):
        first = self.tasks.claim_task(
            self.task["id"], now=100.0, expected_next_run_at=100.0
        )
        second = self.tasks.claim_task(
            self.task["id"], now=100.0, expected_next_run_at=100.0
        )
        self.assertTrue(first)
        self.assertFalse(second)
        updated = self.tasks.get_task(self.task["id"])
        self.assertEqual(updated["next_run_at"], 160.0)

    def test_claim_skips_stopped_tasks(self):
        self.tasks.stop_task(self.task["id"], now=100.0)
        claimed = self.tasks.claim_task(
            self.task["id"], now=100.0, expected_next_run_at=100.0
        )
        self.assertFalse(claimed)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
