"""Scheduled-search service used by the MCP tools and the scheduler.

A task belongs to exactly one chat and stores a query and an interval. The
service validates arguments and limits, persists tasks and runs through
``storage.tasks`` and runs the search through the same
:class:`mcp_server.web_search.WebSearchService` the ``search_web`` tool uses,
so the search API key stays inside the MCP server process.

The service only writes ``tasks`` and ``runs`` and only reads ``chats`` to
validate a ``chat_id``; it never touches ``messages``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import web_search
from mcp_server.config import resolve_db_path
from storage.chats import ChatRepository
from storage.db import Database
from storage.tasks import TaskRepository

MAX_TASKS_PER_CHAT = 3
MAX_TASKS_TOTAL = 10
TASK_MIN_INTERVAL_SECONDS = 60
TASK_MAX_INTERVAL_SECONDS = 2592000  # 30 days

NEEDS_CHAT_MESSAGE = "This tool needs an active chat context"
UNKNOWN_TASK_MESSAGE = "No scheduled task with this id exists in the current chat"
NO_TASKS_NOTE = "No scheduled tasks in this chat."
PENDING_NOTE = "The first run has not finished yet."
ALREADY_STOPPED_NOTE = "This task was already stopped."
STOPPED_NOTE = "No further runs will start."


def _iso(timestamp) -> str | None:
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_query(query) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("Argument 'query' must be a non-empty string")
    text = " ".join(query.split())
    if len(text) > web_search.MAX_QUERY_LENGTH:
        text = text[: web_search.MAX_QUERY_LENGTH]
    return text


def _normalize_interval(interval_seconds) -> int:
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise ToolError("Argument 'interval_seconds' must be an integer")
    if interval_seconds < TASK_MIN_INTERVAL_SECONDS:
        raise ToolError(
            f"The minimum interval is {TASK_MIN_INTERVAL_SECONDS} seconds; "
            "use search_web for a one-off search"
        )
    return min(interval_seconds, TASK_MAX_INTERVAL_SECONDS)


def _normalize_max_results(value) -> int:
    """Return the per-run result limit, or raise a controlled error.

    ``0`` keeps the server default (resolved by the search service at run
    time); a positive value is capped at the shared search cap.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("Argument 'max_results' must be an integer")
    if value < 0:
        raise ToolError(
            "Argument 'max_results' must not be negative; use 0 for the "
            "server default"
        )
    if value > web_search.SEARCH_MAX_RESULTS_CAP:
        return web_search.SEARCH_MAX_RESULTS_CAP
    return value


