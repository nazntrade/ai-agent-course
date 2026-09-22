"""Unit tests of the harness helpers (run directory, env sanitizing, ports)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import unittest

from harness.live_e2e import verify_trace
from harness.mcp_unavailable_e2e import (
    duplicate_sentences,
    error_text_problems,
    verify_trace as verify_mcp_unavailable_trace,
)
from harness.qa_bridge import PROJECT_DIR
from harness.processes import (
    PrerequisiteError,
    python_module,
    require_free_ports,
    sanitized_env,
)
from harness.run_dir import create_run_dir, relative, write_report


def _trace_records(tool_completed: dict | None = None) -> list:
    """The full ordered live chain, with an optional ``tool_completed`` override."""
    completed = tool_completed or {
        "event": "tool_completed",
        "request_id": "r1",
        "tool": "calculate",
        "ok": True,
        "result": {"operation": "multiply", "a": 23, "b": 17, "result": 391},
    }
    return [
        {"event": "request_start", "request_id": "r1"},
        {"event": "mcp_connect", "request_id": "r1", "ok": True},
        {"event": "mcp_list_tools", "request_id": "r1", "tools_count": 2},
        {"event": "model_request", "request_id": "r1", "phase": "tool_selection"},
        {"event": "tool_selected", "request_id": "r1", "tool": "calculate"},
        completed,
        {"event": "model_request", "request_id": "r1", "phase": "final_answer"},
        {"event": "request_done", "request_id": "r1", "ok": True},
    ]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SanitizedEnvTest(unittest.TestCase):
    """Child processes only see an explicit allow-list."""

    def test_secret_keys_are_dropped(self):
        import os

        os.environ["DAY16_TEST_SECRET_TOKEN"] = "should-not-leak"
        try:
            env = sanitized_env({"EXTRA": "1"})
        finally:
            os.environ.pop("DAY16_TEST_SECRET_TOKEN", None)
        self.assertNotIn("DAY16_TEST_SECRET_TOKEN", env)
        self.assertEqual(env["EXTRA"], "1")

    def test_utf8_is_forced(self):
        env = sanitized_env()
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_none_values_are_not_added(self):
        env = sanitized_env({"MAYBE": None})
        self.assertNotIn("MAYBE", env)


class PortTest(unittest.TestCase):
    """A busy port is a prerequisite error, never a silent failure."""

    def test_free_port_is_accepted(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertIn(port, require_free_ports([port]))

    def test_busy_port_is_rejected(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            with self.assertRaises(PrerequisiteError):
                require_free_ports([port])


class CommandTest(unittest.TestCase):
    """Commands are built from the current interpreter."""

    def test_python_module_command(self):
        command = python_module("agent", "--flag", 1)
        self.assertEqual(command[1:4], ["-m", "agent", "--flag"])
        self.assertTrue(command[0])


class RunDirTest(unittest.TestCase):
    """Run directories are unique and reports stay repository-relative."""

    def test_create_run_dir_is_unique_and_has_screenshots(self):
        first = create_run_dir("unit")
        second = create_run_dir("unit")
        self.assertNotEqual(first, second)
        self.assertTrue((first / "screenshots").is_dir())

    def test_relative_never_contains_a_drive_letter(self):
        run_dir = create_run_dir("unit")
        label = relative(run_dir)
        self.assertNotIn(":", label)
        self.assertIn(".runs", label)

    def test_write_report_adds_the_run_dir(self):
        run_dir = create_run_dir("unit")
        path = write_report(run_dir, {"status": "pass"})
        self.assertTrue(path.exists())
        self.assertIn(".runs", path.read_text(encoding="utf-8"))


class LiveTraceVerificationTest(unittest.TestCase):
    """The live trace check rejects an event that only shares the right name.

    AC-19 requires proof of the real model → MCP tool → model path, so a
    ``tool_completed`` without ``ok``/``result`` must not pass as a tool call.
    """

    def test_full_chain_passes(self):
        result = verify_trace(_trace_records(), "r1")
        self.assertTrue(result["ok"], msg=result)
        self.assertIn("tool_completed", result["matched"])

    def test_tool_completed_without_ok_fails(self):
        records = _trace_records(
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "calculate",
                "result": {"result": 391},
            }
        )
        result = verify_trace(records, "r1")
        self.assertFalse(result["ok"])
        self.assertIn("ok", result["reason"])

    def test_tool_completed_with_empty_result_fails(self):
        records = _trace_records(
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "calculate",
                "ok": True,
                "result": {},
            }
        )
        result = verify_trace(records, "r1")
        self.assertFalse(result["ok"])
        self.assertIn("result", result["reason"])

    def test_tool_selected_without_tool_fails(self):
        records = _trace_records()
        selected = next(
            record for record in records if record["event"] == "tool_selected"
        )
        selected.pop("tool")
        result = verify_trace(records, "r1")
        self.assertFalse(result["ok"])
        self.assertIn("tool", result["reason"])


class McpUnavailableTextTest(unittest.TestCase):
    """The MCP-unavailable error text is checked for duplicates and leaks.

    Regression for the hint that repeated the backend sentence: the browser E2E
    is the permanent proof; these checks keep the detection itself covered.
    """

    BACKEND_MESSAGE = (
        "The MCP server is not reachable (127.0.0.1:8791) "
        "Start it (run_app.bat) and try again."
    )

    def test_single_clear_message_is_accepted(self):
        self.assertEqual(error_text_problems(self.BACKEND_MESSAGE), [])

    def test_repeated_hint_is_rejected(self):
        duplicate = (
            "The MCP server is not reachable (127.0.0.1:8791) "
            "The MCP server is not reachable. Start it (run_app.bat) and try again."
        )
        problems = error_text_problems(duplicate)
        self.assertTrue(problems, msg="a duplicated sentence must be reported")
        self.assertTrue(
            any("not reachable" in problem for problem in problems), msg=problems
        )

    def test_duplicate_sentence_detection(self):
        fragments = duplicate_sentences(
            "The MCP server is not reachable. The MCP server is not reachable."
        )
        self.assertIn("the mcp server is not reachable", fragments)

    def test_internal_detail_is_rejected(self):
        problems = error_text_problems(
            'The MCP server is not reachable. {"tool_call": "calculate"}'
        )
        self.assertTrue(any("{" in problem for problem in problems), msg=problems)

    def test_trace_requires_the_failure_before_the_model(self):
        records = [
            {"event": "request_start", "request_id": "r1"},
            {"event": "mcp_connect", "request_id": "r1", "ok": False},
            {
                "event": "request_error",
                "request_id": "r1",
                "category": "mcp_unavailable",
                "message": "The MCP server is not reachable (127.0.0.1:8791)",
            },
        ]
        self.assertTrue(verify_mcp_unavailable_trace(records)["ok"])
        records.append({"event": "model_request", "request_id": "r1"})
        self.assertFalse(verify_mcp_unavailable_trace(records)["ok"])


class HarnessEntryPointTest(unittest.TestCase):
    """The ``.bat`` files run the harness scripts as plain files.

    Running ``python harness\\live_mcp.py`` puts ``harness\\`` on ``sys.path``
    instead of the project root, so each directly invoked entry script has to
    restore the root itself; otherwise its own ``harness.*`` imports fail
    before any work happens (regression: ``ModuleNotFoundError: harness``).
    """

    def _run(self, script: str, *, extra_env: dict | None = None, argv=()):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # keep the script-as-file condition faithful
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(PROJECT_DIR / "harness" / script), *argv],
            cwd=str(PROJECT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def test_live_mcp_script_runs_from_a_file_path(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            completed = self._run(
                "live_mcp.py",
                extra_env={
                    "MCP_TEST_PORT": str(busy_port),
                    "BACKEND_TEST_PORT": str(_free_port()),
                    "STUB_MODEL_TEST_PORT": str(_free_port()),
                },
            )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 2, msg=completed.stderr)
        self.assertIn("PREREQUISITE", completed.stdout)

    def test_live_e2e_script_runs_from_a_file_path(self):
        completed = self._run("live_e2e.py", argv=("--help",))
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_acceptance_script_runs_from_a_file_path(self):
        completed = self._run("acceptance.py", argv=("--help",))
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
