"""SQLite dialogue store: dialogues, messages and task memory (SPEC 5.5, 7).

Stores the factual answer model and available metrics for every assistant
message. The schema is versioned; an unknown version is an honest refusal.
Test databases are explicit absolute paths and are fully separate from any
user database. ``foreign_keys`` is enabled and the journal uses WAL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..errors import DialogueNotFound, MemoryConflict, StorageSchemaUnsupported

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dialogues (
    dialogue_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    usage_json TEXT,
    finish_reason TEXT,
    latency_ms REAL,
    parameters_json TEXT,
    is_error INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_dialogue ON messages(dialogue_id, ordinal);
CREATE TABLE IF NOT EXISTS task_memory (
    dialogue_id TEXT PRIMARY KEY REFERENCES dialogues(dialogue_id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 0,
    memory_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteDialogueStore:
    def __init__(self, db_path: str | Path, schema_version: int = SCHEMA_VERSION) -> None:
        self.db_path = str(db_path)
        self.schema_version = schema_version
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(DDL)
            row = self._conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(self.schema_version),),
                )
                self._conn.commit()
            else:
                try:
                    actual = int(row["value"])
                except (TypeError, ValueError):
                    actual = None
                if actual != self.schema_version:
                    self._conn.close()
                    raise StorageSchemaUnsupported(
                        "The dialogue schema version is not supported.",
                        details={"expected": self.schema_version, "actual": actual},
                    )

    # -- dialogues --------------------------------------------------------
    def create_dialogue(self, name: str | None = None) -> dict[str, Any]:
        dialogue_id = uuid.uuid4().hex
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO dialogues(dialogue_id, name, created_at, updated_at) VALUES (?,?,?,?)",
                (dialogue_id, name or "New dialogue", now, now),
            )
            self._conn.commit()
        return self.get_dialogue(dialogue_id)

    def list_dialogues(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dialogue_id, name, created_at, updated_at FROM dialogues ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dialogue(self, dialogue_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT dialogue_id, name, created_at, updated_at FROM dialogues WHERE dialogue_id=?",
                (dialogue_id,),
            ).fetchone()
        if row is None:
            raise DialogueNotFound("The dialogue was not found.", details={"dialogue_id": dialogue_id})
        return dict(row)

    def rename_dialogue(self, dialogue_id: str, name: str) -> dict[str, Any]:
        self.get_dialogue(dialogue_id)
        with self._lock:
            self._conn.execute(
                "UPDATE dialogues SET name=?, updated_at=? WHERE dialogue_id=?",
                (name, _utcnow(), dialogue_id),
            )
            self._conn.commit()
        return self.get_dialogue(dialogue_id)

    def delete_dialogue(self, dialogue_id: str) -> None:
        self.get_dialogue(dialogue_id)
        with self._lock:
            self._conn.execute("DELETE FROM dialogues WHERE dialogue_id=?", (dialogue_id,))
            self._conn.commit()

    # -- messages ---------------------------------------------------------
    def append_message(self, message: Mapping[str, Any]) -> dict[str, Any]:
        dialogue_id = str(message["dialogue_id"])
        self.get_dialogue(dialogue_id)
        message_id = uuid.uuid4().hex
        with self._lock:
            ordinal_row = self._conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM messages WHERE dialogue_id=?",
                (dialogue_id,),
            ).fetchone()
            ordinal = int(ordinal_row["n"])
            self._conn.execute(
                "INSERT INTO messages(message_id, dialogue_id, ordinal, role, text, provider, model,"
                " usage_json, finish_reason, latency_ms, parameters_json, is_error, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    dialogue_id,
                    ordinal,
                    str(message.get("role", "assistant")),
                    str(message.get("text", "")),
                    message.get("provider"),
                    message.get("model"),
                    json.dumps(message.get("usage")) if message.get("usage") is not None else None,
                    message.get("finish_reason"),
                    message.get("latency_ms"),
                    json.dumps(message.get("parameters")) if message.get("parameters") is not None else None,
                    1 if message.get("is_error") else 0,
                    _utcnow(),
                ),
            )
            self._conn.execute("UPDATE dialogues SET updated_at=? WHERE dialogue_id=?", (_utcnow(), dialogue_id))
            self._conn.commit()
        return self.get_message(dialogue_id, message_id)

    def get_message(self, dialogue_id: str, message_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE dialogue_id=? AND message_id=?",
                (dialogue_id, message_id),
            ).fetchone()
        if row is None:
            raise DialogueNotFound("The message was not found.", details={"message_id": message_id})
        return _message_row(row)

    def list_messages(self, dialogue_id: str, limit: int = 200) -> list[dict[str, Any]]:
        self.get_dialogue(dialogue_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE dialogue_id=? ORDER BY ordinal DESC LIMIT ?",
                (dialogue_id, limit),
            ).fetchall()
        return [_message_row(row) for row in reversed(rows)]

    def count_messages(self, dialogue_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE dialogue_id=?", (dialogue_id,)
            ).fetchone()
        return int(row["c"])

    # -- task memory ------------------------------------------------------
    def get_memory(self, dialogue_id: str) -> dict[str, Any]:
        self.get_dialogue(dialogue_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT version, memory_json FROM task_memory WHERE dialogue_id=?", (dialogue_id,)
            ).fetchone()
        if row is None:
            return {"dialogue_id": dialogue_id, "version": 0, "goal": None, "constraints": []}
        memory = json.loads(row["memory_json"]) if row["memory_json"] else {}
        memory["version"] = int(row["version"])
        return memory

    def save_memory(
        self, dialogue_id: str, memory: Mapping[str, Any], *, expected_version: int
    ) -> dict[str, Any]:
        self.get_dialogue(dialogue_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM task_memory WHERE dialogue_id=?", (dialogue_id,)
            ).fetchone()
            current = int(row["version"]) if row else 0
            if current != expected_version:
                raise MemoryConflict(
                    "The task memory version changed.",
                    details={"expected": expected_version, "actual": current},
                )
            new_version = current + 1
            payload = {k: v for k, v in memory.items() if k != "version"}
            payload["dialogue_id"] = dialogue_id
            self._conn.execute(
                "INSERT INTO task_memory(dialogue_id, version, memory_json, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(dialogue_id) DO UPDATE SET"
                " version=excluded.version, memory_json=excluded.memory_json, updated_at=excluded.updated_at",
                (dialogue_id, new_version, json.dumps(payload), _utcnow()),
            )
            self._conn.commit()
        result = self.get_memory(dialogue_id)
        return result

    # -- app state --------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_state(key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "dialogue_id": row["dialogue_id"],
        "ordinal": row["ordinal"],
        "role": row["role"],
        "text": row["text"],
        "provider": row["provider"],
        "model": row["model"],
        "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
        "finish_reason": row["finish_reason"],
        "latency_ms": row["latency_ms"],
        "parameters": json.loads(row["parameters_json"]) if row["parameters_json"] else None,
        "is_error": bool(row["is_error"]),
        "created_at": row["created_at"],
    }
