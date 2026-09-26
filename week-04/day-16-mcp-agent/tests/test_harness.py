"""Unit tests of the harness helpers (run directory, env sanitizing, ports)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import unittest

from harness.live_e2e import (
    COMPOSITION_CHAIN,
    COMPOSITION_NO_SAVE_CHAIN,
    COMPOSITION_SAVE_TOOL,
    SEARCH_TRACE_CHAIN,
    TASKS_EXPECTED_TOOL_1,
    TASKS_SCHEDULE_CHAIN,
    TASKS_SUMMARY_CHAIN,
    key_is_isolated,
    verify_composition_sse,
    verify_no_save_sse,
    verify_search_sse,
    verify_task_schedule_sse,
    verify_task_summary_sse,
    verify_tool_absent,
    verify_tools_listed,
    verify_trace,
)
from harness.live_mcp import _status_from_output
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


class SearchHarnessVerificationTest(unittest.TestCase):
    """The search scenario's trace chain and SSE check (D17-09/D17-10 logic).

    The real LIVE run needs a running model and the missing ``test.bat search``
    mode; these unit checks keep the verification logic itself covered.
    """

    @staticmethod
    def _search_records() -> list:
        return [
            {"event": "request_start", "request_id": "r1"},
            {"event": "mcp_connect", "request_id": "r1", "ok": True},
            {
                "event": "mcp_list_tools",
                "request_id": "r1",
                "tools_count": 3,
                "tool_names": ["calculate", "get_server_info", "search_web"],
            },
            {"event": "model_request", "request_id": "r1", "phase": "tool_selection"},
            {"event": "tool_selected", "request_id": "r1", "tool": "search_web"},
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "search_web",
                "ok": True,
                "result": {"query": "cats", "count": 2, "results": ["<omitted>"]},
            },
            {"event": "model_request", "request_id": "r1", "phase": "final_answer"},
            {"event": "request_done", "request_id": "r1", "ok": True},
        ]

    def test_search_chain_passes(self):
        result = verify_trace(self._search_records(), "r1", SEARCH_TRACE_CHAIN)
        self.assertTrue(result["ok"], msg=result)
        self.assertIn("tool_completed", result["matched"])

    def test_arithmetic_chain_rejects_the_search_tool(self):
        result = verify_trace(self._search_records(), "r1")
        self.assertFalse(result["ok"])
        self.assertIn("tool_selected", result["reason"])

    def test_sse_requires_two_tool_urls(self):
        urls = ("https://docs.example.test/1", "https://docs.example.test/2")
        events = [
            ("tool_call", {"tool": "search_web"}),
            ("tool_result", {"tool": "search_web", "ok": True}),
            ("delta", {"text": f"[a]({urls[0]}) and [b]({urls[1]})"}),
            ("done", {"ok": True}),
        ]
        result = verify_search_sse(events, urls)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["url_count"], 2)

    def test_sse_with_one_url_fails(self):
        urls = ("https://docs.example.test/1", "https://docs.example.test/2")
        events = [
            ("tool_call", {"tool": "search_web"}),
            ("tool_result", {"tool": "search_web", "ok": True}),
            ("delta", {"text": f"[a]({urls[0]})"}),
            ("done", {"ok": True}),
        ]
        self.assertFalse(verify_search_sse(events, urls)["ok"])

    def test_sse_url_match_is_case_insensitive(self):
        urls = ("https://docs.example.test/1", "https://docs.example.test/2")
        events = [
            ("tool_call", {"tool": "search_web"}),
            ("tool_result", {"tool": "search_web", "ok": True}),
            (
                "delta",
                {
                    "text": (
                        "[a](HTTPS://Docs.Example.Test/1) "
                        "[b](HTTPS://DOCS.EXAMPLE.TEST/2)"
                    )
                },
            ),
            ("done", {"ok": True}),
        ]
        result = verify_search_sse(events, urls)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["url_count"], 2)

    def test_sse_records_observed_tool_urls(self):
        urls = ("https://docs.example.test/1", "https://docs.example.test/2")
        events = [
            (
                "delta",
                {
                    "text": (
                        "[a](https://docs.example.test/1) "
                        "[x](https://docs.python.org/)"
                    )
                },
            ),
            ("done", {"ok": True}),
        ]
        result = verify_search_sse(events, urls)
        self.assertEqual(result["urls_observed"], ["https://docs.example.test/1"])

    def test_tools_listed_requires_the_search_tool(self):
        records = self._search_records()
        listed = next(r for r in records if r["event"] == "mcp_list_tools")
        listed["tool_names"] = ["calculate", "get_server_info", "search_web"]
        self.assertTrue(verify_tools_listed(records, "r1", "search_web")["ok"])

    def test_tools_listed_without_the_search_tool_fails(self):
        records = self._search_records()
        listed = next(r for r in records if r["event"] == "mcp_list_tools")
        listed["tool_names"] = ["calculate", "get_server_info"]
        self.assertFalse(verify_tools_listed(records, "r1", "search_web")["ok"])


class TasksHarnessVerificationTest(unittest.TestCase):
    """The tasks scenario's trace chains, SSE check and status parsing."""

    TOOL_NAMES = [
        "calculate",
        "digest_search_results",
        "get_latest_search_run",
        "get_server_info",
        "list_search_tasks",
        "save_report",
        "schedule_search_task",
        "search_web",
        "stop_search_task",
    ]

    @staticmethod
    def _records() -> list:
        return [
            {"event": "request_start", "request_id": "r1"},
            {"event": "mcp_connect", "request_id": "r1", "ok": True},
            {
                "event": "mcp_list_tools",
                "request_id": "r1",
                "tools_count": 9,
                "tool_names": TasksHarnessVerificationTest.TOOL_NAMES,
            },
            {"event": "model_request", "request_id": "r1", "phase": "tool_selection"},
            {"event": "tool_selected", "request_id": "r1", "tool": "schedule_search_task"},
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "schedule_search_task",
                "ok": True,
                "result": {"task_id": "t1", "status": "active", "created": True},
            },
            {"event": "model_request", "request_id": "r1", "phase": "final_answer"},
            {"event": "request_done", "request_id": "r1", "ok": True},
            {"event": "request_start", "request_id": "r2"},
            {"event": "model_request", "request_id": "r2", "phase": "tool_selection"},
            {"event": "tool_selected", "request_id": "r2", "tool": "get_latest_search_run"},
            {
                "event": "tool_completed",
                "request_id": "r2",
                "tool": "get_latest_search_run",
                "ok": True,
                "result": {"status": "ok", "count": 2, "results": ["<omitted>"]},
            },
            {"event": "model_request", "request_id": "r2", "phase": "final_answer"},
            {"event": "request_done", "request_id": "r2", "ok": True},
        ]

    def test_schedule_chain_passes(self):
        result = verify_trace(self._records(), "r1", TASKS_SCHEDULE_CHAIN)
        self.assertTrue(result["ok"], msg=result)

    def test_summary_chain_passes(self):
        result = verify_trace(self._records(), "r2", TASKS_SUMMARY_CHAIN)
        self.assertTrue(result["ok"], msg=result)

    def test_schedule_chain_rejects_the_wrong_tool(self):
        records = self._records()
        selected = next(
            record
            for record in records
            if record["event"] == "tool_selected" and record.get("request_id") == "r1"
        )
        selected["tool"] = "search_web"
        self.assertFalse(verify_trace(records, "r1", TASKS_SCHEDULE_CHAIN)["ok"])

    def test_tools_listed_requires_the_scheduling_tool(self):
        self.assertTrue(
            verify_tools_listed(self._records(), "r1", TASKS_EXPECTED_TOOL_1)["ok"]
        )
        self.assertFalse(
            verify_tools_listed(self._records(), "r1", "does_not_exist")["ok"]
        )

    def test_task_schedule_sse_requires_a_successful_result(self):
        events = [
            ("tool_call", {"tool": "schedule_search_task", "arguments": {"query": "x"}}),
            ("tool_result", {"tool": "schedule_search_task", "ok": False, "summary": "boom"}),
            ("done", {"ok": True}),
        ]
        self.assertFalse(verify_task_schedule_sse(events)["ok"])
        events[1] = (
            "tool_result",
            {"tool": "schedule_search_task", "ok": True, "summary": "scheduled"},
        )
        self.assertTrue(verify_task_schedule_sse(events)["ok"])

    def test_summary_sse_tolerates_a_preamble_before_the_tool(self):
        urls = ("https://docs.example.test/1", "https://docs.example.test/2")
        events = [
            ("delta", {"text": "Let me check the saved results."}),
            ("tool_call", {"tool": "get_latest_search_run", "arguments": {}}),
            (
                "tool_result",
                {"tool": "get_latest_search_run", "ok": True, "summary": "ok"},
            ),
            ("delta", {"text": f"[a]({urls[0]}) [b]({urls[1]})"}),
            ("done", {"ok": True}),
        ]
        result = verify_task_summary_sse(events, urls)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["url_count"], 2)

    def test_summary_sse_requires_the_tool(self):
        urls = ("https://docs.example.test/1", "https://docs.example.test/2")
        events = [
            ("delta", {"text": f"[a]({urls[0]}) [b]({urls[1]})"}),
            ("done", {"ok": True}),
        ]
        self.assertFalse(verify_task_summary_sse(events, urls)["ok"])

    def test_status_from_output_reads_the_last_token(self):
        text = "CHATS_UI_STATUS: PASS\nCHATS_UI_STATUS: FAIL - broken\n"
        self.assertEqual(_status_from_output(text, "CHATS_UI_STATUS"), "FAIL")
        self.assertEqual(_status_from_output("", "CHATS_UI_STATUS"), "UNKNOWN")


