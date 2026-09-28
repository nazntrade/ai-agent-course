"""Restart check for the notifier server B (D20-12, D20-28).

One real scenario over a shared temporary database:

* server A (``mcp_server``) runs with its real scheduler and a loopback
  ``FakeSearchServer``;
* server B (``notifier_server``) runs with its own temporary database and a
  loopback ``FakeTelegramServer``, so the real Telegram service is never called;
* after a watch, a baseline, a seen set and a ``sent`` delivery exist, server B
  is actually stopped and started again on the same database file;
* after the restart the watch, the seen fingerprints and the delivery must still
  be there, a repeated ``send_notification`` must be a ``duplicate``, and the
  runs that accumulated while B was down must resolve to the latest one only
  (no backfill of the intermediate run).

Only the processes this module started are stopped. Exit codes: 0 PASS, 1 FAIL,
2 prerequisite.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# The .bat entry points run this file directly; restore the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from harness.qa_bridge import PROJECT_DIR, ensure_paths, qa_port_is_free  # noqa: E402
from harness.processes import (  # noqa: E402
    ManagedProcess,
    PrerequisiteError,
    python_module,
    require_free_ports,
    sanitized_env,
    wait_tcp,
)
from harness.run_dir import create_run_dir, relative, write_report  # noqa: E402

ensure_paths()

from agent.mcp_adapter import SdkMcpClient, inspect_tools  # noqa: E402
from storage.chats import ChatRepository  # noqa: E402
from storage.db import Database  # noqa: E402
from tests.support.fake_search import (  # noqa: E402
    DUPLICATES_MARKER,
    FAKE_API_KEY,
    MANY_MARKER,
    RESULT_URLS,
    FakeSearchServer,
)
from tests.support.fake_telegram import FakeTelegramServer  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_MCP_PORT = 8773
DEFAULT_NOTIFIER_PORT = 8774

MCP_READY_TIMEOUT_SECONDS = 45.0
RUN_POLL_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.5

NOTIFIER_SERVER_NAME = "day-20-notifier"

WATCH_QUERY = "python documentation"
WATCH_KEYWORDS = ["python", "guide"]
WATCH_INTERVAL_SECONDS = 60
WATCH_SUMMARY_INTERVAL_SECONDS = 3600


def _mcp_env(port: int, db_path: Path, search_server: FakeSearchServer) -> dict:
    return sanitized_env(
        {
            "MCP_SERVER_HOST": "127.0.0.1",
            "MCP_SERVER_PORT": str(port),
            "MCP_LOAD_DOTENV": "0",
            "MCP_TASK_TICK_SECONDS": "0.5",
            "MCP_SEARCH_API_KEY_ENV": "TAVILY_API_KEY",
            "TAVILY_API_KEY": FAKE_API_KEY,
            "MCP_SEARCH_BASE_URL": search_server.base_url,
            "MCP_SEARCH_TIMEOUT_SECONDS": "3",
            "MCP_SEARCH_MAX_RESULTS": "5",
            "AGENT_DB_PATH": str(db_path),
        }
    )


def _notifier_env(port: int, notifier_db: Path, telegram_base: str) -> dict:
    return sanitized_env(
        {
            "MCP_NOTIFIER_HOST": "127.0.0.1",
            "MCP_NOTIFIER_PORT": str(port),
            # Keep server B away from the real .env and the real Telegram API.
            "NOTIFIER_LOAD_DOTENV": "0",
            "TELEGRAM_BOT_TOKEN": "restart-fake-token",
            "TELEGRAM_CHAT_ID": "restart-fake-chat",
            "NOTIFIER_TELEGRAM_API_BASE_URL": telegram_base,
            "NOTIFIER_DB_PATH": str(notifier_db),
        }
    )


async def _notifier_ready(url: str, timeout: float) -> bool:
    """Poll a real notifier handshake and confirm the expected server identity."""
    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        try:
            status, _tools = await inspect_tools(url, connect_timeout_s=3.0)
        except Exception:  # noqa: BLE001 - readiness polling only
            status = None
        if (
            status is not None
            and status.connected
            and status.server_name == NOTIFIER_SERVER_NAME
        ):
            return True
        await asyncio.sleep(0.4)
    return False


def _notifier_ready_sync(url: str, timeout: float) -> bool:
    """Sync wrapper used before the scenario's event loop starts."""
    return asyncio.run(_notifier_ready(url, timeout))


