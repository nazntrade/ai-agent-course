"""Restart check for saved reports (D19-11).

One real report is created through the real MCP tool, then the backend and the
MCP server are actually stopped and started again. After both restarts the report
must still be listed and readable over the real HTTP API, because it lives in the
shared SQLite file rather than in a process.

The MCP server, the backend, the model stub and the fake search API are real
loopback processes; nothing is mocked and no paid API is called. Exit codes:
0 PASS, 1 FAIL, 2 prerequisite.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# The .bat entry points run this file directly; restore the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx  # noqa: E402

from harness.qa_bridge import PROJECT_DIR, ensure_paths, qa_port_is_free  # noqa: E402
from harness.processes import (  # noqa: E402
    ManagedProcess,
    PrerequisiteError,
    python_module,
    require_free_ports,
    sanitized_env,
    wait_http,
    wait_tcp,
)
from harness.run_dir import create_run_dir, relative, write_report  # noqa: E402

ensure_paths()

from agent.mcp_adapter import SdkMcpClient  # noqa: E402
from mcp_server.reports import build_digest  # noqa: E402
from storage.chats import ChatRepository  # noqa: E402
from storage.db import Database  # noqa: E402
from tests.support.fake_search import FAKE_API_KEY, FakeSearchServer  # noqa: E402
from tests.support.stub_model import MODEL_ID, StubModelServer  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_MCP_PORT = 8771
DEFAULT_BACKEND_PORT = 8605
DEFAULT_STUB_PORT = 8101

MCP_READY_TIMEOUT_SECONDS = 45.0
BACKEND_READY_TIMEOUT_SECONDS = 60.0

SEARCH_RESULT = {
    "query": "Kotlin restart report",
    "count": 1,
    "results": [
        {
            "title": "Kotlin documentation",
            "url": "https://docs.example.test/restart/1",
            "description": "A deterministic source for the restart report.",
        }
    ],
    "more_results_available": False,
    "note": "Snippets only; the pages were not opened.",
}


def _backend_env(port: int, mcp_url: str, model_url: str, db_path: Path, run_dir: Path) -> dict:
    return sanitized_env(
        {
            "BACKEND_HOST": "127.0.0.1",
            "BACKEND_PORT": str(port),
            "MCP_SERVER_URL": mcp_url,
            "AGENT_MODEL_BASE_URL": model_url,
            "AGENT_MODEL_NAME": MODEL_ID,
            "AGENT_MODEL_API_KEY_ENV": "LOCAL_LLM_API_KEY",
            "LOCAL_LLM_API_KEY": "local-e2e",
            "AGENT_MODEL_TIMEOUT_SECONDS": "30",
            "AGENT_TRACE_PATH": str(run_dir / "trace.jsonl"),
            "AGENT_LOG_LEVEL": "INFO",
            "AGENT_DB_PATH": str(db_path),
        }
    )


def _mcp_env(port: int, db_path: Path, search_server: FakeSearchServer) -> dict:
    return sanitized_env(
        {
            "MCP_SERVER_HOST": "127.0.0.1",
            "MCP_SERVER_PORT": str(port),
            "MCP_LOAD_DOTENV": "0",
            "MCP_SEARCH_API_KEY_ENV": "TAVILY_API_KEY",
            "TAVILY_API_KEY": FAKE_API_KEY,
            "MCP_SEARCH_BASE_URL": search_server.base_url,
            "MCP_SEARCH_TIMEOUT_SECONDS": "3",
            "AGENT_DB_PATH": str(db_path),
        }
    )


def _start_mcp(port: int, db_path: Path, search_server, run_dir: Path) -> ManagedProcess:
    process = ManagedProcess(
        name="mcp-server",
        args=python_module("mcp_server"),
        cwd=PROJECT_DIR,
        env=_mcp_env(port, db_path, search_server),
        log_path=run_dir / "mcp_server.log",
    ).start()
    if not wait_tcp("127.0.0.1", port, MCP_READY_TIMEOUT_SECONDS, process):
        raise PrerequisiteError("the MCP server did not start")
    return process


def _start_backend(
    port: int, mcp_url: str, model_url: str, db_path: Path, run_dir: Path
) -> ManagedProcess:
    process = ManagedProcess(
        name="backend",
        args=python_module("agent"),
        cwd=PROJECT_DIR,
        env=_backend_env(port, mcp_url, model_url, db_path, run_dir),
        log_path=run_dir / "backend.log",
    ).start()
    if not wait_http(
        f"http://127.0.0.1:{port}/api/health", BACKEND_READY_TIMEOUT_SECONDS, process
    ):
        raise PrerequisiteError("the backend did not start")
    return process


def run() -> int:
    """Execute the report restart scenario and return an exit code."""
    mcp_port = int(os.environ.get("REPORTS_RESTART_MCP_PORT") or DEFAULT_MCP_PORT)
    backend_port = int(
        os.environ.get("REPORTS_RESTART_BACKEND_PORT") or DEFAULT_BACKEND_PORT
    )
    stub_port = int(os.environ.get("REPORTS_RESTART_STUB_PORT") or DEFAULT_STUB_PORT)

    run_dir = create_run_dir("reports-restart")
    db_path = run_dir / "day18.sqlite3"
    report: dict = {
        "scenario": "reports-restart",
        "ports": {"mcp": mcp_port, "backend": backend_port, "stub_model": stub_port},
        "db": relative(db_path),
    }

    try:
        require_free_ports([mcp_port, backend_port, stub_port])
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        write_report(run_dir, {**report, "status": "prerequisite", "reason": str(exc)})
        return EXIT_PREREQUISITE

    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    backend_url = f"http://127.0.0.1:{backend_port}"

    stub: StubModelServer | None = None
    search_server: FakeSearchServer | None = None
    mcp_process: ManagedProcess | None = None
    backend_process: ManagedProcess | None = None
    reports_status = "FAIL"
    exit_code = EXIT_FAIL

    try:
        stub = StubModelServer(stub_port).start()
        search_server = FakeSearchServer(0).start()
        report["fake_search"] = {"loopback_port": search_server.port}

        chat = ChatRepository(Database(db_path)).create_chat("Reports restart")
        chat_id = chat["id"]

        mcp_process = _start_mcp(mcp_port, db_path, search_server, run_dir)
        backend_process = _start_backend(
            backend_port, mcp_url, stub.base_url, db_path, run_dir
        )

        # Create the report through the real MCP tool (the same path the model
        # uses), not by writing the table directly.
        client = SdkMcpClient(mcp_url, call_timeout_s=30.0)
        import asyncio

        digest = build_digest(SEARCH_RESULT)
        saved = asyncio.run(
            client.call_tool(
                "save_report", {"digest": digest, "chat_id": chat_id}
            )
        )
        if not saved.ok:
            raise RuntimeError(f"save_report failed: {saved.text}")
        report_id = (saved.structured or {}).get("report_id")
        report["report_created"] = bool(report_id)

        def _listed():
            return httpx.get(
                f"{backend_url}/api/chats/{chat_id}/reports", timeout=20.0
            ).json()

        before = _listed()
        backend_process.stop()
        backend_process = _start_backend(
            backend_port, mcp_url, stub.base_url, db_path, run_dir
        )
        after_backend = _listed()

        mcp_process.stop()
        mcp_process = _start_mcp(mcp_port, db_path, search_server, run_dir)
        after_mcp = httpx.get(
            f"{backend_url}/api/chats/{chat_id}/reports/{report_id}", timeout=20.0
        )

        persist_backend = (
            before.get("count") == 1 and after_backend.get("count") == 1
        )
        persist_mcp = (
            after_mcp.status_code == 200
            and "Kotlin documentation" in after_mcp.json().get("summary", "")
        )
        reports_status = "PASS" if (persist_backend and persist_mcp) else "FAIL"
        report["reports_restart"] = {
            "count_before": before.get("count"),
            "count_after_backend_restart": after_backend.get("count"),
            "detail_after_mcp_restart_status": after_mcp.status_code,
            "persist_backend": persist_backend,
            "persist_mcp": persist_mcp,
        }

        exit_code = EXIT_OK if reports_status == "PASS" else EXIT_FAIL
        return exit_code
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        return EXIT_PREREQUISITE
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"FAIL: the reports restart scenario failed: {type(exc).__name__}: {exc}")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        for process in (backend_process, mcp_process):
            if process is not None:
                report.setdefault("stopped", []).append(process.stop())
        if stub is not None:
            stub.stop()
        if search_server is not None:
            search_server.stop()
        report["reports_restart_status"] = reports_status
        report["ports_released"] = {
            "mcp": qa_port_is_free(mcp_port, "127.0.0.1"),
            "backend": qa_port_is_free(backend_port, "127.0.0.1"),
        }
        report["status"] = "pass" if exit_code == EXIT_OK else "fail"
        write_report(run_dir, report)
        print(f"REPORTS_RESTART_STATUS: {reports_status}")
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point used by ``harness/live_mcp.py``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