class CompositionHarnessVerificationTest(unittest.TestCase):
    """The composition scenario's trace chains and SSE checks (D19-10/D19-16)."""

    @staticmethod
    def _records() -> list:
        return [
            {"event": "request_start", "request_id": "r1"},
            {"event": "mcp_connect", "request_id": "r1", "ok": True},
            {
                "event": "mcp_list_tools",
                "request_id": "r1",
                "tools_count": 9,
            },
            {"event": "model_request", "request_id": "r1", "phase": "tool_selection"},
            {"event": "tool_selected", "request_id": "r1", "tool": "search_web"},
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "search_web",
                "ok": True,
                "result": {"query": "kotlin", "count": 3},
            },
            {
                "event": "tool_selected",
                "request_id": "r1",
                "tool": "digest_search_results",
            },
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "digest_search_results",
                "ok": True,
                "result": {"status": "ok", "count": 3},
            },
            {"event": "tool_selected", "request_id": "r1", "tool": "save_report"},
            {
                "event": "tool_completed",
                "request_id": "r1",
                "tool": "save_report",
                "ok": True,
                "result": {"report_id": "r1"},
            },
            {"event": "model_request", "request_id": "r1", "phase": "final_answer"},
            {"event": "request_done", "request_id": "r1", "ok": True},
        ]

    def test_composition_chain_passes(self):
        result = verify_trace(self._records(), "r1", COMPOSITION_CHAIN)
        self.assertTrue(result["ok"], msg=result)

    def test_composition_chain_rejects_a_missing_step(self):
        records = self._records()
        records = [
            record
            for record in records
            if record.get("tool") != "digest_search_results"
        ]
        self.assertFalse(verify_trace(records, "r1", COMPOSITION_CHAIN)["ok"])

    def test_no_save_chain_rejects_the_save_tool(self):
        records = self._records()
        # The ordered chain is a subsequence check, so the direct guard is the
        # absent-tool check: save_report must never be selected.
        self.assertFalse(verify_tool_absent(records, "r1", COMPOSITION_SAVE_TOOL)["ok"])
        without_save = [
            record for record in records if record.get("tool") != "save_report"
        ]
        self.assertTrue(
            verify_trace(without_save, "r1", COMPOSITION_NO_SAVE_CHAIN)["ok"]
        )
        self.assertTrue(
            verify_tool_absent(without_save, "r1", COMPOSITION_SAVE_TOOL)["ok"]
        )

    def test_composition_sse_requires_the_three_calls_in_order(self):
        events = [
            ("tool_call", {"tool": "search_web", "arguments": {"query": "kotlin"}}),
            (
                "tool_result",
                {"tool": "search_web", "ok": True, "summary": "3 results"},
            ),
            ("tool_call", {"tool": "digest_search_results", "arguments": {}}),
            (
                "tool_result",
                {"tool": "digest_search_results", "ok": True, "summary": "ok"},
            ),
            ("tool_call", {"tool": "save_report", "arguments": {}}),
            (
                "tool_result",
                {"tool": "save_report", "ok": True, "summary": "saved"},
            ),
            ("delta", {"text": "Saved. See the Saved reports panel."}),
            ("done", {"ok": True}),
        ]
        self.assertTrue(verify_composition_sse(events)["ok"])
        events[4] = ("tool_call", {"tool": "save_report", "arguments": {}})
        events[5] = (
            "tool_result",
            {"tool": "save_report", "ok": False, "summary": "boom"},
        )
        self.assertFalse(verify_composition_sse(events)["ok"])

    def test_no_save_sse_rejects_a_save_call(self):
        events = [
            ("tool_call", {"tool": "search_web"}),
            ("tool_result", {"tool": "search_web", "ok": True}),
            ("tool_call", {"tool": "digest_search_results"}),
            ("tool_result", {"tool": "digest_search_results", "ok": True}),
            ("done", {"ok": True}),
        ]
        self.assertTrue(verify_no_save_sse(events)["ok"])
        events.insert(4, ("tool_call", {"tool": "save_report"}))
        self.assertFalse(verify_no_save_sse(events)["ok"])


