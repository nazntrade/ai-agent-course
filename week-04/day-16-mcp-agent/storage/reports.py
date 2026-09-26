"""SQL repository of saved reports (owned by the MCP server).

The MCP server writes ``reports``; the backend only reads them for the
``Saved reports`` panel and deletes them through the chat cascade. A report
belongs to exactly one chat and stores a plain-text summary plus the sanitized
sources as JSON. The repository parses that JSON defensively: a corrupted row
degrades to an empty source list instead of crashing the API.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from storage.db import Database

MAX_REPORTS_PER_CHAT = 100


def new_report_id() -> str:
    """Return a fresh opaque report identifier."""
    return uuid.uuid4().hex[:12]


def _parse_sources(raw) -> list:
    """Return the stored sources, or ``[]`` when the JSON is unusable."""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _report_dict(row) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "topic": row["topic"],
        "summary": row["summary"],
        "sources": _parse_sources(row["sources_json"]),
        "digest_id": row["digest_id"],
        "source_count": int(row["source_count"]),
        "created_at": float(row["created_at"]),
    }


class ReportRepository:
    """Reads and writes ``reports`` rows."""

    def __init__(self, database: Database):
        self._db = database

    def insert_report(
        self,
        chat_id: str,
        *,
        topic: str,
        summary: str,
        sources: list,
        digest_id: str,
        created_at: float | None = None,
        report_id: str | None = None,
    ) -> dict:
        """Insert one report and prune the oldest ones beyond the chat cap."""
        moment = float(created_at if created_at is not None else time.time())
        identifier = str(report_id) if report_id else new_report_id()
        payload = json.dumps(
            [item for item in (sources or []) if isinstance(item, dict)],
            ensure_ascii=False,
        )
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO reports(id, chat_id, topic, summary, sources_json, "
                "digest_id, source_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    str(chat_id),
                    str(topic),
                    str(summary),
                    payload,
                    str(digest_id),
                    int(len(sources or [])),
                    moment,
                ),
            )
            connection.execute(
                "DELETE FROM reports WHERE chat_id = ? AND id NOT IN ("
                "SELECT id FROM reports WHERE chat_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (str(chat_id), str(chat_id), MAX_REPORTS_PER_CHAT),
            )
        return self.get_report(chat_id, identifier) or {}

    def get_report(self, chat_id: str, report_id: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM reports WHERE chat_id = ? AND id = ?",
                (str(chat_id), str(report_id)),
            ).fetchone()
        return _report_dict(row) if row is not None else None

    def list_reports(self, chat_id: str) -> list:
        """Return the reports of one chat, newest first."""
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE chat_id = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (str(chat_id),),
            ).fetchall()
        return [_report_dict(row) for row in rows]

    def count_reports(self, chat_id: str) -> int:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM reports WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0
