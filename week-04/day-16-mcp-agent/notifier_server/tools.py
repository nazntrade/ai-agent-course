"""Implementations of the six notifier MCP tools.

Every function is a thin, synchronous wrapper over
:mod:`notifier_server.watches`, which owns the business rules and the database.
A controlled failure raises the SDK's :class:`ToolError`; its message is safe to
show to a caller and contains no configuration, path or secret.

The concrete ``dict[str, Any]`` return annotations are deliberate: a bare
``dict`` makes the MCP server serve the payload as text only, while the SPEC
requires a structured tool result. The docstrings are the model-facing tool
descriptions. ``chat_id`` is injected by the host and hidden from the model.
"""

from __future__ import annotations

from typing import Any

from notifier_server import watches as watch_service


def create_notification_watch(
    query: str,
    keywords: list[str],
    exclude: list[str] = [],
    interval_seconds: int = 0,
    summary_interval_seconds: int = 0,
    source_task_id: str = "",
    chat_id: str = "",
) -> dict[str, Any]:
    """Create a Telegram notification watch for the current chat.

    A watch checks a search query and notifies you only about items that match
    the explicit criterion. Pass the search query, a non-empty list of
    ``keywords`` (an item matches when any keyword appears, case-insensitively,
    in its title or description) and an optional ``exclude`` list that removes
    items. ``interval_seconds`` is how often the watch is checked and
    ``summary_interval_seconds`` how often a regular summary is allowed; both are
    whole seconds of at least 60. ``source_task_id`` is the id of the scheduled
    search in server A that this watch monitors: pass the id returned by
    'schedule_search_task' (or 'list_search_tasks') so the watch reads the result
    of its own task; an empty value leaves the watch without a task to read. The
    first check is the starting point: it records the current matching items and
    sends nothing, so historical results are never delivered. Once that baseline
    exists, a summary is sent once per ``summary_interval_seconds`` period even
    when there are no matching results. The criterion and the schedules are
    stored and returned.
    """
    return watch_service.default_watch_service().create_watch(
        chat_id,
        query,
        keywords,
        exclude,
        interval_seconds,
        summary_interval_seconds,
        source_task_id,
    )


def list_notification_watches(chat_id: str = "") -> dict[str, Any]:
    """List the notification watches of the current chat.

    Use it to find a watch id before evaluating a run, sending a notification or
    stopping a watch, and to report the stored criterion, the schedule and the
    last delivery of each watch. Each item also reports ``source_task_id`` (the
    scheduled task in server A this watch reads), ``summary_due`` (whether the
    watch still owes a summary for the current period) and ``next_summary_at``
    (when the next summary period starts, or ``null`` when summaries are not
    configured).
    """
    return watch_service.default_watch_service().list_watches(chat_id)


def evaluate_run(watch_id: str, run: dict, chat_id: str = "") -> dict[str, Any]:
    """Compare a scheduled-search result with the stored criterion of a watch.

    Pass the whole structured result of 'get_latest_search_run' unchanged. The
    answer reports how many items matched, how many were already seen and which
    ones are new. It also reports ``matched_items`` (every matching item of the
    run, deduplicated, including already-seen ones) for a summary, plus
    ``summary_due`` and ``next_summary_at``. A malformed result or an unknown
    watch is reported in the 'status' field instead of raising. This call sends
    nothing and does not record a delivery: call 'send_notification' with
    'new_items' when 'should_notify' is true, or with 'matched_items' and
    ``kind='summary'`` when a summary is due.
    """
    return watch_service.default_watch_service().evaluate_run(
        watch_id, run, chat_id
    )


def send_notification(
    watch_id: str,
    chat_id: str = "",
    kind: str = "new_items",
    items: list = [],
) -> dict[str, Any]:
    """Send the matching items of a watch to the configured Telegram chat.

    This is the only tool that sends a message. For ``kind='new_items'`` call it
    only after 'evaluate_run' returned 'should_notify' true and pass the returned
    'new_items' unchanged; an empty ``new_items`` needs no delivery. For
    ``kind='summary'`` pass the 'matched_items' of 'evaluate_run': the message is
    labelled as a summary (never as new items) and an empty list still sends an
    honest "no matching results in this period" message. Delivery is idempotent
    per watch, kind and period: sending the same content again returns
    'duplicate' without a second message, and a summary is sent at most once per
    summary period even across a restart. A failed or unconfigured delivery can
    be retried, and the retry updates the same delivery. Do not claim a message
    was sent unless the returned 'status' is 'sent'.
    """
    return watch_service.default_watch_service().send_notification(
        watch_id, chat_id, kind, items
    )


def get_delivery_status(
    watch_id: str = "", chat_id: str = ""
) -> dict[str, Any]:
    """Report the recent deliveries of a watch, or of the current chat.

    Use it to check whether a notification was actually sent. Without
    ``watch_id`` it returns the recent deliveries of the chat. The status is
    read from the stored delivery, not from any text.
    """
    return watch_service.default_watch_service().get_delivery_status(
        watch_id, chat_id
    )


def stop_notification_watch(
    watch_id: str, chat_id: str = ""
) -> dict[str, Any]:
    """Stop a notification watch of the current chat.

    The watch keeps its history but will not be checked again. Calling it again
    for an already stopped watch is not an error.
    """
    return watch_service.default_watch_service().stop_watch(watch_id, chat_id)
