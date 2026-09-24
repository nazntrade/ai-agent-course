"""Real-browser negative E2E: the backend runs, the MCP server does not.

The harness starts only the backend. Its MCP URL points at a loopback port where
nothing listens, and the harness deliberately starts no MCP process, so the MCP
boundary stays unavailable for the whole run. A real system browser then drives
the real UI over the real HTTP/SSE API:

* the MCP status pill turns ``disconnected`` (class ``pill bad``);
* one message produces exactly one assistant error bubble whose text names the
  unavailable MCP server, is actionable and never leaks an internal detail;
* the answer text and the visible chat history carry no raw JSON, tool event
  names, ``Status:`` lines, traceback, full URL, credential or placeholder key;
* every ``.tool-line`` is folded into a collapsed ``details.technical``;
* the error text does not repeat itself (no duplicated sentence, ``not
  reachable`` at most once);
* the UI recovers and a second send yields exactly one new error bubble (two in
  total, never three).

The provider is configured (a placeholder key on an unused loopback endpoint)
but must never be called: the MCP boundary is decided before the model turn, so
the trace holds ``mcp_connect{ok:false}`` and ``request_error{mcp_unavailable}``
and no ``model_request``/``tool_selected``/``tool_completed``.

Only the processes this harness started (the backend) and its own browser are
stopped. Exit codes: 0 PASS, 1 FAIL, 2 prerequisite (a busy port or no system
browser).
"""

from __future__ import annotations

import json
import os
import re
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

# The .bat entry points run harness scripts as plain files, which puts
# ``harness\`` on sys.path instead of the project root. Add the root explicitly
# so the ``harness.*`` imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from harness.qa_bridge import PROJECT_DIR, ensure_paths, qa_browser, qa_port_is_free
from harness.processes import (
    ManagedProcess,
    PrerequisiteError,
    python_module,
    require_free_ports,
    sanitized_env,
    wait_http,
)
from harness.run_dir import create_run_dir, relative, write_report

ensure_paths()

from agent.settings import DEFAULT_MODEL_NAME  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

DEFAULT_BACKEND_PORT = 8603
DEFAULT_MCP_UNAVAILABLE_PORT = 8791
DEFAULT_MODEL_UNUSED_PORT = 8792

BACKEND_READY_TIMEOUT_SECONDS = 60.0
UI_TIMEOUT_MS = 60000

MESSAGE = "What is 23 multiplied by 17?"

# Tokens that must never reach the answer text or the visible chat history: raw
# JSON, internal tool event names, progress lines, tracebacks, full URLs,
# authorization material and the placeholder API key value.
FORBIDDEN_IN_TEXT = (
    '{"',
    "tool_call",
    "tool_result",
    "Status:",
    "Traceback",
    'File "',
    "http://",
    "https://",
    "Authorization",
    "Bearer",
    "api_key",
    "sk-",
    "local-e2e",
)

