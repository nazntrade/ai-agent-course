"""JSONL trace of the agent request path.

One line is one event, so the file can be parsed and checked as an ordered
subsequence per ``request_id``. Every payload is sanitized before writing:

* known secret-bearing keys are dropped;
* tool arguments are limited to atomic allow-listed primitives;
* strings are truncated.

The trace is a diagnostics artifact: it must never contain an API key, an
``Authorization`` header, an environment variable, the system prompt or an
absolute filesystem path.

The full user message is **not** protected by a key name: the caller must never
pass it. ``request_start`` carries ``message_chars`` (the length) instead, which
:mod:`agent.orchestrator` is the only place allowed to write. ``message`` and
``content`` stay writable because ``request_error`` and ``tool_completed`` use
them for our own already-sanitized values.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

SECRET_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "api-key",
        "token",
        "access_token",
        "secret",
        "password",
        "headers",
        "prompt",
        "system_prompt",
        "base_url",
        "url",
        "path",
    }
)

MAX_STRING_LENGTH = 200
MAX_ITEMS = 32
MAX_DEPTH = 3


def _truncate(value: str) -> str:
    text = str(value)
    if len(text) <= MAX_STRING_LENGTH:
        return text
    return text[: MAX_STRING_LENGTH - 3] + "..."


def sanitize_value(value, depth: int = 0):
    """Return an allow-listed, truncated copy of ``value``."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value)
    if depth >= MAX_DEPTH:
        return "<omitted>"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                break
            name = str(key)
            if name.strip().lower() in SECRET_KEYS:
                continue
            result[name] = sanitize_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_ITEMS]
        return [sanitize_value(item, depth + 1) for item in items]
    return _truncate(repr(value))


def sanitize(payload: dict) -> dict:
    """Sanitize a whole event payload."""
    return sanitize_value(dict(payload or {}), 0)


class TraceWriter:
    """Append sanitized JSONL events to a file."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        """Whether events are actually written."""
        return self.path is not None

    def write(self, event: str, **fields) -> None:
        """Append one sanitized event; tracing failures never break a request."""
        if self.path is None:
            return
        record = {"ts": round(time.time(), 3), "event": str(event)}
        try:
            record.update(sanitize(fields))
        except Exception:  # pragma: no cover - sanitizing is total by design
            record["sanitize_error"] = True
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            return

    def close(self) -> None:
        """Kept for symmetry; the writer holds no open handle."""
        return None


class NullTraceWriter(TraceWriter):
    """A trace writer that discards every event."""

    def __init__(self):  # noqa: D107 - trivial
        super().__init__(None)
