"""SQLite persistence for the Day 13 task state machine.

The repository stores the domain objects of :mod:`tasks` and never decides
which transition is allowed: the FSM validates the transition first, and
``commit_transition`` writes its result in one transaction (optimistic lock on
``tasks.version`` + append-only event + immutable artifacts).

The schema is created next to the existing chat tables and reuses
``ChatStore``'s database file, so a database from an earlier day opens with the
new tables added and no data loss. Each connection enables foreign keys, WAL and
a busy timeout.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from stats import sum_optional
from storage import resolve_db_path
from tasks import (
    ARTIFACT_KINDS,
    ARTIFACT_PLAN,
    EVENT_API_ERROR,
    EVENT_RETRY,
    STAGES,
    Task,
    TaskArtifact,
    TaskEvent,
    WorkflowProfile,
    apply_transition,
    compute_step_progress,
    creation_transition,
)

DEFAULT_WORKFLOW_NAME = "default"
ACTIVE_TASK_KEY_TEMPLATE = "active_task:{chat_id}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_profiles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    display_name      TEXT    NOT NULL DEFAULT '',
    stages_json       TEXT    NOT NULL DEFAULT '[]',
    instructions_json TEXT    NOT NULL DEFAULT '{}',
    executors_json    TEXT    NOT NULL DEFAULT '{}',
    models_json       TEXT    NOT NULL DEFAULT '{}',
    validation_json   TEXT    NOT NULL DEFAULT '{}',
    is_default        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_profiles_default
    ON workflow_profiles(is_default) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS tasks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id              INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    workflow_profile_id  INTEGER REFERENCES workflow_profiles(id),
    title                TEXT    NOT NULL,
    goal                 TEXT    NOT NULL,
    stage                TEXT    NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN
        ('active', 'paused', 'blocked', 'completed', 'cancelled')),
    current_step         TEXT    NOT NULL DEFAULT '',
    current_step_index   INTEGER,
    expected_action_type TEXT    NOT NULL DEFAULT 'none',
    expected_action_text TEXT    NOT NULL DEFAULT '',
    pause_reason         TEXT    NOT NULL DEFAULT '',
    version              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_active ON tasks(chat_id, status)
    WHERE status IN ('active', 'paused', 'blocked');

CREATE TABLE IF NOT EXISTS task_artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage      TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    revision   INTEGER NOT NULL,
    content    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_artifacts_revision
    ON task_artifacts(task_id, kind, revision);

CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type      TEXT    NOT NULL,
    from_stage      TEXT,
    to_stage        TEXT,
    from_status     TEXT,
    to_status       TEXT,
    payload_json    TEXT    NOT NULL DEFAULT '{}',
    idempotency_key TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- A journal entry is never rewritten: the only allowed mutation is the update
-- of ``tasks``, which is a different table. The trigger is the database-level
-- guarantee, the missing update API is the application-level one.
CREATE TRIGGER IF NOT EXISTS trg_task_events_no_update
    BEFORE UPDATE ON task_events
    BEGIN
        SELECT RAISE(ABORT, 'task_events is append-only');
    END;
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_events_idempotency
    ON task_events(task_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Day 15: every refused transition attempt is appended here, separately from
-- ``task_events``. The audit never changes the task: it only records what was
-- asked, from which state, why it was refused and what was allowed instead.
CREATE TABLE IF NOT EXISTS task_transition_attempts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id              INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action               TEXT    NOT NULL,
    from_stage           TEXT,
    from_status          TEXT,
    expected_action_type TEXT,
    reason               TEXT    NOT NULL DEFAULT '',
    allowed_actions_json TEXT    NOT NULL DEFAULT '[]',
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_transition_attempts_task
    ON task_transition_attempts(task_id, id);
CREATE TRIGGER IF NOT EXISTS trg_task_transition_attempts_no_update
    BEFORE UPDATE ON task_transition_attempts
    BEGIN
        SELECT RAISE(ABORT, 'task_transition_attempts is append-only');
    END;
"""

# Columns that may be added after the Day 13 schema; the migration adds
# whichever are missing, so opening an older database stays idempotent.
_NEW_TASK_COLUMNS: dict = {}
_NEW_TASK_ARTIFACT_COLUMNS: dict = {}
_NEW_TASK_EVENT_COLUMNS: dict = {}

