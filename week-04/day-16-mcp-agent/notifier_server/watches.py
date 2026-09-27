"""Notification service: criteria, baseline, deduplication and delivery.

This is the business layer of server B. It decides which items of a server A
run match a watch, whether a run is the first (baseline) check, whether a
delivery is new or a duplicate, and retries a failed delivery on the same row.

The rules are deterministic and never trust the model's text:

* the criterion is explicit (``keywords``/``exclude``), stored and returned;
* the first run with a usable ``status`` (``ok``/``empty``) is the baseline and
  never sends historical items; ``pending``/``error`` runs do not consume it;
* ``send_notification`` is the only place that sends to Telegram and the only
  place that writes the seen set and a delivery row;
* ``duplicate`` is returned only for an existing ``sent`` row, so a failed or
  unconfigured attempt can be retried through the same row.

Fingerprints reuse :func:`mcp_server.reports.normalize_url`; the logic is not
duplicated here.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.reports import normalize_url

from notifier_server.config import resolve_db_path
from notifier_server.db import Database
from notifier_server.repository import (
    DeliveryRepository,
    SeenItemRepository,
    WatchRepository,
)
from notifier_server.telegram import (
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SENT,
    default_telegram_client,
)

# Matches day 18's ``TASK_MIN_INTERVAL_SECONDS``: a watch cannot be checked
# more often than once a minute.
MIN_INTERVAL_SECONDS = 60

RUN_STATUSES = frozenset({"ok", "empty", "error", "pending"})
DELIVERY_KINDS = frozenset({"new_items", "summary"})

NEEDS_CHAT_MESSAGE = "This tool needs an active chat context"
UNKNOWN_WATCH_MESSAGE = (
    "No notification watch with this id exists in the current chat"
)
STOPPED_WATCH_MESSAGE = (
    "This notification watch is stopped; start a new watch instead"
)
NO_WATCHES_NOTE = "No notification watches in this chat."
CREATED_NOTE = (
    "The first check runs immediately and treats the current results as the "
    "starting point."
)
BASELINE_NOTE = (
    "First check for this watch: matching items were recorded as seen and "
    "nothing was sent."
)
NEW_ITEMS_NOTE = "New matching items were found; send them with send_notification."
NO_NEW_ITEMS_NOTE = "No new matching items."
RUN_ERROR_NOTE = "The search run reported an error; there is nothing to notify."
RUN_PENDING_NOTE = "The search run has not finished yet."
MALFORMED_RUN_NOTE = "The search run is malformed; it was ignored."
UNKNOWN_WATCH_NOTE = "No notification watch with this id exists in the current chat."
ALREADY_STOPPED_NOTE = "This watch was already stopped."
STOPPED_NOTE = "No further checks will run for this watch."
NOT_REQUIRED_NOTE = "No items were supplied, so no delivery was needed."
DUPLICATE_NOTE = "This content was already delivered."
SENT_NOTE = "The notification was sent."
FAILED_NOTE = "The notification could not be sent."
NOT_CONFIGURED_NOTE = "Telegram is not configured; the notification was not sent."


def _iso(timestamp) -> str | None:
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _collapse(value) -> str:
    return " ".join(str(value or "").split())


def _normalize_query(query) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("Argument 'query' must be a non-empty string")
    return _collapse(query)


def _normalize_keywords(keywords) -> list:
    if not isinstance(keywords, list) or not keywords:
        raise ToolError("Argument 'keywords' must be a non-empty list of strings")
    cleaned: list = []
    for item in keywords:
        if not isinstance(item, str) or not item.strip():
            raise ToolError("Argument 'keywords' must be a non-empty list of strings")
        text = item.strip()
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _normalize_exclude(exclude) -> list:
    if exclude is None:
        return []
    if not isinstance(exclude, list):
        raise ToolError("Argument 'exclude' must be a list of strings")
    cleaned: list = []
    for item in exclude:
        if not isinstance(item, str) or not item.strip():
            raise ToolError("Argument 'exclude' must be a list of non-empty strings")
        text = item.strip()
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _normalize_interval(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"Argument '{name}' must be an integer")
    if value < MIN_INTERVAL_SECONDS:
        raise ToolError(
            f"Argument '{name}' must be at least {MIN_INTERVAL_SECONDS} seconds"
        )
    return value


def _normalize_items(items) -> list:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ToolError("Argument 'items' must be a list of items")
    cleaned: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _collapse(item.get("title"))
        url = str(item.get("url") or "").strip()
        if not title and not url:
            continue
        cleaned.append({"title": title, "url": url})
    return cleaned


def _parse_run(run):
    """Return ``(status, results)`` for a usable run, or ``None`` when malformed."""
    if not isinstance(run, dict):
        return None
    status = run.get("status")
    if not isinstance(status, str) or status not in RUN_STATUSES:
        return None
    if status in ("ok", "empty"):
        results = run.get("results")
        if not isinstance(results, list):
            return None
        return status, [item for item in results if isinstance(item, dict)]
    return status, []


def _matches(item: dict, keywords, exclude) -> bool:
    haystack = (
        str(item.get("title") or "") + "\n" + str(item.get("description") or "")
    ).lower()
    if not any(keyword.lower() in haystack for keyword in keywords):
        return False
    if any(term.lower() in haystack for term in exclude):
        return False
    return True


def _fingerprint(item: dict) -> str:
    canonical = normalize_url(item.get("url"))
    if canonical:
        return "url:" + canonical
    return "title:" + _collapse(item.get("title")).lower()


def _content_period_key(items) -> str:
    fingerprints = sorted({_fingerprint(item) for item in items})
    digest = hashlib.sha256("\n".join(fingerprints).encode("utf-8")).hexdigest()
    return "new_items:" + digest


class WatchService:
    """Business rules and persistence of notification watches."""

    def __init__(self, database: Database, *, telegram=None, clock=time.time):
        self._db = database
        self._watches = WatchRepository(database)
        self._seen = SeenItemRepository(database)
        self._deliveries = DeliveryRepository(database)
        self._telegram = telegram
        self._clock = clock

    # -- tools -------------------------------------------------------------

    def create_watch(
        self,
        chat_id: str,
        query: str,
        keywords,
        exclude=None,
        interval_seconds=0,
        summary_interval_seconds=0,
        source_task_id: str = "",
    ) -> dict:
        chat_key = self._require_chat(chat_id)
        text = _normalize_query(query)
        keyword_list = _normalize_keywords(keywords)
        exclude_list = _normalize_exclude(exclude)
        interval = _normalize_interval(interval_seconds, "interval_seconds")
        summary = _normalize_interval(
            summary_interval_seconds, "summary_interval_seconds"
        )

        now = float(self._clock())
        watch = self._watches.create_watch(
            chat_key,
            text,
            keyword_list,
            exclude_list,
            interval,
            summary,
            source_task_id=str(source_task_id or "").strip(),
            now=now,
            next_check_at=now,
        )
        return {
            "watch_id": watch["id"],
            "status": watch["status"],
            "query": watch["query"],
            "keywords": watch["keywords"],
            "exclude": watch["exclude"],
            "interval_seconds": watch["interval_seconds"],
            "summary_interval_seconds": watch["summary_interval_seconds"],
            "source_task_id": watch["source_task_id"],
            "created_at": _iso(watch["created_at"]),
            "next_check_at": _iso(watch["next_check_at"]),
            "note": CREATED_NOTE,
        }

    def list_watches(self, chat_id: str = "") -> dict:
        chat_key = self._require_chat(chat_id)
        watches = [self._watch_item(watch) for watch in self._watches.list_watches(chat_key)]
        if not watches:
            return {"count": 0, "watches": [], "note": NO_WATCHES_NOTE}
        return {"count": len(watches), "watches": watches}

    def evaluate_run(self, watch_id, run, chat_id: str = "") -> dict:
        """Apply the stored criterion to one server A run.

        This method never raises ``ToolError`` for user data: a malformed run
        and an unknown or foreign watch return a structured status instead.
        """
        watch = self._resolve_watch(watch_id, chat_id)
        if watch is None:
            return {
                "status": "unknown_watch",
                "new_items": [],
                "matched_count": 0,
                "known_count": 0,
                "is_baseline": False,
                "should_notify": False,
                "note": UNKNOWN_WATCH_NOTE,
            }

        parsed = _parse_run(run)
        if parsed is None:
            return {
                "status": "error",
                "new_items": [],
                "matched_count": 0,
                "known_count": 0,
                "is_baseline": False,
                "should_notify": False,
                "note": MALFORMED_RUN_NOTE,
            }

        status, results = parsed
        now = float(self._clock())
        interval = int(watch["interval_seconds"])

        if status in ("error", "pending"):
            # A run without a usable snapshot still advances the schedule, but it
            # must not consume the baseline: ``last_check_at`` stays empty so the
            # next trustworthy run is treated as the starting point again.
            self._watches.advance_schedule(
                watch["id"], now=now, next_check_at=now + interval
            )
            return {
                "status": status,
                "new_items": [],
                "matched_count": 0,
                "known_count": 0,
                "is_baseline": False,
                "should_notify": False,
                "note": RUN_ERROR_NOTE if status == "error" else RUN_PENDING_NOTE,
            }

        matched = [
            item
            for item in results
            if _matches(item, watch["keywords"], watch["exclude"])
        ]
        seen = self._seen.fingerprints(watch["id"])
        known = [item for item in matched if _fingerprint(item) in seen]
        # Duplicate URLs inside one run collapse to one item, so the same page is
        # never reported (or delivered) twice for a single run.
        fresh_by_fingerprint: dict = {}
        for item in matched:
            fingerprint = _fingerprint(item)
            if fingerprint in seen:
                continue
            fresh_by_fingerprint.setdefault(fingerprint, item)
        fresh = list(fresh_by_fingerprint.values())

        if watch["last_check_at"] is None:
            # Baseline: record every matching item as seen and send nothing.
            records = [
                (_fingerprint(item), _collapse(item.get("title")), item.get("url") or "")
                for item in matched
            ]
            self._seen.add_seen_many(watch["id"], records, now=now)
            self._watches.advance_schedule(
                watch["id"],
                now=now,
                next_check_at=now + interval,
                last_check_at=now,
            )
            return {
                "status": status,
                "new_items": [],
                "matched_count": len(matched),
                "known_count": len(known),
                "is_baseline": True,
                "should_notify": False,
                "note": BASELINE_NOTE,
            }

        should_notify = bool(fresh)
        if not should_notify:
            # Nothing to send: the check is complete, so the schedule moves now.
            # When something is new the schedule is left to ``send_notification``,
            # otherwise an incomplete turn could lose the notification.
            self._watches.advance_schedule(
                watch["id"],
                now=now,
                next_check_at=now + interval,
                last_check_at=now,
            )
        return {
            "status": status,
            "new_items": [
                {"title": _collapse(item.get("title")), "url": item.get("url") or ""}
                for item in fresh
            ],
            "matched_count": len(matched),
            "known_count": len(known),
            "is_baseline": False,
            "should_notify": should_notify,
            "note": NEW_ITEMS_NOTE if should_notify else NO_NEW_ITEMS_NOTE,
        }

    def send_notification(
        self,
        watch_id,
        chat_id: str = "",
        kind: str = "new_items",
        items=None,
    ) -> dict:
        """Send one notification and record its delivery and seen fingerprints.

        Idempotent on ``(watch_id, kind, period_key)``: a repeated content key
        with an existing ``sent`` row returns ``duplicate`` without a network
        call, while ``failed``/``not_configured`` retries update the same row.
        """
        watch = self._resolve_watch(watch_id, chat_id)
        if watch is None:
            raise ToolError(UNKNOWN_WATCH_MESSAGE)
        if watch["status"] == "stopped":
            raise ToolError(STOPPED_WATCH_MESSAGE)

        kind_key = str(kind or "new_items").strip()
        if kind_key not in DELIVERY_KINDS:
            raise ToolError(
                "Argument 'kind' must be 'new_items' or 'summary'"
            )

        normalized = _normalize_items(items)
        now = float(self._clock())

        if not normalized:
            self._move_schedule(watch, now)
            return {
                "delivery_id": None,
                "status": "not_required",
                "kind": kind_key,
                "period_key": None,
                "items_count": 0,
                "note": NOT_REQUIRED_NOTE,
            }

        if kind_key == "new_items":
            period_key = _content_period_key(normalized)
        else:
            period_key = self._summary_period_key(watch, now)

        existing = self._deliveries.find(watch["id"], kind_key, period_key)
        if existing is not None and existing["status"] == STATUS_SENT:
            self._move_schedule(watch, now)
            return {
                "delivery_id": existing["id"],
                "status": "duplicate",
                "kind": kind_key,
                "period_key": period_key,
                "items_count": existing["items_count"],
                "attempts": existing["attempts"],
                "note": DUPLICATE_NOTE,
            }

        message = self._build_message(watch, normalized)
        result = self._telegram_client().send_message(message)
        status = str(getattr(result, "status", STATUS_FAILED))
        error = getattr(result, "error", None)

        record = self._deliveries.upsert(
            watch["id"],
            watch["chat_id"],
            kind_key,
            period_key,
            status=status,
            items=normalized,
            error=error,
            now=now,
        )
        if status == STATUS_SENT:
            self._seen.add_seen_many(
                watch["id"],
                [
                    (_fingerprint(item), item["title"], item["url"])
                    for item in normalized
                ],
                now=now,
            )
        self._move_schedule(watch, now)
        self._watches.set_last_delivery_status(watch["id"], status, now=now)

        note = SENT_NOTE
        if status == STATUS_FAILED:
            note = FAILED_NOTE
        elif status == STATUS_NOT_CONFIGURED:
            note = NOT_CONFIGURED_NOTE
        return {
            "delivery_id": record["id"],
            "status": status,
            "kind": kind_key,
            "period_key": period_key,
            "items_count": record["items_count"],
            "attempts": record["attempts"],
            "error": record["error"],
            "note": note,
        }

    def get_delivery_status(self, watch_id: str = "", chat_id: str = "") -> dict:
        chat_key = self._require_chat(chat_id)
        wanted = str(watch_id or "").strip()
        if wanted:
            watch = self._watches.get_watch(wanted)
            if watch is None or watch["chat_id"] != chat_key:
                raise ToolError(UNKNOWN_WATCH_MESSAGE)
            deliveries = self._deliveries.list_deliveries(
                watch_id=wanted, limit=20
            )
        else:
            deliveries = self._deliveries.list_deliveries(
                chat_id=chat_key, limit=20
            )
        return {
            "count": len(deliveries),
            "deliveries": [self._delivery_payload(row) for row in deliveries],
        }

    def stop_watch(self, watch_id: str, chat_id: str = "") -> dict:
        chat_key = self._require_chat(chat_id)
        wanted = str(watch_id or "").strip()
        watch = self._watches.get_watch(wanted) if wanted else None
        if watch is None or watch["chat_id"] != chat_key:
            raise ToolError(UNKNOWN_WATCH_MESSAGE)

        if watch["status"] == "stopped":
            return {
                "watch_id": watch["id"],
                "status": "stopped",
                "stopped_at": _iso(watch["updated_at"]),
                "note": ALREADY_STOPPED_NOTE,
            }

        now = float(self._clock())
        self._watches.stop_watch(watch["id"], now=now)
        return {
            "watch_id": watch["id"],
            "status": "stopped",
            "stopped_at": _iso(now),
            "note": STOPPED_NOTE,
        }

    # -- internals ---------------------------------------------------------

    def _require_chat(self, chat_id) -> str:
        key = str(chat_id or "").strip()
        if not key:
            raise ToolError(NEEDS_CHAT_MESSAGE)
        return key

    def _resolve_watch(self, watch_id, chat_id) -> dict | None:
        wanted = str(watch_id or "").strip()
        if not wanted:
            return None
        watch = self._watches.get_watch(wanted)
        if watch is None:
            return None
        chat_key = str(chat_id or "").strip()
        if not chat_key or watch["chat_id"] != chat_key:
            return None
        return watch

    def _telegram_client(self):
        if self._telegram is None:
            self._telegram = default_telegram_client()
        return self._telegram

    def _move_schedule(self, watch: dict, now: float) -> None:
        self._watches.advance_schedule(
            watch["id"],
            now=now,
            next_check_at=now + int(watch["interval_seconds"]),
            last_check_at=now,
        )

    def _summary_period_key(self, watch: dict, now: float) -> str:
        interval = max(int(watch["summary_interval_seconds"]), 1)
        return "summary:" + str(int(now // interval))

    def _build_message(self, watch: dict, items) -> str:
        heading = f"{watch['query']}: {len(items)} new item(s)"
        lines = [heading]
        for item in items[:5]:
            label = item.get("title") or item.get("url") or ""
            if label:
                lines.append(f"- {label}")
            if item.get("url"):
                lines.append(item["url"])
        return "\n".join(lines)

    def _watch_item(self, watch: dict) -> dict:
        return {
            "watch_id": watch["id"],
            "query": watch["query"],
            "keywords": watch["keywords"],
            "exclude": watch["exclude"],
            "interval_seconds": watch["interval_seconds"],
            "summary_interval_seconds": watch["summary_interval_seconds"],
            "status": watch["status"],
            "created_at": _iso(watch["created_at"]),
            "next_check_at": _iso(watch["next_check_at"]),
            "last_check_at": _iso(watch["last_check_at"]),
            "seen_count": self._seen.count(watch["id"]),
            "last_delivery": self._delivery_payload(
                self._deliveries.last_for_watch(watch["id"])
            ),
        }

    def _delivery_payload(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        return {
            "delivery_id": row["id"],
            "watch_id": row["watch_id"],
            "kind": row["kind"],
            "status": row["status"],
            "period_key": row["period_key"],
            "items_count": row["items_count"],
            "error": row["error"],
            "attempts": row["attempts"],
            "created_at": _iso(row["created_at"]),
        }


# The service is built lazily, on the first tool call, so importing the tools
# and serving ``tools/list`` never opens the database.
_DEFAULT_SERVICE: WatchService | None = None


def default_watch_service() -> WatchService:
    """Return the process-wide service, built lazily from the environment."""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = WatchService(
            Database(resolve_db_path()), telegram=default_telegram_client()
        )
    return _DEFAULT_SERVICE


def reset_default_watch_service() -> None:
    """Drop the cached service (tests and diagnostics)."""
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = None
