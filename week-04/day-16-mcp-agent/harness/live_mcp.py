"""Live MCP smoke test: real MCP server, real backend, real HTTP transport.

Everything here is a real process or a real HTTP call:

* the MCP server runs as its own process on a private test port;
* a deterministic model stub stands in for the model (documented as a double);
* the backend runs as its own process and talks to the real MCP server;
* the discovery CLI connects with the real official SDK over Streamable HTTP;
* the integration test modules then exercise the same live endpoints.

No mock replaces the MCP transport. Only processes started here are stopped.
Exit codes: 0 PASS, 1 FAIL, 2 prerequisite.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

# The .bat entry points run this file directly (``python harness\live_mcp.py``),
# which puts ``harness\`` on sys.path instead of the project root. Add the root
# explicitly so the ``harness.*`` and ``tests.*`` imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from harness.qa_bridge import PROJECT_DIR, ensure_paths, qa_port_is_free
from harness.processes import (
    ManagedProcess,
    PrerequisiteError,
    http_ok,
    python_module,
    require_free_ports,
    sanitized_env,
    wait_http,
    wait_tcp,
)
from harness.run_dir import create_run_dir, relative, write_report

ensure_paths()

from agent.mcp_adapter import inspect_tools  # noqa: E402
from tests.support.fake_search import FAKE_API_KEY, FakeSearchServer  # noqa: E402
from tests.support.stub_model import MODEL_ID, StubModelServer  # noqa: E402

try:
    import httpx  # noqa: E402
except ImportError:  # pragma: no cover - httpx is a pinned dependency
    httpx = None

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_MCP_TEST_PORT = 8766
DEFAULT_BACKEND_TEST_PORT = 8601
DEFAULT_STUB_PORT = 8099
# A loopback port that is deliberately left unused, to prove the unreachable path.
DEFAULT_UNREACHABLE_PORT = 8790

MCP_READY_TIMEOUT_SECONDS = 45.0
BACKEND_READY_TIMEOUT_SECONDS = 60.0
INTEGRATION_TIMEOUT_SECONDS = 300
NO_TOOL_CHAT_TIMEOUT_SECONDS = 60.0

# One probe (mode="auto") sends `server/discover` and `tools/list`, so one MCP
# session is two outgoing POST records in the backend log. The duplicate-session
# regression produced four; this constant is checked against the real log.
# The MCP SDK v2 uses httpx2, whose INFO log line looks like:
#   INFO:httpx2:HTTP Request: POST http://127.0.0.1:8766/mcp "HTTP/1.1 200 OK"
EXPECTED_MCP_POSTS_PER_PROBE = 2
# The regression probe window must not overlap any other MCP traffic.
NO_TOOL_MESSAGE = "Hello there"


def _mcp_ready(url: str, timeout: float) -> bool:
    """Poll a real MCP handshake until the server answers or time runs out."""
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


def _run(command: list, env: dict, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(item) for item in command],
        cwd=str(PROJECT_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _status_from_output(text: str, prefix: str) -> str:
    """Return the last ``<prefix>: <STATUS>`` token of a harness output."""
    value = "UNKNOWN"
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix + ":"):
            remainder = stripped.split(":", 1)[1].strip()
            value = remainder.split()[0] if remainder else "UNKNOWN"
    return value


def _run_discovery(url: str, env: dict) -> subprocess.CompletedProcess:
    return _run(
        [sys.executable, str(PROJECT_DIR / "discovery_cli.py"), "--url", url],
        env,
        90,
    )


def _run_unittest(target: str, env: dict) -> subprocess.CompletedProcess:
    return _run(
        [sys.executable, "-m", "unittest", "-v", target],
        env,
        INTEGRATION_TIMEOUT_SECONDS,
    )


def _tail(text: str, limit: int = 4000) -> str:
    return (text or "")[-limit:]


def _read_log(process: ManagedProcess) -> str:
    """Read the whole captured backend log, or an empty string."""
    if process is None or process.log_path is None:
        return ""
    try:
        return Path(process.log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _count_mcp_posts(text: str) -> int:
    """Count the backend's outgoing MCP POST records.

    The MCP SDK v2 uses httpx2, which logs every request at INFO; the server
    side of the loopback MCP connection is a separate process, so these lines
    are the only record of the backend's MCP traffic.
    """
    return sum(
        1
        for line in (text or "").splitlines()
        if "HTTP Request: POST" in line and "/mcp " in line
    )


def _create_chat(backend_url: str, title: str = "") -> str:
    """Create a chat through the real API and return its opaque id."""
    response = httpx.post(
        f"{backend_url}/api/chats", json={"title": title}, timeout=20.0
    )
    response.raise_for_status()
    return response.json()["id"]


def _check_mcp_probe_count(backend_url: str, process: ManagedProcess, report: dict) -> bool:
    """Isolated no-tool chat request: exactly one MCP probe must be opened.

    Regression for the duplicated-session bug, where one chat request opened the
    MCP session twice. The backend log window is measured around a single
    request that never calls a tool, so all ``POST /mcp`` lines in the window
    belong to the orchestrator's tool-list probe.
    """
    if httpx is None:
        report["mcp_probe_regression"] = {
            "ok": None,
            "reason": "httpx is not installed; the regression was not checked",
        }
        return True
    before = _read_log(process)
    try:
        chat_id = _create_chat(backend_url, "probe")
        response = httpx.post(
            f"{backend_url}/api/chat/stream",
            json={"chat_id": chat_id, "message": NO_TOOL_MESSAGE},
            timeout=httpx.Timeout(
                connect=10.0, read=NO_TOOL_CHAT_TIMEOUT_SECONDS, write=10.0, pool=10.0
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reported as a failed regression
        report["mcp_probe_regression"] = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
        return False
    window = _read_log(process)[len(before):]
    posts = _count_mcp_posts(window)
    ok = response.status_code == 200 and posts == EXPECTED_MCP_POSTS_PER_PROBE
    report["mcp_probe_regression"] = {
        "ok": ok,
        "posts": posts,
        "expected": EXPECTED_MCP_POSTS_PER_PROBE,
        "status_code": response.status_code,
    }
    print(
        "MCP_PROBE_REGRESSION: "
        + ("PASS" if ok else "FAIL")
        + f" (posts={posts}, expected={EXPECTED_MCP_POSTS_PER_PROBE})"
    )
    return ok


def run() -> int:
    """Execute the smoke scenario and return its exit code."""
    mcp_port = int(os.environ.get("MCP_TEST_PORT") or DEFAULT_MCP_TEST_PORT)
    backend_port = int(
        os.environ.get("BACKEND_TEST_PORT") or DEFAULT_BACKEND_TEST_PORT
    )
    stub_port = int(os.environ.get("STUB_MODEL_TEST_PORT") or DEFAULT_STUB_PORT)
    unreachable_port = int(
        os.environ.get("MCP_UNREACHABLE_PORT") or DEFAULT_UNREACHABLE_PORT
    )

    run_dir = create_run_dir("live-mcp")
    db_path = run_dir / "day18.sqlite3"
    report: dict = {
        "scenario": "live-mcp-smoke",
        "ports": {
            "mcp": mcp_port,
            "backend": backend_port,
            "stub_model": stub_port,
        },
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
    unreachable_url = f"http://127.0.0.1:{unreachable_port}/mcp"

    mcp_process: ManagedProcess | None = None
    backend_process: ManagedProcess | None = None
    stub: StubModelServer | None = None
    search_server: FakeSearchServer | None = None
    cleanup: list = []
    exit_code = EXIT_FAIL

    try:
        stub = StubModelServer(stub_port).start()
        report["stub_model"] = {"url": stub.base_url, "model": MODEL_ID}

        # The fake search API keeps the live MCP smoke independent from the
        # paid Tavily service. The key and base URL are passed only to the MCP
        # child process; MCP_LOAD_DOTENV=0 keeps it away from a local .env.
        search_server = FakeSearchServer(0).start()
        report["fake_search"] = {"loopback_port": search_server.port}

        mcp_process = ManagedProcess(
            name="mcp-server",
            args=python_module("mcp_server"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "MCP_SERVER_HOST": "127.0.0.1",
                    "MCP_SERVER_PORT": str(mcp_port),
                    "MCP_LOAD_DOTENV": "0",
                    "MCP_TASK_TICK_SECONDS": "0.5",
                    "MCP_SEARCH_API_KEY_ENV": "TAVILY_API_KEY",
                    "TAVILY_API_KEY": FAKE_API_KEY,
                    "MCP_SEARCH_BASE_URL": search_server.base_url,
                    "MCP_SEARCH_TIMEOUT_SECONDS": "3",
                    "MCP_SEARCH_MAX_RESULTS": "5",
                    "AGENT_DB_PATH": str(db_path),
                }
            ),
            log_path=run_dir / "mcp_server.log",
        ).start()
        cleanup.append(mcp_process)

        if not wait_tcp("127.0.0.1", mcp_port, MCP_READY_TIMEOUT_SECONDS, mcp_process):
            print("FAIL: the MCP server did not start")
            print(_tail(mcp_process.tail_log()))
            report["mcp_server_started"] = False
            return EXIT_FAIL
        if not _mcp_ready(mcp_url, MCP_READY_TIMEOUT_SECONDS):
            print("FAIL: the MCP server did not complete a handshake")
            print(_tail(mcp_process.tail_log()))
            report["mcp_handshake"] = False
            return EXIT_FAIL
        report["mcp_handshake"] = True

        backend_process = ManagedProcess(
            name="backend",
            args=python_module("agent"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "BACKEND_HOST": "127.0.0.1",
                    "BACKEND_PORT": str(backend_port),
                    "MCP_SERVER_URL": mcp_url,
                    "MCP_SERVER_HOST": "127.0.0.1",
                    "MCP_SERVER_PORT": str(mcp_port),
                    "AGENT_MODEL_BASE_URL": stub.base_url,
                    "AGENT_MODEL_NAME": MODEL_ID,
                    "AGENT_MODEL_API_KEY_ENV": "LOCAL_LLM_API_KEY",
                    "LOCAL_LLM_API_KEY": "local-e2e",
                    "AGENT_MODEL_TIMEOUT_SECONDS": "30",
                    "AGENT_TRACE_PATH": str(run_dir / "trace.jsonl"),
                    "AGENT_LOG_LEVEL": "INFO",
                    "AGENT_DB_PATH": str(db_path),
                }
            ),
            log_path=run_dir / "backend.log",
        ).start()
        cleanup.append(backend_process)

        if not wait_http(f"{backend_url}/api/health", BACKEND_READY_TIMEOUT_SECONDS, backend_process):
            print("FAIL: the backend did not become ready")
            print(_tail(backend_process.tail_log()))
            report["backend_started"] = False
            return EXIT_FAIL
        report["backend_started"] = True

        if not _check_mcp_probe_count(backend_url, backend_process, report):
            print("FAIL: a no-tool chat request did not open exactly one MCP probe")
            return EXIT_FAIL

        discovery = _run_discovery(mcp_url, sanitized_env())
        discovery_ok = (
            discovery.returncode == 0
            and "CONNECTED" in discovery.stdout
            and "TOOLS_COUNT" in discovery.stdout
            and "PROTOCOL_VERSION" in discovery.stdout
        )
        print("--- discovery CLI ---")
        print(discovery.stdout.strip() or discovery.stderr.strip())
        report["discovery"] = {
            "ok": discovery_ok,
            "exit_code": discovery.returncode,
            "stdout": _tail(discovery.stdout, 2000),
        }
        if not discovery_ok:
            print("FAIL: the discovery CLI did not report the connected server")
            return EXIT_FAIL

        negative = _run_discovery(unreachable_url, sanitized_env())
        negative_ok = negative.returncode == 2 and "ERROR_CATEGORY" in negative.stdout
        print("--- discovery CLI (unreachable endpoint) ---")
        print(negative.stdout.strip() or negative.stderr.strip())
        report["discovery_unreachable"] = {
            "ok": negative_ok,
            "exit_code": negative.returncode,
        }
        if not negative_ok:
            print("FAIL: the discovery CLI did not report a controlled error")
            return EXIT_FAIL

        if not http_ok(f"{backend_url}/openapi.json"):
            print("FAIL: the backend does not serve /openapi.json")
            report["openapi"] = False
            return EXIT_FAIL

        test_env = sanitized_env(
            {
                "RUN_LIVE_MCP": "1",
                "MCP_TEST_URL": mcp_url,
                "BACKEND_TEST_URL": backend_url,
                "MCP_UNREACHABLE_URL": unreachable_url,
                "TASKS_TEST_DB": str(db_path),
                "FAKE_SEARCH_URL": search_server.base_url,
            }
        )

        mcp_tests = _run_unittest("tests.integration.test_mcp_live", test_env)
        print("--- integration: MCP ---")
        print(_tail(mcp_tests.stdout or mcp_tests.stderr))
        report["mcp_integration"] = {
            "ok": mcp_tests.returncode == 0,
            "exit_code": mcp_tests.returncode,
        }
        print(
            "MCP_INTEGRATION_STATUS: "
            + ("PASS" if mcp_tests.returncode == 0 else "FAIL")
        )

        backend_tests = _run_unittest("tests.integration.test_backend_live", test_env)
        print("--- integration: backend ---")
        print(_tail(backend_tests.stdout or backend_tests.stderr))
        report["backend_integration"] = {
            "ok": backend_tests.returncode == 0,
            "exit_code": backend_tests.returncode,
        }
        print(
            "BACKEND_INTEGRATION_STATUS: "
            + ("PASS" if backend_tests.returncode == 0 else "FAIL")
        )

        search_tests = _run_unittest("tests.integration.test_search_live", test_env)
        print("--- integration: web search ---")
        print(_tail(search_tests.stdout or search_tests.stderr))
        report["search_integration"] = {
            "ok": search_tests.returncode == 0,
            "exit_code": search_tests.returncode,
        }
        print(
            "SEARCH_INTEGRATION_STATUS: "
            + ("PASS" if search_tests.returncode == 0 else "FAIL")
        )

        tasks_tests = _run_unittest("tests.integration.test_tasks_live", test_env)
        print("--- integration: scheduled tasks ---")
        print(_tail(tasks_tests.stdout or tasks_tests.stderr))
        report["tasks_integration"] = {
            "ok": tasks_tests.returncode == 0,
            "exit_code": tasks_tests.returncode,
        }
        print(
            "TASKS_INTEGRATION_STATUS: "
            + ("PASS" if tasks_tests.returncode == 0 else "FAIL")
        )

        # The restart checks run their own processes on private ports, so the
        # running smoke processes do not interfere.
        restart = _run(
            [sys.executable, str(PROJECT_DIR / "harness" / "scheduler_restart.py")],
            sanitized_env(),
            INTEGRATION_TIMEOUT_SECONDS,
        )
        print("--- restart: persistence and scheduler ---")
        print(_tail(restart.stdout or restart.stderr))
        persistence_status = _status_from_output(
            restart.stdout, "PERSISTENCE_RESTART_STATUS"
        )
        scheduler_status = _status_from_output(
            restart.stdout, "SCHEDULER_RESTART_STATUS"
        )
        if restart.returncode == 2:
            persistence_status = scheduler_status = "BLOCKED"
        report["restart"] = {
            "exit_code": restart.returncode,
            "persistence_status": persistence_status,
            "scheduler_status": scheduler_status,
        }
        print(f"PERSISTENCE_RESTART_STATUS: {persistence_status}")
        print(f"SCHEDULER_RESTART_STATUS: {scheduler_status}")

        ok = (
            mcp_tests.returncode == 0
            and backend_tests.returncode == 0
            and search_tests.returncode == 0
            and tasks_tests.returncode == 0
            and restart.returncode == 0
        )
        exit_code = EXIT_OK if ok else EXIT_FAIL
        return exit_code
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        return EXIT_PREREQUISITE
    except subprocess.TimeoutExpired as exc:
        print(f"FAIL: a smoke step timed out: {exc}")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 - the smoke run reports, never crashes
        print(f"FAIL: the smoke run failed: {exc}")
        return EXIT_FAIL
    finally:
        for process in reversed(cleanup):
            result = process.stop()
            report.setdefault("stopped", []).append(result)
        if stub is not None:
            stub.stop()
        if search_server is not None:
            search_server.stop()
        report["ports_released"] = {
            "mcp": qa_port_is_free(mcp_port, "127.0.0.1"),
            "backend": qa_port_is_free(backend_port, "127.0.0.1"),
        }
        report["status"] = "pass" if exit_code == EXIT_OK else "fail"
        report["trace"] = relative(run_dir / "trace.jsonl")
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point used by ``smoke_test.bat``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
