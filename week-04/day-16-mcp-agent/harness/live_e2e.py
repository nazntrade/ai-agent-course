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
import threading
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

from agent.mcp_adapter import SdkMcpClient, inspect_tools  # noqa: E402
from agent.settings import DEFAULT_MODEL_NAME  # noqa: E402
from mcp_server.reports import build_digest  # noqa: E402
from storage.chats import ChatRepository  # noqa: E402
from storage.db import Database  # noqa: E402
from storage.reports import ReportRepository  # noqa: E402
from tests.support.fake_search import (  # noqa: E402
    FAKE_API_KEY,
    MANY_MARKER,
    RESULT_URLS,
    FakeSearchServer,
)

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

# The web-search scenario asks the model to use the online search tool. The
# fake search API returns deterministic links under this prefix, so the answer
# can be checked for real tool output without opening any page.
SEARCH_QUESTION = "Find the official Python documentation online and give me the links."
SEARCH_EXPECTED_TOOL = "search_web"
SEARCH_EXPECTED_PREFIX = "https://docs.example.test/"

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

# Same ordered chain for the search scenario, with ``search_web`` as the tool.
SEARCH_TRACE_CHAIN = (
    ("mcp_connect", {"ok": True}, ()),
    ("mcp_list_tools", None, ()),
    ("model_request", {"phase": "tool_selection"}, ()),
    ("tool_selected", {"tool": SEARCH_EXPECTED_TOOL}, ()),
    ("tool_completed", {"tool": SEARCH_EXPECTED_TOOL, "ok": True}, ("result",)),
    ("model_request", {"phase": "final_answer"}, ()),
    ("request_done", {"ok": True}, ()),
)

# The tasks scenario schedules a repeating search and then reads its summary.
TASKS_QUESTION_1 = (
    "Schedule a search for the official Python documentation to run every day "
    "and tell me when the first run will start."
)
TASKS_QUESTION_2 = (
    "Show me the latest saved results of that scheduled search and include the links."
)
TASKS_EXPECTED_TOOL_1 = "schedule_search_task"
TASKS_EXPECTED_TOOL_2 = "get_latest_search_run"

TASKS_SCHEDULE_CHAIN = (
    ("mcp_connect", {"ok": True}, ()),
    ("mcp_list_tools", None, ()),
    ("model_request", {"phase": "tool_selection"}, ()),
    ("tool_selected", {"tool": TASKS_EXPECTED_TOOL_1}, ()),
    ("tool_completed", {"tool": TASKS_EXPECTED_TOOL_1, "ok": True}, ("result",)),
    ("model_request", {"phase": "final_answer"}, ()),
    ("request_done", {"ok": True}, ()),
)

TASKS_SUMMARY_CHAIN = (
    ("model_request", {"phase": "tool_selection"}, ()),
    ("tool_selected", {"tool": TASKS_EXPECTED_TOOL_2}, ()),
    ("tool_completed", {"tool": TASKS_EXPECTED_TOOL_2, "ok": True}, ("result",)),
    ("model_request", {"phase": "final_answer"}, ()),
    ("request_done", {"ok": True}, ()),
)

# The composition scenario drives three dependent tools from one message:
# search_web → digest_search_results → save_report, then the final answer.
COMPOSITION_QUESTION = (
    "Find news about Kotlin, make a short summary with sources and save it"
)
COMPOSITION_NO_SAVE_QUESTION = "Find news about Kotlin and tell me briefly"
COMPOSITION_SEARCH_TOOL = "search_web"
COMPOSITION_DIGEST_TOOL = "digest_search_results"
COMPOSITION_SAVE_TOOL = "save_report"

COMPOSITION_CHAIN = (
    ("mcp_connect", {"ok": True}, ()),
    ("mcp_list_tools", None, ()),
    ("model_request", {"phase": "tool_selection"}, ()),
    ("tool_selected", {"tool": COMPOSITION_SEARCH_TOOL}, ()),
    ("tool_completed", {"tool": COMPOSITION_SEARCH_TOOL, "ok": True}, ("result",)),
    ("tool_selected", {"tool": COMPOSITION_DIGEST_TOOL}, ()),
    ("tool_completed", {"tool": COMPOSITION_DIGEST_TOOL, "ok": True}, ("result",)),
    ("tool_selected", {"tool": COMPOSITION_SAVE_TOOL}, ()),
    ("tool_completed", {"tool": COMPOSITION_SAVE_TOOL, "ok": True}, ("result",)),
    ("model_request", {"phase": "final_answer"}, ()),
    ("request_done", {"ok": True}, ()),
)

COMPOSITION_NO_SAVE_CHAIN = (
    ("mcp_connect", {"ok": True}, ()),
    ("mcp_list_tools", None, ()),
    ("model_request", {"phase": "tool_selection"}, ()),
    ("tool_selected", {"tool": COMPOSITION_SEARCH_TOOL}, ()),
    ("tool_completed", {"tool": COMPOSITION_SEARCH_TOOL, "ok": True}, ("result",)),
    ("tool_selected", {"tool": COMPOSITION_DIGEST_TOOL}, ()),
    ("tool_completed", {"tool": COMPOSITION_DIGEST_TOOL, "ok": True}, ("result",)),
    ("model_request", {"phase": "final_answer"}, ()),
    ("request_done", {"ok": True}, ()),
)

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
# Bare http(s) URLs of the streamed answer, for the search-source diagnostics.
URL_RE = re.compile(r"""https?://[^\s)\]"'>]+""", re.IGNORECASE)


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