class _NotifierHandle:
    """Starts and stops only the notifier process this module owns."""

    def __init__(self, port: int, notifier_db: Path, telegram_base: str, run_dir: Path):
        self._port = int(port)
        self._db = notifier_db
        self._telegram_base = telegram_base
        self._run_dir = run_dir
        self.process: ManagedProcess | None = None

    def start(self) -> None:
        self.process = ManagedProcess(
            name="notifier-server",
            args=python_module("notifier_server"),
            cwd=PROJECT_DIR,
            env=_notifier_env(self._port, self._db, self._telegram_base),
            log_path=self._run_dir / "notifier_server.log",
        ).start()
        if not wait_tcp("127.0.0.1", self._port, MCP_READY_TIMEOUT_SECONDS, self.process):
            raise PrerequisiteError("the notifier server did not start")

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
            self.process = None

    def alive(self) -> bool:
        return self.process is not None and self.process.alive()


async def _call(url: str, tool: str, arguments: dict):
    return await SdkMcpClient(url, call_timeout_s=30.0).call_tool(tool, arguments)


async def _schedule(mcp_url: str, chat_id: str, query: str) -> str:
    created = await _call(
        mcp_url,
        "schedule_search_task",
        {"query": query, "interval_seconds": 3600, "chat_id": chat_id},
    )
    if not created.ok:
        raise RuntimeError(f"schedule_search_task failed: {created.text}")
    return created.structured["task_id"]


async def _wait_run(
    mcp_url: str, chat_id: str, task_id: str, statuses=("ok", "empty", "error")
) -> dict:
    deadline = time.monotonic() + RUN_POLL_TIMEOUT_SECONDS
    payload: dict = {"status": "pending", "task_id": task_id}
    while time.monotonic() < deadline:
        result = await _call(
            mcp_url,
            "get_latest_search_run",
            {"chat_id": chat_id, "task_id": task_id},
        )
        if result.ok:
            payload = result.structured or payload
            if payload.get("status") in statuses:
                return payload
        await asyncio.sleep(POLL_SECONDS)
    raise RuntimeError(f"run for task {task_id} did not finish: {payload}")


async def _stop_task(mcp_url: str, chat_id: str, task_id: str) -> None:
    """Stop a finished task so its chat slot is free for the next one."""
    stopped = await _call(
        mcp_url,
        "stop_search_task",
        {"task_id": task_id, "chat_id": chat_id},
    )
    if not stopped.ok:
        raise RuntimeError(f"stop_search_task failed: {stopped.text}")


