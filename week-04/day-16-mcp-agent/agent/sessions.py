"""In-memory chat history of one session.

There is no database: a session lives for the lifetime of the backend process
and is serialized by its own ``asyncio.Lock`` so two requests of the same
session never interleave. The store is bounded, so a long-running process does
not grow without limit.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

MAX_SESSIONS = 200
MAX_MESSAGES_PER_SESSION = 40


@dataclass
class Session:
    """One chat session: its message history and its serialization lock."""

    session_id: str
    messages: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def _trim(self) -> None:
        if len(self.messages) > MAX_MESSAGES_PER_SESSION:
            self.messages = self.messages[-MAX_MESSAGES_PER_SESSION:]

    def append_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": str(text)})
        self.updated_at = time.time()
        self._trim()

    def append_assistant(self, text: str) -> None:
        if not text:
            return
        self.messages.append({"role": "assistant", "content": str(text)})
        self.updated_at = time.time()
        self._trim()

    def append_tool(self, tool_call_id: str, text: str) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": str(tool_call_id),
                "content": str(text),
            }
        )
        self.updated_at = time.time()
        self._trim()

    def pop_last_user(self) -> None:
        """Undo the user turn of a failed request so a retry stays clean."""
        if self.messages and self.messages[-1].get("role") == "user":
            self.messages.pop()


class SessionStore:
    """A bounded, thread-safe registry of sessions."""

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        max_messages: int = MAX_MESSAGES_PER_SESSION,
    ):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._max_sessions = max(int(max_sessions), 1)
        self._max_messages = max(int(max_messages), 1)

    def get(self, session_id: str) -> Session:
        """Return the session, creating it on first use."""
        key = str(session_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = Session(session_id=key)
                self._sessions[key] = session
                self._evict_locked()
            return session

    def drop(self, session_id: str) -> None:
        """Forget a session (used by tests and diagnostics)."""
        with self._lock:
            self._sessions.pop(str(session_id), None)

    def ids(self) -> list:
        """Return the known session ids."""
        with self._lock:
            return list(self._sessions)

    def _evict_locked(self) -> None:
        if len(self._sessions) <= self._max_sessions:
            return
        ordered = sorted(
            self._sessions.values(), key=lambda item: item.updated_at
        )
        for session in ordered[: len(self._sessions) - self._max_sessions]:
            self._sessions.pop(session.session_id, None)