def _read_text(path) -> str:
    """Read a text artifact, or an empty string when it is missing."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def key_is_isolated(key: str, artifacts) -> bool:
    """Whether ``key`` is absent from every observable artifact (D17-07).

    ``artifacts`` is any iterable of strings: the streamed answer, the JSONL
    trace, the MCP server log and the backend log. ``True`` means the search key
    never left the MCP process. An empty key is trivially isolated.
    """
    if not key:
        return True
    return all(key not in (text or "") for text in artifacts)


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


def verify_trace(records: list, request_id: str | None, chain=None) -> dict:
    """Check the ordered tool-call chain of one request in the trace.

    ``chain`` defaults to the arithmetic chain so existing callers keep their
    behaviour; the search scenario passes :data:`SEARCH_TRACE_CHAIN`.
    """
    required_chain = REQUIRED_TRACE_CHAIN if chain is None else chain
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
    for name, fields, non_empty in required_chain:
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


def verify_tools_listed(records: list, request_id: str | None, tool_name: str) -> dict:
    """Check that ``tools/list`` in the trace advertised ``tool_name``.

    ``verify_trace`` proves the event exists; this proves the server actually
    offered the searched tool, which is the precondition of the search chain.
    """
    scoped = [
        record
        for record in records
        if record.get("event") == "mcp_list_tools"
        and (request_id is None or record.get("request_id") == request_id)
    ]
    names: list = []
    for record in scoped:
        value = record.get("tool_names")
        if isinstance(value, list):
            names = [str(item) for item in value]
            break
    return {"ok": tool_name in names, "tool": tool_name, "tool_names": names}


def verify_search_sse(events: list, expected_urls) -> dict:
    """Check the SSE order and that the answer cites at least two tool links."""
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

    # The browser normalizes the host case, so compare case-insensitively: a
    # link the model wrote as ``HTTPS://Docs.Example.Test/1`` still points at
    # the tool URL. Any other occurrence is recorded for diagnostics only.
    text_lower = text.lower()
    found = sorted({url for url in expected_urls if url.lower() in text_lower})
    observed = sorted(
        {
            match.group(0).strip(".,;")
            for match in URL_RE.finditer(text)
            if "example.test" in match.group(0).lower()
        }
    )
    ok = (
        not errors
        and bool(done)
        and ordered("tool_call", "tool_result")
        and ordered("tool_result", "delta")
        and ordered("delta", "done")
        and len(found) >= 2
    )
    return {
        "ok": ok,
        "events": names,
        "urls_found": found,
        "urls_observed": observed,
        "url_count": len(found),
        "text_chars": len(text),
        "errors": errors,
        "done": done,
    }


def _start_fresh_chat(page) -> None:
    """Create and select a new chat so the answer count starts from zero.

    Chats and history persist, so a page reload shows earlier answers; the UI
    checks below count assistant bubbles for one turn only. A full chat list is
    not fatal: the previously selected chat just keeps its history.
    """
    try:
        before = page.locator("#chats-list .chat-item").count()
        page.click("#new-chat-button")
        page.wait_for_function(
            "(n) => document.querySelectorAll('#chats-list .chat-item').length > n",
            arg=before,
            timeout=8000,
        )
    except Exception:  # noqa: BLE001 - a full list keeps the selected chat
        return
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#messages .bubble.assistant').length === 0",
            timeout=8000,
        )
    except Exception:  # noqa: BLE001 - the empty state is asserted later
        return


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

        _start_fresh_chat(page)
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


def run_ui_search_e2e(url: str, run_dir: Path, report: dict) -> str:
    """Drive the search scenario through the system browser; return the status.

    The answer must render at least two real anchor tags from the tool result,
    with the technical details collapsed and the loader gone.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "BLOCKED"

    shot = run_dir / "screenshots" / "01_chat_search.png"
    manager = None
    browser = None
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

        _start_fresh_chat(page)
        page.fill("#message-input", SEARCH_QUESTION)
        page.click("#send-button")

        loader_seen = False
        try:
            page.wait_for_selector(".loader", timeout=5000)
            loader_seen = True
        except Exception:  # noqa: BLE001 - the loader is short-lived
            loader_seen = False
        report["ui_loader_seen"] = loader_seen

        # The tool URLs live in the anchor ``href`` (Markdown links), not in the
        # visible text, so wait for rendered anchors instead of the URL string.
        page.wait_for_function(
            """() => {
                return document.querySelectorAll(
                    '.bubble.assistant .text a[href^="https://docs.example.test/"]'
                ).length >= 2;
            }""",
            timeout=900000,
        )
        ui_checks = page.evaluate(
            """() => {
                const bubbles = Array.from(document.querySelectorAll(".bubble.assistant"));
                const withText = bubbles.filter((b) => b.querySelector(".text"));
                const answer = withText[withText.length - 1];
                if (!answer) {
                    return { answer: false };
                }
                const details = answer.querySelector("details.technical");
                const anchors = Array.from(answer.querySelectorAll(".text a"));
                const valid = anchors.filter((anchor) =>
                    (anchor.getAttribute("href") || "").startsWith(
                        "https://docs.example.test/"
                    )
                );
                const linesOutside = Array.from(
                    document.querySelectorAll(".tool-line")
                ).filter((line) => !line.closest("details.technical"));
                // A numbered source answer must stay one ordered list: three or
                // more items split across several <ol> elements is a rendering
                // failure. A missing list is acceptable (ordered_items: 0).
                const textElement = answer.querySelector(".text");
                const orderedLists = textElement
                    ? Array.from(textElement.querySelectorAll("ol"))
                    : [];
                const orderedItems = orderedLists.reduce(
                    (total, list) => total + list.querySelectorAll("li").length,
                    0
                );
                return {
                    answer: true,
                    details: !!details,
                    details_open: details ? details.open : null,
                    loader: !!answer.querySelector(".loader"),
                    anchor_count: anchors.length,
                    valid_anchor_count: valid.length,
                    hrefs: anchors.map((anchor) => anchor.getAttribute("href")),
                    tool_lines_outside: linesOutside.length,
                    ordered_lists: orderedLists.length,
                    ordered_items: orderedItems,
                    ordered_starts: orderedLists.map((list) => list.start),
                    assistant_bubbles: bubbles.length,
                };
            }"""
        )
        report["ui_checks"] = ui_checks
        ordered_items = ui_checks.get("ordered_items", 0)
        ordered_lists = ui_checks.get("ordered_lists", 0)
        ordered_ok = ordered_items < 3 or ordered_lists == 1
        ui_ok = (
            bool(ui_checks.get("answer"))
            and bool(ui_checks.get("details"))
            and ui_checks.get("details_open") is False
            and ui_checks.get("tool_lines_outside") == 0
            and not ui_checks.get("loader")
            and ui_checks.get("anchor_count", 0) >= 2
            and ui_checks.get("valid_anchor_count", 0) >= 2
            and ordered_ok
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


def run_ui_chats_e2e(url: str, db_path: Path, run_dir: Path, report: dict) -> str:
    """Drive the chats panel through the system browser; return the status.

    Checks the limit message, rename through the browser dialog, persistence
    across a real page reload and ``Clear chat`` scoped to one chat. The message
    history is seeded through the storage layer, so no model is needed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "BLOCKED"

    shot = run_dir / "screenshots" / "10_chats.png"
    manager = None
    browser = None
    page_errors: list = []
    try:
        manager = sync_playwright().start()
        browser, channel = qa_browser.launch_browser(manager, headless=True)
        report["ui_channel"] = channel
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(60000)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#chats-list .chat-item", timeout=30000)

        # Create chats up to the limit, then confirm the limit message.
        attempts = 0
        while page.locator("#chats-list .chat-item").count() < 5 and attempts < 8:
            before = page.locator("#chats-list .chat-item").count()
            page.click("#new-chat-button")
            try:
                page.wait_for_function(
                    "(n) => document.querySelectorAll('#chats-list .chat-item').length > n",
                    arg=before,
                    timeout=15000,
                )
            except Exception:  # noqa: BLE001 - the count is asserted below
                break
            attempts += 1
        chat_count = page.locator("#chats-list .chat-item").count()
        page.click("#new-chat-button")
        page.wait_for_selector("#chat-limit-message:not([hidden])", timeout=15000)
        limit_text = page.inner_text("#chat-limit-message")
        limit_ok = chat_count == 5 and "limit of 5" in limit_text.lower()

        # Rename one chat through the prompt dialog.
        page.once("dialog", lambda dialog: dialog.accept("Renamed chat"))
        page.locator("#chats-list .chat-item").first.locator(".chat-rename").click()
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#chats-list .chat-select'))"
            ".some((button) => button.textContent.trim() === 'Renamed chat')",
            timeout=15000,
        )
        rename_ok = True

        # Seed history into the selected chat, reload and expect it back.
        active_id = page.evaluate(
            "() => window.localStorage.getItem('day18.activeChatId')"
        )
        persist_ok = False
        clear_ok = False
        if active_id:
            ChatRepository(Database(db_path)).add_exchange(
                active_id, "seeded question", "seeded answer"
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#messages .bubble", timeout=30000)
            persist_ok = "seeded answer" in page.inner_text("#messages")
        if persist_ok:
            page.click("#clear-button")
            page.wait_for_function(
                "() => !document.querySelector('#messages').textContent"
                ".includes('seeded answer')",
                timeout=15000,
            )
            clear_ok = ChatRepository(Database(db_path)).message_count(active_id) == 0

        qa_browser.screenshot(page, shot)
        context.close()
        ok = limit_ok and rename_ok and persist_ok and clear_ok and not page_errors
        report["chats_ui"] = {
            "chat_count": chat_count,
            "limit": limit_ok,
            "rename": rename_ok,
            "reload": persist_ok,
            "clear": clear_ok,
            "page_errors": page_errors,
        }
        return "PASS" if ok else "FAIL"
    except qa_browser.PrerequisiteError as exc:
        report["chats_ui_error"] = str(exc)
        return "BLOCKED"
    except Exception as exc:  # noqa: BLE001 - reported as a UI status
        report["chats_ui_error"] = f"{type(exc).__name__}: {exc}"
        return "FAIL"
    finally:
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


def _fake_search_request_count(report: dict) -> int:
    """Read the loopback fake-search request counter recorded in the report."""
    port = (report.get("fake_search") or {}).get("loopback_port")
    if not port:
        return -1
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/__stats__", timeout=5.0)
        return int(response.json().get("requests") or 0)
    except Exception:  # noqa: BLE001 - the check reports the -1 below
        return -1


def run_ui_tasks_e2e(url: str, mcp_url: str, run_dir: Path, report: dict) -> str:
    """Drive the tasks panel through the system browser; return the status.

    The chat is selected while it has none of the test queries, so the tasks are
    created only after the page is open (from a background thread, because the
    sync Playwright API owns the main thread): the new cards and links can then
    reach the panel only through its own poll timer, never through the selection
    request or a Refresh click. The limited task must render three links and the
    default one five. One poll cycle must not add a search request, and switching
    chats must refresh the panel at once. Deleting the chat must warn about the
    active tasks and then remove them.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "BLOCKED"

    shot = run_dir / "screenshots" / "11_tasks.png"
    limit_query = f"{MANY_MARKER} limit"
    default_query = f"{MANY_MARKER} default"
    manager = None
    browser = None
    page_errors: list = []

    # Create the tasks before the browser starts: the sync Playwright API owns
    # the main thread's event loop, so asyncio.run must happen first.
    try:
        chats = httpx.get(f"{url}/api/chats", timeout=20.0).json().get("chats", [])
    except Exception as exc:  # noqa: BLE001 - reported as a UI status
        report["tasks_ui_error"] = f"{type(exc).__name__}: {exc}"
        return "FAIL"
    if not chats:
        report["tasks_ui_error"] = "there is no chat to attach the task to"
        return "FAIL"
    chat_id = chats[0]["id"]

    other_chat_id = None
    for chat in chats:
        if chat["id"] == chat_id:
            continue
        try:
            tasks = httpx.get(
                f"{url}/api/chats/{chat['id']}/tasks", timeout=20.0
            ).json()
        except Exception:  # noqa: BLE001 - an unreadable chat is just skipped
            continue
        if not tasks.get("count"):
            other_chat_id = chat["id"]
            break

    # The task queries the selected chat already has, read directly from the
    # API: the panel must show exactly these before any new task is created.
    try:
        chat_tasks = httpx.get(
            f"{url}/api/chats/{chat_id}/tasks", timeout=20.0
        ).json().get("tasks", [])
    except Exception:  # noqa: BLE001 - an unreadable chat just yields no expectation
        chat_tasks = []
    pre_task_queries = sorted(str(task.get("query") or "") for task in chat_tasks)

    client = SdkMcpClient(mcp_url, call_timeout_s=30.0)

    try:
        manager = sync_playwright().start()
        browser, channel = qa_browser.launch_browser(manager, headless=True)
        report["ui_channel"] = channel
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(60000)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#chats-list .chat-item", timeout=30000)
        page.wait_for_selector(".pill.ok", timeout=30000)

        # Select the chat before the tasks exist. Its own tasks request resolves
        # with the pre-task state, so a later appearance of the cards can only
        # come from the poll timer; no Refresh click happens anywhere below.
        page.click(
            f'#chats-list .chat-item[data-chat-id="{chat_id}"] .chat-select'
        )
        page.wait_for_selector(
            f'#chats-list .chat-item.active[data-chat-id="{chat_id}"]',
            timeout=15000,
        )
        # The panel must reflect the pre-task state of this chat before the MCP
        # calls start, so the selection request has already resolved and cannot
        # deliver the tasks that are created next.
        page.wait_for_function(
            """(expected) => {
                const cards = Array.from(
                    document.querySelectorAll('#tasks-list .task-card')
                );
                const queries = cards.map(
                    (card) => (card.querySelector('.task-query')?.textContent || '').trim()
                );
                return expected.length === queries.length
                    && expected.every((query) => queries.includes(query));
            }""",
            arg=pre_task_queries,
            timeout=30000,
        )
        # A local read resolves in milliseconds; this settle guarantees the
        # selection request finished before any task is created.
        page.wait_for_timeout(1000)

        # The sync Playwright API owns the main thread, so the real MCP calls run
        # in a background thread with their own event loop. The page stays
        # untouched: only its ten-second timer can bring the new state in.
        schedule_state: dict = {}

        def _schedule_from_thread():
            requests = (
                (
                    "limited",
                    {
                        "query": limit_query,
                        "interval_seconds": 3600,
                        "chat_id": chat_id,
                        "max_results": 3,
                    },
                ),
                (
                    "defaulted",
                    {
                        "query": default_query,
                        "interval_seconds": 3600,
                        "chat_id": chat_id,
                    },
                ),
            )
            try:
                for key, arguments in requests:
                    schedule_state[key] = asyncio.run(
                        client.call_tool("schedule_search_task", arguments)
                    )
            except Exception as exc:  # noqa: BLE001 - reported as FAIL below
                schedule_state["error"] = f"{type(exc).__name__}: {exc}"

        scheduler_thread = threading.Thread(
            target=_schedule_from_thread, name="ui-tasks-schedule", daemon=True
        )
        scheduler_thread.start()

        auto_refreshed = False
        try:
            page.wait_for_function(
                """() => {
                    const cards = Array.from(
                        document.querySelectorAll('#tasks-list .task-card')
                    );
                    const links = (needle) => cards
                        .filter((card) => (card.querySelector('.task-query')?.textContent || '').includes(needle))
                        .map((card) => card.querySelectorAll('.task-links a').length);
                    const limited = links('limit')[0] || 0;
                    const defaulted = links('default')[0] || 0;
                    return limited === 3 && defaulted === 5;
                }""",
                timeout=90000,
            )
            auto_refreshed = True
        except Exception:  # noqa: BLE001 - the link counts are asserted below
            auto_refreshed = False

        scheduler_thread.join(timeout=60.0)
        reported_limited = schedule_state.get("limited")
        reported_defaulted = schedule_state.get("defaulted")
        task_created = (
            reported_limited is not None
            and reported_defaulted is not None
            and bool(reported_limited.ok)
            and bool((reported_limited.structured or {}).get("created"))
            and bool(reported_defaulted.ok)
            and bool((reported_defaulted.structured or {}).get("created"))
        )
        if "error" in schedule_state:
            report["tasks_ui_schedule_error"] = schedule_state["error"]

        link_counts = page.evaluate(
            """() => {
                const cards = Array.from(
                    document.querySelectorAll('#tasks-list .task-card')
                );
                const count = (needle) => {
                    const card = cards.find((item) =>
                        (item.querySelector('.task-query')?.textContent || '').includes(needle)
                    );
                    return card ? card.querySelectorAll('.task-links a').length : -1;
                };
                return { limited: count('limit'), defaulted: count('default') };
            }"""
        )

        # One poll cycle re-reads the tasks endpoint only: the fake search
        # counter must not move. The scheduled runs are already stored and the
        # task interval is an hour, so no new search can start in this window.
        before = _fake_search_request_count(report)
        time.sleep(13.0)
        after = _fake_search_request_count(report)
        poll_is_read_only = before >= 0 and after == before
        report["tasks_ui_poll"] = {
            "requests_before": before,
            "requests_after": after,
        }

        # Switching chats refreshes the panel immediately (well under one poll).
        switch_ok = True
        if other_chat_id:
            page.click(
                f'#chats-list .chat-item[data-chat-id="{other_chat_id}"] .chat-select'
            )
            try:
                page.wait_for_function(
                    """() => document.querySelector('#tasks-list')
                        .textContent.includes('No scheduled tasks')""",
                    timeout=5000,
                )
            except Exception:  # noqa: BLE001 - asserted as a failed switch below
                switch_ok = False
            page.click(
                f'#chats-list .chat-item[data-chat-id="{chat_id}"] .chat-select'
            )
            page.wait_for_function(
                """() => document.querySelector('#tasks-list')
                    .textContent.includes('limit')""",
                timeout=5000,
            )

        warned = {"value": False}

        def _accept(dialog):
            warned["value"] = "task" in dialog.message.lower()
            dialog.accept()

        page.once("dialog", _accept)
        page.locator("#chats-list .chat-item.active .chat-delete").click()
        page.wait_for_function(
            "(needle) => !document.querySelector('#tasks-list').textContent.includes(needle)",
            arg=limit_query,
            timeout=30000,
        )

        qa_browser.screenshot(page, shot)
        context.close()
        links_ok = (
            link_counts.get("limited") == 3 and link_counts.get("defaulted") == 5
        )
        ok = (
            task_created
            and auto_refreshed
            and links_ok
            and poll_is_read_only
            and switch_ok
            and warned["value"]
            and not page_errors
        )
        report["tasks_ui"] = {
            "task_created": task_created,
            "auto_refreshed": auto_refreshed,
            "link_counts": link_counts,
            "poll_is_read_only": poll_is_read_only,
            "switch_immediate": switch_ok,
            "delete_warning": warned["value"],
            "page_errors": page_errors,
        }
        return "PASS" if ok else "FAIL"
    except qa_browser.PrerequisiteError as exc:
        report["tasks_ui_error"] = str(exc)
        return "BLOCKED"
    except Exception as exc:  # noqa: BLE001 - reported as a UI status
        report["tasks_ui_error"] = f"{type(exc).__name__}: {exc}"
        return "FAIL"
    finally:
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


def run_ui_reports_e2e(url: str, db_path: Path, run_dir: Path, report: dict) -> str:
    """Drive the Saved reports panel through the system browser; return status.

    A deterministic report is seeded into the first chat through the storage
    layer, then the panel must list it, open it lazily into plain text with real
    anchors, survive a page reload, refresh on demand, show the empty state for a
    chat without reports and switch back.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "BLOCKED"

    shot = run_dir / "screenshots" / "12_reports.png"
    manager = None
    browser = None
    page_errors: list = []
    try:
        chats = httpx.get(f"{url}/api/chats", timeout=20.0).json().get("chats", [])
    except Exception as exc:  # noqa: BLE001 - reported as a UI status
        report["reports_ui_error"] = f"{type(exc).__name__}: {exc}"
        return "FAIL"
    if not chats:
        report["reports_ui_error"] = "there is no chat to attach the report to"
        return "FAIL"
    chat_id = chats[0]["id"]
    # A chat is only a valid "empty state" counterpart if it has no reports.
    other_chat_id = None
    for chat in chats:
        if chat["id"] == chat_id:
            continue
        try:
            payload = httpx.get(
                f"{url}/api/chats/{chat['id']}/reports", timeout=20.0
            ).json()
        except Exception:  # noqa: BLE001 - an unreadable chat is skipped
            continue
        if not payload.get("count"):
            other_chat_id = chat["id"]
            break

    sources = [
        {
            "title": "Kotlin UI source one",
            "url": "https://docs.example.test/ui/1",
            "description": "First deterministic source.",
        },
        {
            "title": "Kotlin UI source two",
            "url": "https://docs.example.test/ui/2",
            "description": "Second deterministic source.",
        },
    ]
    repository = ReportRepository(Database(db_path))
    if repository.count_reports(chat_id) == 0:
        repository.insert_report(
            chat_id,
            topic="Kotlin UI report",
            summary=(
                "1. Kotlin UI source one - First deterministic source.\n"
                "   Source: https://docs.example.test/ui/1\n"
                "2. Kotlin UI source two - Second deterministic source.\n"
                "   Source: https://docs.example.test/ui/2"
            ),
            sources=sources,
            digest_id=build_digest(
                {
                    "query": "Kotlin UI report",
                    "results": [
                        {
                            "title": source["title"],
                            "url": source["url"],
                            "description": source["description"],
                        }
                        for source in sources
                    ],
                }
            )["digest_id"],
        )

    try:
        manager = sync_playwright().start()
        browser, channel = qa_browser.launch_browser(manager, headless=True)
        report["ui_channel"] = channel
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(60000)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#chats-list .chat-item", timeout=30000)

        def _select(chat_id_value):
            page.click(
                f'#chats-list .chat-item[data-chat-id="{chat_id_value}"] .chat-select'
            )

        _select(chat_id)
        page.wait_for_selector("#reports-list .report-card", timeout=30000)

        # Opening the card fetches the detail lazily and renders plain text.
        page.click("#reports-list .report-card summary")
        page.wait_for_selector("#reports-list .report-card .report-summary", timeout=15000)
        summary_text = page.inner_text("#reports-list .report-card .report-summary")
        anchors = page.locator(
            '#reports-list .report-card .report-sources a[href^="https://docs.example.test/"]'
        )
        anchor_count = anchors.count()
        open_ok = "Kotlin UI source" in summary_text and anchor_count >= 2

        # The report survives a real page reload (F5).
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#chats-list .chat-item", timeout=30000)
        _select(chat_id)
        page.wait_for_selector("#reports-list .report-card", timeout=30000)
        reload_ok = page.locator("#reports-list .report-card").count() >= 1

        # Refresh re-reads the list without losing the card.
        page.click("#reports-refresh-button")
        page.wait_for_selector("#reports-list .report-card", timeout=30000)
        refresh_ok = page.locator("#reports-list .report-card").count() >= 1

        # A chat without reports shows the empty state and switches back.
        switch_ok = True
        if other_chat_id:
            _select(other_chat_id)
            try:
                page.wait_for_function(
                    "() => document.querySelector('#reports-list')"
                    ".textContent.includes('No reports in this chat.')",
                    timeout=8000,
                )
            except Exception:  # noqa: BLE001 - asserted as a failed switch below
                switch_ok = False
            _select(chat_id)
            page.wait_for_selector("#reports-list .report-card", timeout=15000)

        qa_browser.screenshot(page, shot)
        context.close()
        ok = open_ok and reload_ok and refresh_ok and switch_ok and not page_errors
        report["reports_ui"] = {
            "open": open_ok,
            "anchor_count": anchor_count,
            "reload": reload_ok,
            "refresh": refresh_ok,
            "switch": switch_ok,
            "page_errors": page_errors,
        }
        return "PASS" if ok else "FAIL"
    except qa_browser.PrerequisiteError as exc:
        report["reports_ui_error"] = str(exc)
        return "BLOCKED"
    except Exception as exc:  # noqa: BLE001 - reported as a UI status
        report["reports_ui_error"] = f"{type(exc).__name__}: {exc}"
        return "FAIL"
    finally:
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


def _create_chat(backend_url: str, title: str = "") -> str:
    """Create a chat through the real API and return its opaque id."""
    response = httpx.post(
        f"{backend_url}/api/chats", json={"title": title}, timeout=20.0
    )
    response.raise_for_status()
    return response.json()["id"]


def _request_ids(records: list) -> list:
    """Return the unique request ids of a trace, in arrival order."""
    ids: list = []
    for record in records:
        request_id = record.get("request_id")
        if request_id and request_id not in ids:
            ids.append(request_id)
    return ids


def verify_task_schedule_sse(events: list) -> dict:
    """Check that turn 1 scheduled a task through the real MCP tool."""
    names = [name for name, _ in events]
    errors = [data for name, data in events if name == "error"]
    done = [data for name, data in events if name == "done"]
    calls = [
        data
        for name, data in events
        if name == "tool_call" and data.get("tool") == TASKS_EXPECTED_TOOL_1
    ]
    results = [
        data for name, data in events if name == "tool_result"
    ]
    ok = (
        not errors
        and bool(done)
        and bool(calls)
        and any(
            result.get("tool") == TASKS_EXPECTED_TOOL_1 and result.get("ok")
            for result in results
        )
    )
    return {"ok": ok, "events": names, "schedule_calls": len(calls), "errors": errors}


def verify_task_summary_sse(events: list, expected_urls) -> dict:
    """Check that turn 2 read a summary with real tool links.

    A model may stream a preamble before it calls the tool, so the check does
    not require the first delta to follow the tool result; it requires the tool
    call, a successful result and at least two links from the result.
    """
    names = [name for name, _ in events]
    text = "".join(
        str(data.get("text") or "") for name, data in events if name == "delta"
    )
    errors = [data for name, data in events if name == "error"]
    done = [data for name, data in events if name == "done"]
    calls = [
        data
        for name, data in events
        if name == "tool_call" and data.get("tool") == TASKS_EXPECTED_TOOL_2
    ]
    results = [
        data
        for name, data in events
        if name == "tool_result" and data.get("tool") == TASKS_EXPECTED_TOOL_2
    ]
    text_lower = text.lower()
    found = sorted({url for url in expected_urls if url.lower() in text_lower})
    ok = (
        not errors
        and bool(done)
        and bool(calls)
        and any(result.get("ok") for result in results)
        and len(found) >= 2
    )
    return {
        "ok": ok,
        "events": names,
        "urls_found": found,
        "url_count": len(found),
        "errors": errors,
    }


def _tool_calls(events: list) -> list:
    return [data.get("tool") for name, data in events if name == "tool_call"]


def verify_composition_sse(events: list) -> dict:
    """Check that one answer drove the three dependent tools in order."""
    names = [name for name, _ in events]
    errors = [data for name, data in events if name == "error"]
    done = [data for name, data in events if name == "done"]
    calls = _tool_calls(events)
    results = [data for name, data in events if name == "tool_result"]
    expected = [
        COMPOSITION_SEARCH_TOOL,
        COMPOSITION_DIGEST_TOOL,
        COMPOSITION_SAVE_TOOL,
    ]
    sequence_ok = all(tool in calls for tool in expected) and (
        calls.index(COMPOSITION_SEARCH_TOOL)
        < calls.index(COMPOSITION_DIGEST_TOOL)
        < calls.index(COMPOSITION_SAVE_TOOL)
    )
    results_ok = all(
        any(result.get("tool") == tool and result.get("ok") for result in results)
        for tool in expected
    )
    ok = not errors and bool(done) and sequence_ok and results_ok
    return {
        "ok": ok,
        "events": names,
        "calls": calls,
        "sequence_ok": sequence_ok,
        "results_ok": results_ok,
        "errors": errors,
    }


def verify_no_save_sse(events: list) -> dict:
    """Check that a plain search ran search_web + digest and saved nothing."""
    names = [name for name, _ in events]
    errors = [data for name, data in events if name == "error"]
    done = [data for name, data in events if name == "done"]
    calls = _tool_calls(events)
    results = [data for name, data in events if name == "tool_result"]
    required_ok = all(
        tool in calls and any(
            result.get("tool") == tool and result.get("ok") for result in results
        )
        for tool in (COMPOSITION_SEARCH_TOOL, COMPOSITION_DIGEST_TOOL)
    )
    save_absent = COMPOSITION_SAVE_TOOL not in calls
    ok = not errors and bool(done) and required_ok and save_absent
    return {
        "ok": ok,
        "events": names,
        "calls": calls,
        "required_ok": required_ok,
        "save_absent": save_absent,
        "errors": errors,
    }


def verify_tool_absent(records: list, request_id: str | None, tool_name: str) -> dict:
    """Check that a tool was never selected in a request's trace."""
    scoped = [
        record
        for record in records
        if record.get("event") == "tool_selected"
        and record.get("tool") == tool_name
        and (request_id is None or record.get("request_id") == request_id)
    ]
    return {"ok": not scoped, "tool": tool_name}


def _run_tasks_turns(
    backend_url: str, records_path: Path, run_dir: Path, report: dict, model_ready: bool
) -> str:
    """Run the two LIVE turns of the tasks scenario and return its status."""
    if not model_ready:
        report["tasks_live_reason"] = "no local OpenAI-compatible model"
        return "BLOCKED"

    chat_id = _create_chat(backend_url, "Tasks live")
    events_schedule = collect_sse(
        f"{backend_url}/api/chat/stream",
        {"chat_id": chat_id, "message": TASKS_QUESTION_1},
        timeout_s=1800.0,
    )
    sse_schedule = verify_task_schedule_sse(events_schedule)
    report["sse_schedule"] = sse_schedule

    # The scheduler runs the first search within a tick; wait a short bounded
    # time so the summary turn has a stored run to read.
    time.sleep(3.0)
    events_summary = collect_sse(
        f"{backend_url}/api/chat/stream",
        {"chat_id": chat_id, "message": TASKS_QUESTION_2},
        timeout_s=1800.0,
    )
    sse_summary = verify_task_summary_sse(events_summary, RESULT_URLS)
    report["sse_summary"] = sse_summary

    records = read_trace(records_path)
    ids = _request_ids(records)
    trace_schedule = verify_trace(records, ids[0] if ids else None, TASKS_SCHEDULE_CHAIN)
    trace_summary = verify_trace(
        records, ids[-1] if ids else None, TASKS_SUMMARY_CHAIN
    )
    tools_listed = verify_tools_listed(
        records, ids[0] if ids else None, TASKS_EXPECTED_TOOL_1
    )
    report["trace_schedule"] = trace_schedule
    report["trace_summary"] = trace_summary
    report["tools_listed"] = tools_listed

    ok = (
        bool(sse_schedule["ok"])
        and bool(sse_summary["ok"])
        and bool(trace_schedule["ok"])
        and bool(trace_summary["ok"])
        and bool(tools_listed["ok"])
    )
    return "PASS" if ok else "FAIL"


def _run_composition_turns(
    backend_url: str, records_path: Path, run_dir: Path, report: dict, model_ready: bool
) -> tuple[str, str]:
    """Run the LIVE composition turns and return ``(save_status, no_save_status)``."""
    if not model_ready:
        report["composition_live_reason"] = "no local OpenAI-compatible model"
        return "BLOCKED", "BLOCKED"

    chat_id = _create_chat(backend_url, "Composition live")
    events = collect_sse(
        f"{backend_url}/api/chat/stream",
        {"chat_id": chat_id, "message": COMPOSITION_QUESTION},
        timeout_s=1800.0,
    )
    sse = verify_composition_sse(events)
    report["sse_composition"] = sse

    listing = httpx.get(
        f"{backend_url}/api/chats/{chat_id}/reports", timeout=20.0
    ).json()
    reports = listing.get("reports") or []
    report_created = len(reports) >= 1
    stored_urls: list = []
    identity_ok = False
    if report_created:
        detail = httpx.get(
            f"{backend_url}/api/chats/{chat_id}/reports/"
            f"{reports[0]['report_id']}",
            timeout=20.0,
        ).json()
        stored_urls = [source.get("url") for source in detail.get("sources", [])]
        # Identity: the saved sources are exactly the deterministic tool URLs.
        identity_ok = bool(stored_urls) and set(stored_urls) <= set(RESULT_URLS)
    report["composition_report"] = {
        "created": report_created,
        "stored_urls": stored_urls,
        "identity_ok": identity_ok,
    }

    records = read_trace(records_path)
    ids = _request_ids(records)
    trace = verify_trace(records, ids[-1] if ids else None, COMPOSITION_CHAIN)
    tools_listed = verify_tools_listed(
        records, ids[-1] if ids else None, COMPOSITION_SAVE_TOOL
    )
    report["composition_trace"] = trace
    report["composition_tools_listed"] = tools_listed
    save_ok = (
        bool(sse["ok"])
        and report_created
        and identity_ok
        and bool(trace["ok"])
        and bool(tools_listed["ok"])
    )
    save_status = "PASS" if save_ok else "FAIL"

    # Scenario B: a plain "find and tell me briefly" must not create a report.
    chat_b = _create_chat(backend_url, "Composition no save")
    events_b = collect_sse(
        f"{backend_url}/api/chat/stream",
        {"chat_id": chat_b, "message": COMPOSITION_NO_SAVE_QUESTION},
        timeout_s=1800.0,
    )
    sse_b = verify_no_save_sse(events_b)
    report["sse_composition_no_save"] = sse_b
    listing_b = httpx.get(
        f"{backend_url}/api/chats/{chat_b}/reports", timeout=20.0
    ).json()
    no_report_b = int(listing_b.get("count") or 0) == 0
    records_b = read_trace(records_path)
    ids_b = _request_ids(records_b)
    trace_b = verify_trace(
        records_b, ids_b[-1] if ids_b else None, COMPOSITION_NO_SAVE_CHAIN
    )
    save_absent = verify_tool_absent(
        records_b, ids_b[-1] if ids_b else None, COMPOSITION_SAVE_TOOL
    )
    report["composition_no_save_trace"] = trace_b
    report["composition_no_save_absent"] = save_absent
    no_save_ok = (
        bool(sse_b["ok"])
        and no_report_b
        and bool(trace_b["ok"])
        and bool(save_absent["ok"])
    )
    no_save_status = "PASS" if no_save_ok else "FAIL"
    return save_status, no_save_status


def run(ui: bool = False, scenario: str = "arithmetic") -> int:
    """Execute the live E2E scenario and return its exit code."""
    scenario = str(scenario or "arithmetic").strip().lower()
    is_search = scenario == "search"
    is_tasks = scenario == "tasks"
    is_composition = scenario == "composition"
    label = (
        "live-e2e-search"
        if is_search
        else "live-e2e-tasks"
        if is_tasks
        else "live-e2e-composition"
        if is_composition
        else "live-e2e"
    )
    question = (
        SEARCH_QUESTION
        if is_search
        else TASKS_QUESTION_1
        if is_tasks
        else COMPOSITION_QUESTION
        if is_composition
        else QUESTION
    )
    chain = (
        SEARCH_TRACE_CHAIN
        if is_search
        else TASKS_SCHEDULE_CHAIN
        if is_tasks
        else COMPOSITION_CHAIN
        if is_composition
        else REQUIRED_TRACE_CHAIN
    )

    mcp_port = int(os.environ.get("MCP_TEST_PORT") or DEFAULT_MCP_TEST_PORT)
    backend_port = int(os.environ.get("BACKEND_TEST_PORT") or DEFAULT_BACKEND_TEST_PORT)

    run_dir = create_run_dir(label)
    db_path = run_dir / "day18.sqlite3"
    report: dict = {
        "scenario": label,
        "question": question,
        "expected_answer": "" if (is_search or is_tasks or is_composition) else EXPECTED_ANSWER,
        "expected_tool": chain[3][1]["tool"],
        "ui_requested": bool(ui),
        "db": relative(db_path),
    }

    try:
        require_free_ports([mcp_port, backend_port])
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        write_report(run_dir, {**report, "status": "prerequisite", "reason": str(exc)})
        return EXIT_PREREQUISITE

    config, model, launcher = ensure_local_model(run_dir, report)
    model_ready = bool(model) and qa_local_llm.probe(
        config.base_url, api_key=config.api_key
    ) is not None
    if not model_ready and not (is_tasks or is_composition):
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
    # The tasks and composition scenarios still produce their UI statuses
    # without a model.

    report["model"] = model
    report["model_ready"] = model_ready
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    backend_url = f"http://127.0.0.1:{backend_port}"

    mcp_process: ManagedProcess | None = None
    backend_process: ManagedProcess | None = None
    fake_search: FakeSearchServer | None = None
    cleanup: list = []
    exit_code = EXIT_FAIL
    live_status = "BLOCKED" if (is_tasks or is_composition) and not model_ready else "FAIL"
    ui_status = "NOT_REQUESTED"
    chats_ui_status = "NOT_REQUESTED"
    tasks_ui_status = "NOT_REQUESTED"
    reports_ui_status = "NOT_REQUESTED"
    composition_no_save_status = "NOT_REQUESTED"

    try:
        mcp_env = {
            "MCP_SERVER_HOST": "127.0.0.1",
            "MCP_SERVER_PORT": str(mcp_port),
            # The harness always isolates the MCP child from a local .env. In
            # the arithmetic scenario this prevents an accidental real search
            # call; the search and tasks scenarios pass an explicit fake API.
            "MCP_LOAD_DOTENV": "0",
            "AGENT_DB_PATH": str(db_path),
        }
        if is_tasks:
            # The scheduler must run soon so the summary turn has a stored run.
            mcp_env["MCP_TASK_TICK_SECONDS"] = "0.5"
        if is_search or is_tasks or is_composition:
            fake_search = FakeSearchServer(0).start()
            report["fake_search"] = {"loopback_port": fake_search.port}
            # The key lives only in the MCP child process; the harness keeps the
            # deterministic fake API on loopback and never reads the real one.
            mcp_env.update(
                {
                    "MCP_SEARCH_API_KEY_ENV": "TAVILY_API_KEY",
                    "TAVILY_API_KEY": FAKE_API_KEY,
                    "MCP_SEARCH_BASE_URL": fake_search.base_url,
                    "MCP_SEARCH_TIMEOUT_SECONDS": "3",
                    "MCP_SEARCH_MAX_RESULTS": "5",
                }
            )

        mcp_process = ManagedProcess(
            name="mcp-server",
            args=python_module("mcp_server"),
            cwd=PROJECT_DIR,
            env=sanitized_env(mcp_env),
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
                    "AGENT_DB_PATH": str(db_path),
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

        records_path = run_dir / "trace.jsonl"

        if is_tasks:
            live_status = _run_tasks_turns(
                backend_url, records_path, run_dir, report, model_ready
            )
            print(f"TASKS_LIVE_STATUS: {live_status}")
            report["tasks_live_status"] = live_status
        elif is_composition:
            live_status, composition_no_save_status = _run_composition_turns(
                backend_url, records_path, run_dir, report, model_ready
            )
            report["composition_live_status"] = live_status
            report["composition_no_save_live_status"] = composition_no_save_status
            print(f"COMPOSITION_LIVE_STATUS: {live_status}")
            print(f"COMPOSITION_NO_SAVE_LIVE_STATUS: {composition_no_save_status}")
        else:
            events = collect_sse(
                f"{backend_url}/api/chat/stream",
                {"chat_id": _create_chat(backend_url), "message": question},
                timeout_s=1800.0,
            )
            sse = (
                verify_search_sse(events, RESULT_URLS)
                if is_search
                else verify_sse(events)
            )
            report["sse"] = sse

            records = read_trace(records_path)
            trace = verify_trace(records, None, chain)
            report["trace"] = trace
            report["trace_events"] = [
                {key: record.get(key) for key in ("event", "phase", "tool", "ok") if key in record}
                for record in records
            ]

            tools_listed = {"ok": True, "tool": None, "tool_names": []}
            key_isolation = None
            if is_search:
                tools_listed = verify_tools_listed(records, None, SEARCH_EXPECTED_TOOL)
                report["tools_listed"] = tools_listed
                sse_text = "".join(
                    str(data.get("text") or "") for name, data in events if name == "delta"
                )
                key_isolation = key_is_isolated(
                    FAKE_API_KEY,
                    (
                        sse_text,
                        _read_text(records_path),
                        _read_text(run_dir / "mcp_server.log"),
                        _read_text(run_dir / "backend.log"),
                    ),
                )
                # The verdict is stored, never the key itself.
                report["key_isolation"] = key_isolation
                print(f"KEY_ISOLATION: {'PASS' if key_isolation else 'FAIL'}")

            ok = bool(sse["ok"]) and bool(trace["ok"]) and bool(tools_listed["ok"])
            if key_isolation is False:
                ok = False
            live_status = "PASS" if ok else "FAIL"
            report["live_llm_status"] = live_status
            print(f"LIVE_LLM_STATUS: {live_status}")
            if not ok:
                print("  sse: " + json.dumps(sse, ensure_ascii=False))
                print("  trace: " + json.dumps(trace, ensure_ascii=False))
                print("  tools: " + json.dumps(tools_listed, ensure_ascii=False))
                if key_isolation is False:
                    print("  key_isolation: FAIL - the search key leaked into an artifact")
                print((backend_process.tail_log() or "")[-2000:])

        if ui:
            if is_tasks:
                chats_ui_status = run_ui_chats_e2e(
                    backend_url, db_path, run_dir, report
                )
                print(f"CHATS_UI_STATUS: {chats_ui_status}")
                tasks_ui_status = run_ui_tasks_e2e(backend_url, mcp_url, run_dir, report)
                print(f"TASKS_UI_STATUS: {tasks_ui_status}")
                report["chats_ui_status"] = chats_ui_status
                report["tasks_ui_status"] = tasks_ui_status
            elif is_search:
                ui_status = run_ui_search_e2e(backend_url, run_dir, report)
                print(f"UI_E2E_STATUS: {ui_status}")
                report["ui_e2e_status"] = ui_status
            elif is_composition:
                reports_ui_status = run_ui_reports_e2e(
                    backend_url, db_path, run_dir, report
                )
                print(f"REPORTS_UI_STATUS: {reports_ui_status}")
                report["reports_ui_status"] = reports_ui_status
            else:
                ui_status = run_ui_e2e(backend_url, run_dir, report)
                print(f"UI_E2E_STATUS: {ui_status}")
                report["ui_e2e_status"] = ui_status

        if live_status == "PASS":
            exit_code = EXIT_OK
        elif live_status == "BLOCKED":
            exit_code = EXIT_PREREQUISITE
        else:
            exit_code = EXIT_FAIL
        if is_tasks and ui and "FAIL" in (chats_ui_status, tasks_ui_status):
            exit_code = EXIT_FAIL
        if is_composition and composition_no_save_status == "FAIL":
            exit_code = EXIT_FAIL
        if is_composition and ui and reports_ui_status == "FAIL":
            exit_code = EXIT_FAIL
        report["status"] = (
            "pass"
            if exit_code == EXIT_OK
            else "blocked"
            if exit_code == EXIT_PREREQUISITE
            else "fail"
        )
        return exit_code
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"LIVE_LLM_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["live_llm_status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        for process in reversed(cleanup):
            report.setdefault("stopped", []).append(process.stop())
        if fake_search is not None:
            fake_search.stop()
        if launcher is not None:
            report["local_llm"]["stop"] = launcher.stop()
        report.setdefault("live_llm_status", live_status)
        if ui:
            report.setdefault("ui_e2e_status", ui_status)
            report.setdefault("chats_ui_status", chats_ui_status)
            report.setdefault("tasks_ui_status", tasks_ui_status)
            report.setdefault("reports_ui_status", reports_ui_status)
        if is_composition:
            report.setdefault("composition_live_status", live_status)
            report.setdefault("composition_no_save_live_status", composition_no_save_status)
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point of ``harness/live_e2e.py``."""
    parser = argparse.ArgumentParser(prog="live_e2e")
    parser.add_argument("--ui", action="store_true", help="also run the UI E2E")
    parser.add_argument(
        "--scenario",
        choices=("arithmetic", "search", "tasks", "composition"),
        default=str(os.environ.get("LIVE_E2E_SCENARIO") or "arithmetic"),
        help="which live scenario to run (default: arithmetic)",
    )
    args = parser.parse_args(argv)
    ui = bool(args.ui or str(os.environ.get("RUN_UI_E2E") or "").strip() == "1")
    return run(ui=ui, scenario=args.scenario)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
