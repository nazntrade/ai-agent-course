"""SQLite persistence for chats, messages, turns and app state.

The module is independent from ``agent.py``: it accepts duck-typed config
objects and exposes plain data through a small public API. Each public method
opens a fresh connection so the store is safe to reuse across Streamlit reruns.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from context import SUMMARY_FORMAT_VERSION
from models import LEGACY_MODEL_ALIASES
from stats import TurnStats
from tokens import estimate_tokens

DEFAULT_CHAT_TITLE = "Новый чат"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "chat_history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
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
    summary_cache_hit_tokens  INTEGER,
    summary_cache_miss_tokens INTEGER,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS turns (
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
CREATE INDEX IF NOT EXISTS idx_turns_chat ON turns(chat_id, id);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_chat ON summaries(chat_id);
"""

# Columns added after the Day 7 schema; the migration adds whichever are missing.
# The ``NOT NULL DEFAULT`` columns fill existing rows with the default on
# ``ALTER TABLE``, so no backfill is required and the migration is idempotent.
_NEW_CHAT_COLUMNS = {
    "demo_context_limit": "INTEGER",
    "history_tokens_est": "INTEGER",
    "cost_usd": "REAL",
    "summarize": "INTEGER NOT NULL DEFAULT 1",
    "keep_recent_turns": "INTEGER NOT NULL DEFAULT 3",
    "summary_input_tokens": "INTEGER",
    "summary_output_tokens": "INTEGER",
    "summary_cost_usd": "REAL",
    "summary_cache_hit_tokens": "INTEGER",
    "summary_cache_miss_tokens": "INTEGER",
}


@dataclass
class ChatSummary:
    """Lightweight projection of a chat row for the sidebar list."""

    id: int
    title: str
    updated_at: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    history_tokens_est: int | None = None
    cost_usd: float | None = None
    turns_count: int = 0
    summary_cost_usd: float | None = None


@dataclass
class StoredMessage:
    """A persisted message joined with the statistics of its turn (if any)."""

    role: str
    content: str
    turn: TurnStats | None = None


@dataclass
class ChatStats:
    """Aggregated usage/cost counters for a single chat."""

    input_tokens: int | None
    output_tokens: int | None
    history_tokens_est: int | None
    cost_usd: float | None
    turns_count: int
    summary_input_tokens: int | None = None
    summary_output_tokens: int | None = None
    summary_cost_usd: float | None = None
    summary_cache_hit_tokens: int | None = None
    summary_cache_miss_tokens: int | None = None


@dataclass
class StoredSummary:
    """The current per-chat summary and the tokens of its last generation call."""

    content: str
    covered_messages_count: int
    format_version: int
    prompt_tokens: int | None = None
    response_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_assumption: str | None = None
    updated_at: str | None = None


@dataclass
class TurnRecord:
    """A persisted turn with its creation time and statistics."""

    id: int
    created_at: str
    stats: TurnStats