# The single default workflow of Day 13. It is seeded once by unique name and
# never updated afterwards, so a user edit survives reopening the database.
_DEFAULT_WORKFLOW = {
    "display_name": "Default workflow",
    "stages": list(STAGES),
    "instructions": {
        "planning": "Составь проверяемый план выполнения задачи и верни его строгим JSON.",
        "execution": "Выполни текущий шаг плана и верни готовый результат шага.",
        "validation": "Проверь результат по критериям приёмки и верни вердикт строгим JSON.",
    },
    "executors": {"planning": "chat", "execution": "chat", "validation": "chat"},
    "models": {},
    "validation": {"require_defects_on_failure": True, "retries": 1},
}


class TaskNotFoundError(KeyError):
    """Raised when a task, chat or workflow does not exist."""


class TaskVersionConflictError(RuntimeError):
    """Raised when the optimistic lock does not match the stored version.

    Nothing has been written: the caller re-reads the task and retries the
    action with the new version.
    """

    def __init__(self, task_id, expected_version):
        self.task_id = task_id
        self.expected_version = expected_version
        super().__init__(
            f"Task {task_id} version conflict: expected version {expected_version} "
            "no longer matches the stored row"
        )


class TaskDuplicateEventError(RuntimeError):
    """Raised when the same idempotency key would create a second event.

    The whole commit is rolled back, so neither the task nor the journal change.
    """

    def __init__(self, task_id, idempotency_key):
        self.task_id = task_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Task {task_id} already has an event {idempotency_key}"
        )


@dataclass
class TaskUsage:
    """Aggregated provider usage of all calls recorded for one task."""

    calls: int = 0
    attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cost_usd: float | None = None
    finish_reasons: tuple = ()
    models: tuple = ()


@dataclass
class TransitionAttempt:
    """One append-only audit row of a refused or rejected transition attempt."""

    id: int | None = None
    task_id: int | None = None
    action: str = ""
    from_stage: str | None = None
    from_status: str | None = None
    expected_action_type: str | None = None
    reason: str = ""
    allowed_actions: tuple = ()
    created_at: str | None = None


def _load_json(raw, default):
    """Parse a JSON column; a corrupt value falls back instead of raising."""
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value


def _typed(value, expected):
    """Return ``value`` only when it has the expected type."""
    return value if isinstance(value, expected) else expected()


def _dump_json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


