"""Host-side notification monitor (background, no browser).

The monitor is the second channel of the same orchestration: on every tick it
lists the app chats, asks server B (host→B ``list_notification_watches``) for the
active watches that are due, and runs the **same** :class:`Orchestrator` with the
same model provider for each due watch. The model then follows the A→B chain
(A ``get_latest_search_run`` → B ``evaluate_run`` → B ``send_notification``) with a
restricted tool subset. The host owns the A-read scope per watch through
:func:`monitor_injected_arguments` (``source_task_id`` of the watch), so the model
cannot substitute the watch id — or any other value — for the ``task_id``. A watch
whose ``source_task_id`` is empty is not run at all (``unknown_task``). An
incomplete turn is recorded as ``monitor_incomplete`` instead of a success, so
the next tick repeats the check.

When ``list_notification_watches`` reports ``summary_due`` for a watch, a second
monitor turn runs after the ordinary ``new_items`` turn: the model reads the same
task and sends ``send_notification(kind="summary")`` with the run's
``matched_items`` (an empty list still sends an honest "no results" summary).

The monitor never opens the notifier database, never touches the Telegram token
and never writes to the chat history: its session has no persistence callback.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from agent.notifier_client import NotifierUnavailable
from agent.sessions import ChatSession, SessionRegistry

logger = logging.getLogger("agent.monitor")

MONITOR_TRIGGER = "monitor"
MONITOR_ALLOWED_TOOLS = ("get_latest_search_run", "evaluate_run", "send_notification")
MAX_MONITOR_CHATS = 5

# Reason codes of the ordinary and the summary completeness rules.
REASON_EVALUATE_ABSENT = "evaluate_run_absent"
REASON_SEND_MISSING = "send_notification_missing"
REASON_SEND_FAILED = "send_notification_failed"
REASON_SUMMARY_EVALUATE_ABSENT = "summary_evaluate_absent"
REASON_SUMMARY_SEND_MISSING = "summary_send_missing"
REASON_SUMMARY_SEND_FAILED = "summary_send_failed"

STATUS_SENT = "sent"
STATUS_DUPLICATE = "duplicate"
SUMMARY_KIND = "summary"


def monitor_injected_arguments(source_task_id) -> dict:
    """Host-owned per-watch A-read scope.

    The model tends to copy the watch id into the ``task_id`` argument of
    ``get_latest_search_run`` (LIVE regression), which server A rejects. The host
    fixes the ``task_id`` to the watch's own ``source_task_id`` after parsing and
    before validation, so no model-supplied value (including the watch id) can
    widen or forge the read scope.
    """
    return {"get_latest_search_run": {"task_id": str(source_task_id or "")}}

MONITOR_SYSTEM_PROMPT = (
    "You are the background notification monitor of a news agent. Work only "
    "through the tools and do not address a user. The request names the watch id "
    "to check; that id is the one to pass to 'evaluate_run' and "
    "'send_notification', never a literal placeholder. For the given watch: read "
    "the latest scheduled search result from server A with "
    "'get_latest_search_run' and pass its whole result as the 'run' argument of "
    "'evaluate_run'. The host fixes the read scope of 'get_latest_search_run' to "
    "the task linked to this watch, so call it without a 'task_id' and never pass "
    "the watch id as a 'task_id'. Compare the run with the watch using "
    "'evaluate_run' on server B, and call 'send_notification' on server B only "
    "when 'evaluate_run' returned 'should_notify' true, passing its 'new_items' "
    "unchanged. When 'should_notify' is false or the run is empty, error or "
    "pending, do not send anything. Never call a tool of one server from the "
    "other and never claim a notification was delivered unless "
    "'send_notification' returned 'sent' or 'duplicate'."
)

# The model must know which watch it is checking: the watch id is not part of any
# system prompt, so the synthetic user message carries it. Without it the model
# would have to guess an id and ``evaluate_run`` would answer ``unknown_watch``.
# The watch id is only for ``evaluate_run``/``send_notification``: the A-read
# scope is host-owned per watch (:func:`monitor_injected_arguments`), so the
# message tells the model to call ``get_latest_search_run`` without a ``task_id``.
MONITOR_USER_MESSAGE_TEMPLATE = (
    "Check the notification watch with id '{watch_id}' now. Read the latest "
    "scheduled search run of this watch's task with 'get_latest_search_run' "
    "(without a 'task_id'; the host fixes the read scope to that task) and pass "
    "its whole result as the 'run' argument of 'evaluate_run' using exactly this "
    "watch id, then call 'send_notification' only when 'evaluate_run' returned "
    "'should_notify' true."
)

# The scheduled summary is a separate turn with its own prompt: it must never
# present already-seen results as new, and an empty result must still be
# delivered as an honest "no matching results" summary.
MONITOR_SUMMARY_SYSTEM_PROMPT = (
    "You are the background notification monitor of a news agent. Work only "
    "through the tools and do not address a user. The request names the watch id "
    "to summarise and this is a scheduled SUMMARY turn, not a new-items alert. "
    "For this watch: read the latest scheduled search result from server A with "
    "'get_latest_search_run' and pass its whole result as the 'run' argument of "
    "'evaluate_run'. The host fixes the read scope of 'get_latest_search_run' to "
    "the task linked to this watch, so call it without a 'task_id' and never pass "
    "the watch id as a 'task_id'. Then call 'send_notification' with kind "
    "'summary' and pass the 'matched_items' list returned by 'evaluate_run'. "
    "Never label old results as new; this message is a summary and must be "
    "clearly a summary. If there are no matching results, still send the summary "
    "with an empty list: the message honestly says that nothing matched in this "
    "period. Never call a tool of one server from the other and never claim a "
    "notification was delivered unless 'send_notification' returned 'sent' or "
    "'duplicate'."
)

MONITOR_SUMMARY_USER_MESSAGE_TEMPLATE = (
    "Send the scheduled summary for notification watch id '{watch_id}' now. Read "
    "the latest scheduled search run of this watch's task with "
    "'get_latest_search_run' (without a 'task_id'; the host fixes the read scope "
    "to that task) and pass its whole result as the 'run' argument of "
    "'evaluate_run' using exactly this watch id. Then call 'send_notification' "
    "with kind 'summary' and the 'matched_items' returned by 'evaluate_run'; if "
    "nothing matched, send the summary with an empty list."
)


def monitor_result_incomplete(outcomes: dict) -> str | None:
    """The completeness rule of one monitor turn.

    Complete means ``evaluate_run`` ran and either ``should_notify`` is false or
    ``send_notification`` returned ``sent``/``duplicate``. Otherwise the returned
    reason names what the next tick must repeat.
    """
    evaluate = outcomes.get("evaluate_run")
    if evaluate is None:
        return REASON_EVALUATE_ABSENT
    structured = evaluate.structured if isinstance(evaluate.structured, dict) else {}
    if not bool(structured.get("should_notify", False)):
        return None
    send = outcomes.get("send_notification")
    if send is None:
        return REASON_SEND_MISSING
    send_structured = send.structured if isinstance(send.structured, dict) else {}
    if send_structured.get("status") in (STATUS_SENT, STATUS_DUPLICATE):
        return None
    return REASON_SEND_FAILED


def summary_result_incomplete(outcomes: dict) -> str | None:
    """The completeness rule of one scheduled-summary monitor turn.

    Complete means ``evaluate_run`` ran and ``send_notification`` returned the
    ``summary`` kind with ``sent``/``duplicate``. Unlike the ordinary rule, an
    empty result is not a reason to skip: the summary must be delivered.
    """
    evaluate = outcomes.get("evaluate_run")
    if evaluate is None:
        return REASON_SUMMARY_EVALUATE_ABSENT
    send = outcomes.get("send_notification")
    if send is None:
        return REASON_SUMMARY_SEND_MISSING
    send_structured = send.structured if isinstance(send.structured, dict) else {}
    if send_structured.get("kind") != SUMMARY_KIND:
        return REASON_SUMMARY_SEND_FAILED
    if send_structured.get("status") in (STATUS_SENT, STATUS_DUPLICATE):
        return None
    return REASON_SUMMARY_SEND_FAILED


def _parse_iso(value) -> float | None:
    """Parse an ISO-8601 UTC timestamp into an epoch, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


