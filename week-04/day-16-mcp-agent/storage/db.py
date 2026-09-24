"""SQLite access shared by the backend and the MCP server.

One file holds chats, messages, tasks and runs. Connections are short-lived:
every operation opens its own connection, sets the pragmas it needs and closes
it again, so a connection is never shared between threads. ``WAL`` allows many
readers and one writer, which is what two local processes on one machine need.

Opening is lazy. Building an application, listing MCP tools or probing a server
must not create the file: only the first real operation calls
:meth:`Database.initialize`, which creates the schema once per process. The
schema version lives in ``meta``; a file written by an older application version
is upgraded in place by :data:`MIGRATIONS`, while a file written by a newer
application version raises :class:`SchemaVersionError` before any write.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

CONNECT_TIMEOUT_SECONDS = 5.0
BUSY_TIMEOUT_MS = 5000

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chats (
      id         TEXT PRIMARY KEY,
      title      TEXT NOT NULL,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id    TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
      role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
      content    TEXT NOT NULL,
      created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id)",
    """
    CREATE TABLE IF NOT EXISTS tasks (
      id               TEXT PRIMARY KEY,
      chat_id          TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
      query            TEXT NOT NULL,
      interval_seconds INTEGER NOT NULL,
      max_results      INTEGER NOT NULL DEFAULT 0,
      status           TEXT NOT NULL CHECK (status IN ('active','stopped')),
      created_at       REAL NOT NULL,
      updated_at       REAL NOT NULL,
      next_run_at      REAL NOT NULL,
      stopped_at       REAL,
      last_run_at      REAL,
      last_status      TEXT,
      last_error       TEXT,
      run_count        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_due  ON tasks(status, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks(chat_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS runs (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      started_at   REAL NOT NULL,
      finished_at  REAL NOT NULL,
      status       TEXT NOT NULL CHECK (status IN ('ok','empty','error')),
      result_json  TEXT,
      result_count INTEGER NOT NULL DEFAULT 0,
      error        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id)",
)

# Forward-only migrations keyed by the version they produce. A database whose
# stored version is below ``SCHEMA_VERSION`` is upgraded in place inside the same
# transaction that creates the schema; a database from a newer version is still
# rejected before any statement runs.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE tasks ADD COLUMN max_results INTEGER NOT NULL DEFAULT 0",
    ),
}


class StorageError(Exception):
    """A controlled persistence failure.

    ``category`` maps to the HTTP error category, and ``message`` is safe to
    show to a caller: it never contains a path, a query or a secret.
    """

    def __init__(self, message: str, *, category: str = "chat_storage_unavailable"):
        super().__init__(message)
        self.category = str(category)
        self.message = str(message)


class SchemaVersionError(StorageError):
    """The database was written by a newer application version."""

    def __init__(self, message: str):
        super().__init__(message, category="chat_storage_unavailable")


class Database:
    """Lazy owner of the SQLite file and its schema."""

    def __init__(self, path):
        self.path = Path(path)
        self._initialized = False
        self._init_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        """Open a fresh connection with the pragmas this project relies on."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path), timeout=CONNECT_TIMEOUT_SECONDS
        )
        connection.row_factory = sqlite3.Row
        # Transactions are controlled explicitly (BEGIN IMMEDIATE/COMMIT).
        connection.isolation_level = None
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection

    def initialize(self) -> None:
        """Create the schema once per process and enforce the version gate."""
        with self._init_lock:
            if self._initialized:
                return
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                else:
                    existing = _as_version(row["value"])
                    if existing > SCHEMA_VERSION:
                        raise SchemaVersionError(
                            "The database schema is newer than this application "
                            "supports; refusing to write to it"
                        )
                    _apply_migrations(connection, existing)
                    if existing < SCHEMA_VERSION:
                        connection.execute(
                            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                            (str(SCHEMA_VERSION),),
                        )
                connection.execute("COMMIT")
            except BaseException:
                _rollback_quietly(connection)
                raise
            finally:
                connection.close()
            self._initialized = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection after the schema is guaranteed to exist."""
        self.initialize()
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside one ``BEGIN IMMEDIATE`` transaction."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                _rollback_quietly(connection)
                raise


def _as_version(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return SCHEMA_VERSION


def _apply_migrations(connection: sqlite3.Connection, existing: int) -> None:
    """Run every forward migration between ``existing`` and ``SCHEMA_VERSION``."""
    for version in range(existing + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS.get(version, ()):
            connection.execute(statement)


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:  # pragma: no cover - no transaction to roll back
        pass
