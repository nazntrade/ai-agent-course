"""Unit tests of the notifier database schema (separate file, version gate).

Every test uses its own temporary database, so the real ``data/day20-notifier.sqlite3``
and the shared database of server A are never touched.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mcp_server import config as mcp_config
from notifier_server import config as notifier_config
from notifier_server.db import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    Database,
    SchemaVersionError,
)


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "day20-notifier.sqlite3"
        self.db = Database(self.path)


class LazyOpenTest(_TempDbTestCase):
    """Building does not create the file; the first operation does."""

    def test_construction_does_not_create_the_file(self):
        self.assertFalse(self.path.exists())

    def test_connection_creates_the_schema(self):
        with self.db.connection() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            version = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertTrue({"meta", "watches", "seen_items", "deliveries"} <= tables, tables)
        self.assertEqual(version, str(SCHEMA_VERSION))

    def test_pragmas(self):
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


class SchemaShapeTest(_TempDbTestCase):
    """The three tables carry the columns the service relies on."""

    def _columns(self, table: str) -> set:
        with self.db.connection() as connection:
            return {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }

    def test_watches_columns(self):
        self.assertTrue(
            {
                "id",
                "chat_id",
                "query",
                "keywords_json",
                "exclude_json",
                "interval_seconds",
                "summary_interval_seconds",
                "source_task_id",
                "status",
                "created_at",
                "updated_at",
                "next_check_at",
                "last_check_at",
                "last_delivery_status",
            }
            <= self._columns("watches")
        )

    def test_seen_items_columns(self):
        self.assertEqual(
            self._columns("seen_items"),
            {"watch_id", "fingerprint", "title", "url", "seen_at"},
        )

    def test_deliveries_columns(self):
        self.assertTrue(
            {
                "id",
                "watch_id",
                "chat_id",
                "kind",
                "period_key",
                "status",
                "items_json",
                "error",
                "attempts",
                "created_at",
                "updated_at",
            }
            <= self._columns("deliveries")
        )
        with self.db.connection() as connection:
            info = {
                row["name"]: row
                for row in connection.execute(
                    "PRAGMA table_info(deliveries)"
                ).fetchall()
            }
        self.assertEqual(int(info["attempts"]["notnull"]), 1)
        self.assertEqual(str(info["attempts"]["dflt_value"]).strip(), "1")
        self.assertEqual(int(info["updated_at"]["notnull"]), 1)

    def test_unique_delivery_key(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO deliveries(id, watch_id, chat_id, kind, period_key, "
                "status, items_json, created_at, updated_at) "
                "VALUES ('d1', 'w1', 'c1', 'new_items', 'k1', 'sent', '[]', 1.0, 1.0)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO deliveries(id, watch_id, chat_id, kind, period_key, "
                    "status, items_json, created_at, updated_at) "
                    "VALUES ('d2', 'w1', 'c1', 'new_items', 'k1', 'sent', '[]', 2.0, 2.0)"
                )

    def test_status_check_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO deliveries(id, watch_id, chat_id, kind, period_key, "
                    "status, created_at, updated_at) "
                    "VALUES ('d3', 'w1', 'c1', 'bogus', 'k2', 'sent', 1.0, 1.0)"
                )

    def test_seen_primary_key_rejects_duplicates(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO seen_items(watch_id, fingerprint, title, seen_at) "
                "VALUES ('w1', 'fp', 't', 1.0)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO seen_items(watch_id, fingerprint, title, seen_at) "
                    "VALUES ('w1', 'fp', 't', 2.0)"
                )


class VersionGateTest(_TempDbTestCase):
    """A newer schema version is refused without writing to the file."""

    def test_newer_version_raises(self):
        connection = sqlite3.connect(str(self.path))
        connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '99')"
        )
        connection.commit()
        connection.close()

        with self.assertRaises(SchemaVersionError):
            self.db.initialize()


class SeparateFileTest(unittest.TestCase):
    """Server B opens a different default file than server A."""

    def test_default_paths_differ(self):
        notifier_path = notifier_config.resolve_db_path(env={})
        mcp_path = mcp_config.resolve_db_path(env={})
        self.assertNotEqual(notifier_path, mcp_path)
        self.assertTrue(str(notifier_path).endswith("day20-notifier.sqlite3"))
        self.assertTrue(str(mcp_path).endswith("day18.sqlite3"))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
