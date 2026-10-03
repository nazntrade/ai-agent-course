"""SQLite dialogue store: history and task memory in a SEPARATE database file.

The D25 conversation store is deliberately independent from the D21 document
index (``KNOWLEDGE_DB_PATH``) and from the legacy single-run records. Task
memory lives in its own table (``task_memories``) distinct from the turns table,
so "separately stored" holds at the schema level while documents stay in a
different file entirely (SPEC D25 6.3, 10).

The migration is additive and idempotent: it never imports ``chat-run-v1``
records and never fabricates dialogue history. An unknown/newer schema version
fails closed with ``conversation_store_schema_unsupported``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..domain.contracts import ConversationStore, TaskMemory
from ..domain.errors import (
    ConversationStoreSchemaUnsupported,
    MemoryConflict,
)

CONVERSATION_SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteConversationStore(ConversationStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the FastAPI threadpool share the handle;
        # a lock serialises writes so optimistic-lock transactions are safe.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self.ensure_schema()

    # -- schema -----------------------------------------------------------
    def ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS conversation_meta ("
                "  key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT value FROM conversation_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO conversation_meta(key, value) VALUES('schema_version', ?)",
                    (str(CONVERSATION_SCHEMA_VERSION),),
                )
            else:
                try:
                    stored = int(row["value"])
                except (TypeError, ValueError):
                    stored = -1
                if stored != CONVERSATION_SCHEMA_VERSION:
                    raise ConversationStoreSchemaUnsupported(
                        "The conversation store schema version is unsupported.",
                        details={"expected": CONVERSATION_SCHEMA_VERSION, "actual": stored},
                    )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS dialogues ("
                "  dialogue_id TEXT PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  created_at TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "  turn_id TEXT NOT NULL,"
                "  dialogue_id TEXT NOT NULL,"
                "  ordinal INTEGER NOT NULL,"
                "  client_turn_id TEXT NOT NULL,"
                "  created_at TEXT NOT NULL,"
                "  record_json TEXT NOT NULL,"
                "  PRIMARY KEY(dialogue_id, turn_id),"
                "  UNIQUE(dialogue_id, client_turn_id),"
                "  FOREIGN KEY(dialogue_id) REFERENCES dialogues(dialogue_id) ON DELETE CASCADE)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_dialogue_ordinal"
                " ON turns(dialogue_id, ordinal)"
            )
            # Task memory is a distinct table, never mixed with turns.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS task_memories ("
                "  dialogue_id TEXT PRIMARY KEY,"
                "  version INTEGER NOT NULL,"
                "  memory_json TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL,"
                "  FOREIGN KEY(dialogue_id) REFERENCES dialogues(dialogue_id) ON DELETE CASCADE)"
            )

    # -- dialogues --------------------------------------------------------
    def create_dialogue(self, name: str | None = None) -> dict[str, Any]:
        dialogue_id = "dlg-" + uuid.uuid4().hex
        display_name = (str(name).strip() if name else "") or "New dialogue"
        now = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO dialogues(dialogue_id, name, created_at, updated_at)"
                " VALUES(?, ?, ?, ?)",
                (dialogue_id, display_name, now, now),
            )
        return self._dialogue_row(dialogue_id)

    def list_dialogues(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM turns t WHERE t.dialogue_id=d.dialogue_id)"
                " AS turn_count FROM dialogues d ORDER BY d.updated_at DESC, d.created_at DESC"
            ).fetchall()
        return [_dialogue_projection(row) for row in rows]

    def get_dialogue(self, dialogue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM turns t WHERE t.dialogue_id=d.dialogue_id)"
                " AS turn_count FROM dialogues d WHERE d.dialogue_id=?",
                (dialogue_id,),
            ).fetchone()
        return _dialogue_projection(row) if row else None

    def rename_dialogue(self, dialogue_id: str, name: str) -> dict[str, Any]:
        display_name = (str(name).strip() if name else "") or "New dialogue"
        now = _utcnow()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE dialogues SET name=?, updated_at=? WHERE dialogue_id=?",
                (display_name, now, dialogue_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(dialogue_id)
        return self.get_dialogue(dialogue_id)  # type: ignore[return-value]

    def delete_dialogue(self, dialogue_id: str) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM dialogues WHERE dialogue_id=?", (dialogue_id,)
            )
        if cursor.rowcount == 0:
            raise KeyError(dialogue_id)

    # -- turns ------------------------------------------------------------
    def append_turn(self, turn: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._conn:
            return self._append_turn_locked(turn)

    def commit_turn(
        self, turn: Mapping[str, Any], memory: Mapping[str, Any] | None,
        *, expected_version: int,
    ) -> dict[str, Any]:
        """Idempotent turn + confirmed memory update in one transaction.

        Any insert failure rolls memory back too, so a confirmed item never
        references a user turn that failed to persist. A concurrent duplicate
        returns its existing response before the optimistic-version check.
        """
        dialogue_id = str(turn.get("dialogue_id") or "")
        with self._lock, self._conn:
            existing = self.get_turn_by_client_id(dialogue_id, str(turn.get("client_turn_id") or ""))
            if existing is not None:
                return existing
            current = int(self.get_memory(dialogue_id).get("version") or 0)
            if current != int(expected_version):
                raise MemoryConflict(
                    "Task memory changed while the answer was being prepared; reload and retry.",
                    details={"expected_version": int(expected_version), "current_version": current},
                )
            if memory is not None:
                self._save_memory_locked(dialogue_id, memory, expected_version=expected_version)
            return self._append_turn_locked(turn)

    def _append_turn_locked(self, turn: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(turn)
        dialogue_id = str(payload.get("dialogue_id") or "")
        turn_id = str(payload.get("turn_id") or "")
        client_turn_id = str(payload.get("client_turn_id") or "")
        if not dialogue_id or not turn_id or not client_turn_id:
            raise ValueError("A dialogue turn requires dialogue_id, turn_id and client_turn_id.")
        if self.get_dialogue(dialogue_id) is None:
            raise KeyError(dialogue_id)
        now = _utcnow()
        existing = self._conn.execute(
            "SELECT record_json FROM turns WHERE dialogue_id=? AND"
            " (client_turn_id=? OR turn_id=?)",
            (dialogue_id, client_turn_id, turn_id),
        ).fetchone()
        if existing is not None:
            return json.loads(existing["record_json"])
        ordinal = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM turns WHERE dialogue_id=?",
                (dialogue_id,),
            ).fetchone()[0]
        )
        # The DB ordinal is authoritative (SPEC D25 6.2): persist it inside
        # the record so clients can page with ``before=<ordinal>``; the
        # service assembles turns before the store assigns the ordinal.
        payload["ordinal"] = ordinal
        self._conn.execute(
            "INSERT INTO turns(turn_id, dialogue_id, ordinal, client_turn_id, created_at, record_json)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                dialogue_id,
                ordinal,
                client_turn_id,
                str(payload.get("created_at") or now),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._conn.execute(
            "UPDATE dialogues SET updated_at=? WHERE dialogue_id=?", (now, dialogue_id)
        )
        return payload

    def get_turn(self, dialogue_id: str, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record_json FROM turns WHERE dialogue_id=? AND turn_id=?",
                (dialogue_id, turn_id),
            ).fetchone()
        return json.loads(row["record_json"]) if row else None

    def get_turn_by_client_id(
        self, dialogue_id: str, client_turn_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record_json FROM turns WHERE dialogue_id=? AND client_turn_id=?",
                (dialogue_id, client_turn_id),
            ).fetchone()
        return json.loads(row["record_json"]) if row else None

    def list_turns(
        self,
        dialogue_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        order = "ASC" if ascending else "DESC"
        params: list[Any] = [dialogue_id]
        clause = ""
        if before is not None:
            try:
                before_ordinal = int(before)
            except (TypeError, ValueError):
                before_ordinal = 0
            clause = " AND ordinal < ?"
            params.append(before_ordinal)
        params.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT record_json FROM turns WHERE dialogue_id=?{clause}"
                f" ORDER BY ordinal {order} LIMIT ?",
                params,
            ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def count_turns(self, dialogue_id: str) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM turns WHERE dialogue_id=?", (dialogue_id,)
                ).fetchone()[0]
            )

    # -- task memory ------------------------------------------------------
    def get_memory(self, dialogue_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT memory_json FROM task_memories WHERE dialogue_id=?",
                (dialogue_id,),
            ).fetchone()
        if row is None:
            return TaskMemory(dialogue_id=dialogue_id, version=0).to_dict()
        return json.loads(row["memory_json"])

    def save_memory(
        self, dialogue_id: str, memory: Mapping[str, Any], *, expected_version: int,
    ) -> dict[str, Any]:
        with self._lock, self._conn:
            return self._save_memory_locked(dialogue_id, memory, expected_version=expected_version)

    def user_turn_ids(self, dialogue_id: str) -> set[str]:
        """Ground validation needs identifiers, not an unbounded prompt history."""
        with self._lock:
            rows = self._conn.execute("SELECT turn_id FROM turns WHERE dialogue_id=?", (dialogue_id,)).fetchall()
        return {str(row["turn_id"]) for row in rows}

    def _save_memory_locked(
        self,
        dialogue_id: str,
        memory: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        if self.get_dialogue(dialogue_id) is None:
            raise KeyError(dialogue_id)
        payload = dict(memory)
        payload["dialogue_id"] = dialogue_id
        now = _utcnow()
        row = self._conn.execute(
            "SELECT version FROM task_memories WHERE dialogue_id=?", (dialogue_id,)
        ).fetchone()
        current = int(row["version"]) if row else 0
        if current != int(expected_version):
            raise MemoryConflict(
                "Task memory changed since it was read; reload and retry.",
                details={"expected_version": int(expected_version), "current_version": current},
            )
        new_version = int(payload.get("version") or current + 1)
        if new_version <= current:
            new_version = current + 1
        payload["version"] = new_version
        payload.setdefault("schema_version", "task-memory-v1")
        if row is None:
            self._conn.execute(
                "INSERT INTO task_memories(dialogue_id, version, memory_json, updated_at)"
                " VALUES(?, ?, ?, ?)",
                (dialogue_id, new_version, json.dumps(payload, ensure_ascii=False), now),
            )
        else:
            self._conn.execute(
                "UPDATE task_memories SET version=?, memory_json=?, updated_at=?"
                " WHERE dialogue_id=?",
                (new_version, json.dumps(payload, ensure_ascii=False), now, dialogue_id),
            )
        return payload

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- helpers ----------------------------------------------------------
    def _dialogue_row(self, dialogue_id: str) -> dict[str, Any]:
        return self.get_dialogue(dialogue_id)  # type: ignore[return-value]


def _dialogue_projection(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "dialogue_id": row["dialogue_id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "turn_count": int(row["turn_count"]),
    }