async def _scenario(
    mcp_url: str,
    notifier_url: str,
    chat_id: str,
    notifier: _NotifierHandle,
    telegram: FakeTelegramServer,
    report: dict,
) -> bool:
    # --- create state -----------------------------------------------------
    # The watch reads the result of its own scheduled task, so the results task
    # exists first and the watch is created with its ``source_task_id``.
    first_task = await _schedule(mcp_url, chat_id, WATCH_QUERY)
    first_run = await _wait_run(mcp_url, chat_id, first_task, statuses=("ok",))

    created = await _call(
        notifier_url,
        "create_notification_watch",
        {
            "query": WATCH_QUERY,
            "keywords": list(WATCH_KEYWORDS),
            "interval_seconds": WATCH_INTERVAL_SECONDS,
            "summary_interval_seconds": WATCH_SUMMARY_INTERVAL_SECONDS,
            "source_task_id": first_task,
            "chat_id": chat_id,
        },
    )
    if not created.ok:
        raise RuntimeError(f"create_notification_watch failed: {created.text}")
    watch_id = created.structured["watch_id"]

    # Host-side baseline with an empty run, then deliver the task's results.
    # The baseline must not send; the next check finds every link as new.
    baseline = await _call(
        notifier_url,
        "evaluate_run",
        {
            "watch_id": watch_id,
            "run": {"status": "empty", "results": []},
            "chat_id": chat_id,
        },
    )
    baseline_payload = baseline.structured or {}
    evaluated = await _call(
        notifier_url,
        "evaluate_run",
        {"watch_id": watch_id, "run": first_run, "chat_id": chat_id},
    )
    items1 = (evaluated.structured or {}).get("new_items") or []
    sent1 = await _call(
        notifier_url,
        "send_notification",
        {
            "watch_id": watch_id,
            "chat_id": chat_id,
            "kind": "new_items",
            "items": items1,
        },
    )
    sent1_payload = sent1.structured or {}
    delivery1 = sent1_payload.get("delivery_id")

    # A scheduled summary is sent once per period, even with no matching results.
    summary1 = await _call(
        notifier_url,
        "send_notification",
        {"watch_id": watch_id, "chat_id": chat_id, "kind": "summary", "items": []},
    )
    summary1_payload = summary1.structured or {}
    summary_delivery = summary1_payload.get("delivery_id")

    # --- a second task and a second watch in the same chat -----------------
    second_task = await _schedule(mcp_url, chat_id, MANY_MARKER)
    second_run = await _wait_run(mcp_url, chat_id, second_task, statuses=("ok",))
    created2 = await _call(
        notifier_url,
        "create_notification_watch",
        {
            "query": WATCH_QUERY,
            "keywords": list(WATCH_KEYWORDS),
            "interval_seconds": WATCH_INTERVAL_SECONDS,
            "summary_interval_seconds": WATCH_SUMMARY_INTERVAL_SECONDS,
            "source_task_id": second_task,
            "chat_id": chat_id,
        },
    )
    if not created2.ok:
        raise RuntimeError(f"second create_notification_watch failed: {created2.text}")
    watch2 = created2.structured["watch_id"]
    await _call(
        notifier_url,
        "evaluate_run",
        {
            "watch_id": watch2,
            "run": {"status": "empty", "results": []},
            "chat_id": chat_id,
        },
    )
    evaluated2 = await _call(
        notifier_url,
        "evaluate_run",
        {"watch_id": watch2, "run": second_run, "chat_id": chat_id},
    )
    items2 = (evaluated2.structured or {}).get("new_items") or []
    sent2a = await _call(
        notifier_url,
        "send_notification",
        {
            "watch_id": watch2,
            "chat_id": chat_id,
            "kind": "new_items",
            "items": items2,
        },
    )
    # Free both active task slots so the later ``_schedule`` calls create fresh
    # tasks instead of reusing an existing active one with the same query.
    await _stop_task(mcp_url, chat_id, first_task)
    await _stop_task(mcp_url, chat_id, second_task)

    listed_before = await _call(
        notifier_url, "list_notification_watches", {"chat_id": chat_id}
    )
    listed_before_watches = (listed_before.structured or {}).get("watches") or []
    by_id = {w.get("watch_id"): w for w in listed_before_watches}
    seen_before = int((by_id.get(watch_id) or {}).get("seen_count") or 0)
    seen2_before = int((by_id.get(watch2) or {}).get("seen_count") or 0)
    scope1_before = (by_id.get(watch_id) or {}).get("source_task_id")
    scope2_before = (by_id.get(watch2) or {}).get("source_task_id")
    summary_due_before = (by_id.get(watch_id) or {}).get("summary_due")
    deliveries_before = await _call(
        notifier_url,
        "get_delivery_status",
        {"watch_id": watch_id, "chat_id": chat_id},
    )
    count_before = (deliveries_before.structured or {}).get("count")
    chat_deliveries_before = await _call(
        notifier_url, "get_delivery_status", {"chat_id": chat_id}
    )
    chat_list_before = (chat_deliveries_before.structured or {}).get("deliveries") or []
    messages_before_stop = telegram.message_count

    report["before_stop"] = {
        "watch_id": watch_id,
        "second_watch_id": watch2,
        "baseline_is_baseline": bool(baseline_payload.get("is_baseline")),
        "first_status": sent1_payload.get("status"),
        "summary_status": summary1_payload.get("status"),
        "summary_due_after_summary": summary_due_before,
        "seen_count": seen_before,
        "second_seen_count": seen2_before,
        "delivery_count": count_before,
        "watch_count": len(listed_before_watches),
        "delivery_watch_ids": sorted(
            {str(entry.get("watch_id")) for entry in chat_list_before}
        ),
    }

    # --- stop server B, accumulate server A runs --------------------------
    notifier.stop()
    report["notifier_stopped"] = not notifier.alive()

    old_task = await _schedule(mcp_url, chat_id, DUPLICATES_MARKER)
    old_run = await _wait_run(mcp_url, chat_id, old_task, statuses=("ok",))
    await _stop_task(mcp_url, chat_id, old_task)
    new_task = await _schedule(mcp_url, chat_id, MANY_MARKER)
    new_run = await _wait_run(mcp_url, chat_id, new_task, statuses=("ok",))
    report["accumulated"] = {
        "old_task": old_task,
        "old_count": old_run.get("result_count"),
        "new_task": new_task,
        "new_count": new_run.get("result_count"),
    }

    # --- restart server B on the same database ----------------------------
    notifier.start()
    if not await _notifier_ready(notifier_url, MCP_READY_TIMEOUT_SECONDS):
        raise PrerequisiteError(
            "the notifier server did not complete a handshake after restart"
        )
    report["notifier_restarted"] = notifier.alive()

    listed_after = await _call(
        notifier_url, "list_notification_watches", {"chat_id": chat_id}
    )
    listed_after_watches = (listed_after.structured or {}).get("watches") or []
    by_id_after = {w.get("watch_id"): w for w in listed_after_watches}
    watch1_after = by_id_after.get(watch_id) or {}
    watch2_after = by_id_after.get(watch2) or {}
    seen_after = int(watch1_after.get("seen_count") or 0)
    seen2_after = int(watch2_after.get("seen_count") or 0)
    last_delivery = watch1_after.get("last_delivery")
    deliveries_after = await _call(
        notifier_url,
        "get_delivery_status",
        {"watch_id": watch_id, "chat_id": chat_id},
    )
    count_after = (deliveries_after.structured or {}).get("count")

    # The same summary period after a restart must be a duplicate, not a second
    # message: the period key and the sent row are persisted in ``deliveries``.
    repeat_summary = await _call(
        notifier_url,
        "send_notification",
        {"watch_id": watch_id, "chat_id": chat_id, "kind": "summary", "items": []},
    )
    repeat_summary_payload = repeat_summary.structured or {}

    repeat = await _call(
        notifier_url,
        "send_notification",
        {
            "watch_id": watch_id,
            "chat_id": chat_id,
            "kind": "new_items",
            "items": items1,
        },
    )
    repeat_payload = repeat.structured or {}

    # --- a failed delivery is recorded and retried on the same row --------
    error_run = {
        "status": "ok",
        "results": [
            {
                "title": "Python retry page",
                "url": "https://docs.example.test/retry/1",
                "description": "python retry",
            }
        ],
    }
    error_eval = await _call(
        notifier_url,
        "evaluate_run",
        {"watch_id": watch2, "run": error_run, "chat_id": chat_id},
    )
    error_items = (error_eval.structured or {}).get("new_items") or []
    telegram.fail_next(1)
    failed_send = await _call(
        notifier_url,
        "send_notification",
        {
            "watch_id": watch2,
            "chat_id": chat_id,
            "kind": "new_items",
            "items": error_items,
        },
    )
    failed_payload = failed_send.structured or {}
    retry_send = await _call(
        notifier_url,
        "send_notification",
        {
            "watch_id": watch2,
            "chat_id": chat_id,
            "kind": "new_items",
            "items": error_items,
        },
    )
    retry_payload = retry_send.structured or {}

    # --- no backfill: only the latest accumulated run ---------------------
    latest = await _call(mcp_url, "get_latest_search_run", {"chat_id": chat_id})
    latest_payload = latest.structured or {}
    latest_eval = await _call(
        notifier_url,
        "evaluate_run",
        {"watch_id": watch_id, "run": latest_payload, "chat_id": chat_id},
    )
    latest_items = (latest_eval.structured or {}).get("new_items") or []
    latest_urls = {item["url"] for item in latest_items}

    messages_before_send2 = telegram.message_count
    sent2 = await _call(
        notifier_url,
        "send_notification",
        {
            "watch_id": watch_id,
            "chat_id": chat_id,
            "kind": "new_items",
            "items": latest_items,
        },
    )
    sent2_payload = sent2.structured or {}
    sent_messages = telegram.messages[messages_before_send2:]
    chat_deliveries_after = await _call(
        notifier_url, "get_delivery_status", {"chat_id": chat_id}
    )
    chat_list_after = (chat_deliveries_after.structured or {}).get("deliveries") or []

    report["after_restart"] = {
        "seen_count": seen_after,
        "seen_before": seen_before,
        "second_seen_count": seen2_after,
        "second_seen_before": seen2_before,
        "delivery_count": count_after,
        "last_delivery_status": (last_delivery or {}).get("status"),
        "summary_repeat_status": repeat_summary_payload.get("status"),
        "summary_repeat_same_id": repeat_summary_payload.get("delivery_id")
        == summary_delivery,
        "repeat_status": repeat_payload.get("status"),
        "repeat_same_id": repeat_payload.get("delivery_id") == delivery1,
        "latest_task": latest_payload.get("task_id"),
        "latest_urls": sorted(latest_urls),
        "second_send_status": sent2_payload.get("status"),
        "messages_sent_after_restart": len(sent_messages),
        "delivery_watch_ids": sorted(
            {str(entry.get("watch_id")) for entry in chat_list_after}
        ),
    }
    report["delivery_error"] = {
        "failed_status": failed_payload.get("status"),
        "failed_attempts": failed_payload.get("attempts"),
        "retry_status": retry_payload.get("status"),
        "retry_attempts": retry_payload.get("attempts"),
        "same_delivery_id": retry_payload.get("delivery_id")
        == failed_payload.get("delivery_id"),
    }

    scope1_after = watch1_after.get("source_task_id")
    scope2_after = watch2_after.get("source_task_id")

    checks = {
        "baseline": bool(baseline_payload.get("is_baseline"))
        and not baseline_payload.get("should_notify"),
        "first_sent": sent1_payload.get("status") == "sent",
        "summary_sent": summary1_payload.get("status") == "sent"
        and summary_due_before is False,
        "summary_duplicate_after_restart": repeat_summary_payload.get("status")
        == "duplicate"
        and repeat_summary_payload.get("delivery_id") == summary_delivery,
        "two_watches": len(listed_before_watches) == 2
        and len(listed_after_watches) == 2,
        "watch_scopes_persisted": scope1_before == first_task
        and scope2_before == second_task
        and scope1_after == first_task
        and scope2_after == second_task,
        "no_seen_mixing": seen_before == len(RESULT_URLS)
        and seen2_before == int(second_run.get("result_count") or -1)
        and seen_after == seen_before
        and seen2_after == seen2_before,
        "deliveries_not_mixed": {watch_id, watch2} <= set(
            str(entry.get("watch_id")) for entry in chat_list_before
        ),
        "seen_persisted": seen_after == seen_before,
        "delivery_persisted": count_after == count_before == 2,
        "last_delivery_sent": (last_delivery or {}).get("status") == "sent",
        "repeat_duplicate": repeat_payload.get("status") == "duplicate"
        and repeat_payload.get("delivery_id") == delivery1,
        "failed_delivery_retried": failed_payload.get("status") == "failed"
        and failed_payload.get("attempts") == 1
        and retry_payload.get("status") == "sent"
        and retry_payload.get("attempts") == 2
        and retry_payload.get("delivery_id") == failed_payload.get("delivery_id"),
        "latest_is_the_new_task": latest_payload.get("task_id") == new_task,
        "no_backfill": bool(latest_urls)
        and all("reference/" in url for url in latest_urls)
        and not any("dup/" in url for url in latest_urls),
        "second_sent": sent2_payload.get("status") == "sent",
        "one_more_message": len(sent_messages) == 1
        and any("reference/" in (message or "") for message in sent_messages),
        "no_message_before_stop_mismatch": messages_before_stop >= 1,
    }
    report["checks"] = checks
    return all(checks.values())