class TaskRepository:
    """SQLite store for tasks, artifacts, events and workflow profiles."""

    def __init__(self, db_path=None):
        self._db_path = resolve_db_path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
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
            self._add_columns(conn, "tasks", _NEW_TASK_COLUMNS)
            self._add_columns(conn, "task_artifacts", _NEW_TASK_ARTIFACT_COLUMNS)
            self._add_columns(conn, "task_events", _NEW_TASK_EVENT_COLUMNS)
            self._seed_default_workflow(conn)
            conn.commit()

    @staticmethod
    def _add_columns(conn, table: str, columns: dict) -> None:
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @staticmethod
    def _seed_default_workflow(conn) -> None:
        """Insert the default workflow if it is missing; never overwrite it."""
        conn.execute(
            "INSERT OR IGNORE INTO workflow_profiles (name, display_name, "
            "stages_json, instructions_json, executors_json, models_json, "
            "validation_json, is_default) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                DEFAULT_WORKFLOW_NAME,
                _DEFAULT_WORKFLOW["display_name"],
                _dump_json(_DEFAULT_WORKFLOW["stages"]),
                _dump_json(_DEFAULT_WORKFLOW["instructions"]),
                _dump_json(_DEFAULT_WORKFLOW["executors"]),
                _dump_json(_DEFAULT_WORKFLOW["models"]),
                _dump_json(_DEFAULT_WORKFLOW["validation"]),
            ),
        )

    # --- Workflows ---------------------------------------------------------

    _WORKFLOW_SELECT = (
        "SELECT id, name, display_name, stages_json, instructions_json, "
        "executors_json, models_json, validation_json, is_default "
        "FROM workflow_profiles"
    )

    @staticmethod
    def _workflow_from_row(row) -> WorkflowProfile:
        raw_stages = _load_json(row[3], None)
        if not isinstance(raw_stages, (list, tuple)) or not raw_stages:
            stages = STAGES
        else:
            stages = tuple(raw_stages)
        return WorkflowProfile(
            id=row[0],
            name=row[1],
            display_name=row[2],
            stages=stages,
            instructions=_typed(_load_json(row[4], {}), dict),
            executors=_typed(_load_json(row[5], {}), dict),
            models=_typed(_load_json(row[6], {}), dict),
            validation=_typed(_load_json(row[7], {}), dict),
            is_default=bool(row[8]),
        )

    def list_workflows(self) -> list:
        """Return all workflow profiles, the default one first."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                self._WORKFLOW_SELECT + " ORDER BY is_default DESC, id"
            ).fetchall()
        return [self._workflow_from_row(row) for row in rows]

    def get_workflow(self, name=DEFAULT_WORKFLOW_NAME) -> WorkflowProfile | None:
        """Return a workflow by name, falling back to the default one.

        A missing profile, a missing default and unreadable JSON columns all
        resolve to a usable value instead of raising.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                self._WORKFLOW_SELECT + " WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    self._WORKFLOW_SELECT
                    + " ORDER BY is_default DESC, id LIMIT 1"
                ).fetchone()
        return self._workflow_from_row(row) if row is not None else None

    # --- Tasks -------------------------------------------------------------

    _TASK_SELECT = (
        "SELECT t.id, t.chat_id, t.workflow_profile_id, w.name, t.title, t.goal, "
        "t.stage, t.status, t.current_step, t.current_step_index, "
        "t.expected_action_type, t.expected_action_text, t.pause_reason, "
        "t.version, t.created_at, t.updated_at "
        "FROM tasks t LEFT JOIN workflow_profiles w ON w.id = t.workflow_profile_id"
    )

    @staticmethod
    def _task_from_row(row) -> Task:
        return Task(
            id=row[0],
            chat_id=row[1],
            workflow_profile_id=row[2],
            workflow_name=row[3],
            title=row[4],
            goal=row[5],
            stage=row[6],
            status=row[7],
            current_step=row[8],
            current_step_index=row[9],
            expected_action_type=row[10],
            expected_action_text=row[11],
            pause_reason=row[12],
            version=row[13],
            created_at=row[14],
            updated_at=row[15],
        )

    def create_task(
        self,
        chat_id,
        title,
        goal,
        workflow_name=DEFAULT_WORKFLOW_NAME,
        task_brief=None,
    ) -> Task:
        """Create a task with TASK_CREATED and an optional brief, atomically."""
        profile = self.get_workflow(workflow_name)
        if profile is None:
            raise TaskNotFoundError(f"workflow {workflow_name} not found")

        transition = creation_transition(
            chat_id,
            title,
            goal,
            workflow_profile_id=profile.id,
            workflow_name=profile.name,
            task_brief=task_brief,
        )
        task = transition.task

        with closing(self._connect()) as conn:
            try:
                chat = conn.execute(
                    "SELECT 1 FROM chats WHERE id = ?", (chat_id,)
                ).fetchone()
                if chat is None:
                    raise TaskNotFoundError(f"chat {chat_id} not found")
                cursor = conn.execute(
                    "INSERT INTO tasks (chat_id, workflow_profile_id, title, goal, "
                    "stage, status, current_step, current_step_index, "
                    "expected_action_type, expected_action_text, pause_reason, "
                    "version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.chat_id,
                        task.workflow_profile_id,
                        task.title,
                        task.goal,
                        task.stage,
                        task.status,
                        task.current_step,
                        task.current_step_index,
                        task.expected_action_type,
                        task.expected_action_text,
                        task.pause_reason,
                        task.version,
                    ),
                )
                task_id = cursor.lastrowid
                self._insert_event(conn, task_id, transition.event)
                revisions: dict = {}
                for artifact in transition.artifacts:
                    self._insert_artifact(conn, task_id, artifact, revisions)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_task(task_id)

    def get_task(self, task_id) -> Task | None:
        """Return one task joined with its workflow name, or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                self._TASK_SELECT + " WHERE t.id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_tasks(self, chat_id) -> list:
        """Return the chat's tasks ordered by id."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                self._TASK_SELECT + " WHERE t.chat_id = ? ORDER BY t.id", (chat_id,)
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def latest_event_type(self, task_id) -> str | None:
        """Return the type of the newest journal event, or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT event_type FROM task_events WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return row[0] if row is not None else None

    # --- Artifacts and events ---------------------------------------------

    @staticmethod
    def _artifact_from_row(row) -> TaskArtifact:
        return TaskArtifact(
            id=row[0],
            task_id=row[1],
            stage=row[2],
            kind=row[3],
            revision=row[4],
            content=_typed(_load_json(row[5], {}), dict),
            created_at=row[6],
        )

    def list_artifacts(self, task_id, kind=None) -> list:
        """Return artifacts ordered by id; ``kind`` filters one kind."""
        query = (
            "SELECT id, task_id, stage, kind, revision, content, created_at "
            "FROM task_artifacts WHERE task_id = ?"
        )
        params: list = [task_id]
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def add_artifact(self, task_id, stage, kind, content) -> TaskArtifact:
        """Append one artifact in its own transaction and return the stored row.

        The revision is assigned inside the transaction, so an existing revision
        is never overwritten. The FSM writes its artifacts through
        :meth:`commit_transition`; this method exists for callers that need to
        attach a standalone artifact.
        """
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact kind: {kind}")
        if self.get_task(task_id) is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        artifact = TaskArtifact(
            task_id=task_id, stage=stage, kind=kind, content=dict(content or {})
        )
        with closing(self._connect()) as conn:
            try:
                artifact_id = self._insert_artifact(conn, task_id, artifact, {})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id) -> TaskArtifact | None:
        """Return one artifact by id, or ``None``."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, task_id, stage, kind, revision, content, created_at "
                "FROM task_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        return self._artifact_from_row(row) if row is not None else None

    def load_plan(self, task_id) -> dict | None:
        """Return the latest plan artifact content, or ``None`` before planning."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT content FROM task_artifacts WHERE task_id = ? AND kind = ? "
                "ORDER BY revision DESC, id DESC LIMIT 1",
                (task_id, ARTIFACT_PLAN),
            ).fetchone()
        if row is None:
            return None
        content = _load_json(row[0], None)
        return content if isinstance(content, dict) else None

    def step_progress(self, task_id) -> list:
        """Derive the step progress of the task's current plan from artifacts."""
        plan = self.load_plan(task_id)
        if plan is None:
            return []
        return compute_step_progress(plan, self.list_artifacts(task_id))

    @staticmethod
    def _event_from_row(row) -> TaskEvent:
        return TaskEvent(
            id=row[0],
            task_id=row[1],
            event_type=row[2],
            from_stage=row[3],
            to_stage=row[4],
            from_status=row[5],
            to_status=row[6],
            payload=_typed(_load_json(row[7], {}), dict),
            idempotency_key=row[8],
            created_at=row[9],
        )

    def list_events(self, task_id) -> list:
        """Return the append-only journal of the task ordered by id."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, task_id, event_type, from_stage, to_stage, "
                "from_status, to_status, payload_json, idempotency_key, created_at "
                "FROM task_events WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _insert_event(conn, task_id, event, from_stage=None, to_stage=None,
                      from_status=None, to_status=None, idempotency_key=None) -> int:
        cursor = conn.execute(
            "INSERT INTO task_events (task_id, event_type, from_stage, to_stage, "
            "from_status, to_status, payload_json, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                event.event_type,
                from_stage if from_stage is not None else event.from_stage,
                to_stage if to_stage is not None else event.to_stage,
                from_status if from_status is not None else event.from_status,
                to_status if to_status is not None else event.to_status,
                _dump_json(event.payload),
                idempotency_key if idempotency_key is not None else event.idempotency_key,
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _insert_artifact(conn, task_id, artifact, revisions=None) -> int:
        """Insert the next revision of the artifact in the caller's transaction.

        Revisions are assigned here, inside the same transaction as the event,
        so a repeated step can never overwrite an earlier result.
        """
        if revisions is None:
            revisions = {}
        current = revisions.get(artifact.kind)
        if current is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM task_artifacts "
                "WHERE task_id = ? AND kind = ?",
                (task_id, artifact.kind),
            ).fetchone()
            current = row[0] if row is not None else 0
        revision = current + 1
        revisions[artifact.kind] = revision
        cursor = conn.execute(
            "INSERT INTO task_artifacts (task_id, stage, kind, revision, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                artifact.stage,
                artifact.kind,
                revision,
                _dump_json(artifact.content),
            ),
        )
        return cursor.lastrowid

    def commit_transition(self, task_id, expected_version, transition) -> Task:
        """Commit an FSM result: optimistic update, event and artifacts at once.

        ``expected_version`` is the version the caller observed; a mismatch
        raises :class:`TaskVersionConflictError` and writes nothing. Resubmitting
        the same transition after it was committed therefore raises a version
        conflict rather than creating a second event, and the partial unique
        index on ``idempotency_key`` is the second line of defence.
        """
        new_task = transition.task
        with closing(self._connect()) as conn:
            try:
                cursor = conn.execute(
                    "UPDATE tasks SET stage = ?, status = ?, current_step = ?, "
                    "current_step_index = ?, expected_action_type = ?, "
                    "expected_action_text = ?, pause_reason = ?, version = ?, "
                    "updated_at = ? WHERE id = ? AND version = ?",
                    (
                        new_task.stage,
                        new_task.status,
                        new_task.current_step,
                        new_task.current_step_index,
                        new_task.expected_action_type,
                        new_task.expected_action_text,
                        new_task.pause_reason,
                        new_task.version,
                        new_task.updated_at,
                        task_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount == 0:
                    exists = conn.execute(
                        "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    if exists is None:
                        raise TaskNotFoundError(f"task {task_id} not found")
                    raise TaskVersionConflictError(task_id, expected_version)

                if transition.event is not None:
                    try:
                        self._insert_event(conn, task_id, transition.event)
                    except sqlite3.IntegrityError as exc:
                        raise TaskDuplicateEventError(
                            task_id, transition.idempotency_key
                        ) from exc

                revisions: dict = {}
                for artifact in transition.artifacts:
                    self._insert_artifact(conn, task_id, artifact, revisions)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_task(task_id)

    def append_event(
        self,
        task_id,
        event_type,
        payload=None,
        from_stage=None,
        to_stage=None,
        from_status=None,
        to_status=None,
        idempotency_key=None,
    ) -> TaskEvent:
        """Append an ``API_ERROR`` or ``RETRY`` event without changing the task.

        The payload is validated by the FSM, so an unknown error kind or a retry
        without a preceding ``API_ERROR`` is rejected and nothing is written.
        """
        if event_type not in (EVENT_API_ERROR, EVENT_RETRY):
            raise ValueError(
                "append_event is only for API_ERROR and RETRY; "
                "state-changing events go through commit_transition"
            )
        task = self.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        last_event_type = self.latest_event_type(task_id)
        transition = apply_transition(
            task,
            event_type,
            payload=payload,
            last_event_type=last_event_type,
        )
        with closing(self._connect()) as conn:
            try:
                event_id = self._insert_event(
                    conn,
                    task_id,
                    transition.event,
                    from_stage=from_stage,
                    to_stage=to_stage,
                    from_status=from_status,
                    to_status=to_status,
                    idempotency_key=idempotency_key,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_event(event_id)

    def get_event(self, event_id) -> TaskEvent | None:
        """Return one journal entry by id."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, task_id, event_type, from_stage, to_stage, "
                "from_status, to_status, payload_json, idempotency_key, created_at "
                "FROM task_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    # --- Transition attempts (Day 15) -------------------------------------

    _ATTEMPT_SELECT = (
        "SELECT id, task_id, action, from_stage, from_status, "
        "expected_action_type, reason, allowed_actions_json, created_at "
        "FROM task_transition_attempts"
    )

    @staticmethod
    def _attempt_from_row(row) -> TransitionAttempt:
        raw_allowed = _load_json(row[7], [])
        if not isinstance(raw_allowed, (list, tuple)):
            raw_allowed = []
        return TransitionAttempt(
            id=row[0],
            task_id=row[1],
            action=row[2],
            from_stage=row[3],
            from_status=row[4],
            expected_action_type=row[5],
            reason=row[6],
            allowed_actions=tuple(raw_allowed),
            created_at=row[8],
        )

    def record_transition_attempt(
        self,
        task_id,
        action,
        *,
        reason,
        allowed_actions=(),
        from_stage=None,
        from_status=None,
        expected_action_type=None,
    ) -> int:
        """Append one refusal to the audit; the task itself is not touched.

        The table is append-only (the trigger rejects UPDATE), so a repeated
        refusal is a new row and the history is never rewritten. A task that no
        longer exists is rejected before anything is written.
        """
        if self.get_task(task_id) is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        with closing(self._connect()) as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO task_transition_attempts (task_id, action, "
                    "from_stage, from_status, expected_action_type, reason, "
                    "allowed_actions_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        str(action or ""),
                        from_stage,
                        from_status,
                        expected_action_type,
                        str(reason or ""),
                        _dump_json(list(allowed_actions or ())),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return cursor.lastrowid

    def list_transition_attempts(self, task_id, limit=None) -> list:
        """Return the refusals chronologically; ``limit`` keeps the newest N.

        A limited read still returns the newest rows in ascending id order, so
        the UI shows the most recent refusals in the order they happened.
        """
        query = self._ATTEMPT_SELECT + " WHERE task_id = ?"
        params: list = [task_id]
        if limit is not None:
            query += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
        else:
            query += " ORDER BY id"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        if limit is not None:
            rows = list(reversed(rows))
        return [self._attempt_from_row(row) for row in rows]

    # --- Selected task -----------------------------------------------------

    @staticmethod
    def _selection_key(chat_id) -> str:
        return ACTIVE_TASK_KEY_TEMPLATE.format(chat_id=chat_id)

    def set_active_task_id(self, chat_id, task_id) -> None:
        """Remember the task selected in the sidebar for this chat."""
        task = self.get_task(task_id)
        if task is None or task.chat_id != chat_id:
            raise TaskNotFoundError(f"task {task_id} does not belong to chat {chat_id}")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                (self._selection_key(chat_id), str(task_id)),
            )
            conn.commit()

    def get_active_task_id(self, chat_id) -> int | None:
        """Return the remembered task id, or ``None`` when unset or corrupt."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?",
                (self._selection_key(chat_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def clear_selection(self, chat_id) -> None:
        """Forget the selected task of the chat."""
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM app_state WHERE key = ?", (self._selection_key(chat_id),)
            )
            conn.commit()

    def resolve_display_task(self, chat_id) -> Task | None:
        """Return the selected task, or a safe fallback when it is gone.

        A dangling selection (the task was deleted) is cleared and the newest
        task of the chat is returned instead; a chat without tasks yields
        ``None``.
        """
        active_id = self.get_active_task_id(chat_id)
        if active_id is not None:
            task = self.get_task(active_id)
            if task is not None and task.chat_id == chat_id:
                return task
            self.clear_selection(chat_id)
        tasks = self.list_tasks(chat_id)
        return tasks[-1] if tasks else None

    # --- Usage -------------------------------------------------------------

    def get_task_usage(self, task_id) -> TaskUsage:
        """Aggregate the provider usage stored in the task's journal events."""
        input_values: list = []
        output_values: list = []
        total_values: list = []
        hit_values: list = []
        miss_values: list = []
        cost_values: list = []
        finish_reasons: list = []
        models: list = []
        calls = 0
        attempts = 0

        for event in self.list_events(task_id):
            payload = event.payload or {}
            usage = payload.get("usage")
            usage = usage if isinstance(usage, dict) else {}

            prompt = usage.get("prompt_tokens", payload.get("prompt_tokens"))
            completion = usage.get("completion_tokens", payload.get("completion_tokens"))
            total = usage.get("total_tokens", payload.get("total_tokens"))
            hit = usage.get(
                "prompt_cache_hit_tokens", payload.get("prompt_cache_hit_tokens")
            )
            miss = usage.get(
                "prompt_cache_miss_tokens", payload.get("prompt_cache_miss_tokens")
            )
            cost = usage.get("cost_usd", payload.get("cost_usd"))
            if any(value is not None for value in (prompt, completion, total, cost)):
                calls += 1
            input_values.append(prompt)
            output_values.append(completion)
            total_values.append(total)
            hit_values.append(hit)
            miss_values.append(miss)
            cost_values.append(cost)

            raw_attempts = payload.get("attempts", usage.get("attempts"))
            if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool):
                attempts += raw_attempts

            finish_reason = payload.get("finish_reason", usage.get("finish_reason"))
            if finish_reason and finish_reason not in finish_reasons:
                finish_reasons.append(finish_reason)
            model = payload.get("model", usage.get("model"))
            if model and model not in models:
                models.append(model)

        return TaskUsage(
            calls=calls,
            attempts=attempts,
            input_tokens=sum_optional(input_values),
            output_tokens=sum_optional(output_values),
            total_tokens=sum_optional(total_values),
            cache_hit_tokens=sum_optional(hit_values),
            cache_miss_tokens=sum_optional(miss_values),
            cost_usd=sum_optional(cost_values),
            finish_reasons=tuple(finish_reasons),
            models=tuple(models),
        )
