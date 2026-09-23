"""Real (opt-in) live check of the ``search_web`` tool against Tavily Search.

Unlike the smoke run this harness talks to the paid Tavily Search API and spends
the operator's quota, so it is gated by an explicit opt-in:

* the environment must contain ``TAVILY_LIVE_ALLOW=1`` (the operator's explicit
  permission); otherwise the script reports ``BLOCKED`` and exits 2 without
  starting anything;
* the MCP server is started on its own test port **without**
  ``MCP_LOAD_DOTENV=0``, so it reads the real key from the local ``.env`` itself;
  the harness never reads the key and never prints it.

One direct ``search_web`` call is made through the real MCP client. A missing
key is reported as a prerequisite (``TAVILY_STATUS: BLOCKED``, exit 2), never as
a pass. Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED/PREREQUISITE.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# The .bat entry points run this file directly (``python harness\tavily_live.py``),
# which puts ``harness\`` on sys.path instead of the project root. Add the root
# explicitly so the ``harness.*`` imports resolve.
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

MCP_TEST_PORT = 8768
MCP_READY_TIMEOUT_SECONDS = 45.0
TAVILY_QUERY = "Python programming language official documentation"
ALLOW_ENV = "TAVILY_LIVE_ALLOW"


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


def run() -> int:
    """Run the opt-in Tavily live check and return its exit code."""
    port = int(os.environ.get("TAVILY_TEST_PORT") or MCP_TEST_PORT)
    run_dir = create_run_dir("tavily-live")
    report: dict = {"scenario": "tavily-live", "port": port}

    if str(os.environ.get(ALLOW_ENV) or "").strip() != "1":
        print(
            "TAVILY_STATUS: BLOCKED - real Tavily Search calls are opt-in; "
            f"set {ALLOW_ENV}=1 to allow one call and run this mode again."
        )
        report["status"] = "blocked"
        report["tavily_status"] = "BLOCKED"
        report["reason"] = "explicit opt-in is not set"
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")
        return EXIT_PREREQUISITE

    try:
        require_free_ports([port])
    except PrerequisiteError as exc:
        print(f"TAVILY_STATUS: BLOCKED - {exc}")
        report["status"] = "blocked"
        report["tavily_status"] = "BLOCKED"
        report["reason"] = str(exc)
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")
        return EXIT_PREREQUISITE

    mcp_url = f"http://127.0.0.1:{port}/mcp"
    mcp_process: ManagedProcess | None = None
    exit_code = EXIT_FAIL
    tavily_status = "FAIL"
    try:
        # No MCP_LOAD_DOTENV here on purpose: the MCP server reads the real key
        # from the local .env itself. sanitized_env never forwards the key.
        mcp_process = ManagedProcess(
            name="mcp-server",
            args=python_module("mcp_server"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {"MCP_SERVER_HOST": "127.0.0.1", "MCP_SERVER_PORT": str(port)}
            ),
            log_path=run_dir / "mcp_server.log",
        ).start()
        if not wait_tcp("127.0.0.1", port, MCP_READY_TIMEOUT_SECONDS, mcp_process):
            print("TAVILY_STATUS: FAIL - the MCP server did not start")
            report["tavily_status"] = "FAIL"
            return EXIT_FAIL
        if not _mcp_ready(mcp_url, MCP_READY_TIMEOUT_SECONDS):
            print("TAVILY_STATUS: FAIL - the MCP server did not complete a handshake")
            report["tavily_status"] = "FAIL"
            return EXIT_FAIL

        client = SdkMcpClient(mcp_url, call_timeout_s=30.0)
        result = asyncio.run(
            client.call_tool("search_web", {"query": TAVILY_QUERY, "max_results": 3})
        )
        if not result.ok:
            message = result.text or ""
            report["tool_message"] = message
            lowered = message.lower()
            if "not configured" in lowered:
                print(
                    "TAVILY_STATUS: BLOCKED - the search API key is not configured "
                    "on the MCP server."
                )
                report["tavily_status"] = "BLOCKED"
                report["reason"] = "search_not_configured"
                return EXIT_PREREQUISITE
            if "HTTP 401" in message or "HTTP 403" in message:
                print("TAVILY_STATUS: BLOCKED - Tavily rejected the configured key.")
                report["tavily_status"] = "BLOCKED"
                report["reason"] = "credentials_rejected"
                return EXIT_PREREQUISITE
            if "HTTP 432" in message or "HTTP 433" in message:
                print("TAVILY_STATUS: BLOCKED - the Tavily quota was reached.")
                report["tavily_status"] = "BLOCKED"
                report["reason"] = "quota_exceeded"
                return EXIT_PREREQUISITE
            if "HTTP 429" in message:
                print("TAVILY_STATUS: BLOCKED - the Tavily rate limit was reached.")
                report["tavily_status"] = "BLOCKED"
                report["reason"] = "rate_limited"
                return EXIT_PREREQUISITE
            print(f"TAVILY_STATUS: FAIL - {message}")
            report["tavily_status"] = "FAIL"
            return EXIT_FAIL

        payload = result.structured or {}
        report["count"] = payload.get("count")
        report["urls"] = [item.get("url") for item in payload.get("results", [])]
        print(f"TAVILY_STATUS: PASS - {payload.get('count', 0)} result(s)")
        for item in payload.get("results", []):
            print(f"  {item.get('title')} -> {item.get('url')}")
        tavily_status = "PASS"
        exit_code = EXIT_OK
        return exit_code
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"TAVILY_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["tavily_status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        if mcp_process is not None:
            report.setdefault("stopped", []).append(mcp_process.stop())
        report.setdefault("tavily_status", tavily_status)
        report["status"] = "pass" if exit_code == EXIT_OK else "blocked_or_failed"
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Manual standalone entry point; there is no ``test.bat`` mode for it.

    Run it by hand like ``harness\\mcp_unavailable_e2e.py``::

        .venv\\Scripts\\python.exe harness\\tavily_live.py

    It only performs the paid call when ``TAVILY_LIVE_ALLOW=1`` is set; without
    it (or without a configured key) it reports ``TAVILY_STATUS: BLOCKED``.
    """
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
