"""Real (opt-in) live check of one scheduled search against Tavily Search.

This is the only check that spends the operator's Tavily quota, so it is gated
by an explicit opt-in:

* ``TASKS_REAL_ALLOW=1`` must be set; otherwise the script reports
  ``TASKS_REAL_STATUS: BLOCKED`` and exits 2 without starting anything;
* the MCP server is started **without** ``MCP_LOAD_DOTENV=0`` so it reads the
  real key from the local ``.env`` itself; the harness never reads the key and
  never prints it.

The chat is seeded directly through the storage layer (the MCP server only
accepts an existing ``chat_id``), one task is created through the real MCP tool
and exactly one real background run is awaited. Exit codes: 0 PASS, 1 FAIL,
2 BLOCKED/PREREQUISITE.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# The .bat-free manual entry point runs this file directly; add the project root
# so the project packages resolve.
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
from storage.chats import ChatRepository  # noqa: E402
from storage.db import Database  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

MCP_TEST_PORT = 8770
MCP_READY_TIMEOUT_SECONDS = 45.0
RUN_TIMEOUT_SECONDS = 60.0
ALLOW_ENV = "TASKS_REAL_ALLOW"
REAL_QUERY = "Python programming language official documentation"


def _mcp_ready(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        try:
            status, _tools = asyncio.run(inspect_tools(url, connect_timeout_s=3.0))
        except Exception:  # noqa: BLE001 - readiness polling only
            status = None
        if status is not None and status.connected:
            return True
        time.sleep(0.4)
    return False


def _blocked(run_dir: Path, report: dict, reason: str) -> int:
    report["status"] = "blocked"
    report["tasks_real_status"] = "BLOCKED"
    report["reason"] = reason
    write_report(run_dir, report)
    print(f"TASKS_REAL_STATUS: BLOCKED - {reason}")
    print(f"RUN_DIR: {relative(run_dir)}")
    return EXIT_PREREQUISITE


def run() -> int:
    """Run the opt-in real scheduled-search check and return its exit code."""
    port = int(os.environ.get("TASKS_REAL_MCP_PORT") or MCP_TEST_PORT)
    run_dir = create_run_dir("tasks-real-live")
    report: dict = {"scenario": "tasks-real-live", "port": port}

    if str(os.environ.get(ALLOW_ENV) or "").strip() != "1":
        return _blocked(
            run_dir,
            report,
            "real Tavily calls are opt-in; set TASKS_REAL_ALLOW=1 to allow one run",
        )

    try:
        require_free_ports([port])
    except PrerequisiteError as exc:
        return _blocked(run_dir, report, str(exc))

    mcp_url = f"http://127.0.0.1:{port}/mcp"
    mcp_process: ManagedProcess | None = None
    tasks_real_status = "FAIL"
    exit_code = EXIT_FAIL

    try:
        # The chat is seeded through the storage layer before the MCP server
        # starts, because the tool only accepts an existing chat.
        db_path = run_dir / "day18.sqlite3"
        database = Database(db_path)
        chat = ChatRepository(database).create_chat("Real task live")
        report["chat_created"] = True

        # No MCP_LOAD_DOTENV=0: the MCP server reads the real key from .env.
        mcp_process = ManagedProcess(
            name="mcp-server",
            args=python_module("mcp_server"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "MCP_SERVER_HOST": "127.0.0.1",
                    "MCP_SERVER_PORT": str(port),
                    "MCP_TASK_TICK_SECONDS": "1",
                    "AGENT_DB_PATH": str(db_path),
                }
            ),
            log_path=run_dir / "mcp_server.log",
        ).start()
        if not wait_tcp("127.0.0.1", port, MCP_READY_TIMEOUT_SECONDS, mcp_process):
            print("TASKS_REAL_STATUS: FAIL - the MCP server did not start")
            report["tasks_real_status"] = "FAIL"
            return EXIT_FAIL
        if not _mcp_ready(mcp_url, MCP_READY_TIMEOUT_SECONDS):
            print("TASKS_REAL_STATUS: FAIL - the MCP server did not handshake")
            report["tasks_real_status"] = "FAIL"
            return EXIT_FAIL

        client = SdkMcpClient(mcp_url, call_timeout_s=30.0)
        created = asyncio.run(
            client.call_tool(
                "schedule_search_task",
                {
                    "query": REAL_QUERY,
                    "interval_seconds": 86400,
                    "chat_id": chat["id"],
                },
            )
        )
        if not created.ok:
            message = created.text or ""
            if "not configured" in message.lower():
                return _blocked(run_dir, report, "the search API key is not configured")
            print(f"TASKS_REAL_STATUS: FAIL - {message}")
            report["tasks_real_status"] = "FAIL"
            return EXIT_FAIL
        task_id = (created.structured or {}).get("task_id")
        report["task_id_created"] = bool(task_id)

        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        payload: dict = {"status": "pending"}
        while time.monotonic() < deadline:
            result = asyncio.run(
                client.call_tool(
                    "get_latest_search_run",
                    {"chat_id": chat["id"], "task_id": task_id},
                )
            )
            if result.ok:
                payload = result.structured or {"status": "pending"}
                if payload.get("status") in ("ok", "empty", "error"):
                    break
            time.sleep(1.0)

        report["run_status"] = payload.get("status")
        report["run_count"] = payload.get("result_count")
        status = payload.get("status")
        if status == "ok":
            print(f"TASKS_REAL_STATUS: PASS - {payload.get('result_count', 0)} result(s)")
            tasks_real_status = "PASS"
            exit_code = EXIT_OK
        elif status == "empty":
            print("TASKS_REAL_STATUS: PASS - the real search returned no results")
            tasks_real_status = "PASS"
            exit_code = EXIT_OK
        elif status == "error":
            message = str(payload.get("error") or "")
            if any(
                marker in message
                for marker in ("not configured", "HTTP 401", "HTTP 403", "HTTP 429", "HTTP 432", "HTTP 433")
            ):
                return _blocked(run_dir, report, "the search service is unavailable")
            print(f"TASKS_REAL_STATUS: FAIL - {message}")
            report["tasks_real_status"] = "FAIL"
            return EXIT_FAIL
        else:
            print("TASKS_REAL_STATUS: FAIL - no run finished within the timeout")
            report["tasks_real_status"] = "FAIL"
            return EXIT_FAIL
        return exit_code
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"TASKS_REAL_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["tasks_real_status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        if mcp_process is not None:
            report.setdefault("stopped", []).append(mcp_process.stop())
        report.setdefault("tasks_real_status", tasks_real_status)
        report["status"] = "pass" if exit_code == EXIT_OK else "blocked_or_failed"
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Manual standalone entry point; there is no ``test.bat`` mode for it.

    Run it by hand like ``harness/tavily_live.py``::

        .venv\\Scripts\\python.exe harness\\tasks_real_live.py

    It only performs the paid call when ``TASKS_REAL_ALLOW=1`` is set; without
    it (or without a configured key) it reports ``TASKS_REAL_STATUS: BLOCKED``.
    """
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
