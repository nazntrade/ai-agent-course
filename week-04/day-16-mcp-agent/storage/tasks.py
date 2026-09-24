"""SQL repository of scheduled tasks and their runs (owned by the MCP server).

The MCP server writes ``tasks`` and ``runs``; the backend only reads them for
the task panel. The repository provides the CAS slot claim and the run
retention used by the scheduler, but it does not enforce the per-chat and total
task limits: ``mcp_server.tasks.TaskService`` checks those before writing.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from storage.db import Database

MAX_RUNS_PER_TASK = 50


def new_task_id() -> str:
    """Return a fresh opaque task identifier."""
    return uuid.uuid4().hex[:12]


def _task_dict(row) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "query": row["query"],
        "interval_seconds": int(row["interval_seconds"]),
        "max_results": int(row["max_results"]),
        "status": row["status"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "next_run_at": float(row["next_run_at"]),
        "stopped_at": None if row["stopped_at"] is None else float(row["stopped_at"]),
        "last_run_at": None if row["last_run_at"] is None else float(row["last_run_at"]),
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "run_count": int(row["run_count"]),
    }


def _run_dict(row) -> dict:
    result = None
    raw = row["result_json"]
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            result = parsed
    return {
        "id": int(row["id"]),
        "task_id": row["task_id"],
        "started_at": float(row["started_at"]),
        "finished_at": float(row["finished_at"]),
        "status": row["status"],
        "result": result,
        "result_count": int(row["result_count"]),
        "error": row["error"],
    }


class TaskRepository:
    """Reads and writes ``tasks`` and ``runs`` rows."""

    def __init__(self, database: Database):
        self._db = database

    def create_task(
        self,
        chat_id: str,
        query: str,
        interval_seconds: int,
        *,
        max_results: int = 0,
        now: float | None = None,
        next_run_at: float | None = None,
        status: str = "active",
        task_id: str | None = None,
    ) -> dict:
        moment = float(now if now is not None else time.time())
        first_run = float(next_run_at if next_run_at is not None else moment)
        identifier = str(task_id) if task_id else new_task_id()
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(id, chat_id, query, interval_seconds, "
                "max_results, status, created_at, updated_at, next_run_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    str(chat_id),
                    str(query),
                    int(interval_seconds),
                    int(max_results),
                    str(status),
                    moment,
                    moment,
                    first_run,
                ),
            )
        return self.get_task(identifier)

    def get_task(self, task_id: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
        return _task_dict(row) if row is not None else None

    def list_tasks(self, chat_id: str) -> list:
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE chat_id = ? ORDER BY created_at, id",
                (str(chat_id),),
            ).fetchall()
        return [_task_dict(row) for row in rows]

    def find_active_task(self, chat_id: str, query: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND status = 'active' "
                "AND query = ? ORDER BY created_at LIMIT 1",
                (str(chat_id), str(query)),
            ).fetchone()
        return _task_dict(row) if row is not None else None

    def count_active_tasks(self, chat_id: str) -> int:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM tasks "
                "WHERE chat_id = ? AND status = 'active'",
                (str(chat_id),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def count_active_total(self) -> int:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE status = 'active'"
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def due_tasks(self, now: float, limit: int = 5) -> list:
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = 'active' AND next_run_at <= ? "
                "ORDER BY next_run_at LIMIT ?",
                (float(now), max(int(limit), 1)),
            ).fetchall()
        return [_task_dict(row) for row in rows]

    def claim_task(
        self, task_id: str, *, now: float, expected_next_run_at: float
    ) -> bool:
        """CAS the next slot: exactly one caller wins a due task.

        ``next_run_at`` is advanced to ``now + interval_seconds`` before the
        run, so a crash mid-run loses the slot instead of duplicating it.
        """
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tasks "
                "SET next_run_at = ? + interval_seconds, updated_at = ? "
                "WHERE id = ? AND status = 'active' AND next_run_at = ?",
                (
                    float(now),
                    float(now),
                    str(task_id),
                    float(expected_next_run_at),
                ),
            )
            return cursor.rowcount == 1

    def stop_task(self, task_id: str, *, now: float) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = 'stopped', stopped_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'active'",
                (float(now), float(now), str(task_id)),
            )
            return cursor.rowcount == 1

    def record_run(
        self,
        task_id: str,
        *,
        started_at: float,
        finished_at: float,
        status: str,
        result: dict | None = None,
        result_count: int = 0,
        error: str | None = None,
    ) -> int | None:
        """Insert one run, but only while the task still exists.

        A run whose task was deleted meanwhile is dropped: the foreign key would
        reject it and the task must never be recreated by a stale scheduler tick.
        """
        payload = None
        if result is not None:
            try:
                payload = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                payload = None
        try:
            with self._db.transaction() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (str(task_id),)
                ).fetchone()
                if exists is None:
                    return None
                cursor = connection.execute(
                    "INSERT INTO runs(task_id, started_at, finished_at, status, "
                    "result_json, result_count, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(task_id),
                        float(started_at),
                        float(finished_at),
                        str(status),
                        payload,
                        int(result_count),
                        error,
                    ),
                )
                run_id = cursor.lastrowid
                connection.execute(
                    "DELETE FROM runs WHERE task_id = ? AND id NOT IN ("
                    "SELECT id FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT ?)",
                    (str(task_id), str(task_id), MAX_RUNS_PER_TASK),
                )
        except sqlite3.IntegrityError:
            return None
        return int(run_id) if run_id is not None else None

    def update_after_run(
        self,
        task_id: str,
        *,
        status: str,
        error: str | None,
        finished_at: float,
    ) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET last_run_at = ?, last_status = ?, last_error = ?, "
                "run_count = run_count + 1, updated_at = ? WHERE id = ?",
                (float(finished_at), str(status), error, float(finished_at), str(task_id)),
            )

    def latest_run(self, chat_id: str, task_id: str | None = None) -> dict | None:
        query = (
            "SELECT r.* FROM runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE t.chat_id = ?"
        )
        params: list = [str(chat_id)]
        if task_id:
            query += " AND r.task_id = ?"
            params.append(str(task_id))
        query += " ORDER BY r.id DESC LIMIT 1"
        with self._db.connection() as connection:
            row = connection.execute(query, params).fetchone()
        return _run_dict(row) if row is not None else None

    def run_count(self, task_id: str) -> int:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM runs WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0
