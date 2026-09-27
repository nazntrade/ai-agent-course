"""Repositories of the notifier database (server B).

The repositories contain no business rules: limits, criteria and delivery
statuses are decided by :mod:`notifier_server.watches`. They only translate
between the SQLite rows of ``watches``, ``seen_items`` and ``deliveries`` and
plain dictionaries, and they make the idempotent delivery write atomic.

The delivery write is the only subtle part. ``(watch_id, kind, period_key)`` is
unique: a terminal ``sent`` row is never overwritten, while a ``failed`` or
``not_configured`` retry updates the same row (``attempts`` grows) instead of
creating a second one, so a temporary failure can never block the retry
forever.
"""

from __future__ import annotations

import json
import time
import uuid

from notifier_server.db import Database


def new_watch_id() -> str:
    """Return a fresh opaque watch identifier."""
    return uuid.uuid4().hex[:12]


def new_delivery_id() -> str:
    """Return a fresh opaque delivery identifier."""
    return "d20-" + uuid.uuid4().hex[:16]


def _load_string_list(raw) -> list:
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _load_items(raw) -> list:
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    items: list = []
    for item in parsed:
        if isinstance(item, dict):
            items.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                }
            )
    return items


def _watch_dict(row) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "query": row["query"],
        "keywords": _load_string_list(row["keywords_json"]),
        "exclude": _load_string_list(row["exclude_json"]),
        "interval_seconds": int(row["interval_seconds"]),
        "summary_interval_seconds": int(row["summary_interval_seconds"]),
        "source_task_id": row["source_task_id"],
        "status": row["status"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "next_check_at": float(row["next_check_at"]),
        "last_check_at": (
            None if row["last_check_at"] is None else float(row["last_check_at"])
        ),
        "last_delivery_status": row["last_delivery_status"],
    }


def _delivery_dict(row) -> dict:
    items = _load_items(row["items_json"])
    return {
        "id": row["id"],
        "watch_id": row["watch_id"],
        "chat_id": row["chat_id"],
        "kind": row["kind"],
        "period_key": row["period_key"],
        "status": row["status"],
        "items": items,
        "items_count": len(items),
        "error": row["error"],
        "attempts": int(row["attempts"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


class WatchRepository:
    """Reads and writes ``watches`` rows."""

    def __init__(self, database: Database):
        self._db = database

    def create_watch(
        self,
        chat_id: str,
        query: str,
        keywords,
        exclude,
        interval_seconds: int,
        summary_interval_seconds: int,
        *,
        source_task_id: str = "",
        now: float | None = None,
        next_check_at: float | None = None,
        status: str = "active",
        watch_id: str | None = None,
    ) -> dict:
        moment = float(now if now is not None else time.time())
        first_check = float(next_check_at if next_check_at is not None else moment)
        identifier = str(watch_id) if watch_id else new_watch_id()
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO watches(id, chat_id, query, keywords_json, "
                "exclude_json, interval_seconds, summary_interval_seconds, "
                "source_task_id, status, created_at, updated_at, next_check_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    str(chat_id),
                    str(query),
                    json.dumps([str(item) for item in keywords], ensure_ascii=False),
                    json.dumps([str(item) for item in exclude], ensure_ascii=False),
                    int(interval_seconds),
                    int(summary_interval_seconds),
                    str(source_task_id),
                    str(status),
                    moment,
                    moment,
                    first_check,
                ),
            )
        return self.get_watch(identifier)

    def get_watch(self, watch_id: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM watches WHERE id = ?", (str(watch_id),)
            ).fetchone()
        return _watch_dict(row) if row is not None else None

    def list_watches(self, chat_id: str) -> list:
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM watches WHERE chat_id = ? ORDER BY created_at, id",
                (str(chat_id),),
            ).fetchall()
        return [_watch_dict(row) for row in rows]

    def advance_schedule(
        self,
        watch_id: str,
        *,
        now: float,
        next_check_at: float,
        last_check_at: float | None = None,
    ) -> None:
        """Move the schedule forward.

        ``last_check_at`` is only written when given: a run that did not yield a
        trustworthy snapshot (``pending``/``error``) moves ``next_check_at`` but
        keeps ``last_check_at`` empty, which is how the baseline contract tells
        "never checked" from "checked without a usable run".
        """
        with self._db.transaction() as connection:
            if last_check_at is None:
                connection.execute(
                    "UPDATE watches SET next_check_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (float(next_check_at), float(now), str(watch_id)),
                )
            else:
                connection.execute(
                    "UPDATE watches SET last_check_at = ?, next_check_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        float(last_check_at),
                        float(next_check_at),
                        float(now),
                        str(watch_id),
                    ),
                )

    def stop_watch(self, watch_id: str, *, now: float) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE watches SET status = 'stopped', updated_at = ? "
                "WHERE id = ? AND status = 'active'",
                (float(now), str(watch_id)),
            )
            return cursor.rowcount == 1

    def set_last_delivery_status(
        self, watch_id: str, status: str, *, now: float
    ) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE watches SET last_delivery_status = ?, updated_at = ? "
                "WHERE id = ?",
                (str(status), float(now), str(watch_id)),
            )


