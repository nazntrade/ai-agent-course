"""SQLite persistence for the Day 14 structural invariants.

``InvariantRepository`` stores the :class:`invariants.Invariant` value objects in
the same database file as the chats and tasks. The schema is created with
``CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS`` only, so a Day 13 database opens
with the new tables added and no data loss. Rules are never deleted: an edit
bumps ``version`` and keeps the previous rule readable through the append-only
``invariant_events`` journal, which records creation, edits, activation changes
and refusals.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from invariants import (
    CHECK_NONE,
    CONFLICT_REQUEST_MAX_LENGTH,
    DECISION_REFUSED,
    EVENT_ACTIVATED,
    EVENT_CONFLICT,
    EVENT_CREATED,
    EVENT_DEACTIVATED,
    EVENT_UPDATED,
    Invariant,
    PHASE_ACTION,
    SOURCE_SEED,
    SOURCE_USER,
    select_applicable,
    normalize_invariant,
)
from storage import DEFAULT_DB_PATH

DEFAULT_INVARIANTS_SEED_MARKER = "default_invariants_seeded"

_UNSET = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invariants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    text              TEXT    NOT NULL,
    scope             TEXT    NOT NULL CHECK (scope IN ('global', 'task')),
    task_id           INTEGER,
    enforcement       TEXT    NOT NULL CHECK (enforcement IN ('hard', 'advisory')),
    kind              TEXT    NOT NULL CHECK (kind IN ('guard', 'data', 'policy')),
    check_kind        TEXT    NOT NULL DEFAULT '',
    triggers_json     TEXT    NOT NULL DEFAULT '[]',
    guard_actions_json TEXT   NOT NULL DEFAULT '[]',
    guard_events_json TEXT    NOT NULL DEFAULT '[]',
    alternative       TEXT    NOT NULL DEFAULT '',
    version           INTEGER NOT NULL DEFAULT 1,
    is_active         INTEGER NOT NULL DEFAULT 1,
    source            TEXT    NOT NULL DEFAULT 'seed',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invariants_code
    ON invariants(code);
CREATE INDEX IF NOT EXISTS idx_invariants_scope
    ON invariants(scope, task_id, is_active);

-- The journal deliberately has no foreign key to chats/tasks: removing a chat
-- must not erase the record of the rules that were enforced or refused.
CREATE TABLE IF NOT EXISTS invariant_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id INTEGER,
    code         TEXT    NOT NULL DEFAULT '',
    event_type   TEXT    NOT NULL,
    chat_id      INTEGER,
    task_id      INTEGER,
    details_json TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_invariant_events_task
    ON invariant_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_invariant_events_code
    ON invariant_events(code, id);
CREATE TRIGGER IF NOT EXISTS trg_invariant_events_no_update
    BEFORE UPDATE ON invariant_events
    BEGIN
        SELECT RAISE(ABORT, 'invariant_events is append-only');
    END;
"""

# The three rules seeded once on the first database initialization. Insertion is
# guarded by the ``default_invariants_seeded`` marker, so deleting a seed does
# not resurrect it and reopening the database is a no-op.
_SEED_INVARIANTS = (
    Invariant(
        code="INV-NO-FSM-BYPASS",
        title="No validation bypass",
        text=(
            "Never skip validation or finish/complete a task without a successful "
            "validation run."
        ),
        scope="global",
        enforcement="hard",
        kind="guard",
        check_kind="no_validation_bypass",
        triggers=(
            "skip validation",
            "skip the validation",
            "bypass validation",
            "bypass the validation",
            "without validation",
            "finish without validation",
            "complete without validation",
            "skip the check",
            "пропустить валидацию",
            "пропустить проверку",
            "обойти валидацию",
            "обойти проверку",
            "без валидации",
            "без проверки",
            "завершить без проверки",
            "завершить без валидации",
        ),
        guard_events=("VALIDATION_PASSED",),
        alternative=(
            "Run validation normally; if it fails, fix the defects and run the "
            "step again."
        ),
    ),
    Invariant(
        code="INV-NO-DATA-RESET",
        title="No bulk data reset",
        text=(
            "Never mass-reset, wipe or bulk-delete stored conversations, messages, "
            "tasks, memory or profiles."
        ),
        scope="global",
        enforcement="hard",
        kind="data",
        check_kind="no_bulk_reset",
        triggers=(
            "reset all data",
            "reset the data",
            "reset everything",
            "wipe all",
            "wipe the data",
            "delete all data",
            "delete all chats",
            "delete everything",
            "clear all data",
            "сбросить все данные",
            "сбросить всё",
            "удалить все данные",
            "удалить все чаты",
            "очистить все данные",
            "стереть все данные",
        ),
        alternative=(
            "Deleting a single chat or branch with explicit confirmation is "
            "allowed."
        ),
    ),
    Invariant(
        code="INV-ADV-CONFIRM-DESTRUCTIVE",
        title="Confirm destructive actions",
        text=(
            "Ask the user to confirm before any destructive action; this rule is "
            "advisory context and is not code-enforced."
        ),
        scope="global",
        enforcement="advisory",
        kind="policy",
        alternative="Explain the consequence and ask for explicit confirmation.",
    ),
)


