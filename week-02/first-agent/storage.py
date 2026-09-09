"""SQLite persistence for chats, messages and app state.

The module is independent from ``agent.py``: it accepts duck-typed config
objects and exposes plain data through a small public API. Each public method
opens a fresh connection so the store is safe to reuse across Streamlit reruns.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHAT_TITLE = "Новый чат"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "chat_history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
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

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class ChatSummary:
    """Lightweight projection of a chat row for the sidebar list."""

    id: int
    title: str
    updated_at: str


class ChatStore:
    """SQLite-backed store for chats, messages and last-selected state."""

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def list_chats(self) -> list[ChatSummary]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, title, updated_at FROM chats ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [ChatSummary(id=row[0], title=row[1], updated_at=row[2]) for row in rows]

    def create_chat(self, config) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO chats (title, system_prompt, model, temperature, max_tokens, stream) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    DEFAULT_CHAT_TITLE,
                    config.system_prompt,
                    config.model,
                    config.temperature,
                    config.max_tokens,
                    int(config.stream),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def load_config(self, chat_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT system_prompt, model, temperature, max_tokens, stream "
                "FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"chat {chat_id} not found")
        return {
            "system_prompt": row[0],
            "model": row[1],
            "temperature": row[2],
            "max_tokens": row[3],
            "stream": bool(row[4]),
        }

    def save_config(self, chat_id: int, config) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE chats SET system_prompt = ?, model = ?, temperature = ?, "
                "max_tokens = ?, stream = ?, updated_at = datetime('now') WHERE id = ?",
                (
                    config.system_prompt,
                    config.model,
                    config.temperature,
                    config.max_tokens,
                    int(config.stream),
                    chat_id,
                ),
            )
            conn.commit()

    def load_messages(self, chat_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

    def save_turn(
        self,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Persist one user/assistant exchange atomically and bump usage/title."""
        with closing(self._connect()) as conn:
            try:
                conn.execute(
                    "INSERT INTO messages (chat_id, role, content) VALUES (?, 'user', ?)",
                    (chat_id, user_text),
                )
                conn.execute(
                    "INSERT INTO messages (chat_id, role, content) VALUES (?, 'assistant', ?)",
                    (chat_id, assistant_text),
                )

                if input_tokens is not None:
                    conn.execute(
                        "UPDATE chats SET input_tokens = COALESCE(input_tokens, 0) + ? WHERE id = ?",
                        (input_tokens, chat_id),
                    )
                if output_tokens is not None:
                    conn.execute(
                        "UPDATE chats SET output_tokens = COALESCE(output_tokens, 0) + ? WHERE id = ?",
                        (output_tokens, chat_id),
                    )

                conn.execute(
                    "UPDATE chats SET updated_at = datetime('now') WHERE id = ?",
                    (chat_id,),
                )

                # Derive the title from the first user message, but only once.
                row = conn.execute(
                    "SELECT title FROM chats WHERE id = ?", (chat_id,)
                ).fetchone()
                if row is not None and row[0] == DEFAULT_CHAT_TITLE:
                    first_line = user_text.strip().splitlines()[0]
                    title = first_line[:40]
                    if len(first_line) > 40:
                        title += "…"
                    conn.execute(
                        "UPDATE chats SET title = ? WHERE id = ?", (title, chat_id)
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def delete_chat(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            conn.execute(
                "DELETE FROM app_state WHERE key = 'last_chat_id' AND value = ?",
                (str(chat_id),),
            )
            conn.commit()

    def get_usage(self, chat_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT input_tokens, output_tokens FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"chat {chat_id} not found")
        return {"input_tokens": row[0], "output_tokens": row[1]}

    def get_last_selected_id(self) -> int | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = 'last_chat_id'"
            ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def set_last_selected(self, chat_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (key, value) VALUES ('last_chat_id', ?)",
                (str(chat_id),),
            )
            conn.commit()