class TaskService:
    """Business rules and persistence of scheduled searches."""

    def __init__(self, database: Database, *, search=None, clock=time.time):
        self._database = database
        self._repository = TaskRepository(database)
        self._chats = ChatRepository(database)
        self._search = search
        self._clock = clock

    # -- tools -------------------------------------------------------------

    def create_task(
        self, chat_id: str, query: str, interval_seconds, max_results=0
    ) -> dict:
        chat_key = self._require_chat(chat_id)
        text = _normalize_query(query)
        interval = _normalize_interval(interval_seconds)
        limit = _normalize_max_results(max_results)
        if not self._search_service().configured:
            raise ToolError(web_search.NOT_CONFIGURED_MESSAGE)

        if self._repository.count_active_tasks(chat_key) >= MAX_TASKS_PER_CHAT:
            raise ToolError(
                f"This chat already has {MAX_TASKS_PER_CHAT} scheduled tasks. "
                "Stop one before creating another"
            )
        if self._repository.count_active_total() >= MAX_TASKS_TOTAL:
            raise ToolError(
                f"The server already has {MAX_TASKS_TOTAL} scheduled tasks. "
                "Stop one before creating another"
            )

        existing = self._repository.find_active_task(chat_key, text)
        if existing is not None:
            return self._created_payload(existing, created=False)

        now = float(self._clock())
        task = self._repository.create_task(
            chat_key, text, interval, max_results=limit, now=now, next_run_at=now
        )
        return self._created_payload(task, created=True)

    def list_tasks(self, chat_id: str) -> dict:
        chat_key = self._require_chat(chat_id)
        tasks = [self._list_item(task) for task in self._repository.list_tasks(chat_key)]
        if not tasks:
            return {"count": 0, "tasks": [], "note": NO_TASKS_NOTE}
        return {"count": len(tasks), "tasks": tasks}

    def get_latest_run(self, chat_id: str, task_id: str = "") -> dict:
        chat_key = self._require_chat(chat_id)
        wanted = str(task_id or "").strip()
        if wanted:
            task = self._repository.get_task(wanted)
            if task is None or task["chat_id"] != chat_key:
                raise ToolError(UNKNOWN_TASK_MESSAGE)
        run = self._repository.latest_run(chat_key, wanted or None)
        if run is None:
            return {"status": "pending", "note": PENDING_NOTE}
        return self._run_payload(run)

    def stop_task(self, chat_id: str, task_id: str) -> dict:
        chat_key = self._require_chat(chat_id)
        wanted = str(task_id or "").strip()
        task = self._repository.get_task(wanted) if wanted else None
        if task is None or task["chat_id"] != chat_key:
            raise ToolError(UNKNOWN_TASK_MESSAGE)

        if task["status"] == "stopped":
            return {
                "task_id": task["id"],
                "status": "stopped",
                "stopped_at": _iso(task["stopped_at"] or task["updated_at"]),
                "note": ALREADY_STOPPED_NOTE,
            }

        now = float(self._clock())
        self._repository.stop_task(task["id"], now=now)
        return {
            "task_id": task["id"],
            "status": "stopped",
            "stopped_at": _iso(now),
            "note": STOPPED_NOTE,
        }

    # -- scheduler ---------------------------------------------------------

    def claim_due_tasks(self, *, now=None, limit: int = 5) -> list:
        """CAS-claim every due task and return the tasks won by this process."""
        moment = float(self._clock() if now is None else now)
        claimed = []
        for task in self._repository.due_tasks(moment, limit):
            if self._repository.claim_task(
                task["id"], now=moment, expected_next_run_at=task["next_run_at"]
            ):
                claimed.append(task)
        return claimed

    def execute_claimed(self, task: dict) -> dict | None:
        """Run one claimed task and persist its outcome.

        The run row is only written while the task still exists, so a task that
        was deleted meanwhile is never recreated and produces no result.
        """
        search = self._search_service()
        started = float(self._clock())
        status = "error"
        result: dict | None = None
        error: str | None = None
        count = 0
        try:
            limit = int(task.get("max_results") or 0)
            payload = search.search(task["query"], limit)
            count = int(payload.get("count") or 0)
            status = "ok" if count > 0 else "empty"
            result = payload
        except web_search.SearchError as exc:
            status = "error"
            error = exc.message
        finished = float(self._clock())

        run_id = self._repository.record_run(
            task["id"],
            started_at=started,
            finished_at=finished,
            status=status,
            result=result,
            result_count=count,
            error=error,
        )
        if run_id is None:
            return None
        self._repository.update_after_run(
            task["id"], status=status, error=error, finished_at=finished
        )
        return {"task_id": task["id"], "status": status, "result_count": count}

    # -- internals ---------------------------------------------------------

    def _search_service(self):
        if self._search is not None:
            return self._search
        return web_search.default_service()

    def _require_chat(self, chat_id) -> str:
        key = str(chat_id or "").strip()
        if not key:
            raise ToolError(NEEDS_CHAT_MESSAGE)
        if self._chats.get_chat(key) is None:
            raise ToolError(NEEDS_CHAT_MESSAGE)
        return key

    def _created_payload(self, task: dict, *, created: bool) -> dict:
        if created:
            note = (
                "The first run starts within a few seconds; later runs repeat "
                f"every {task['interval_seconds']} seconds."
            )
        else:
            note = "An active task with the same query already exists in this chat."
        return {
            "task_id": task["id"],
            "status": task["status"],
            "query": task["query"],
            "interval_seconds": task["interval_seconds"],
            "max_results": int(task.get("max_results") or 0),
            "created_at": _iso(task["created_at"]),
            "next_run_at": _iso(task["next_run_at"]),
            "created": created,
            "note": note,
        }

    def _list_item(self, task: dict) -> dict:
        return {
            "task_id": task["id"],
            "query": task["query"],
            "interval_seconds": task["interval_seconds"],
            "max_results": int(task.get("max_results") or 0),
            "status": task["status"],
            "created_at": _iso(task["created_at"]),
            "next_run_at": _iso(task["next_run_at"]),
            "last_run_at": _iso(task["last_run_at"]),
            "last_status": task["last_status"],
            "last_error": task["last_error"],
            "run_count": task["run_count"],
        }

    def _run_payload(self, run: dict) -> dict:
        if run["status"] == "error":
            return {
                "status": "error",
                "task_id": run["task_id"],
                "result_count": 0,
                "results": [],
                "error": run["error"],
            }
        result = run.get("result") or {}
        results = result.get("results") if isinstance(result, dict) else None
        note = result.get("note") if isinstance(result, dict) else None
        if not note:
            note = (
                web_search.SEARCH_NOTE
                if run["status"] == "ok"
                else web_search.EMPTY_NOTE
            )
        return {
            "status": run["status"],
            "task_id": run["task_id"],
            "result_count": run["result_count"],
            "results": results if isinstance(results, list) else [],
            "note": note,
        }


# The service is built lazily, on the first tool call or scheduler tick, so
# importing the tools and serving ``tools/list`` never opens the database.
_DEFAULT_SERVICE: TaskService | None = None


def default_task_service() -> TaskService:
    """Return the process-wide service, built lazily from the environment."""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = TaskService(Database(resolve_db_path()))
    return _DEFAULT_SERVICE


def reset_default_task_service() -> None:
    """Drop the cached service (tests and diagnostics)."""
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = None
