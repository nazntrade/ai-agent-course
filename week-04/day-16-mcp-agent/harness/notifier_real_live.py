"""Real (opt-in) live check of a Telegram delivery through server B.

This is the only day-20 check that sends a real Telegram message, so it is gated
by an explicit opt-in:

* ``NOTIFIER_REAL_ALLOW=1`` must be set; otherwise the script reports
  ``NOTIFIER_REAL_STATUS: BLOCKED`` and exits 2 without starting anything;
* server B is started **without** ``NOTIFIER_LOAD_DOTENV=0``, so it reads the
  real ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` from the local ``.env``
  itself. The harness never reads or prints those values, and ``sanitized_env``
  never forwards them.

Exactly one real ``sendNotification`` is made: a watch is created and a single
``send_notification`` call delivers one synthetic item. No real Tavily search is
made. A missing token is an honest ``BLOCKED`` (``not_configured``), never a
fabricated pass. Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# The manual entry point runs this file directly; restore the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from harness.qa_bridge import PROJECT_DIR, ensure_paths  # noqa: E402
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

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_NOTIFIER_PORT = 8775
MCP_READY_TIMEOUT_SECONDS = 45.0
ALLOW_ENV = "NOTIFIER_REAL_ALLOW"

NOTIFIER_SERVER_NAME = "day-20-notifier"
WATCH_QUERY = "Day 20 real Telegram check"
WATCH_KEYWORDS = ["telegram"]
WATCH_INTERVAL_SECONDS = 60
WATCH_SUMMARY_INTERVAL_SECONDS = 3600
REAL_ITEM = {
    "title": "Day 20 notifier real check",
    "url": "https://example.test/day20-real-check",
}


def _notifier_ready(url: str, timeout: float) -> bool:
    import time

    stop_at = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < stop_at:
        try:
            status, _tools = asyncio.run(inspect_tools(url, connect_timeout_s=3.0))
        except Exception:  # noqa: BLE001 - readiness polling only
            status = None
        if (
            status is not None
            and status.connected
            and status.server_name == NOTIFIER_SERVER_NAME
        ):
            return True
        time.sleep(0.4)
    return False


def _blocked(run_dir: Path, report: dict, reason: str, calls: int = 0) -> int:
    report["status"] = "blocked"
    report["notifier_real_status"] = "BLOCKED"
    report["reason"] = reason
    report["calls"] = int(calls)
    write_report(run_dir, report)
    print(f"NOTIFIER_REAL_STATUS: BLOCKED - {reason}")
    print(f"NOTIFIER_REAL_CALLS: {int(calls)}")
    print(f"RUN_DIR: {relative(run_dir)}")
    return EXIT_PREREQUISITE


def run() -> int:
    """Run the opt-in real Telegram check and return its exit code."""
    port = int(os.environ.get("NOTIFIER_REAL_PORT") or DEFAULT_NOTIFIER_PORT)
    run_dir = create_run_dir("notifier-real-live")
    notifier_db = run_dir / "day20-notifier.sqlite3"
    report: dict = {
        "scenario": "notifier-real-live",
        "port": port,
        "notifier_db": relative(notifier_db),
    }

    if str(os.environ.get(ALLOW_ENV) or "").strip() != "1":
        return _blocked(
            run_dir,
            report,
            "real Telegram delivery is opt-in; set NOTIFIER_REAL_ALLOW=1 to "
            "allow one message",
        )

    try:
        require_free_ports([port])
    except PrerequisiteError as exc:
        return _blocked(run_dir, report, str(exc))

    notifier_url = f"http://127.0.0.1:{port}/mcp"
    notifier_process: ManagedProcess | None = None
    status = "FAIL"
    exit_code = EXIT_FAIL
    calls = 0

    try:
        # No NOTIFIER_LOAD_DOTENV here on purpose: server B reads the real token
        # and recipient from the local .env itself. The harness only points the
        # child at its own temporary database.
        notifier_process = ManagedProcess(
            name="notifier-server",
            args=python_module("notifier_server"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "MCP_NOTIFIER_HOST": "127.0.0.1",
                    "MCP_NOTIFIER_PORT": str(port),
                    "NOTIFIER_DB_PATH": str(notifier_db),
                }
            ),
            log_path=run_dir / "notifier_server.log",
        ).start()
        if not wait_tcp(
            "127.0.0.1", port, MCP_READY_TIMEOUT_SECONDS, notifier_process
        ):
            print("NOTIFIER_REAL_STATUS: FAIL - the notifier server did not start")
            report["notifier_real_status"] = "FAIL"
            return EXIT_FAIL
        if not _notifier_ready(notifier_url, MCP_READY_TIMEOUT_SECONDS):
            print(
                "NOTIFIER_REAL_STATUS: FAIL - the notifier server did not complete "
                "the expected handshake"
            )
            report["notifier_real_status"] = "FAIL"
            return EXIT_FAIL

        client = SdkMcpClient(notifier_url, call_timeout_s=60.0)
        chat_id = "day20-real-live"
        created = asyncio.run(
            client.call_tool(
                "create_notification_watch",
                {
                    "query": WATCH_QUERY,
                    "keywords": list(WATCH_KEYWORDS),
                    "interval_seconds": WATCH_INTERVAL_SECONDS,
                    "summary_interval_seconds": WATCH_SUMMARY_INTERVAL_SECONDS,
                    "chat_id": chat_id,
                },
            )
        )
        if not created.ok:
            print(f"NOTIFIER_REAL_STATUS: FAIL - {created.text}")
            report["notifier_real_status"] = "FAIL"
            return EXIT_FAIL
        watch_id = (created.structured or {}).get("watch_id", "")
        report["watch_created"] = bool(watch_id)

        calls = 1
        sent = asyncio.run(
            client.call_tool(
                "send_notification",
                {
                    "watch_id": watch_id,
                    "chat_id": chat_id,
                    "kind": "new_items",
                    "items": [dict(REAL_ITEM)],
                },
            )
        )
        payload = sent.structured or {}
        send_status = str(payload.get("status") or "")
        report["send_status"] = send_status
        report["calls"] = calls
        if send_status == "sent":
            print(
                "NOTIFIER_REAL_STATUS: PASS - one Telegram message was delivered "
                f"(delivery status '{send_status}')"
            )
            print(f"NOTIFIER_REAL_CALLS: {calls}")
            status = "PASS"
            exit_code = EXIT_OK
            return exit_code
        if send_status == "not_configured":
            return _blocked(
                run_dir,
                report,
                "Telegram is not configured in the local .env "
                "(empty token or recipient)",
                calls=calls,
            )
        if send_status == "duplicate":
            print(
                "NOTIFIER_REAL_STATUS: PASS - the content was already delivered "
                "(duplicate, no new message)"
            )
            print(f"NOTIFIER_REAL_CALLS: {calls}")
            status = "PASS"
            exit_code = EXIT_OK
            return exit_code
        print(
            f"NOTIFIER_REAL_STATUS: FAIL - the delivery status is '{send_status}'"
        )
        report["notifier_real_status"] = "FAIL"
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"NOTIFIER_REAL_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["notifier_real_status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        if notifier_process is not None:
            report.setdefault("stopped", []).append(notifier_process.stop())
        report.setdefault("notifier_real_status", status)
        report.setdefault("calls", calls)
        report["status"] = "pass" if exit_code == EXIT_OK else "blocked_or_failed"
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Manual standalone entry point; there is no ``test.bat`` mode for it.

    It only sends a real Telegram message when ``NOTIFIER_REAL_ALLOW=1`` is set;
    without it (or without a configured token) it reports
    ``NOTIFIER_REAL_STATUS: BLOCKED``.
    """
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