class NotifierMonitor:
    """A single background task that runs due notification watches."""

    def __init__(
        self,
        *,
        chats,
        notifier,
        orchestrator_factory,
        tick_seconds: int = 30,
        session_registry: SessionRegistry | None = None,
        clock=time.time,
        max_chats: int = MAX_MONITOR_CHATS,
    ):
        self._chats = chats
        self._notifier = notifier
        self._orchestrator_factory = orchestrator_factory
        self._tick_seconds = max(int(tick_seconds), 1)
        self._sessions = session_registry
        self._clock = clock
        self._max_chats = max(int(max_chats), 1)
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    @property
    def tick_seconds(self) -> int:
        return self._tick_seconds

    async def start(self) -> None:
        """Start the background task; a second start is a no-op."""
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background task; a second stop is a no-op."""
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a tick must never kill the loop
                logger.exception("notifier monitor tick failed")
            assert self._stop_event is not None
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                continue
            return

    async def tick(self) -> dict:
        """Run one pass over the chats and their due watches.

        The first unavailable server B aborts the pass (one failed session, no
        model call, no direct database read); the next tick retries.
        """
        summary = {
            "chats": 0,
            "due": 0,
            "runs": 0,
            "summaries": 0,
            "unknown_task": 0,
            "aborted": False,
        }
        chats = self._chats.list_chats()
        for chat in list(chats)[: self._max_chats]:
            chat_id = str((chat or {}).get("id") or "")
            if not chat_id:
                continue
            summary["chats"] += 1
            try:
                payload = await self._notifier.list_watches(chat_id)
            except NotifierUnavailable:
                summary["aborted"] = True
                logger.warning("notifier is unavailable; monitor tick aborted")
                return summary

            watches = payload.get("watches") if isinstance(payload, dict) else None
            if not isinstance(watches, list):
                continue
            now = float(self._clock())
            for watch in watches:
                if not isinstance(watch, dict) or watch.get("status") != "active":
                    continue
                due_at = _parse_iso(watch.get("next_check_at"))
                if due_at is None or due_at > now:
                    continue
                watch_id = str(watch.get("watch_id") or "")
                if not watch_id:
                    continue
                # The A-read scope is the watch's own task. Without it there is
                # no safe default: falling back to "the latest run of the chat"
                # could read another task's results, so the watch is skipped.
                source_task_id = str(watch.get("source_task_id") or "").strip()
                if not source_task_id:
                    summary["unknown_task"] += 1
                    logger.warning(
                        "notification watch %s has no source_task_id; skipped",
                        watch_id,
                    )
                    continue
                summary["due"] += 1
                await self._run_watch(
                    chat_id, watch_id, source_task_id, summary=False
                )
                summary["runs"] += 1
                if bool(watch.get("summary_due")):
                    await self._run_watch(
                        chat_id, watch_id, source_task_id, summary=True
                    )
                    summary["summaries"] += 1
        return summary

    async def _run_watch(
        self, chat_id: str, watch_id: str, source_task_id: str, *, summary: bool
    ) -> None:
        session = self._session_for(chat_id)
        orchestrator = self._orchestrator_factory()
        request_id = uuid.uuid4().hex
        if summary:
            user_message = MONITOR_SUMMARY_USER_MESSAGE_TEMPLATE.format(
                watch_id=watch_id
            )
            system_prompt = MONITOR_SUMMARY_SYSTEM_PROMPT
            require_result = summary_result_incomplete
        else:
            user_message = MONITOR_USER_MESSAGE_TEMPLATE.format(watch_id=watch_id)
            system_prompt = MONITOR_SYSTEM_PROMPT
            require_result = monitor_result_incomplete
        async for _event in orchestrator.run(
            request_id,
            session,
            user_message,
            trigger=MONITOR_TRIGGER,
            watch_id=watch_id,
            system_prompt=system_prompt,
            allowed_tools=MONITOR_ALLOWED_TOOLS,
            require_result=require_result,
            injected_arguments=monitor_injected_arguments(source_task_id),
        ):
            pass

    def _session_for(self, chat_id: str) -> ChatSession:
        # No history provider and no success callback: a monitor turn is never
        # written to the chat history.
        if self._sessions is not None:
            return self._sessions.session(chat_id)
        return ChatSession(chat_id)
