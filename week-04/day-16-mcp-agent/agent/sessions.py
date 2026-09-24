"""Per-chat serialization of chat requests.

Chat history is persisted in SQLite, so a session no longer stores messages: it
only carries the ``chat_id``, the per-chat :class:`asyncio.Lock` that keeps two
requests of one chat from interleaving, and the callbacks the orchestrator uses
to load the context window and to persist a successful turn.

The registry hands out one shared lock per chat id. Locks are cheap and bounded
by the number of chats, so they are not evicted.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable


class ChatSession:
    """One request against one chat: its lock, its window and its callbacks."""

    def __init__(
        self,
        chat_id: str,
        *,
        lock: asyncio.Lock | None = None,
        history: list | None = None,
        history_provider: Callable[[], list] | None = None,
        on_success: Callable[[str, str], None] | None = None,
    ):
        self.chat_id = str(chat_id)
        self.lock = lock if lock is not None else asyncio.Lock()
        self._static_history = list(history or [])
        self._history_provider = history_provider
        self._on_success = on_success

    def history(self) -> list:
        """The context window for this request, loaded under the chat lock."""
        if self._history_provider is not None:
            return list(self._history_provider())
        return list(self._static_history)

    def record_success(self, user_text: str, assistant_text: str) -> None:
        """Persist a finished turn; a failed turn is never written."""
        if self._on_success is not None:
            self._on_success(str(user_text), str(assistant_text))


class SessionRegistry:
    """A thread-safe registry of one lock per chat id."""

    def __init__(self, *, max_sessions: int = 200):
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = threading.Lock()
        self._max_sessions = max(int(max_sessions), 1)

    def lock_for(self, chat_id: str) -> asyncio.Lock:
        key = str(chat_id)
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def session(self, chat_id: str, **kwargs) -> ChatSession:
        """Build a request session sharing the chat's lock."""
        return ChatSession(chat_id, lock=self.lock_for(chat_id), **kwargs)

    def ids(self) -> list:
        with self._guard:
            return list(self._locks)