@dataclass
class InvariantEvent:
    """One append-only entry of the invariant journal."""

    id: int | None = None
    invariant_id: int | None = None
    code: str = ""
    event_type: str = ""
    chat_id: int | None = None
    task_id: int | None = None
    details: dict = None
    created_at: str | None = None


class DuplicateInvariantCodeError(ValueError):
    """Raised when a stable invariant code already exists.

    Nothing is written; the existing rule stays untouched.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Invariant code already exists: {code}")


def _load_json(raw, default):
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value


def _typed_tuple(raw) -> tuple:
    value = _load_json(raw, [])
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _dump_json(value) -> str:
    if isinstance(value, (list, tuple)):
        value = list(value)
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


class InvariantRepository:
    """SQLite store for structural invariants and their journal."""

    def __init__(self, db_path=None, *, seed_defaults=True):
        self._db_path = (
            Path(db_path) if db_path is not None else Path(DEFAULT_DB_PATH)
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._seed_defaults = bool(seed_defaults)
        self._init_db()

    @property
    def db_path(self):
        """The database file this repository works on."""
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            if self._seed_defaults:
                self._seed(conn)
            conn.commit()

    @staticmethod
    def _insert_invariant(conn, invariant: Invariant) -> int:
        cursor = conn.execute(
            "INSERT INTO invariants (code, title, text, scope, task_id, "
            "enforcement, kind, check_kind, triggers_json, guard_actions_json, "
            "guard_events_json, alternative, version, is_active, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invariant.code,
                invariant.title,
                invariant.text,
                invariant.scope,
                invariant.task_id,
                invariant.enforcement,
                invariant.kind,
                invariant.check_kind,
                _dump_json(invariant.triggers),
                _dump_json(invariant.guard_actions),
                _dump_json(invariant.guard_events),
                invariant.alternative,
                invariant.version,
                int(bool(invariant.is_active)),
                invariant.source,
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _insert_event(
        conn,
        *,
        invariant_id=None,
        code="",
        event_type,
        chat_id=None,
        task_id=None,
        details=None,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO invariant_events (invariant_id, code, event_type, "
            "chat_id, task_id, details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                invariant_id,
                code or "",
                event_type,
                chat_id,
                task_id,
                _dump_json(details or {}),
            ),
        )
        return cursor.lastrowid

    def _seed(self, conn) -> None:
        """Insert the default rules once, guarded by the app_state marker."""
        marker = conn.execute(
            "SELECT 1 FROM app_state WHERE key = ?",
            (DEFAULT_INVARIANTS_SEED_MARKER,),
        ).fetchone()
        if marker is not None:
            return
        count = conn.execute("SELECT COUNT(*) FROM invariants").fetchone()[0]
        if count == 0:
            for invariant in _SEED_INVARIANTS:
                normalized = normalize_invariant(
                    replace(invariant, source=SOURCE_SEED)
                )
                invariant_id = self._insert_invariant(conn, normalized)
                self._insert_event(
                    conn,
                    invariant_id=invariant_id,
                    code=normalized.code,
                    event_type=EVENT_CREATED,
                    task_id=normalized.task_id,
                    details={"source": SOURCE_SEED},
                )
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, '1')",
            (DEFAULT_INVARIANTS_SEED_MARKER,),
        )

    # --- Reads -------------------------------------------------------------

    _SELECT = (
        "SELECT id, code, title, text, scope, task_id, enforcement, kind, "
        "check_kind, triggers_json, guard_actions_json, guard_events_json, "
        "alternative, version, is_active, source, created_at, updated_at "
        "FROM invariants"
    )

    @staticmethod
    def _from_row(row) -> Invariant:
        return Invariant(
            id=row[0],
            code=row[1],
            title=row[2],
            text=row[3],
            scope=row[4],
            task_id=row[5],
            enforcement=row[6],
            kind=row[7],
            check_kind=row[8] or CHECK_NONE,
            triggers=_typed_tuple(row[9]),
            guard_actions=_typed_tuple(row[10]),
            guard_events=_typed_tuple(row[11]),
            alternative=row[12],
            version=row[13],
            is_active=bool(row[14]),
            source=row[15],
            created_at=row[16],
            updated_at=row[17],
        )

    def get_invariant(self, invariant_id) -> Invariant | None:
        """Return one rule by id, or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                self._SELECT + " WHERE id = ?", (invariant_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_by_code(self, code) -> Invariant | None:
        """Return one rule by its stable code, or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                self._SELECT + " WHERE code = ?", (str(code),)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_invariants(self, *, scope=None, task_id=None, active_only=False) -> list:
        """Return rules ordered by id, optionally filtered by scope/activity."""
        query = self._SELECT
        clauses = []
        params: list = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if active_only:
            clauses.append("is_active = 1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    def list_applicable(self, task_id=None) -> list:
        """Return the active rules that apply to ``task_id``, sorted."""
        return select_applicable(self.list_invariants(), task_id=task_id)

    # --- Writes ------------------------------------------------------------

    def create_invariant(
        self, invariant, *, chat_id=None, task_id=None
    ) -> Invariant:
        """Insert a new rule and journal its creation.

        A duplicate ``code`` raises :class:`DuplicateInvariantCodeError` and
        writes nothing.
        """
        normalized = replace(
            normalize_invariant(invariant), source=SOURCE_USER
        )
        with closing(self._connect()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM invariants WHERE code = ?", (normalized.code,)
            ).fetchone()
            if exists is not None:
                raise DuplicateInvariantCodeError(normalized.code)
            try:
                invariant_id = self._insert_invariant(conn, normalized)
                self._insert_event(
                    conn,
                    invariant_id=invariant_id,
                    code=normalized.code,
                    event_type=EVENT_CREATED,
                    chat_id=chat_id,
                    task_id=task_id if task_id is not None else normalized.task_id,
                    details={"source": normalized.source},
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise DuplicateInvariantCodeError(normalized.code) from exc
            except Exception:
                conn.rollback()
                raise
        return self.get_invariant(invariant_id)

    def update_invariant(
        self,
        invariant_id,
        *,
        code=None,
        title=None,
        text=None,
        scope=None,
        task_id=_UNSET,
        enforcement=None,
        kind=None,
        check_kind=None,
        triggers=None,
        guard_actions=None,
        guard_events=None,
        alternative=None,
        is_active=None,
        chat_id=None,
        source=SOURCE_USER,
    ) -> Invariant:
        """Edit a rule, bump its version and journal the change.

        The stable ``code`` is immutable: passing a different code raises
        ``ValueError``. The previous values are never lost, because the journal
        records the update before the caller can observe the new revision.
        """
        current = self.get_invariant(invariant_id)
        if current is None:
            raise KeyError(f"invariant {invariant_id} not found")
        if code is not None and str(code).strip() != current.code:
            raise ValueError("Invariant code cannot be changed")

        candidate = replace(
            current,
            title=current.title if title is None else title,
            text=current.text if text is None else text,
            scope=current.scope if scope is None else scope,
            task_id=current.task_id if task_id is _UNSET else task_id,
            enforcement=current.enforcement if enforcement is None else enforcement,
            kind=current.kind if kind is None else kind,
            check_kind=current.check_kind if check_kind is None else check_kind,
            triggers=current.triggers if triggers is None else triggers,
            guard_actions=(
                current.guard_actions if guard_actions is None else guard_actions
            ),
            guard_events=(
                current.guard_events if guard_events is None else guard_events
            ),
            alternative=current.alternative if alternative is None else alternative,
            is_active=current.is_active if is_active is None else bool(is_active),
        )
        normalized = replace(
            normalize_invariant(candidate),
            version=current.version + 1,
            source=source,
        )
        self._write_changes(
            invariant_id,
            normalized,
            chat_id=chat_id,
            event_types=[EVENT_UPDATED],
            details={"previous_version": current.version},
        )
        if current.is_active != normalized.is_active:
            event_type = EVENT_ACTIVATED if normalized.is_active else EVENT_DEACTIVATED
            self._record_event(
                invariant_id,
                normalized.code,
                event_type,
                chat_id=chat_id,
                task_id=normalized.task_id,
                details={"version": normalized.version},
            )
        return self.get_invariant(invariant_id)

    def set_active(self, invariant_id, is_active, *, chat_id=None) -> Invariant:
        """Activate or deactivate a rule, bumping the version and journaling it."""
        current = self.get_invariant(invariant_id)
        if current is None:
            raise KeyError(f"invariant {invariant_id} not found")
        updated = replace(
            current,
            is_active=bool(is_active),
            version=current.version + 1,
            source=SOURCE_USER,
        )
        event_type = EVENT_ACTIVATED if updated.is_active else EVENT_DEACTIVATED
        self._write_changes(
            invariant_id,
            updated,
            chat_id=chat_id,
            event_types=[event_type],
            details={"previous_version": current.version},
        )
        return self.get_invariant(invariant_id)

    def _write_changes(
        self,
        invariant_id,
        invariant: Invariant,
        *,
        chat_id,
        event_types,
        details,
    ) -> None:
        with closing(self._connect()) as conn:
            try:
                conn.execute(
                    "UPDATE invariants SET title = ?, text = ?, scope = ?, "
                    "task_id = ?, enforcement = ?, kind = ?, check_kind = ?, "
                    "triggers_json = ?, guard_actions_json = ?, "
                    "guard_events_json = ?, alternative = ?, version = ?, "
                    "is_active = ?, source = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (
                        invariant.title,
                        invariant.text,
                        invariant.scope,
                        invariant.task_id,
                        invariant.enforcement,
                        invariant.kind,
                        invariant.check_kind,
                        _dump_json(invariant.triggers),
                        _dump_json(invariant.guard_actions),
                        _dump_json(invariant.guard_events),
                        invariant.alternative,
                        invariant.version,
                        int(bool(invariant.is_active)),
                        invariant.source,
                        invariant_id,
                    ),
                )
                for event_type in event_types:
                    self._insert_event(
                        conn,
                        invariant_id=invariant_id,
                        code=invariant.code,
                        event_type=event_type,
                        chat_id=chat_id,
                        task_id=invariant.task_id,
                        details=details,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _record_event(self, invariant_id, code, event_type, **kwargs) -> int:
        with closing(self._connect()) as conn:
            try:
                event_id = self._insert_event(
                    conn,
                    invariant_id=invariant_id,
                    code=code,
                    event_type=event_type,
                    **kwargs,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return event_id

    # --- Conflicts and journal ---------------------------------------------

    def record_conflict(
        self,
        *,
        chat_id=None,
        task_id=None,
        invariant_id=None,
        code="",
        phase=PHASE_ACTION,
        request=None,
        trigger=None,
        action=None,
        event=None,
        alternative=None,
        decision=DECISION_REFUSED,
    ) -> InvariantEvent:
        """Append a refusal to the journal and return the stored event.

        A long request is truncated to :data:`CONFLICT_REQUEST_MAX_LENGTH`
        characters; no secret is read or stored.
        """
        details = {
            "phase": phase,
            "trigger": trigger,
            "action": action,
            "event": event,
            "decision": decision,
            "alternative": alternative,
        }
        if request is not None:
            details["request"] = str(request).strip()[:CONFLICT_REQUEST_MAX_LENGTH]
        details = {
            key: value for key, value in details.items() if value is not None
        }
        event_id = self._record_event(
            invariant_id,
            code,
            EVENT_CONFLICT,
            chat_id=chat_id,
            task_id=task_id,
            details=details,
        )
        return self.get_event(event_id)

    def list_events(
        self, *, invariant_id=None, code=None, task_id=None, limit=None
    ) -> list:
        """Return journal entries ordered by id, optionally filtered."""
        query = (
            "SELECT id, invariant_id, code, event_type, chat_id, task_id, "
            "details_json, created_at FROM invariant_events"
        )
        clauses = []
        params: list = []
        if invariant_id is not None:
            clauses.append("invariant_id = ?")
            params.append(invariant_id)
        if code is not None:
            clauses.append("code = ?")
            params.append(code)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_event(self, event_id) -> InvariantEvent | None:
        """Return one journal entry by id, or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, invariant_id, code, event_type, chat_id, task_id, "
                "details_json, created_at FROM invariant_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    @staticmethod
    def _event_from_row(row) -> InvariantEvent:
        details = _load_json(row[6], {})
        return InvariantEvent(
            id=row[0],
            invariant_id=row[1],
            code=row[2],
            event_type=row[3],
            chat_id=row[4],
            task_id=row[5],
            details=details if isinstance(details, dict) else {},
            created_at=row[7],
        )
