"""Live end-to-end scenario: model → MCP tool → model.

The harness starts the real MCP server and the real backend, ensures the local
OpenAI-compatible model is running (reusing the owner's configuration through
``qa/lib``, never a hard-coded path) and then drives one unambiguous arithmetic
request through the streamed chat API.

Success requires all of:

* a real ``tool_selected`` for ``calculate`` in the JSONL trace;
* a real ``tool_completed`` with a structured result;
* a second ``model_request`` (the final answer) and a ``request_done``;
* an SSE sequence ``tool_call`` → ``tool_result`` → ``delta`` → ``done``;
* a mathematically correct final answer in the streamed text;
* UI E2E through the system browser when ``--ui`` is requested.

The model is stopped only when this harness started it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

# The .bat entry points run this file directly (``python harness\live_e2e.py``),
# which puts ``harness\`` on sys.path instead of the project root. Add the root
# explicitly so the ``harness.*`` imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx

from harness.qa_bridge import (
    PROJECT_DIR,
    ensure_paths,
    qa_browser,
    qa_config,
    qa_discovery,
    qa_local_llm,
)
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
from agent.settings import DEFAULT_MODEL_NAME  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_MCP_TEST_PORT = 8767
DEFAULT_BACKEND_TEST_PORT = 8602

MCP_READY_TIMEOUT_SECONDS = 45.0
BACKEND_READY_TIMEOUT_SECONDS = 60.0

QUESTION = "What is 23 multiplied by 17?"
EXPECTED_ANSWER = "391"
EXPECTED_TOOL = "calculate"

# Each step is ``(event name, expected fields, non-empty fields)``. A missing
# expected field is a mismatch: an event that merely shares a name is not proof
# of the real model → MCP tool → model path (AC-19). ``tool_completed`` must
# additionally carry a non-empty ``result``, so a bare event name without the
# structured payload cannot pass as a real tool result.
REQUIRED_TRACE_CHAIN = (
    ("mcp_connect", {"ok": True}, ()),
    ("mcp_list_tools", None, ()),
    ("model_request", {"phase": "tool_selection"}, ()),
    ("tool_selected", {"tool": EXPECTED_TOOL}, ()),
    ("tool_completed", {"tool": EXPECTED_TOOL, "ok": True}, ("result",)),
    ("model_request", {"phase": "final_answer"}, ()),
    ("request_done", {"ok": True}, ()),
)

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _mcp_ready(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        try:
            status, _tools = asyncio.run(
                inspect_tools(url, connect_timeout_s=3.0)
            )
        except Exception:  # noqa: BLE001 - readiness polling only
            status = None
        if status is not None and status.connected:
            return True
        time.sleep(0.4)
    return False


def ensure_local_model(run_dir: Path, report: dict):
    """Return ``(config, requested_model, launcher)`` with a live local model.

    The requested model id is always the project canonical one
    (``agent.settings.DEFAULT_MODEL_NAME``); the id from the QA config is only
    recorded as ``qa_model_configured`` and never sent as the requested model.
    """
    config = qa_config.load_config()
    report["local_llm"] = {
        "source": config.source,
        "endpoint_loopback": config.is_loopback,
        "qa_model_configured": config.model,
        "readiness_timeout_seconds": config.readiness_timeout_seconds,
    }

    models = qa_local_llm.probe(config.base_url, api_key=config.api_key)
    if models is not None:
        report["local_llm"]["started_by_harness"] = False
        return config, DEFAULT_MODEL_NAME, None

    handoff = qa_discovery.LauncherHandoff()
    discovery = qa_discovery.discover(config, launcher_handoff=handoff)
    report["local_llm"].update(
        qa_discovery.report_block(discovery, config_written=False, config_path=None)
    )
    # An existing untracked local config already carries its launcher; discovery
    # only fills the handoff when it had to locate a launcher itself. Prefer the
    # configured command, otherwise a valid local config would look "unconfigured"
    # in the probe result above.
    launch_command = config.launch_command or handoff.launch_command
    launch_cwd = config.launch_cwd or handoff.launch_cwd
    if not launch_command:
        return config, "", None

    launcher_config = replace(
        config,
        launch_command=launch_command,
        launch_cwd=launch_cwd,
    )
    launcher = qa_local_llm.LocalLlmLauncher(
        launcher_config, log_path=run_dir / "local_llm.log"
    )
    if not launcher.start():
        return config, "", None
    if not launcher.wait_ready():
        launcher.stop()
        return config, "", None

    models = qa_local_llm.probe(config.base_url, api_key=config.api_key)
    report["local_llm"]["started_by_harness"] = True
    report["local_llm"]["probe_model_count"] = len(models or [])
    return config, DEFAULT_MODEL_NAME, launcher


def collect_sse(url: str, payload: dict, timeout_s: float) -> list:
    """POST the chat request and collect the decoded SSE events."""
    events: list = []
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=30.0, pool=10.0)
    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            event_name = "message"
            data_lines: list = []
            for line in response.iter_lines():
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].strip())
                elif line == "":
                    if data_lines:
                        try:
                            data = json.loads("\n".join(data_lines))
                        except ValueError:
                            data = {"raw": "\n".join(data_lines)}
                        events.append((event_name, data))
                    event_name = "message"
                    data_lines = []
            if data_lines:
                try:
                    data = json.loads("\n".join(data_lines))
                except ValueError:
                    data = {"raw": "\n".join(data_lines)}
                events.append((event_name, data))
    return events


def read_trace(path: Path) -> list:
    """Read the JSONL trace into a list of records."""
    if not path.exists():
        return []
    records: list = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _matches(record: dict, fields) -> tuple[bool, str]:
    """Return whether ``record`` satisfies ``fields`` and the first problem.

    A missing expected key is a mismatch, not a free pass: the strengthened
    check has to fail an event that only carries the right name.
    """
    if fields is None:
        return True, ""
    for key, expected in fields.items():
        if key not in record:
            return False, f"missing field '{key}'"
        if record.get(key) != expected:
            return False, f"field '{key}' is {record.get(key)!r}, expected {expected!r}"
    return True, ""


def _empty_field(record: dict, names) -> str:
    """Return the first required field that is missing or empty."""
    for name in names:
        if name not in record:
            return name
        value = record.get(name)
        if value is None or (isinstance(value, (str, list, dict, tuple)) and not value):
            return name
    return ""


def verify_trace(records: list, request_id: str | None) -> dict:
    """Check the ordered tool-call chain of one request in the trace."""
    scoped = [
        record
        for record in records
        if request_id is None or record.get("request_id") == request_id
    ]
    if request_id is None:
        ids = [record.get("request_id") for record in scoped]
        if ids:
            first = ids[0]
            scoped = [record for record in scoped if record.get("request_id") == first]
            request_id = first

    position = 0
    matched: list = []
    for name, fields, non_empty in REQUIRED_TRACE_CHAIN:
        found = False
        problem = ""
        while position < len(scoped):
            record = scoped[position]
            position += 1
            if record.get("event") != name:
                continue
            fields_ok, reason = _matches(record, fields)
            if not fields_ok:
                problem = reason
                continue
            empty = _empty_field(record, non_empty)
            if empty:
                problem = f"empty field '{empty}'"
                continue
            found = True
            matched.append(record.get("event"))
            break
        if not found:
            detail = f" ({problem})" if problem else ""
            return {
                "ok": False,
                "reason": f"missing trace step {name} after {matched}{detail}",
                "request_id": request_id,
                "matched": matched,
            }
    return {"ok": True, "request_id": request_id, "matched": matched}


def verify_sse(events: list) -> dict:
    """Check the SSE order and the streamed answer."""
    names = [name for name, _ in events]
    text = "".join(
        str(data.get("text") or "") for name, data in events if name == "delta"
    )
    errors = [data for name, data in events if name == "error"]
    done = [data for name, data in events if name == "done"]

    def ordered(first: str, second: str) -> bool:
        try:
            return names.index(first) < names.index(second)
        except ValueError:
            return False

    ok = (
        not errors
        and bool(done)
        and ordered("tool_call", "tool_result")
        and ordered("tool_result", "delta")
        and ordered("delta", "done")
        and EXPECTED_ANSWER in text
    )
    numbers = [value for value in NUMBER_RE.findall(text) if value == EXPECTED_ANSWER]
    return {
        "ok": ok,
        "events": names,
        "answer_found": EXPECTED_ANSWER in text,
        "answer_matches": bool(numbers),
        "text_chars": len(text),
        "errors": errors,
        "done": done,
    }


def run_ui_e2e(url: str, run_dir: Path, report: dict) -> str:
    """Drive the chat UI through the system browser; return the UI status."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "BLOCKED"

    shot = run_dir / "screenshots" / "01_chat.png"
    manager = None
    browser = None
    # Uncaught JS errors are a hard failure; console errors (for example a 404
    # favicon) are recorded for the operator but never fail the UI run.
    page_errors: list = []
    console_errors: list = []
    try:
        manager = sync_playwright().start()
        browser, channel = qa_browser.launch_browser(manager, headless=True)
        report["ui_channel"] = channel
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(60000)
        page = context.new_page()

        def on_page_error(error):
            page_errors.append(f"{type(error).__name__}: {error}")

        def on_console(message):
            if message.type == "error":
                console_errors.append(message.text)

        page.on("pageerror", on_page_error)
        page.on("console", on_console)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#message-input", timeout=30000)

        try:
            page.wait_for_selector(".pill.ok", timeout=30000)
            report["ui_mcp_connected"] = True
        except Exception:  # noqa: BLE001 - the status panel may be slow
            report["ui_mcp_connected"] = False

        page.fill("#message-input", QUESTION)
        page.click("#send-button")

        loader_seen = False
        try:
            page.wait_for_selector(".loader", timeout=5000)
            loader_seen = True
        except Exception:  # noqa: BLE001 - the loader is short-lived
            loader_seen = False
        report["ui_loader_seen"] = loader_seen

        page.wait_for_function(
            """(expected) => {
                const bubbles = document.querySelectorAll(".bubble.assistant");
                return Array.from(bubbles).some((b) => {
                    const text = b.querySelector(".text");
                    return !!text && text.textContent.includes(expected);
                });
            }""",
            arg=EXPECTED_ANSWER,
            timeout=900000,
        )
        # Deltas arrive asynchronously: the check above can succeed while the
        # closing marker of "**391**" is still in flight. Wait (bounded) for the
        # settled markdown of the answer bubble, so the checks below run on the
        # final render instead of a streamed prefix.
        page.wait_for_function(
            """(expected) => {
                const text = document.querySelector(".bubble.assistant .text");
                if (!text || text.textContent.includes("**")) {
                    return false;
                }
                return Array.from(text.querySelectorAll("strong")).some(
                    (el) => el.textContent.includes(expected)
                );
            }""",
            arg=EXPECTED_ANSWER,
            timeout=120000,
        )
        # AC-16/AC-17: the answer lives in .text; the tool path is folded into a
        # collapsed details.technical block, never into the answer text. The
        # markdown checks prove that "**391**" became a real <strong> without a
        # visible marker and that the streamed answer is not duplicated.
        ui_checks = page.evaluate(
            """(expected) => {
                const bubbles = Array.from(document.querySelectorAll(".bubble.assistant"));
                const answer = bubbles.find((b) => {
                    const text = b.querySelector(".text");
                    return !!text && text.textContent.includes(expected);
                });
                if (!answer) {
                    return { answer: false };
                }
                const details = answer.querySelector("details.technical");
                const text = answer.querySelector(".text");
                const textContent = text ? text.textContent : "";
                const lineMatches = Array.from(document.querySelectorAll(".tool-line"));
                const linesOutside = lineMatches.filter(
                    (line) => !line.closest("details.technical")
                );
                const markers = textContent.match(/\\*\\*/g);
                const answerMatches = textContent.match(new RegExp(expected, "g"));
                return {
                    answer: true,
                    text: textContent,
                    details: !!details,
                    details_open: details ? details.open : null,
                    tool_lines_outside: linesOutside.length,
                    loader: !!answer.querySelector(".loader"),
                    strong_texts: Array.from(answer.querySelectorAll("strong")).map(
                        (el) => el.textContent
                    ),
                    literal_marker_count: markers ? markers.length : 0,
                    answer_occurrences: answerMatches ? answerMatches.length : 0,
                    assistant_bubbles: bubbles.length,
                };
            }""",
            arg=EXPECTED_ANSWER,
        )
        report["ui_checks"] = ui_checks
        ui_ok = (
            bool(ui_checks.get("answer"))
            and bool(ui_checks.get("details"))
            and ui_checks.get("details_open") is False
            and ui_checks.get("tool_lines_outside") == 0
            and not ui_checks.get("loader")
            and "MCP tool call" not in ui_checks.get("text", "")
            and "Status:" not in ui_checks.get("text", "")
            and EXPECTED_ANSWER in (ui_checks.get("strong_texts") or [])
            and ui_checks.get("literal_marker_count") == 0
            and ui_checks.get("answer_occurrences") == 1
            and ui_checks.get("assistant_bubbles") == 1
            and not page_errors
        )
        report["ui_answer_visible"] = bool(ui_checks.get("answer"))
        qa_browser.screenshot(page, shot)
        context.close()
        return "PASS" if ui_ok else "FAIL"
    except qa_browser.PrerequisiteError as exc:
        report["ui_error"] = str(exc)
        return "BLOCKED"
    except Exception as exc:  # noqa: BLE001 - reported as a UI status
        report["ui_error"] = f"{type(exc).__name__}: {exc}"
        return "FAIL"
    finally:
        report["ui_page_errors"] = page_errors
        report["ui_console_errors"] = console_errors
        try:
            if browser is not None:
                browser.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        try:
            if manager is not None:
                manager.stop()
        except Exception:  # noqa: BLE001 - best effort
            pass


