"""Chat service of the backend: limits, titles and HTTP-facing chat views.

The service owns the business rules the SQL repository deliberately does not:
at most :data:`MAX_CHATS` chats, title normalization, the automatic first-message
title and the refusal to delete a chat with active scheduled tasks without an
explicit ``force``. It also builds the ``TaskInfo`` view the UI reads; the
writes to tasks and runs stay with the MCP server.
"""

from __future__ import annotations

import time

from storage.chats import ChatRepository
from storage.reports import ReportRepository
from storage.tasks import TaskRepository

MAX_CHATS = 5
MAX_TITLE_LENGTH = 80
AUTO_TITLE_LENGTH = 60
DEFAULT_TITLE = "New chat"

CHAT_NOT_FOUND = "chat_not_found"
CHAT_LIMIT = "chat_limit"
CHAT_HAS_ACTIVE_TASKS = "chat_has_active_tasks"
INVALID_TITLE = "invalid_title"
CHAT_STORAGE_UNAVAILABLE = "chat_storage_unavailable"
REPORT_NOT_FOUND = "report_not_found"

ERROR_STATUS = {
    CHAT_NOT_FOUND: 404,
    CHAT_LIMIT: 409,
    CHAT_HAS_ACTIVE_TASKS: 409,
    INVALID_TITLE: 422,
    CHAT_STORAGE_UNAVAILABLE: 503,
    REPORT_NOT_FOUND: 404,
}

NOT_FOUND_MESSAGE = "The chat was not found"
REPORT_NOT_FOUND_MESSAGE = "The report was not found"


class ChatError(Exception):
    """A controlled chat failure with an HTTP error category.

    ``extra`` carries the structured details of a category (for example the
    active task count of ``chat_has_active_tasks``).
    """

    def __init__(self, category: str, message: str, **extra):
        super().__init__(message)
        self.category = str(category)
        self.message = str(message)
        self.extra = dict(extra)

    @property
    def status_code(self) -> int:
        return ERROR_STATUS.get(self.category, 500)


def _normalize_text(value) -> str:
    return " ".join(str(value or "").split())


def _auto_title(text: str) -> str:
    normalized = _normalize_text(text)
    return normalized[:AUTO_TITLE_LENGTH] or DEFAULT_TITLE