class KeyIsolationTest(unittest.TestCase):
    """The search key must be absent from every observable artifact (D17-07)."""

    KEY = "unit-test-search-key"

    def test_missing_key_is_isolated(self):
        self.assertTrue(key_is_isolated("", [self.KEY, "anything"]))

    def test_absent_key_is_isolated(self):
        artifacts = [
            "Here are the sources: [Docs](https://docs.example.test/1)",
            '{"event": "tool_selected", "tool": "search_web"}',
            "INFO:mcp.server:Tool 'search_web' completed",
            "INFO:agent.server:chat request started",
        ]
        self.assertTrue(key_is_isolated(self.KEY, artifacts))

    def test_leak_in_any_artifact_is_detected(self):
        clean = "no secret here"
        for index in range(4):
            with self.subTest(artifact=index):
                artifacts = [clean, clean, clean, clean]
                artifacts[index] = f"prefix {self.KEY} suffix"
                self.assertFalse(key_is_isolated(self.KEY, artifacts))


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

    def test_live_e2e_accepts_the_search_scenario_option(self):
        completed = self._run("live_e2e.py", argv=("--scenario", "search", "--help"))
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_live_e2e_accepts_the_tasks_scenario_option(self):
        completed = self._run("live_e2e.py", argv=("--scenario", "tasks", "--help"))
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_live_e2e_accepts_the_composition_scenario_option(self):
        completed = self._run(
            "live_e2e.py", argv=("--scenario", "composition", "--help")
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_reports_real_live_blocks_without_explicit_opt_in(self):
        completed = self._run(
            "reports_real_live.py", extra_env={"REPORTS_REAL_ALLOW": ""}
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 2, msg=completed.stderr)
        self.assertIn("REPORTS_REAL_STATUS: BLOCKED", completed.stdout)

    def test_reports_restart_script_runs_from_a_file_path(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            completed = self._run(
                "reports_restart.py",
                extra_env={
                    "REPORTS_RESTART_MCP_PORT": str(busy_port),
                    "REPORTS_RESTART_BACKEND_PORT": str(_free_port()),
                    "REPORTS_RESTART_STUB_PORT": str(_free_port()),
                },
            )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 2, msg=completed.stderr)
        self.assertIn("PREREQUISITE", completed.stdout)

    def test_tavily_live_blocks_without_explicit_opt_in(self):
        completed = self._run(
            "tavily_live.py", extra_env={"TAVILY_LIVE_ALLOW": ""}
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 2, msg=completed.stderr)
        self.assertIn("TAVILY_STATUS: BLOCKED", completed.stdout)

    def test_tasks_real_live_blocks_without_explicit_opt_in(self):
        completed = self._run(
            "tasks_real_live.py", extra_env={"TASKS_REAL_ALLOW": ""}
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 2, msg=completed.stderr)
        self.assertIn("TASKS_REAL_STATUS: BLOCKED", completed.stdout)

    def test_acceptance_script_runs_from_a_file_path(self):
        completed = self._run("acceptance.py", argv=("--help",))
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
