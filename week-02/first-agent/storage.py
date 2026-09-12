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
from facts import FACT_STATUS_CANCELLED, Fact
from models import LEGACY_MODEL_ALIASES
from stats import TurnStats
from strategies import (
    STRATEGY_FULL,
    STRATEGY_SUMMARY,
    normalize_strategy,
    normalize_window,
)
from tokens import estimate_tokens

DEFAULT_CHAT_TITLE = "Новый чат"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "chat_history.db"


class DuplicateBranchNameError(ValueError):
    """Raised when a branch name already exists in the chat.

    Names are compared after whitespace collapsing and case folding, so the
    ``name`` attribute keeps the cleaned name the caller tried to insert.
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Branch name already exists: {name}")


class BranchHasChildrenError(ValueError):
    """Raised when a branch that still has children is deleted directly.

    ``children`` lists the direct children so the UI can tell the user which
    branches to remove first; the rejected delete changes nothing.
    """

    def __init__(self, branch: "Branch", children: list["Branch"]):
        self.branch = branch
        self.children = children
        super().__init__("Branch has child branches")

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

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    category   TEXT    NOT NULL DEFAULT 'other',
    fact_key   TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active',
    reason     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_facts_chat ON facts(chat_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_active_key
    ON facts(chat_id, fact_key) WHERE status = 'active';

-- A checkpoint is a completed assistant turn (``fork_message_id``) from which
-- new messages continue on a child line. Deleting a parent branch cascades to
-- its children so deleting a chat can never leave a dangling parent reference.
CREATE TABLE IF NOT EXISTS branches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    name             TEXT    NOT NULL,
    parent_branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
    fork_message_id  INTEGER NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_branches_chat ON branches(chat_id, id);
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
    # Day 10: context strategy and its per-strategy state.
    "context_strategy": "TEXT",
    "sliding_window_messages": "INTEGER NOT NULL DEFAULT 6",
    "facts_window_messages": "INTEGER NOT NULL DEFAULT 6",
    "facts_anchor_message_id": "INTEGER",
    "active_branch_id": "INTEGER",
    "facts_ok_input_tokens": "INTEGER",
    "facts_ok_output_tokens": "INTEGER",
    "facts_ok_cache_hit_tokens": "INTEGER",
    "facts_ok_cache_miss_tokens": "INTEGER",
    "facts_ok_cost_usd": "REAL",
    "facts_fail_input_tokens": "INTEGER",
    "facts_fail_output_tokens": "INTEGER",
    "facts_fail_cache_hit_tokens": "INTEGER",
    "facts_fail_cache_miss_tokens": "INTEGER",
    "facts_fail_cost_usd": "REAL",
    "last_context_tokens": "INTEGER",
}

# Columns added after the original ``messages``/``summaries`` tables. They stay
# nullable so existing rows keep reading as the main line (NULL = main).
_NEW_MESSAGE_COLUMNS = {"branch_id": "INTEGER"}
_NEW_SUMMARY_COLUMNS = {"branch_id": "INTEGER"}


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
    context_strategy: str | None = None
    last_context_tokens: int | None = None
    facts_cost_usd: float | None = None


@dataclass
class StoredMessage:
    """A persisted message joined with the statistics of its turn (if any)."""

    role: str
    content: str
    turn: TurnStats | None = None
    id: int | None = None


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
    facts_ok_input_tokens: int | None = None
    facts_ok_output_tokens: int | None = None
    facts_ok_cache_hit_tokens: int | None = None
    facts_ok_cache_miss_tokens: int | None = None
    facts_ok_cost_usd: float | None = None
    facts_fail_input_tokens: int | None = None
    facts_fail_output_tokens: int | None = None
    facts_fail_cache_hit_tokens: int | None = None
    facts_fail_cache_miss_tokens: int | None = None
    facts_fail_cost_usd: float | None = None
    facts_anchor_message_id: int | None = None
    last_context_tokens: int | None = None
    context_strategy: str | None = None


@dataclass
class Branch:
    """A persistent line of messages forked from a completed turn."""

    id: int
    chat_id: int
    name: str
    parent_branch_id: int | None
    fork_message_id: int
    created_at: str | None = None


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
    branch_id: int | None = None


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
            # Migration: add columns introduced after the original schema
            # without touching existing rows. Idempotent across repeated opens.
            self._add_columns(conn, "chats", _NEW_CHAT_COLUMNS)
            self._add_columns(conn, "messages", _NEW_MESSAGE_COLUMNS)
            self._add_columns(conn, "summaries", _NEW_SUMMARY_COLUMNS)
            self._migrate_summaries_indexes(conn)
            self._normalize_legacy_models(conn)
            self._backfill_context_strategy(conn)
            self._backfill_history_tokens_est(conn)
            conn.commit()

    @staticmethod
    def _migrate_summaries_indexes(conn) -> None:
        """Replace the legacy per-chat index with per-line partial indexes.

        Memory is bound to a line: ``NULL`` is the main line, a branch id
        otherwise. The legacy unique index on ``chat_id`` alone could not
        express that, so it is dropped and covered by two partial indexes.
        """
        conn.execute("DROP INDEX IF EXISTS idx_summaries_chat")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_main "
            "ON summaries(chat_id) WHERE branch_id IS NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_branch "
            "ON summaries(chat_id, branch_id) WHERE branch_id IS NOT NULL"
        )

    @staticmethod
    def _add_columns(conn, table: str, columns: dict) -> None:
        existing = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _backfill_context_strategy(self, conn) -> None:
        """Derive the strategy for rows written before Day 10.

        Only NULL rows are filled, so a strategy set manually or by a newer
        version is never overwritten and reopening the database is a no-op.
        """
        conn.execute(
            "UPDATE chats SET context_strategy = "
            "CASE WHEN summarize = 0 THEN 'full' ELSE 'summary' END "
            "WHERE context_strategy IS NULL"
        )

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
                       c.summary_cost_usd, c.context_strategy, c.last_context_tokens,
                       c.facts_ok_cost_usd, c.facts_fail_cost_usd,
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
                context_strategy=normalize_strategy(row[8]),
                last_context_tokens=row[9],
                facts_cost_usd=self._sum_known(row[10], row[11]),
                turns_count=row[12],
            )
            for row in rows
        ]

    @staticmethod
    def _sum_known(*values):
        """Sum known values, or return ``None`` when all are ``None``."""
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    @staticmethod
    def _strategy_from_config(config) -> str:
        """Resolve the effective strategy from a duck-typed config object.

        A ``context_strategy`` value wins; when it is missing (older config
        object) the legacy ``summarize`` flag maps onto full/summary.
        """
        strategy = getattr(config, "context_strategy", None)
        if strategy is None:
            strategy = STRATEGY_SUMMARY if getattr(config, "summarize", True) else STRATEGY_FULL
        return normalize_strategy(strategy)

    def create_chat(self, config) -> int:
        strategy = self._strategy_from_config(config)
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO chats (title, system_prompt, model, temperature, "
                "max_tokens, stream, demo_context_limit, summarize, keep_recent_turns, "
                "context_strategy, sliding_window_messages, facts_window_messages) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    DEFAULT_CHAT_TITLE,
                    config.system_prompt,
                    config.model,
                    config.temperature,
                    config.max_tokens,
                    int(config.stream),
                    getattr(config, "demo_context_limit", None),
                    int(strategy == STRATEGY_SUMMARY),
                    int(getattr(config, "keep_recent_turns", 3)),
                    strategy,
                    normalize_window(getattr(config, "sliding_window_messages", None), 6),
                    normalize_window(getattr(config, "facts_window_messages", None), 6),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def load_config(self, chat_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT system_prompt, model, temperature, max_tokens, stream, "
                "demo_context_limit, summarize, keep_recent_turns, context_strategy, "
                "sliding_window_messages, facts_window_messages "
                "FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"chat {chat_id} not found")
        strategy = row[8]
        if strategy is None:
            strategy = STRATEGY_SUMMARY if bool(row[6]) else STRATEGY_FULL
        strategy = normalize_strategy(strategy)
        return {
            "system_prompt": row[0],
            "model": row[1],
            "temperature": row[2],
            "max_tokens": row[3],
            "stream": bool(row[4]),
            "demo_context_limit": row[5],
            "summarize": strategy == STRATEGY_SUMMARY,
            "keep_recent_turns": row[7],
            "context_strategy": strategy,
            "sliding_window_messages": normalize_window(row[9], 6),
            "facts_window_messages": normalize_window(row[10], 6),
        }

    def save_config(self, chat_id: int, config) -> None:
        strategy = self._strategy_from_config(config)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE chats SET system_prompt = ?, model = ?, temperature = ?, "
                "max_tokens = ?, stream = ?, demo_context_limit = ?, "
                "summarize = ?, keep_recent_turns = ?, context_strategy = ?, "
                "sliding_window_messages = ?, facts_window_messages = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    config.system_prompt,
                    config.model,
                    config.temperature,
                    config.max_tokens,
                    int(config.stream),
                    getattr(config, "demo_context_limit", None),
                    int(strategy == STRATEGY_SUMMARY),
                    int(getattr(config, "keep_recent_turns", 3)),
                    strategy,
                    normalize_window(getattr(config, "sliding_window_messages", None), 6),
                    normalize_window(getattr(config, "facts_window_messages", None), 6),
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

    _HISTORY_SELECT = """
        SELECT m.id, m.role, m.content, t.id,
               t.user_message_tokens_est, t.request_tokens, t.response_tokens,
               t.total_tokens, t.prompt_cache_hit_tokens,
               t.prompt_cache_miss_tokens, t.finish_reason,
               t.cost_usd, t.cost_assumption
        FROM messages m
        LEFT JOIN turns t
            ON t.user_message_id = m.id OR t.assistant_message_id = m.id
        WHERE m.chat_id = ?
        ORDER BY m.id
    """

    @staticmethod
    def _stored_messages(rows) -> list[StoredMessage]:
        messages = []
        for row in rows:
            turn_id = row[3]
            turn = None
            if turn_id is not None:
                turn = TurnStats(
                    user_message_tokens_est=row[4],
                    request_tokens=row[5],
                    response_tokens=row[6],
                    total_tokens=row[7],
                    prompt_cache_hit_tokens=row[8],
                    prompt_cache_miss_tokens=row[9],
                    finish_reason=row[10],
                    cost_usd=row[11],
                    cost_assumption=row[12],
                )
            messages.append(
                StoredMessage(role=row[1], content=row[2], turn=turn, id=row[0])
            )
        return messages

    def load_history(self, chat_id: int) -> list[StoredMessage]:
        """Return all messages joined with their turn stats, ordered by id."""
        with closing(self._connect()) as conn:
            rows = conn.execute(self._HISTORY_SELECT, (chat_id,)).fetchall()
        return self._stored_messages(rows)

    def _branch_rows(self, conn, chat_id: int) -> dict:
        rows = conn.execute(
            "SELECT id, chat_id, name, parent_branch_id, fork_message_id, created_at "
            "FROM branches WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
        return {row[0]: row for row in rows}

    @staticmethod
    def _branch_from_row(row) -> Branch:
        """Build a ``Branch`` from a six-column branches row."""
        return Branch(
            id=row[0],
            chat_id=row[1],
            name=row[2],
            parent_branch_id=row[3],
            fork_message_id=row[4],
            created_at=row[5],
        )

    def _branch_chain(self, conn, chat_id: int, target_branch_id) -> list:
        """Return the main-first chain of branch rows ending at the target.

        The first element is always ``None`` (the main line). A cycle or a
        missing parent means the data is corrupt, so the chain falls back to the
        main line instead of looping or raising.
        """
        if target_branch_id is None:
            return [None]
        branches = self._branch_rows(conn, chat_id)
        chain = []
        seen = set()
        current = target_branch_id
        while current is not None:
            if current in seen:
                return [None]
            seen.add(current)
            branch = branches.get(current)
            if branch is None:
                return [None]
            chain.append(branch)
            current = branch[3]
        chain.reverse()
        return [None] + chain

    def _line_ids_for_branch(self, conn, chat_id: int, branch_id) -> list[int]:
        """Return the message ids on the line ending at ``branch_id``.

        Each segment is bounded by the fork message of the next branch, so the
        shared prefix is included exactly once and sibling branches never leak
        into each other.
        """
        chain = self._branch_chain(conn, chat_id, branch_id)
        ids: list[int] = []
        for index, branch in enumerate(chain):
            next_branch = chain[index + 1] if index + 1 < len(chain) else None
            upper = next_branch[4] if next_branch is not None else None
            if branch is None:
                sql = "SELECT id FROM messages WHERE chat_id = ? AND branch_id IS NULL"
                params = [chat_id]
            else:
                sql = "SELECT id FROM messages WHERE chat_id = ? AND branch_id = ?"
                params = [chat_id, branch[0]]
            if upper is not None:
                sql += " AND id <= ?"
                params.append(upper)
            ids.extend(row[0] for row in conn.execute(sql, params).fetchall())
        return sorted(ids)

    def _active_line_ids(self, conn, chat_id: int) -> list[int]:
        row = conn.execute(
            "SELECT active_branch_id FROM chats WHERE id = ?", (chat_id,)
        ).fetchone()
        target = row[0] if row is not None else None
        return self._line_ids_for_branch(conn, chat_id, target)

    def load_branch_line(self, chat_id: int) -> list[StoredMessage]:
        """Return the active line's messages (id filled, no turn stats)."""
        with closing(self._connect()) as conn:
            ids = self._active_line_ids(conn, chat_id)
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, role, content FROM messages "
                f"WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
        return [StoredMessage(role=row[1], content=row[2], id=row[0]) for row in rows]

    def load_branch_history(self, chat_id: int) -> list[StoredMessage]:
        """Return the active line's messages joined with their turn stats."""
        with closing(self._connect()) as conn:
            line_ids = set(self._active_line_ids(conn, chat_id))
            rows = conn.execute(self._HISTORY_SELECT, (chat_id,)).fetchall()
        return self._stored_messages([row for row in rows if row[0] in line_ids])

    def load_line_messages_after(
        self, chat_id: int, after_message_id: int
    ) -> list[StoredMessage]:
        """Return active-line messages with an id greater than the anchor."""
        with closing(self._connect()) as conn:
            ids = [
                message_id
                for message_id in self._active_line_ids(conn, chat_id)
                if message_id > after_message_id
            ]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, role, content FROM messages "
                f"WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
        return [StoredMessage(role=row[1], content=row[2], id=row[0]) for row in rows]

    def load_line_checkpoints(
        self, chat_id: int, branch_id: int | None = None
    ) -> list[StoredMessage]:
        """Return completed assistant turns on a line, ordered by message id.

        ``branch_id`` selects the line ending at that branch; ``None`` means the
        main line. Only assistant messages carrying a ``turns`` row are
        checkpoints, because a branch can only fork from a completed turn. A
        branch id that is not part of the chat raises ``ValueError``; an unknown
        chat returns an empty list.
        """
        with closing(self._connect()) as conn:
            if branch_id is not None:
                owned = conn.execute(
                    "SELECT 1 FROM branches WHERE id = ? AND chat_id = ?",
                    (branch_id, chat_id),
                ).fetchone()
                if owned is None:
                    raise ValueError("Branch does not belong to this chat")
            line_ids = set(self._line_ids_for_branch(conn, chat_id, branch_id))
            rows = conn.execute(self._HISTORY_SELECT, (chat_id,)).fetchall()
        return [
            message
            for message in self._stored_messages(
                [row for row in rows if row[0] in line_ids]
            )
            if message.role == "assistant"
            and message.turn is not None
            and message.id is not None
        ]

    def list_branches(self, chat_id: int) -> list[Branch]:
        with closing(self._connect()) as conn:
            rows = self._branch_rows(conn, chat_id).values()
        return [self._branch_from_row(row) for row in rows]

    def get_active_branch(self, chat_id: int) -> Branch | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT active_branch_id FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return None
            branch = conn.execute(
                "SELECT id, chat_id, name, parent_branch_id, fork_message_id, created_at "
                "FROM branches WHERE id = ? AND chat_id = ?",
                (row[0], chat_id),
            ).fetchone()
        if branch is None:
            return None
        return self._branch_from_row(branch)

    def set_active_branch(self, chat_id: int, branch_id: int | None) -> None:
        """Switch the active line; ``None`` selects the main line."""
        with closing(self._connect()) as conn:
            if branch_id is not None:
                owned = conn.execute(
                    "SELECT 1 FROM branches WHERE id = ? AND chat_id = ?",
                    (branch_id, chat_id),
                ).fetchone()
                if owned is None:
                    raise ValueError("Branch does not belong to this chat")
            conn.execute(
                "UPDATE chats SET active_branch_id = ? WHERE id = ?",
                (branch_id, chat_id),
            )
            conn.commit()

    @staticmethod
    def _normalize_branch_name(name) -> str:
        """Fold a branch name for comparison: spaces collapsed, case-insensitive."""
        return " ".join((name or "").split()).casefold()

    def create_branch(
        self,
        chat_id: int,
        name: str,
        parent_branch_id: int | None = None,
        fork_message_id: int | None = None,
    ) -> int:
        """Create a child line forked from a completed assistant turn.

        The checkpoint must be an assistant message of this chat that has a
        ``turns`` row and lies on the parent line; otherwise the branch would
        share a prefix that never existed and ``ValueError`` is raised. A name
        that normalizes to an existing branch name raises
        ``DuplicateBranchNameError`` and nothing is inserted.
        """
        if not name or not name.strip():
            raise ValueError("Branch name must not be empty")
        if fork_message_id is None:
            raise ValueError("A checkpoint (fork message) is required")
        with closing(self._connect()) as conn:
            if parent_branch_id is not None:
                parent = conn.execute(
                    "SELECT 1 FROM branches WHERE id = ? AND chat_id = ?",
                    (parent_branch_id, chat_id),
                ).fetchone()
                if parent is None:
                    raise ValueError("Parent branch does not belong to this chat")
            message = conn.execute(
                "SELECT role FROM messages WHERE id = ? AND chat_id = ?",
                (fork_message_id, chat_id),
            ).fetchone()
            if message is None or message[0] != "assistant":
                raise ValueError("Checkpoint must be an assistant message of this chat")
            turn = conn.execute(
                "SELECT 1 FROM turns WHERE assistant_message_id = ?",
                (fork_message_id,),
            ).fetchone()
            if turn is None:
                raise ValueError("Checkpoint must be a completed turn")
            line_ids = set(
                self._line_ids_for_branch(conn, chat_id, parent_branch_id)
            )
            if fork_message_id not in line_ids:
                raise ValueError("Checkpoint is not on the parent line")
            cleaned = name.strip()
            normalized = self._normalize_branch_name(cleaned)
            existing_names = conn.execute(
                "SELECT name FROM branches WHERE chat_id = ?", (chat_id,)
            ).fetchall()
            for (existing_name,) in existing_names:
                if self._normalize_branch_name(existing_name) == normalized:
                    raise DuplicateBranchNameError(cleaned)
            cursor = conn.execute(
                "INSERT INTO branches (chat_id, name, parent_branch_id, fork_message_id) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, cleaned, parent_branch_id, fork_message_id),
            )
            conn.commit()
            return cursor.lastrowid

    def delete_branch(self, chat_id: int, branch_id: int) -> None:
        """Delete one branch and only its own messages, turns and summary.

        The shared prefix (owned by ancestors or the main line), sibling
        branches and the chat counters stay untouched. A branch that still has
        children is rejected with ``BranchHasChildrenError`` instead of
        cascading, so descendants are always removed explicitly. When the
        deleted branch is active, its parent line becomes active (or the main
        line for a root branch). Everything runs in one transaction and rolls
        back on any error.
        """
        with closing(self._connect()) as conn:
            try:
                branch = conn.execute(
                    "SELECT id, chat_id, name, parent_branch_id, fork_message_id, "
                    "created_at FROM branches WHERE id = ? AND chat_id = ?",
                    (branch_id, chat_id),
                ).fetchone()
                if branch is None:
                    raise ValueError("Branch does not belong to this chat")

                children = conn.execute(
                    "SELECT id, chat_id, name, parent_branch_id, fork_message_id, "
                    "created_at FROM branches "
                    "WHERE chat_id = ? AND parent_branch_id = ? ORDER BY id",
                    (chat_id, branch_id),
                ).fetchall()
                if children:
                    raise BranchHasChildrenError(
                        self._branch_from_row(branch),
                        [self._branch_from_row(row) for row in children],
                    )

                message_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM messages WHERE chat_id = ? AND branch_id = ?",
                        (chat_id, branch_id),
                    ).fetchall()
                ]

                if message_ids:
                    placeholders = ",".join("?" for _ in message_ids)
                    # A fork always points at an ancestor's message, so this is
                    # a corruption guard rather than a normal case: refuse to
                    # break a branch whose checkpoint would disappear.
                    foreign_fork = conn.execute(
                        f"SELECT id FROM branches WHERE chat_id = ? AND id != ? "
                        f"AND fork_message_id IN ({placeholders})",
                        (chat_id, branch_id, *message_ids),
                    ).fetchone()
                    if foreign_fork is not None:
                        raise ValueError(
                            "Branch cannot be deleted: another branch forks "
                            "from its messages"
                        )
                    conn.execute(
                        f"DELETE FROM turns WHERE user_message_id IN ({placeholders}) "
                        f"OR assistant_message_id IN ({placeholders})",
                        (*message_ids, *message_ids),
                    )
                    conn.execute(
                        "DELETE FROM messages WHERE chat_id = ? AND branch_id = ?",
                        (chat_id, branch_id),
                    )

                conn.execute(
                    "DELETE FROM summaries WHERE chat_id = ? AND branch_id = ?",
                    (chat_id, branch_id),
                )

                active = conn.execute(
                    "SELECT active_branch_id FROM chats WHERE id = ?", (chat_id,)
                ).fetchone()
                if active is not None and active[0] == branch_id:
                    parent_id = branch[3]
                    if parent_id is not None:
                        parent_exists = conn.execute(
                            "SELECT 1 FROM branches WHERE id = ? AND chat_id = ?",
                            (parent_id, chat_id),
                        ).fetchone()
                        if parent_exists is None:
                            parent_id = None
                    conn.execute(
                        "UPDATE chats SET active_branch_id = ? WHERE id = ?",
                        (parent_id, chat_id),
                    )

                conn.execute(
                    "DELETE FROM branches WHERE id = ? AND chat_id = ?",
                    (branch_id, chat_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def load_facts(self, chat_id: int, include_inactive: bool = False) -> list[Fact]:
        with closing(self._connect()) as conn:
            if include_inactive:
                rows = conn.execute(
                    "SELECT id, category, fact_key, value, status, reason, updated_at "
                    "FROM facts WHERE chat_id = ? ORDER BY id",
                    (chat_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, category, fact_key, value, status, reason, updated_at "
                    "FROM facts WHERE chat_id = ? AND status = 'active' ORDER BY id",
                    (chat_id,),
                ).fetchall()
        return [
            Fact(
                id=row[0],
                category=row[1],
                key=row[2],
                value=row[3],
                status=row[4],
                reason=row[5],
                updated_at=row[6],
            )
            for row in rows
        ]

    def get_facts_anchor(self, chat_id: int) -> int | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT facts_anchor_message_id FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"chat {chat_id} not found")
        return row[0]

    def save_facts(self, chat_id: int, operations, anchor_message_id) -> None:
        """Apply fact operations and advance the coverage anchor atomically.

        Operations are merged one by one in order: a repeated active value is a
        no-op, a changed value marks the old row ``replaced`` and inserts a new
        active row, and a cancellation marks the active row ``cancelled``. Any
        failure rolls the whole batch back, leaving facts and anchor unchanged.
        """
        with closing(self._connect()) as conn:
            try:
                for operation in operations:
                    key = (operation.key or "").strip()
                    if not key:
                        raise ValueError("Fact key must not be empty")
                    active = conn.execute(
                        "SELECT id, value FROM facts "
                        "WHERE chat_id = ? AND fact_key = ? AND status = 'active'",
                        (chat_id, key),
                    ).fetchone()
                    if operation.status == FACT_STATUS_CANCELLED:
                        if active is not None:
                            conn.execute(
                                "UPDATE facts SET status = 'cancelled', reason = ?, "
                                "updated_at = datetime('now') WHERE id = ?",
                                (operation.reason, active[0]),
                            )
                        continue
                    value = operation.value or ""
                    if active is None:
                        conn.execute(
                            "INSERT INTO facts (chat_id, category, fact_key, value, status) "
                            "VALUES (?, ?, ?, ?, 'active')",
                            (chat_id, operation.category or "other", key, value),
                        )
                    elif active[1] != value:
                        conn.execute(
                            "UPDATE facts SET status = 'replaced', reason = ?, "
                            "updated_at = datetime('now') WHERE id = ?",
                            ("заменено новым значением", active[0]),
                        )
                        conn.execute(
                            "INSERT INTO facts (chat_id, category, fact_key, value, status) "
                            "VALUES (?, ?, ?, ?, 'active')",
                            (chat_id, operation.category or "other", key, value),
                        )
                if anchor_message_id is not None:
                    conn.execute(
                        "UPDATE chats SET facts_anchor_message_id = "
                        "MAX(COALESCE(facts_anchor_message_id, 0), ?) WHERE id = ?",
                        (anchor_message_id, chat_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _accumulate_facts_stats(self, conn, chat_id: int, stats, failed: bool) -> None:
        """Add one call's usage to the ok/fail facts bucket (None is skipped)."""
        prefix = "facts_fail" if failed else "facts_ok"
        columns = (
            (f"{prefix}_input_tokens", stats.request_tokens),
            (f"{prefix}_output_tokens", stats.response_tokens),
            (f"{prefix}_cache_hit_tokens", stats.prompt_cache_hit_tokens),
            (f"{prefix}_cache_miss_tokens", stats.prompt_cache_miss_tokens),
            (f"{prefix}_cost_usd", stats.cost_usd),
        )
        for column, value in columns:
            if value is None:
                continue
            conn.execute(
                f"UPDATE chats SET {column} = COALESCE({column}, 0) + ? WHERE id = ?",
                (value, chat_id),
            )

    def record_facts_attempt(self, chat_id: int, stats=None, failed: bool = False) -> None:
        """Accumulate a facts call's cost without touching facts or the anchor."""
        stats = stats if stats is not None else TurnStats()
        with closing(self._connect()) as conn:
            try:
                self._accumulate_facts_stats(conn, chat_id, stats, failed)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def set_last_context_tokens(self, chat_id: int, tokens: int | None) -> None:
        """Record the size of the last main payload for the statistics panel."""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE chats SET last_context_tokens = ? WHERE id = ?",
                (tokens, chat_id),
            )
            conn.commit()

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
        branch_id: int | None = None,
    ) -> None:
        """Persist one user/assistant exchange atomically and bump chat counters.

        ``branch_id`` tags both messages with the line they belong to; ``None``
        keeps them on the main line. The checkpoint validity of a branch is
        established by its assistant message and turn row, so nothing else is
        needed here.
        """
        stats = stats if stats is not None else TurnStats()
        with closing(self._connect()) as conn:
            try:
                user_cursor = conn.execute(
                    "INSERT INTO messages (chat_id, role, content, branch_id) "
                    "VALUES (?, 'user', ?, ?)",
                    (chat_id, user_text, branch_id),
                )
                user_id = user_cursor.lastrowid
                assistant_cursor = conn.execute(
                    "INSERT INTO messages (chat_id, role, content, branch_id) "
                    "VALUES (?, 'assistant', ?, ?)",
                    (chat_id, assistant_text, branch_id),
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

    def load_summary(self, chat_id: int, branch_id: int | None = None) -> StoredSummary | None:
        """Return the summary for a chat line; ``None`` is the main line."""
        with closing(self._connect()) as conn:
            if branch_id is None:
                row = conn.execute(
                    "SELECT content, covered_messages_count, format_version, "
                    "prompt_tokens, response_tokens, total_tokens, cost_usd, "
                    "cost_assumption, updated_at, branch_id "
                    "FROM summaries WHERE chat_id = ? AND branch_id IS NULL",
                    (chat_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT content, covered_messages_count, format_version, "
                    "prompt_tokens, response_tokens, total_tokens, cost_usd, "
                    "cost_assumption, updated_at, branch_id "
                    "FROM summaries WHERE chat_id = ? AND branch_id = ?",
                    (chat_id, branch_id),
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
            branch_id=row[9],
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
        branch_id: int | None = None,
    ) -> None:
        """Upsert the current summary for one line and accumulate its stats.

        One transaction: the summary row for ``(chat_id, branch_id)`` is
        replaced (keeping a single current row per line) and the chat's
        cumulative ``summary_*`` counters are bumped. ``stats`` may aggregate
        several attempts; only this successful update is stored on the row.
        """
        stats = stats if stats is not None else TurnStats()
        with closing(self._connect()) as conn:
            try:
                if branch_id is None:
                    existing = conn.execute(
                        "SELECT id FROM summaries WHERE chat_id = ? AND branch_id IS NULL",
                        (chat_id,),
                    ).fetchone()
                else:
                    existing = conn.execute(
                        "SELECT id FROM summaries WHERE chat_id = ? AND branch_id = ?",
                        (chat_id, branch_id),
                    ).fetchone()

                values = (
                    content,
                    covered_messages_count,
                    SUMMARY_FORMAT_VERSION,
                    stats.request_tokens,
                    stats.response_tokens,
                    stats.total_tokens,
                    stats.cost_usd,
                    stats.cost_assumption,
                )
                if existing is None:
                    conn.execute(
                        "INSERT INTO summaries (chat_id, content, covered_messages_count, "
                        "format_version, prompt_tokens, response_tokens, total_tokens, "
                        "cost_usd, cost_assumption, branch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (chat_id, *values, branch_id),
                    )
                else:
                    conn.execute(
                        "UPDATE summaries SET content = ?, covered_messages_count = ?, "
                        "format_version = ?, prompt_tokens = ?, response_tokens = ?, "
                        "total_tokens = ?, cost_usd = ?, cost_assumption = ?, "
                        "updated_at = datetime('now') WHERE id = ?",
                        (*values, existing[0]),
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

    def rename_chat(self, chat_id: int, title: str) -> bool:
        """Set a manual chat title without touching ``updated_at``.

        ``updated_at`` drives the sidebar order, so renaming must not reorder
        chats. A blank title or an unknown chat id is a no-op reported as
        ``False``, letting the UI warn instead of raising. The title is
        parameterised, so quotes and emoji are stored verbatim.
        """
        cleaned = (title or "").strip()
        if not cleaned:
            return False
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE chats SET title = ? WHERE id = ?", (cleaned, chat_id)
            )
            conn.commit()
            return cursor.rowcount > 0

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
                "summary_cache_hit_tokens, summary_cache_miss_tokens, "
                "facts_ok_input_tokens, facts_ok_output_tokens, "
                "facts_ok_cache_hit_tokens, facts_ok_cache_miss_tokens, "
                "facts_ok_cost_usd, facts_fail_input_tokens, facts_fail_output_tokens, "
                "facts_fail_cache_hit_tokens, facts_fail_cache_miss_tokens, "
                "facts_fail_cost_usd, facts_anchor_message_id, last_context_tokens, "
                "context_strategy "
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
            facts_ok_input_tokens=row[9],
            facts_ok_output_tokens=row[10],
            facts_ok_cache_hit_tokens=row[11],
            facts_ok_cache_miss_tokens=row[12],
            facts_ok_cost_usd=row[13],
            facts_fail_input_tokens=row[14],
            facts_fail_output_tokens=row[15],
            facts_fail_cache_hit_tokens=row[16],
            facts_fail_cache_miss_tokens=row[17],
            facts_fail_cost_usd=row[18],
            facts_anchor_message_id=row[19],
            last_context_tokens=row[20],
            context_strategy=normalize_strategy(row[21]),
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
