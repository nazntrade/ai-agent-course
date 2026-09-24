"""SQL repository of chats and messages (owned by the backend).

The backend creates, renames and deletes chats and appends messages; the MCP
server only reads a chat to validate a ``chat_id``. The repository enforces no
business limit: ``agent.chats.ChatService`` checks ``MAX_CHATS`` and the title
rules before it calls here.
"""

from __future__ import annotations

import time
import uuid

from storage.db import Database


def new_chat_id() -> str:
    """Return a fresh opaque chat identifier."""
    return uuid.uuid4().hex


def _chat_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


class ChatRepository:
    """Reads and writes ``chats`` and ``messages`` rows."""

    def __init__(self, database: Database):
        self._db = database

    def count_chats(self) -> int:
        with self._db.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM chats").fetchone()
        return int(row["total"]) if row is not None else 0

    def create_chat(
        self, title: str, *, now: float | None = None, chat_id: str | None = None
    ) -> dict:
        moment = float(now if now is not None else time.time())
        identifier = str(chat_id) if chat_id else new_chat_id()
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (identifier, str(title), moment, moment),
            )
        return {
            "id": identifier,
            "title": str(title),
            "created_at": moment,
            "updated_at": moment,
        }

    def get_chat(self, chat_id: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM chats WHERE id = ?", (str(chat_id),)
            ).fetchone()
        return _chat_dict(row) if row is not None else None

    def list_chats(self) -> list:
        """Return chats, most recently updated first."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM chats ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [_chat_dict(row) for row in rows]

    def rename_chat(
        self, chat_id: str, title: str, *, now: float | None = None
    ) -> dict | None:
        moment = float(now if now is not None else time.time())
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
                (str(title), moment, str(chat_id)),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_chat(chat_id)

    def touch_chat(self, chat_id: str, *, now: float | None = None) -> None:
        moment = float(now if now is not None else time.time())
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (moment, str(chat_id)),
            )

    def delete_chat(self, chat_id: str) -> bool:
        """Delete a chat; foreign keys cascade to messages, tasks and runs."""
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM chats WHERE id = ?", (str(chat_id),)
            )
            return cursor.rowcount == 1

    def append_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        now: float | None = None,
    ) -> None:
        moment = float(now if now is not None else time.time())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO messages(chat_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(chat_id), str(role), str(content), moment),
            )

    def add_exchange(
        self,
        chat_id: str,
        user_content: str,
        assistant_content: str,
        *,
        now: float | None = None,
    ) -> None:
        """Append one user turn and one assistant turn in a single transaction."""
        moment = float(now if now is not None else time.time())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO messages(chat_id, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (str(chat_id), str(user_content), moment),
            )
            connection.execute(
                "INSERT INTO messages(chat_id, role, content, created_at) "
                "VALUES (?, 'assistant', ?, ?)",
                (str(chat_id), str(assistant_content), moment),
            )
            connection.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (moment, str(chat_id)),
            )

    def list_messages(self, chat_id: str) -> list:
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY id",
                (str(chat_id),),
            ).fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def recent_messages(self, chat_id: str, limit: int) -> list:
        """Return the last ``limit`` messages in chronological order."""
        count = max(int(limit), 0)
        if count == 0:
            return []
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (str(chat_id), count),
            ).fetchall()
        messages = [
            {"role": row["role"], "content": row["content"]}
            for row in rows
        ]
        messages.reverse()
        return messages

    def message_count(self, chat_id: str) -> int:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def clear_messages(self, chat_id: str) -> int:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE chat_id = ?", (str(chat_id),)
            )
            return int(cursor.rowcount)