class ChatStore:
    """SQLite-backed store for chats, messages, turns and last-selected state."""

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
            # Migration: add columns introduced after the Day 7 schema without
            # touching existing chats/messages. Idempotent across repeated opens.
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            for name, decl in _NEW_CHAT_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE chats ADD COLUMN {name} {decl}")
            self._normalize_legacy_models(conn)
            self._backfill_history_tokens_est(conn)
            conn.commit()

    def _normalize_legacy_models(self, conn) -> None:
        """Rewrite exact legacy model IDs stored in existing chats.

        Only known aliases are migrated; any other value (custom or
        provider-specific) is left untouched. The UPDATE is idempotent, so
        reopening the database repeatedly is safe.
        """
        for legacy, canonical in LEGACY_MODEL_ALIASES.items():
            conn.execute(
                "UPDATE chats SET model = ? WHERE model = ?", (canonical, legacy)
            )

    def _backfill_history_tokens_est(self, conn) -> None:
        """Fill history_tokens_est for NULL chats that already have messages."""
        rows = conn.execute(
            "SELECT id FROM chats WHERE history_tokens_est IS NULL"
        ).fetchall()
        for (chat_id,) in rows:
            messages = conn.execute(
                "SELECT content FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchall()
            if not messages:
                continue
            total = sum(estimate_tokens(content) for (content,) in messages)
            conn.execute(
                "UPDATE chats SET history_tokens_est = ? WHERE id = ?",
                (total, chat_id),
            )

    def list_chats(self) -> list[ChatSummary]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.title, c.updated_at,
                       c.input_tokens, c.output_tokens, c.history_tokens_est, c.cost_usd,
                       c.summary_cost_usd,
                       (SELECT COUNT(*) FROM turns t WHERE t.chat_id = c.id) AS turns_count
                FROM chats c
                ORDER BY c.updated_at DESC, c.id DESC
                """
            ).fetchall()
        return [
            ChatSummary(
                id=row[0],
                title=row[1],
                updated_at=row[2],
                input_tokens=row[3],
                output_tokens=row[4],
                history_tokens_est=row[5],
                cost_usd=row[6],
                summary_cost_usd=row[7],
                turns_count=row[8],
            )
            for row in rows
        ]

    def create_chat(self, config) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO chats (title, system_prompt, model, temperature, "
                "max_tokens, stream, demo_context_limit, summarize, keep_recent_turns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    DEFAULT_CHAT_TITLE,
                    config.system_prompt,
                    config.model,
                    config.temperature,
                    config.max_tokens,
                    int(config.stream),
                    getattr(config, "demo_context_limit", None),
                    int(getattr(config, "summarize", True)),
                    int(getattr(config, "keep_recent_turns", 3)),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def load_config(self, chat_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT system_prompt, model, temperature, max_tokens, stream, "
                "demo_context_limit, summarize, keep_recent_turns FROM chats WHERE id = ?",
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
            "demo_context_limit": row[5],
            "summarize": bool(row[6]),
            "keep_recent_turns": row[7],
        }

    def save_config(self, chat_id: int, config) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE chats SET system_prompt = ?, model = ?, temperature = ?, "
                "max_tokens = ?, stream = ?, demo_context_limit = ?, "
                "summarize = ?, keep_recent_turns = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    config.system_prompt,
                    config.model,
                    config.temperature,
                    config.max_tokens,
                    int(config.stream),
                    getattr(config, "demo_context_limit", None),
                    int(getattr(config, "summarize", True)),
                    int(getattr(config, "keep_recent_turns", 3)),
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

    def load_history(self, chat_id: int) -> list[StoredMessage]:
        """Return messages joined with their turn stats, ordered by message id."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT m.role, m.content, t.id,
                       t.user_message_tokens_est, t.request_tokens, t.response_tokens,
                       t.total_tokens, t.prompt_cache_hit_tokens,
                       t.prompt_cache_miss_tokens, t.finish_reason,
                       t.cost_usd, t.cost_assumption
                FROM messages m
                LEFT JOIN turns t
                    ON t.user_message_id = m.id OR t.assistant_message_id = m.id
                WHERE m.chat_id = ?
                ORDER BY m.id
                """,
                (chat_id,),
            ).fetchall()

        messages = []
        for row in rows:
            role, content, turn_id = row[0], row[1], row[2]
            if turn_id is None:
                turn = None
            else:
                turn = TurnStats(
                    user_message_tokens_est=row[3],
                    request_tokens=row[4],
                    response_tokens=row[5],
                    total_tokens=row[6],
                    prompt_cache_hit_tokens=row[7],
                    prompt_cache_miss_tokens=row[8],
                    finish_reason=row[9],
                    cost_usd=row[10],
                    cost_assumption=row[11],
                )
            messages.append(StoredMessage(role=role, content=content, turn=turn))
        return messages

    def list_turns(self, chat_id: int) -> list[TurnRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, user_message_tokens_est, request_tokens,
                       response_tokens, total_tokens, prompt_cache_hit_tokens,
                       prompt_cache_miss_tokens, finish_reason, cost_usd,
                       cost_assumption
                FROM turns WHERE chat_id = ? ORDER BY id
                """,
                (chat_id,),
            ).fetchall()
        return [
            TurnRecord(
                id=row[0],
                created_at=row[1],
                stats=TurnStats(
                    user_message_tokens_est=row[2],
                    request_tokens=row[3],
                    response_tokens=row[4],
                    total_tokens=row[5],
                    prompt_cache_hit_tokens=row[6],
                    prompt_cache_miss_tokens=row[7],
                    finish_reason=row[8],
                    cost_usd=row[9],
                    cost_assumption=row[10],
                ),
            )
            for row in rows
        ]

    def save_turn(
        self,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        stats: TurnStats | None = None,
    ) -> None:
        """Persist one user/assistant exchange atomically and bump chat counters."""
        stats = stats if stats is not None else TurnStats()
        with closing(self._connect()) as conn:
            try:
                user_cursor = conn.execute(
                    "INSERT INTO messages (chat_id, role, content) VALUES (?, 'user', ?)",
                    (chat_id, user_text),
                )
                user_id = user_cursor.lastrowid
                assistant_cursor = conn.execute(
                    "INSERT INTO messages (chat_id, role, content) VALUES (?, 'assistant', ?)",
                    (chat_id, assistant_text),
                )
                assistant_id = assistant_cursor.lastrowid

                conn.execute(
                    "INSERT INTO turns (chat_id, user_message_id, assistant_message_id, "
                    "user_message_tokens_est, request_tokens, response_tokens, total_tokens, "
                    "prompt_cache_hit_tokens, prompt_cache_miss_tokens, finish_reason, "
                    "cost_usd, cost_assumption) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chat_id,
                        user_id,
                        assistant_id,
                        stats.user_message_tokens_est,
                        stats.request_tokens,
                        stats.response_tokens,
                        stats.total_tokens,
                        stats.prompt_cache_hit_tokens,
                        stats.prompt_cache_miss_tokens,
                        stats.finish_reason,
                        stats.cost_usd,
                        stats.cost_assumption,
                    ),
                )

                if stats.request_tokens is not None:
                    conn.execute(
                        "UPDATE chats SET input_tokens = COALESCE(input_tokens, 0) + ? "
                        "WHERE id = ?",
                        (stats.request_tokens, chat_id),
                    )
                if stats.response_tokens is not None:
                    conn.execute(
                        "UPDATE chats SET output_tokens = COALESCE(output_tokens, 0) + ? "
                        "WHERE id = ?",
                        (stats.response_tokens, chat_id),
                    )

                # Estimated unique history: exact response size when available,
                # otherwise the local assistant estimate.
                history_est = stats.user_message_tokens_est or 0
                if stats.response_tokens is not None:
                    history_est += stats.response_tokens
                else:
                    history_est += stats.assistant_tokens_est or 0
                conn.execute(
                    "UPDATE chats SET history_tokens_est = "
                    "COALESCE(history_tokens_est, 0) + ? WHERE id = ?",
                    (history_est, chat_id),
                )

                if stats.cost_usd is not None:
                    conn.execute(
                        "UPDATE chats SET cost_usd = COALESCE(cost_usd, 0) + ? "
                        "WHERE id = ?",
                        (stats.cost_usd, chat_id),
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

    def load_summary(self, chat_id: int) -> StoredSummary | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT content, covered_messages_count, format_version, "
                "prompt_tokens, response_tokens, total_tokens, cost_usd, "
                "cost_assumption, updated_at "
                "FROM summaries WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredSummary(
            content=row[0],
            covered_messages_count=row[1],
            format_version=row[2],
            prompt_tokens=row[3],
            response_tokens=row[4],
            total_tokens=row[5],
            cost_usd=row[6],
            cost_assumption=row[7],
            updated_at=row[8],
        )

    def _accumulate_summary_stats(self, conn, chat_id: int, stats) -> None:
        """Add the known ``summary_*`` counters to a chat row within a transaction.

        ``None`` values are skipped so a missing usage never becomes a fake 0.
        """
        columns = (
            ("summary_input_tokens", stats.request_tokens),
            ("summary_output_tokens", stats.response_tokens),
            ("summary_cache_hit_tokens", stats.prompt_cache_hit_tokens),
            ("summary_cache_miss_tokens", stats.prompt_cache_miss_tokens),
            ("summary_cost_usd", stats.cost_usd),
        )
        for column, value in columns:
            if value is None:
                continue
            conn.execute(
                f"UPDATE chats SET {column} = COALESCE({column}, 0) + ? WHERE id = ?",
                (value, chat_id),
            )

    def save_summary(
        self,
        chat_id: int,
        content: str,
        covered_messages_count: int,
        stats: TurnStats | None = None,
    ) -> None:
        """Upsert the current summary and accumulate its tokens/cost on the chat.

        One transaction: the summary row is replaced (keeping a single current
        row per chat) and the chat's cumulative ``summary_*`` counters are
        bumped. ``stats`` may aggregate several summarisation attempts; only
        this successful update is stored on the ``summaries`` row.
        """
        stats = stats if stats is not None else TurnStats()
        with closing(self._connect()) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO summaries (
                        chat_id, content, covered_messages_count, format_version,
                        prompt_tokens, response_tokens, total_tokens,
                        cost_usd, cost_assumption
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        content = excluded.content,
                        covered_messages_count = excluded.covered_messages_count,
                        format_version = excluded.format_version,
                        prompt_tokens = excluded.prompt_tokens,
                        response_tokens = excluded.response_tokens,
                        total_tokens = excluded.total_tokens,
                        cost_usd = excluded.cost_usd,
                        cost_assumption = excluded.cost_assumption,
                        updated_at = datetime('now')
                    """,
                    (
                        chat_id,
                        content,
                        covered_messages_count,
                        SUMMARY_FORMAT_VERSION,
                        stats.request_tokens,
                        stats.response_tokens,
                        stats.total_tokens,
                        stats.cost_usd,
                        stats.cost_assumption,
                    ),
                )

                self._accumulate_summary_stats(conn, chat_id, stats)

                conn.execute(
                    "UPDATE chats SET updated_at = datetime('now') WHERE id = ?",
                    (chat_id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def record_summary_attempt(self, chat_id: int, stats=None) -> None:
        """Accumulate the cost of failed summarisation attempts.

        Adds the known counters to the chat without touching the ``summaries``
        row or ``chats.updated_at``, so billing an attempt never implies the
        summary was updated.
        """
        stats = stats if stats is not None else TurnStats()
        with closing(self._connect()) as conn:
            try:
                self._accumulate_summary_stats(conn, chat_id, stats)
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

    def get_chat_stats(self, chat_id: int) -> ChatStats:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT input_tokens, output_tokens, history_tokens_est, cost_usd, "
                "summary_input_tokens, summary_output_tokens, summary_cost_usd, "
                "summary_cache_hit_tokens, summary_cache_miss_tokens "
                "FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"chat {chat_id} not found")
            turns_count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0]
        return ChatStats(
            input_tokens=row[0],
            output_tokens=row[1],
            history_tokens_est=row[2],
            cost_usd=row[3],
            turns_count=turns_count,
            summary_input_tokens=row[4],
            summary_output_tokens=row[5],
            summary_cost_usd=row[6],
            summary_cache_hit_tokens=row[7],
            summary_cache_miss_tokens=row[8],
        )

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