def run() -> int:
    """Execute the notifier restart scenario and return an exit code."""
    mcp_port = int(os.environ.get("NOTIFIER_RESTART_MCP_PORT") or DEFAULT_MCP_PORT)
    notifier_port = int(
        os.environ.get("NOTIFIER_RESTART_NOTIFIER_PORT") or DEFAULT_NOTIFIER_PORT
    )

    run_dir = create_run_dir("notifier-restart")
    db_path = run_dir / "day18.sqlite3"
    notifier_db = run_dir / "day20-notifier.sqlite3"
    report: dict = {
        "scenario": "notifier-restart",
        "ports": {"mcp": mcp_port, "notifier": notifier_port},
        "db": relative(db_path),
        "notifier_db": relative(notifier_db),
    }

    try:
        require_free_ports([mcp_port, notifier_port])
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        write_report(run_dir, {**report, "status": "prerequisite", "reason": str(exc)})
        return EXIT_PREREQUISITE

    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    notifier_url = f"http://127.0.0.1:{notifier_port}/mcp"

    search_server: FakeSearchServer | None = None
    telegram: FakeTelegramServer | None = None
    mcp_process: ManagedProcess | None = None
    notifier: _NotifierHandle | None = None
    restart_status = "FAIL"
    exit_code = EXIT_FAIL

    try:
        search_server = FakeSearchServer(0).start()
        telegram = FakeTelegramServer(0).start()
        report["fake_search"] = {"loopback_port": search_server.port}
        report["fake_telegram"] = {"loopback_port": telegram.port}

        mcp_process = ManagedProcess(
            name="mcp-server",
            args=python_module("mcp_server"),
            cwd=PROJECT_DIR,
            env=_mcp_env(mcp_port, db_path, search_server),
            log_path=run_dir / "mcp_server.log",
        ).start()
        if not wait_tcp("127.0.0.1", mcp_port, MCP_READY_TIMEOUT_SECONDS, mcp_process):
            raise PrerequisiteError("the MCP server did not start")

        notifier = _NotifierHandle(
            notifier_port, notifier_db, telegram.base_url, run_dir
        )
        notifier.start()
        if not _notifier_ready_sync(notifier_url, MCP_READY_TIMEOUT_SECONDS):
            raise PrerequisiteError("the notifier server did not complete a handshake")
        report["handshake"] = {
            "mcp": True,
            "notifier_server_name": NOTIFIER_SERVER_NAME,
        }

        chat = ChatRepository(Database(db_path)).create_chat("Notifier restart")
        chat_id = chat["id"]

        passed = asyncio.run(
            _scenario(mcp_url, notifier_url, chat_id, notifier, telegram, report)
        )
        restart_status = "PASS" if passed else "FAIL"
        exit_code = EXIT_OK if passed else EXIT_FAIL
        return exit_code
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        return EXIT_PREREQUISITE
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"FAIL: the notifier restart scenario failed: {type(exc).__name__}: {exc}")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        if notifier is not None:
            notifier.stop()
        if mcp_process is not None:
            report.setdefault("stopped", []).append(mcp_process.stop())
        if telegram is not None:
            telegram.stop()
        if search_server is not None:
            search_server.stop()
        report["notifier_restart_status"] = restart_status
        report["ports_released"] = {
            "mcp": qa_port_is_free(mcp_port, "127.0.0.1"),
            "notifier": qa_port_is_free(notifier_port, "127.0.0.1"),
        }
        report["status"] = "pass" if exit_code == EXIT_OK else "fail"
        write_report(run_dir, report)
        print(f"NOTIFIER_RESTART_STATUS: {restart_status}")
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point used by ``harness/live_mcp.py``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