class SeenItemRepository:
    """Reads and writes the per-watch seen fingerprints."""

    def __init__(self, database: Database):
        self._db = database

    def add_seen_many(self, watch_id: str, records, *, now: float) -> None:
        """Insert ``(fingerprint, title, url)`` records, ignoring duplicates."""
        rows = [
            (str(watch_id), str(fingerprint), str(title), str(url), float(now))
            for fingerprint, title, url in records
        ]
        if not rows:
            return
        with self._db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO seen_items"
                "(watch_id, fingerprint, title, url, seen_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def fingerprints(self, watch_id: str) -> set:
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT fingerprint FROM seen_items WHERE watch_id = ?",
                (str(watch_id),),
            ).fetchall()
        return {row["fingerprint"] for row in rows}

    def contains(self, watch_id: str, fingerprint: str) -> bool:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM seen_items WHERE watch_id = ? AND fingerprint = ?",
                (str(watch_id), str(fingerprint)),
            ).fetchone()
        return row is not None

    def count(self, watch_id: str) -> int:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM seen_items WHERE watch_id = ?",
                (str(watch_id),),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def items(self, watch_id: str) -> list:
        with self._db.connection() as connection:
            rows = connection.execute(
                "SELECT fingerprint, title, url, seen_at FROM seen_items "
                "WHERE watch_id = ? ORDER BY seen_at, fingerprint",
                (str(watch_id),),
            ).fetchall()
        return [
            {
                "fingerprint": row["fingerprint"],
                "title": row["title"],
                "url": row["url"],
                "seen_at": float(row["seen_at"]),
            }
            for row in rows
        ]


class DeliveryRepository:
    """Reads and writes ``deliveries`` rows with an idempotent upsert."""

    def __init__(self, database: Database):
        self._db = database

    def find(self, watch_id: str, kind: str, period_key: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE watch_id = ? AND kind = ? "
                "AND period_key = ?",
                (str(watch_id), str(kind), str(period_key)),
            ).fetchone()
        return _delivery_dict(row) if row is not None else None

    def upsert(
        self,
        watch_id: str,
        chat_id: str,
        kind: str,
        period_key: str,
        *,
        status: str,
        items,
        error: str | None = None,
        now: float | None = None,
        delivery_id: str | None = None,
    ) -> dict:
        """Insert one delivery or update the existing row for the same key.

        The unique key ``(watch_id, kind, period_key)`` is preserved: a retry
        increments ``attempts`` and refreshes ``updated_at`` on the existing row
        instead of inserting a duplicate.
        """
        moment = float(now if now is not None else time.time())
        payload = json.dumps(list(items or []), ensure_ascii=False)
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM deliveries WHERE watch_id = ? AND kind = ? "
                "AND period_key = ?",
                (str(watch_id), str(kind), str(period_key)),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE deliveries SET status = ?, items_json = ?, error = ?, "
                    "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (
                        str(status),
                        payload,
                        error,
                        moment,
                        existing["id"],
                    ),
                )
                identifier = existing["id"]
            else:
                identifier = str(delivery_id) if delivery_id else new_delivery_id()
                connection.execute(
                    "INSERT INTO deliveries(id, watch_id, chat_id, kind, "
                    "period_key, status, items_json, error, attempts, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        identifier,
                        str(watch_id),
                        str(chat_id),
                        str(kind),
                        str(period_key),
                        str(status),
                        payload,
                        error,
                        moment,
                        moment,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?", (identifier,)
            ).fetchone()
        return _delivery_dict(row)

    def list_deliveries(
        self,
        *,
        watch_id: str | None = None,
        chat_id: str | None = None,
        limit: int = 20,
    ) -> list:
        query = "SELECT * FROM deliveries"
        filters: list = []
        params: list = []
        if watch_id:
            filters.append("watch_id = ?")
            params.append(str(watch_id))
        if chat_id:
            filters.append("chat_id = ?")
            params.append(str(chat_id))
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(int(limit), 1))
        with self._db.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_delivery_dict(row) for row in rows]

    def last_for_watch(self, watch_id: str) -> dict | None:
        with self._db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE watch_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (str(watch_id),),
            ).fetchone()
        return _delivery_dict(row) if row is not None else None


__all__ = [
    "WatchRepository",
    "SeenItemRepository",
    "DeliveryRepository",
    "new_watch_id",
    "new_delivery_id",
]