MIN_SENTENCE_CHARS = 20
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")
WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Collapse whitespace and case for a stable comparison."""
    return WHITESPACE_RE.sub(" ", str(text or "")).strip().casefold()


def sentence_fragments(text: str) -> list:
    """Split into normalized sentence fragments of at least 20 characters."""
    fragments = []
    for part in SENTENCE_SPLIT_RE.split(normalize_text(text)):
        fragment = " ".join(part.split())
        if len(fragment) >= MIN_SENTENCE_CHARS:
            fragments.append(fragment)
    return fragments


def duplicate_sentences(text: str) -> list:
    """Return the sentence fragments that occur more than once."""
    seen = set()
    duplicates = []
    for fragment in sentence_fragments(text):
        if fragment in seen and fragment not in duplicates:
            duplicates.append(fragment)
        seen.add(fragment)
    return duplicates


def forbidden_hits(text: str) -> list:
    """Return the forbidden substrings present in ``text`` (case-insensitive)."""
    lowered = normalize_text(text)
    return [token for token in FORBIDDEN_IN_TEXT if token.casefold() in lowered]


def error_text_problems(text: str) -> list:
    """Return the problems of one assistant error message (empty means good).

    A message is acceptable only when it names the MCP dependency, explains that
    it is unavailable, leaks nothing internal and does not repeat itself.
    """
    problems = []
    for token in forbidden_hits(text):
        problems.append(f"forbidden text present: {token!r}")
    for fragment in duplicate_sentences(text):
        problems.append(f"duplicated sentence: {fragment!r}")
    lowered = normalize_text(text)
    reachable = lowered.count("not reachable")
    if reachable > 1:
        problems.append(f"'not reachable' appears {reachable} times")
    if "mcp" not in lowered:
        problems.append("the message does not name the MCP dependency")
    if not any(
        marker in lowered
        for marker in ("not reachable", "unreachable", "unavailable")
    ):
        problems.append("the message does not explain that MCP is unavailable")
    return problems


def collect_versions() -> dict:
    """Record the installed MCP SDK versions and the imported constant."""
    versions: dict = {}
    for package in ("mcp", "mcp-types"):
        try:
            versions[package] = package_version(package)
        except PackageNotFoundError:
            versions[package] = None
    try:
        from mcp_types import REQUEST_TIMEOUT

        versions["mcp_types_request_timeout"] = str(REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - reported as a missing prerequisite
        versions["mcp_types_import_error"] = f"{type(exc).__name__}: {exc}"
    return versions


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


def verify_trace(records: list) -> dict:
    """The MCP failure must be decided before any model turn.

    Required: an ``mcp_connect`` with ``ok:false`` and a ``request_error`` with
    ``category:"mcp_unavailable"``. Forbidden: any model or tool event, which
    would prove the model was called even though MCP was down.
    """
    connect_failed = any(
        record.get("event") == "mcp_connect" and record.get("ok") is False
        for record in records
    )
    unavailable = any(
        record.get("event") == "request_error"
        and record.get("category") == "mcp_unavailable"
        for record in records
    )
    forbidden_events = [
        record.get("event")
        for record in records
        if record.get("event") in ("model_request", "tool_selected", "tool_completed")
    ]
    return {
        "ok": bool(connect_failed and unavailable and not forbidden_events),
        "mcp_connect_ok_false": connect_failed,
        "request_error_mcp_unavailable": unavailable,
        "forbidden_events": forbidden_events,
        "events": [record.get("event") for record in records],
    }


DOM_STATE_JS = """
() => {
  const textOf = (el) => (el && el.textContent ? el.textContent : "");
  const messages = document.getElementById("messages");
  const errors = Array.from(document.querySelectorAll(".bubble.assistant.error"));
  const errorTexts = errors.map((b) => textOf(b.querySelector(".text")));
  const bubbleTexts = Array.from(document.querySelectorAll(".bubble .text")).map(textOf);
  const toolLines = Array.from(document.querySelectorAll(".tool-line"));
  const toolLinesOutside = toolLines.filter((line) => !line.closest("details.technical"));
  const errorDetails = Array.from(
    document.querySelectorAll(".bubble.assistant.error details.technical")
  );
  // The visible history: the collapsed technical block is folded away for the
  // reader, so the check below runs on a copy without it. A `.tool-line` that
  // escaped the block is caught separately by `tool_lines_outside`.
  const clone = messages.cloneNode(true);
  clone.querySelectorAll("details.technical").forEach((node) => node.remove());
  const input = document.getElementById("message-input");
  const send = document.getElementById("send-button");
  return {
    error_count: errors.length,
    error_texts: errorTexts,
    bubble_texts: bubbleTexts,
    history_visible_text: clone.textContent || "",
    tool_line_count: toolLines.length,
    tool_lines_outside: toolLinesOutside.length,
    error_details_open: errorDetails.map((details) => !!details.open),
    input_present: !!input,
    input_disabled: !!input && !!input.disabled,
    send_disabled: !!send && !!send.disabled,
    pills: Array.from(document.querySelectorAll(".pill")).map((pill) => ({
      cls: pill.className,
      text: textOf(pill).trim(),
    })),
  };
}
"""


def _has_disconnected_pill(state: dict) -> bool:
    return any(
        "bad" in str(pill.get("cls", "")).split()
        and str(pill.get("text", "")).strip().casefold() == "disconnected"
        for pill in state.get("pills", [])
    )


def check_dom_state(
    state: dict, *, expected_errors: int, require_disconnected_pill: bool
) -> list:
    """Return every problem of one DOM snapshot (empty means good)."""
    problems = []
    if state.get("error_count") != expected_errors:
        problems.append(
            f"expected {expected_errors} error bubble(s), found {state.get('error_count')}"
        )
    if require_disconnected_pill and not _has_disconnected_pill(state):
        problems.append(
            "the MCP status pill is not 'disconnected' with class 'pill bad'"
        )
    if state.get("tool_lines_outside"):
        problems.append(
            f"{state.get('tool_lines_outside')} .tool-line element(s) outside details.technical"
        )
    for is_open in state.get("error_details_open", []):
        if is_open:
            problems.append("an error bubble has an expanded details.technical block")
    if not state.get("input_present"):
        problems.append("#message-input is missing")
    if state.get("input_disabled"):
        problems.append("#message-input is disabled")
    if state.get("send_disabled"):
        problems.append("#send-button is still disabled")

    # The visible history must not carry any internal detail. The answer text is
    # checked per bubble, and each error message is checked as one message.
    for token in forbidden_hits(state.get("history_visible_text", "")):
        problems.append(f"forbidden text in the chat history: {token!r}")
    for text in state.get("bubble_texts", []):
        for token in forbidden_hits(text):
            problems.append(f"forbidden text in a chat bubble: {token!r}")
    for text in state.get("error_texts", []):
        for problem in error_text_problems(text):
            problems.append(problem)
    return problems


def _run_browser(backend_url: str, run_dir: Path, report: dict) -> int:
    """Drive the real UI; return an exit code (0 PASS, 1 FAIL, 2 prerequisite)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - reported as a prerequisite
        print(f"PREREQUISITE: playwright is not installed: {exc}")
        return EXIT_PREREQUISITE

    manager = None
    browser = None
    exit_code = EXIT_FAIL
    try:
        manager = sync_playwright().start()
        try:
            browser, channel = qa_browser.launch_browser(manager, headless=True)
        except qa_browser.PrerequisiteError as exc:
            print(f"PREREQUISITE: {exc}")
            return EXIT_PREREQUISITE
        report["ui_channel"] = channel
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(UI_TIMEOUT_MS)
        page = context.new_page()
        page.goto(backend_url + "/", wait_until="domcontentloaded", timeout=UI_TIMEOUT_MS)
        page.wait_for_selector("#message-input", timeout=UI_TIMEOUT_MS)

        # The status panel is refreshed on load; the pill must read disconnected.
        page.wait_for_selector(".pill.bad", timeout=UI_TIMEOUT_MS)

        page.fill("#message-input", MESSAGE)
        page.click("#send-button")
        page.wait_for_selector(".bubble.assistant.error", timeout=UI_TIMEOUT_MS)
        # ``send()`` re-enables the button in ``finally`` only after the stream
        # ended, so this waits for the full round-trip, not just the first event.
        page.wait_for_function(
            "() => !document.getElementById('send-button').disabled",
            timeout=UI_TIMEOUT_MS,
        )
        first_state = page.evaluate(DOM_STATE_JS)
        report["first_send"] = first_state
        shot_first = run_dir / "screenshots" / "01_mcp_unavailable.png"
        qa_browser.screenshot(page, shot_first)
        report["screenshots"] = [relative(shot_first)]

        problems = check_dom_state(
            first_state, expected_errors=1, require_disconnected_pill=True
        )

        # Recovery: the composer works again and a second send yields exactly one
        # new error bubble (two in total).
        page.fill("#message-input", MESSAGE)
        page.click("#send-button")
        page.wait_for_function(
            "() => document.querySelectorAll('.bubble.assistant.error').length >= 2",
            timeout=UI_TIMEOUT_MS,
        )
        page.wait_for_function(
            "() => !document.getElementById('send-button').disabled",
            timeout=UI_TIMEOUT_MS,
        )
        second_state = page.evaluate(DOM_STATE_JS)
        report["second_send"] = second_state
        shot_second = run_dir / "screenshots" / "02_second_error.png"
        qa_browser.screenshot(page, shot_second)
        report["screenshots"].append(relative(shot_second))
        problems.extend(
            check_dom_state(
                second_state, expected_errors=2, require_disconnected_pill=False
            )
        )

        report["problems"] = problems
        exit_code = EXIT_OK if not problems else EXIT_FAIL
        if not problems:
            print("MCP_UNAVAILABLE_UI_STATUS: PASS")
        else:
            print("MCP_UNAVAILABLE_UI_STATUS: FAIL")
        context.close()
        return exit_code
    except qa_browser.PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        return EXIT_PREREQUISITE
    except Exception as exc:  # noqa: BLE001 - reported as a failed scenario
        print(f"MCP_UNAVAILABLE_UI_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
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


def run() -> int:
    """Execute the MCP-unavailable browser scenario and return its exit code."""
    backend_port = int(
        os.environ.get("MCP_UNAVAILABLE_BACKEND_PORT") or DEFAULT_BACKEND_PORT
    )
    unavailable_port = int(
        os.environ.get("MCP_UNAVAILABLE_PORT") or DEFAULT_MCP_UNAVAILABLE_PORT
    )
    model_port = int(
        os.environ.get("MCP_UNAVAILABLE_MODEL_PORT") or DEFAULT_MODEL_UNUSED_PORT
    )

    run_dir = create_run_dir("mcp-unavailable-e2e")
    trace_path = run_dir / "trace.jsonl"
    report: dict = {
        "scenario": "mcp-unavailable-e2e",
        "message": MESSAGE,
        "ports": {
            "backend": backend_port,
            "mcp_unavailable": unavailable_port,
            "model_unused": model_port,
        },
        "versions": collect_versions(),
    }

    # The "unavailable" MCP port must really be free: a listener would make the
    # scenario meaningless. A busy port is a prerequisite, not a failure.
    try:
        require_free_ports([backend_port, unavailable_port])
    except PrerequisiteError as exc:
        print(f"PREREQUISITE: {exc}")
        write_report(run_dir, {**report, "status": "prerequisite", "reason": str(exc)})
        return EXIT_PREREQUISITE

    backend_url = f"http://127.0.0.1:{backend_port}"
    mcp_url = f"http://127.0.0.1:{unavailable_port}/mcp"
    model_url = f"http://127.0.0.1:{model_port}/v1"

    backend_process: ManagedProcess | None = None
    exit_code = EXIT_FAIL
    ui_status = "FAIL"

    try:
        backend_process = ManagedProcess(
            name="backend",
            args=python_module("agent"),
            cwd=PROJECT_DIR,
            env=sanitized_env(
                {
                    "BACKEND_HOST": "127.0.0.1",
                    "BACKEND_PORT": str(backend_port),
                    "MCP_SERVER_URL": mcp_url,
                    "MCP_CONNECT_TIMEOUT_SECONDS": "5",
                    # The provider is configured but must never be called: the
                    # model endpoint stays unused for the whole scenario.
                    "AGENT_MODEL_BASE_URL": model_url,
                    "AGENT_MODEL_NAME": DEFAULT_MODEL_NAME,
                    "AGENT_MODEL_API_KEY_ENV": "LOCAL_LLM_API_KEY",
                    "LOCAL_LLM_API_KEY": "local-e2e",
                    "AGENT_MODEL_TIMEOUT_SECONDS": "30",
                    "AGENT_TRACE_PATH": str(trace_path),
                    "AGENT_LOG_LEVEL": "INFO",
                    # The UI creates a chat on load; keep it out of the real data/.
                    "AGENT_DB_PATH": str(run_dir / "day18.sqlite3"),
                }
            ),
            log_path=run_dir / "backend.log",
        ).start()
        if not wait_http(
            f"{backend_url}/api/health", BACKEND_READY_TIMEOUT_SECONDS, backend_process
        ):
            print("MCP_UNAVAILABLE_UI_STATUS: FAIL - the backend did not become ready")
            print((backend_process.tail_log() or "")[-2000:])
            report["error"] = "backend did not become ready"
            return EXIT_FAIL

        records = read_trace(trace_path)
        report["trace_at_start"] = verify_trace(records)

        ui_status_code = _run_browser(backend_url, run_dir, report)
        ui_status = {EXIT_OK: "PASS", EXIT_FAIL: "FAIL"}.get(ui_status_code, "PREREQUISITE")
        if ui_status_code == EXIT_PREREQUISITE:
            report["ui_status"] = "PREREQUISITE"
            report["status"] = "prerequisite"
            return EXIT_PREREQUISITE

        records = read_trace(trace_path)
        trace = verify_trace(records)
        report["trace"] = trace
        print("TRACE_STATUS: " + ("PASS" if trace["ok"] else "FAIL"))

        exit_code = EXIT_OK if (ui_status_code == EXIT_OK and trace["ok"]) else EXIT_FAIL
        return exit_code
    except Exception as exc:  # noqa: BLE001 - the run reports, never crashes
        print(f"MCP_UNAVAILABLE_UI_STATUS: FAIL - {type(exc).__name__}: {exc}")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return EXIT_FAIL
    finally:
        if backend_process is not None:
            report.setdefault("stopped", []).append(backend_process.stop())
        report["ports_released"] = {
            "backend": qa_port_is_free(backend_port, "127.0.0.1"),
            "mcp_unavailable": qa_port_is_free(unavailable_port, "127.0.0.1"),
        }
        report["ui_status"] = ui_status
        report.setdefault("versions", collect_versions())
        # A prerequisite return already set the status; keep it.
        report.setdefault("status", {EXIT_OK: "pass", EXIT_FAIL: "fail"}.get(exit_code, "fail"))
        report["trace_path"] = relative(trace_path)
        write_report(run_dir, report)
        print(f"RUN_DIR: {relative(run_dir)}")


def main(argv=None) -> int:
    """Entry point of ``harness/mcp_unavailable_e2e.py``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