class ChatService:
    """CRUD for chats plus the UI-facing message and task views."""

    def __init__(self, database, *, max_chats: int = MAX_CHATS, clock=time.time):
        self._database = database
        self._chats = ChatRepository(database)
        self._tasks = TaskRepository(database)
        self._reports = ReportRepository(database)
        self._max_chats = max(int(max_chats), 1)
        self._clock = clock

    # -- chats -------------------------------------------------------------

    def list_chats(self) -> list:
        return [self._chat_info(row) for row in self._chats.list_chats()]

    def create_chat(self, title: str = "") -> dict:
        if self._chats.count_chats() >= self._max_chats:
            raise ChatError(
                CHAT_LIMIT,
                f"The limit of {self._max_chats} chats is reached. "
                "Delete a chat to create a new one.",
            )
        normalized = _normalize_text(title)[:MAX_TITLE_LENGTH]
        row = self._chats.create_chat(normalized or DEFAULT_TITLE, now=self._clock())
        return self._chat_info(row)

    def rename_chat(self, chat_id: str, title: str) -> dict:
        row = self._chats.get_chat(chat_id)
        if row is None:
            raise ChatError(CHAT_NOT_FOUND, NOT_FOUND_MESSAGE)
        normalized = _normalize_text(title)
        if not normalized:
            raise ChatError(INVALID_TITLE, "The chat title must not be empty")
        row = self._chats.rename_chat(
            chat_id, normalized[:MAX_TITLE_LENGTH], now=self._clock()
        )
        return self._chat_info(row)

    def delete_chat(self, chat_id: str, *, force: bool = False) -> dict:
        row = self._chats.get_chat(chat_id)
        if row is None:
            raise ChatError(CHAT_NOT_FOUND, NOT_FOUND_MESSAGE)
        active = self._tasks.count_active_tasks(chat_id)
        if active and not force:
            raise ChatError(
                CHAT_HAS_ACTIVE_TASKS,
                f"This chat has {active} active scheduled task(s). "
                "Delete it anyway to stop them.",
                active_tasks=active,
            )
        self._chats.delete_chat(chat_id)
        return {"deleted": True, "stopped_tasks": active}

    def chat_exists(self, chat_id: str) -> bool:
        return self._chats.get_chat(chat_id) is not None

    # -- messages ----------------------------------------------------------

    def get_messages(self, chat_id: str) -> list:
        self._require_chat(chat_id)
        return self._chats.list_messages(chat_id)

    def clear_chat(self, chat_id: str) -> dict:
        self._require_chat(chat_id)
        deleted = self._chats.clear_messages(chat_id)
        return {"cleared": True, "messages_deleted": deleted}

    def context_messages(self, chat_id: str, limit: int) -> list:
        """The last ``limit`` messages of one chat, in chronological order."""
        self._require_chat(chat_id)
        return self._chats.recent_messages(chat_id, limit)

    def append_exchange(self, chat_id: str, user_text: str, assistant_text: str) -> None:
        """Persist a successful turn and set the automatic title on first use."""
        row = self._chats.get_chat(chat_id)
        if row is None:
            raise ChatError(CHAT_NOT_FOUND, NOT_FOUND_MESSAGE)
        now = self._clock()
        if row["title"] == DEFAULT_TITLE and _normalize_text(user_text):
            self._chats.rename_chat(chat_id, _auto_title(user_text), now=now)
        self._chats.add_exchange(chat_id, user_text, assistant_text, now=now)

    # -- tasks -------------------------------------------------------------

    def tasks_for_chat(self, chat_id: str) -> list:
        self._require_chat(chat_id)
        tasks = []
        for task in self._tasks.list_tasks(chat_id):
            tasks.append(self._task_info(task))
        return tasks

    # -- reports -----------------------------------------------------------

    def reports_for_chat(self, chat_id: str) -> list:
        """The saved reports of one chat, newest first (read-only panel)."""
        self._require_chat(chat_id)
        return [self._report_info(report) for report in self._reports.list_reports(chat_id)]

    def report_for_chat(self, chat_id: str, report_id: str) -> dict:
        """One saved report of one chat, or ``report_not_found``."""
        self._require_chat(chat_id)
        report = self._reports.get_report(chat_id, report_id)
        if report is None:
            raise ChatError(REPORT_NOT_FOUND, REPORT_NOT_FOUND_MESSAGE)
        return self._report_detail(report)

    # -- internals ---------------------------------------------------------

    def _require_chat(self, chat_id: str) -> dict:
        row = self._chats.get_chat(chat_id)
        if row is None:
            raise ChatError(CHAT_NOT_FOUND, NOT_FOUND_MESSAGE)
        return row

    def _report_info(self, report: dict) -> dict:
        return {
            "report_id": report["id"],
            "topic": report["topic"],
            "created_at": report["created_at"],
            "source_count": report["source_count"],
        }

    def _report_detail(self, report: dict) -> dict:
        return {
            "report_id": report["id"],
            "chat_id": report["chat_id"],
            "topic": report["topic"],
            "summary": report["summary"],
            "created_at": report["created_at"],
            "source_count": report["source_count"],
            "sources": report["sources"],
        }

    def _chat_info(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": self._chats.message_count(row["id"]),
            "active_tasks_count": self._tasks.count_active_tasks(row["id"]),
        }

    def _task_info(self, task: dict) -> dict:
        run = self._tasks.latest_run(task["chat_id"], task["id"])
        last_run = None
        if run is not None:
            result = run.get("result") or {}
            results = result.get("results") if isinstance(result, dict) else None
            last_run = {
                "status": run["status"],
                "ran_at": run["finished_at"],
                "result_count": run["result_count"],
                "results": results if isinstance(results, list) else [],
                "error": run["error"],
            }
        return {
            "task_id": task["id"],
            "query": task["query"],
            "interval_seconds": task["interval_seconds"],
            "status": task["status"],
            "created_at": task["created_at"],
            "next_run_at": task["next_run_at"],
            "last_run_at": task["last_run_at"],
            "last_status": task["last_status"],
            "last_error": task["last_error"],
            "run_count": task["run_count"],
            "last_run": last_run,
        }
