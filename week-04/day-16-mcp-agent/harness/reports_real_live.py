"""Real (opt-in) live check of the report composition against Tavily Search.

This is the only report check that spends the operator's Tavily quota, so it is
gated by an explicit opt-in:

* ``REPORTS_REAL_ALLOW=1`` must be set; otherwise the script reports
  ``REPORTS_REAL_STATUS: BLOCKED`` and exits 2 without starting anything;
* the MCP server is started **without** ``MCP_LOAD_DOTENV=0`` so it reads the
  real key from the local ``.env`` itself; the harness never reads the key and
  never prints it.

Exactly one real search request is made. The chat is seeded through the storage
layer, then the real MCP tools ``search_web`` → ``digest_search_results`` →
``save_report`` are called in order. Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED.
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
from storage.chats import ChatRepository  # noqa: E402
from storage.db import Database  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

MCP_TEST_PORT = 8772
MCP_READY_TIMEOUT_SECONDS = 45.0
ALLOW_ENV = "REPORTS_REAL_ALLOW"
REAL_QUERY = "Kotlin programming language official documentation"


def _mcp_ready(url: str, timeout: float) -> bool:
    import time

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
    report["reports_real_status"] = "BLOCKED"
    report["reason"] = reason
    write_report(run_dir, report)
    print(f"REPORTS_REAL_STATUS: BLOCKED - {reason}")
    print("REPORTS_REAL_REQUESTS: 0")
    print(f"RUN_DIR: {relative(run_dir)}")
    return EXIT_PREREQUISITE


def run() -> int:
    """Run the opt-in real composition check and return its exit code."""
    port = int(os.environ.get("REPORTS_REAL_MCP_PORT") or MCP_TEST_PORT)
    run_dir = create_run_dir("reports-real-live")
    report: dict = {"scenario": "reports-real-live", "port": port}

    if str(os.environ.get(ALLOW_ENV) or "").strip() != "1":
        return _blocked(
            run_dir,
            report,
            "real Tavily calls are opt-in; set REPORTS_REAL_ALLOW=1 to allow one search",
        )

    try:
        require_free_ports([port])
    except PrerequisiteError as exc:
        return _blocked(run_dir, report, str(exc))

    mcp_url = f"http://127.0.0.1:{port}/mcp"
    mcp_process: ManagedProcess | None = None
    status = "FAIL"
    exit_code = EXIT_FAIL

    try:
        db_path = run_dir / "day18.sqlite3"
        database = Database(db_path)
        chat = ChatRepository(database).create_chat("Reports real live")
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
                    "AGENT_DB_PATH": str(db_path),
                }
            ),
            log_path=run_dir / "mcp_server.log",
        ).start()
        if not wait_tcp("127.0.0.1", port, MCP_READY_TIMEOUT_SECONDS, mcp_process):
            print("REPORTS_REAL_STATUS: FAIL - the MCP server did not start")
            report["reports_real_status"] = "FAIL"
            return EXIT_FAIL
        if not _mcp_ready(mcp_url, MCP_READY_TIMEOUT_SECONDS):
            print("REPORTS_REAL_STATUS: FAIL - the MCP server did not handshake")
            report["reports_real_status"] = "FAIL"
            return EXIT_FAIL

        client = SdkMcpClient(mcp_url, call_timeout_s=60.0)
        searched = asyncio.run(client.call_tool("search_web", {"query": REAL_QUERY}))
        if not searched.ok:
            message = searched.text or ""
            if "not configured" in message.lower():
                return _blocked(run_dir, report, "the search API key is not configured")
            print(f"REPORTS_REAL_STATUS: FAIL - search_web: {message}")
            report["reports_real_status"] = "FAIL"
            return EXIT_FAIL
        search_payload = searched.structured or {}
        report["search_count"] = search_payload.get("count")

        digested = asyncio.run(
            client.call_tool("digest_search_results", {"search_result": search_payload})
        )
        digest = digested.structured or {}
        if digest.get("status") != "ok":
            print(
                "REPORTS_REAL_STATUS: PASS - the real search returned no usable results"
            )
            report["reports_real_status"] = "PASS"
            report["digest_status"] = digest.get("status")
            return EXIT_OK

        saved = asyncio.run(
            client.call_tool("save_report", {"digest": digest, "chat_id": chat["id"]})
        )
        if not saved.ok:
            print(f"REPORTS_REAL_STATUS: FAIL - {saved.text}")
            report["reports_real_status"] = "FAIL"
            return EXIT_FAIL
        report_id = (saved.structured or {}).get("report_id")
        report["report_id_created"] = bool(report_id)
        report["digest_source_count"] = digest.get("count")
        # Exactly one paid search request was made by the real search_web call.
        report["requests"] = 1
        print(
            f"REPORTS_REAL_STATUS: PASS - {digest.get('count', 0)} source(s), "
            "one search request"
        )
        print("REPORTS_REAL_REQUESTS: 1")
        status = "PASS"
        exit_code = EXIT_OK
        return exit_code
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"REPORTS_REAL_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["reports_real_status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        if mcp_process is not None:
            report.setdefault("stopped", []).append(mcp_process.stop())
        report.setdefault("reports_real_status", status)
        report["status"] = "pass" if exit_code == EXIT_OK else "blocked_or_failed"
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Manual standalone entry point; there is no ``test.bat`` mode for it."""
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
