"""Restart checks for persistence and the background scheduler (D18-06/D18-18).

Two real restart scenarios over one shared temporary database:

* ``PERSISTENCE_RESTART_STATUS``: a chat and its messages survive an actual
  backend restart (the process is stopped and started again).
* ``SCHEDULER_RESTART_STATUS``: with the backend stopped, the MCP server is
  restarted while an overdue task sits in the database; the scheduler must run
  exactly one catch-up run and must not backfill every missed slot.

The MCP server, the backend, the model stub and the fake search API are real
loopback processes; nothing is mocked and no paid API is called. Exit codes:
0 PASS, 1 FAIL, 2 prerequisite.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# The .bat entry points run this file directly (``python harness\scheduler_restart.py``),
# which puts ``harness\`` on sys.path instead of the project root. Add the root
# explicitly so the ``harness.*`` and ``tests.*`` imports resolve.
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

from storage.chats import ChatRepository  # noqa: E402
from storage.db import Database  # noqa: E402
from storage.tasks import TaskRepository  # noqa: E402
from tests.support.fake_search import FAKE_API_KEY, FakeSearchServer  # noqa: E402
from tests.support.stub_model import MODEL_ID, StubModelServer  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_MCP_PORT = 8769
DEFAULT_BACKEND_PORT = 8604
DEFAULT_STUB_PORT = 8100

MCP_READY_TIMEOUT_SECONDS = 45.0
BACKEND_READY_TIMEOUT_SECONDS = 60.0
RUN_POLL_TIMEOUT_SECONDS = 30.0
NO_TOOL_MESSAGE = "Hello there"

TASK_QUERY = "python documentation restart"
TASK_INTERVAL_SECONDS = 3600


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
            "MCP_TASK_TICK_SECONDS": "0.5",
            "MCP_SEARCH_API_KEY_ENV": "TAVILY_API_KEY",
            "TAVILY_API_KEY": FAKE_API_KEY,
            "MCP_SEARCH_BASE_URL": search_server.base_url,
            "MCP_SEARCH_TIMEOUT_SECONDS": "3",
            "MCP_SEARCH_MAX_RESULTS": "5",
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


def _start_backend(port: int, mcp_url: str, model_url: str, db_path: Path, run_dir: Path) -> ManagedProcess:
    process = ManagedProcess(
        name="backend",
        args=python_module("agent"),
        cwd=PROJECT_DIR,
        env=_backend_env(port, mcp_url, model_url, db_path, run_dir),
        log_path=run_dir / "backend.log",
    ).start()
    if not wait_http(f"http://127.0.0.1:{port}/api/health", BACKEND_READY_TIMEOUT_SECONDS, process):
        raise PrerequisiteError("the backend did not start")
    return process


def _run_count(db_path: Path, task_id: str) -> int:
    return int(TaskRepository(Database(db_path)).run_count(task_id))


def run() -> int:
    """Execute the restart scenarios and return an exit code."""
    mcp_port = int(os.environ.get("SCHEDULER_RESTART_MCP_PORT") or DEFAULT_MCP_PORT)
    backend_port = int(
        os.environ.get("SCHEDULER_RESTART_BACKEND_PORT") or DEFAULT_BACKEND_PORT
    )
    stub_port = int(os.environ.get("SCHEDULER_RESTART_STUB_PORT") or DEFAULT_STUB_PORT)

    run_dir = create_run_dir("scheduler-restart")
    db_path = run_dir / "day18.sqlite3"
    report: dict = {
        "scenario": "scheduler-restart",
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
    persistence_status = "FAIL"
    scheduler_status = "FAIL"
    exit_code = EXIT_FAIL

    try:
        stub = StubModelServer(stub_port).start()
        search_server = FakeSearchServer(0).start()
        report["fake_search"] = {"loopback_port": search_server.port}

        mcp_process = _start_mcp(mcp_port, db_path, search_server, run_dir)
        backend_process = _start_backend(
            backend_port, mcp_url, stub.base_url, db_path, run_dir
        )

        # --- persistence: chat + messages survive a backend restart ---------
        created = httpx.post(
            f"{backend_url}/api/chats", json={"title": "Persistent"}, timeout=20.0
        )
        created.raise_for_status()
        chat_id = created.json()["id"]
        streamed = httpx.post(
            f"{backend_url}/api/chat/stream",
            json={"chat_id": chat_id, "message": NO_TOOL_MESSAGE},
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        )
        report["persistence_stream_status"] = streamed.status_code
        backend_process.stop()
        backend_process = _start_backend(
            backend_port, mcp_url, stub.base_url, db_path, run_dir
        )
        listed = httpx.get(f"{backend_url}/api/chats", timeout=20.0).json()
        history = httpx.get(
            f"{backend_url}/api/chats/{chat_id}/messages", timeout=20.0
        ).json()
        persisted = (
            chat_id in [chat["id"] for chat in listed["chats"]]
            and history["count"] >= 2
        )
        persistence_status = "PASS" if persisted else "FAIL"
        report["persistence"] = {
            "chat_present": chat_id in [chat["id"] for chat in listed["chats"]],
            "message_count": history["count"],
        }

        # --- scheduler restart: exactly one catch-up run, no backfill -------
        backend_process.stop()
        backend_process = None

        tasks = TaskRepository(Database(db_path))
        overdue = tasks.create_task(
            chat_id,
            TASK_QUERY,
            TASK_INTERVAL_SECONDS,
            max_results=1,
            now=time.time() - 7200,
            next_run_at=time.time() - 3600,
        )
        mcp_process.stop()
        mcp_process = _start_mcp(mcp_port, db_path, search_server, run_dir)

        deadline = time.monotonic() + RUN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline and _run_count(db_path, overdue["id"]) < 1:
            time.sleep(0.4)
        first_count = _run_count(db_path, overdue["id"])
        # No backfill: no further run is produced by the missed slots.
        time.sleep(2.0)
        second_count = _run_count(db_path, overdue["id"])
        updated = tasks.get_task(overdue["id"])
        latest = tasks.latest_run(chat_id, overdue["id"])
        result_count = int(latest["result_count"]) if latest else 0
        # The task stores max_results=1, so the fake search's three results must
        # have been trimmed to one; the limit must survive the restart.
        limit_ok = result_count <= 1
        scheduler_ok = first_count == 1 and second_count == 1 and limit_ok
        scheduler_status = "PASS" if scheduler_ok else "FAIL"
        report["scheduler_restart"] = {
            "runs_after_restart": first_count,
            "runs_after_settle": second_count,
            "result_count": result_count,
            "max_results": int(updated["max_results"]) if updated else None,
            "next_run_at": updated["next_run_at"] if updated else None,
            "last_status": updated["last_status"] if updated else None,
        }

        exit_code = EXIT_OK if (persisted and scheduler_ok) else EXIT_FAIL
        return exit_code
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        return EXIT_PREREQUISITE
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"FAIL: the restart scenario failed: {type(exc).__name__}: {exc}")
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
        report["persistence_restart_status"] = persistence_status
        report["scheduler_restart_status"] = scheduler_status
        report["ports_released"] = {
            "mcp": qa_port_is_free(mcp_port, "127.0.0.1"),
            "backend": qa_port_is_free(backend_port, "127.0.0.1"),
        }
        report["status"] = "pass" if exit_code == EXIT_OK else "fail"
        write_report(run_dir, report)
        print(f"PERSISTENCE_RESTART_STATUS: {persistence_status}")
        print(f"SCHEDULER_RESTART_STATUS: {scheduler_status}")
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point used by ``harness/live_mcp.py``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