def run(ui: bool = False) -> int:
    """Execute the live E2E scenario and return its exit code."""
    mcp_port = int(os.environ.get("MCP_TEST_PORT") or DEFAULT_MCP_TEST_PORT)
    backend_port = int(os.environ.get("BACKEND_TEST_PORT") or DEFAULT_BACKEND_TEST_PORT)

    run_dir = create_run_dir("live-e2e")
    report: dict = {
        "scenario": "live-e2e",
        "question": QUESTION,
        "expected_answer": EXPECTED_ANSWER,
        "ui_requested": bool(ui),
    }

    try:
        require_free_ports([mcp_port, backend_port])
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        write_report(run_dir, {**report, "status": "prerequisite", "reason": str(exc)})
        return EXIT_PREREQUISITE

    config, model, launcher = ensure_local_model(run_dir, report)
    if not model or qa_local_llm.probe(config.base_url, api_key=config.api_key) is None:
        print(
            "LIVE_LLM_STATUS: BLOCKED - no local OpenAI-compatible model is "
            "running and no launcher is configured "
            "(compare qa/local.llm.example.json)."
        )
        if launcher is not None:
            launcher.stop()
        report["status"] = "blocked"
        report["live_llm_status"] = "BLOCKED"
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")
        return EXIT_PREREQUISITE

    report["model"] = model
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    backend_url = f"http://127.0.0.1:{backend_port}"

    mcp_process: ManagedProcess | None = None
    backend_process: ManagedProcess | None = None
    cleanup: list = []
    exit_code = EXIT_FAIL
    live_status = "FAIL"
    ui_status = "NOT_REQUESTED"

    try:
        mcp_process = ManagedProcess(
            name="mcp-server",
            args=python_module("mcp_server"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {"MCP_SERVER_HOST": "127.0.0.1", "MCP_SERVER_PORT": str(mcp_port)}
            ),
            log_path=run_dir / "mcp_server.log",
        ).start()
        cleanup.append(mcp_process)
        if not wait_tcp("127.0.0.1", mcp_port, MCP_READY_TIMEOUT_SECONDS, mcp_process):
            print("LIVE_LLM_STATUS: FAIL - the MCP server did not start")
            return EXIT_FAIL

        backend_process = ManagedProcess(
            name="backend",
            args=python_module("agent"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "BACKEND_HOST": "127.0.0.1",
                    "BACKEND_PORT": str(backend_port),
                    "MCP_SERVER_URL": mcp_url,
                    "AGENT_MODEL_BASE_URL": config.base_url,
                    "AGENT_MODEL_NAME": model,
                    "AGENT_MODEL_API_KEY_ENV": "LOCAL_LLM_API_KEY",
                    "LOCAL_LLM_API_KEY": config.api_key,
                    "AGENT_MODEL_TIMEOUT_SECONDS": "1800",
                    "AGENT_TRACE_PATH": str(run_dir / "trace.jsonl"),
                    "AGENT_LOG_LEVEL": "INFO",
                }
            ),
            log_path=run_dir / "backend.log",
        ).start()
        cleanup.append(backend_process)
        if not wait_http(
            f"{backend_url}/api/health", BACKEND_READY_TIMEOUT_SECONDS, backend_process
        ):
            print("LIVE_LLM_STATUS: FAIL - the backend did not become ready")
            print((backend_process.tail_log() or "")[-2000:])
            return EXIT_FAIL

        if not _mcp_ready(mcp_url, MCP_READY_TIMEOUT_SECONDS):
            print("LIVE_LLM_STATUS: FAIL - the MCP server did not complete a handshake")
            return EXIT_FAIL

        events = collect_sse(
            f"{backend_url}/api/chat/stream",
            {"session_id": "live-e2e", "message": QUESTION},
            timeout_s=1800.0,
        )
        sse = verify_sse(events)
        report["sse"] = sse

        records = read_trace(run_dir / "trace.jsonl")
        trace = verify_trace(records, None)
        report["trace"] = trace
        report["trace_events"] = [
            {key: record.get(key) for key in ("event", "phase", "tool", "ok") if key in record}
            for record in records
        ]

        ok = bool(sse["ok"]) and bool(trace["ok"])
        live_status = "PASS" if ok else "FAIL"
        report["live_llm_status"] = live_status
        print(f"LIVE_LLM_STATUS: {live_status}")
        if not ok:
            print("  sse: " + json.dumps(sse, ensure_ascii=False))
            print("  trace: " + json.dumps(trace, ensure_ascii=False))
            print((backend_process.tail_log() or "")[-2000:])
            exit_code = EXIT_FAIL
        else:
            exit_code = EXIT_OK

        if ui:
            ui_status = run_ui_e2e(backend_url, run_dir, report)
            print(f"UI_E2E_STATUS: {ui_status}")
            report["ui_e2e_status"] = ui_status
        report["status"] = "pass" if exit_code == EXIT_OK else "fail"
        return exit_code
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"LIVE_LLM_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["live_llm_status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        for process in reversed(cleanup):
            report.setdefault("stopped", []).append(process.stop())
        if launcher is not None:
            report["local_llm"]["stop"] = launcher.stop()
        report.setdefault("live_llm_status", live_status)
        if ui:
            report.setdefault("ui_e2e_status", ui_status)
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point of ``harness/live_e2e.py``."""
    parser = argparse.ArgumentParser(prog="live_e2e")
    parser.add_argument("--ui", action="store_true", help="also run the UI E2E")
    args = parser.parse_args(argv)
    ui = bool(args.ui or str(os.environ.get("RUN_UI_E2E") or "").strip() == "1")
    return run(ui=ui)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
