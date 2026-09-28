"""Unit tests of the harness helpers (run directory, env sanitizing, ports)."""

from __future__ import annotations

import os
import io
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx

from harness import acceptance, live_e2e

from harness.live_e2e import (
    COMPOSITION_CHAIN,
    COMPOSITION_NO_SAVE_CHAIN,
    COMPOSITION_SAVE_TOOL,
    SEARCH_TRACE_CHAIN,
    TASKS_EXPECTED_TOOL_1,
    TASKS_SCHEDULE_CHAIN,
    TASKS_SUMMARY_CHAIN,
    _watch_by_id,
    chat_run_task_id,
    created_watch_id_from_trace,
    key_is_isolated,
    monitor_run_scope,
    monitor_schedule_state,
    monitor_trace_state,
    resolve_notifications_watch,
    search_result_urls,
    verify_composition_sse,
    verify_no_save_sse,
    verify_notifications_delivery,
    verify_real_search_sse,
    verify_search_sse,
    verify_server_chain,
    verify_servers_connected,
    verify_servers_sse,
    verify_task_schedule_sse,
    verify_task_summary_sse,
    verify_tool_absent,
    verify_tools_listed,
    verify_trace,
)
from harness.live_mcp import (
    CONFIGURED_MCP_SERVERS,
    EXPECTED_MCP_POSTS_PER_PROBE,
    _status_from_output,
)
from harness.mcp_unavailable_e2e import (
    duplicate_sentences,
    error_text_problems,
    verify_trace as verify_mcp_unavailable_trace,
)
from harness.qa_bridge import PROJECT_DIR
from harness.processes import (
    ManagedProcess,
    PrerequisiteError,
    python_module,
    require_free_ports,
    sanitized_env,
)
from harness.run_dir import create_run_dir, relative, write_report
from harness.test_profile import (
    TestModelProfile,
    load_model_profile,
    local_model_id,
    probe_model,
    redact_report,
    tavily_opt_in,
)
from tests.support.fake_telegram import FakeTelegramServer, fetch_stats


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

    def test_managed_process_redacts_before_log_write(self):
        fake = Mock()
        fake.pid = 101
        fake.poll.return_value = 0
        fake.stdout = io.BytesIO(b"before fake-unit-secret after\n")
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "backend.log"
            process = ManagedProcess(
                name="backend",
                args=["unused"],
                cwd=PROJECT_DIR,
                env={"SECRET": "fake-unit-secret"},
                log_path=log_path,
                redact_values=("fake-unit-secret",),
            )
            with patch("harness.processes.subprocess.Popen", return_value=fake) as popen:
                process.start()
                stopped = process.stop()
            saved = log_path.read_text(encoding="utf-8")
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertNotIn("fake-unit-secret", saved)
        self.assertIn("[redacted]", saved)
        self.assertTrue(stopped["redaction_ok"])
        self.assertNotIn("fake-unit-secret", repr(process))


class LocalTestProfileTest(unittest.TestCase):
    """LTP-1/LTP-2: a selected model never falls back silently."""

    @staticmethod
    def remote_env() -> dict:
        return {
            "AI_TEST_MODEL_KIND": "remote",
            "AI_TEST_MODEL_BASE_URL": "https://models.example.test/v1",
            "AI_TEST_MODEL_NAME": "chosen-model",
            "AI_TEST_MODEL_API_KEY": "fake-unit-key",
        }

    def test_any_partial_profile_is_blocked(self):
        for key in self.remote_env():
            with self.subTest(missing=key):
                values = self.remote_env()
                values[key] = ""
                with self.assertRaises(PrerequisiteError):
                    load_model_profile(values)

    def test_unselected_model_keeps_existing_path(self):
        self.assertIsNone(load_model_profile({}))

    def test_local_url_must_be_loopback(self):
        values = self.remote_env()
        values["AI_TEST_MODEL_KIND"] = "local"
        with self.assertRaises(PrerequisiteError):
            load_model_profile(values)

    @staticmethod
    def local_env() -> dict:
        path = "D:\\Models\\chosen.gguf"
        return {
            "AI_TEST_MODEL_KIND": "local",
            "AI_TEST_MODEL_BASE_URL": "http://127.0.0.1:10999/v1",
            "AI_TEST_MODEL_NAME": "local",
            "AI_TEST_MODEL_API_KEY": "fake-unit-key",
            "AI_TEST_MODEL_ID": local_model_id(path),
            "AI_TEST_MODEL_PATH": path,
        }

    def test_local_identity_is_required_and_not_exposed_in_report(self):
        values = self.local_env()
        for key in ("AI_TEST_MODEL_ID", "AI_TEST_MODEL_PATH"):
            with self.subTest(missing=key):
                partial = dict(values)
                partial.pop(key)
                with self.assertRaises(PrerequisiteError):
                    load_model_profile(partial)
        profile = load_model_profile(values)
        self.assertEqual(profile.public_details()["id"], values["AI_TEST_MODEL_ID"])
        self.assertNotIn("D:\\Models", repr(profile))
        self.assertNotIn("model_path", profile.public_details())

    def test_local_id_must_match_path_hash(self):
        values = self.local_env()
        values["AI_TEST_MODEL_ID"] = "0" * 16
        with self.assertRaises(PrerequisiteError):
            load_model_profile(values)

    def test_forbidden_model_drives_are_rejected_without_opening_them(self):
        for drive in ("E:", "F:"):
            with self.subTest(drive=drive):
                with self.assertRaises(PrerequisiteError):
                    local_model_id(drive + "\\Models\\chosen.gguf")

    def test_local_props_path_mismatch_is_blocked(self):
        profile = load_model_profile(self.local_env())
        client = Mock()
        client.get.return_value.json.return_value = {
            "model_path": "D:\\Models\\different.gguf"
        }
        context = MagicMock()
        context.__enter__.return_value = client
        with (
            patch("harness.test_profile.qa_local_llm.probe", return_value=["local"]),
            patch("harness.test_profile.httpx.Client", return_value=context),
        ):
            self.assertFalse(probe_model(profile))
        self.assertEqual(
            client.get.call_args.args[0], "http://127.0.0.1:10999/props"
        )

    def test_local_props_matching_path_is_accepted(self):
        profile = load_model_profile(self.local_env())
        client = Mock()
        client.get.return_value.json.return_value = {
            "model_path": "d:/models/chosen.gguf"
        }
        context = MagicMock()
        context.__enter__.return_value = client
        with (
            patch("harness.test_profile.qa_local_llm.probe", return_value=["local"]),
            patch("harness.test_profile.httpx.Client", return_value=context),
        ):
            self.assertTrue(probe_model(profile))

    def test_nested_report_scrubs_keys_and_model_path(self):
        secret = 'fake"\\key'
        path = "D:\\Models\\chosen.gguf"
        report = {
            "error": f"provider refused {secret}",
            "sse": [{"detail": {"text": f"header={secret}; path={path}"}}],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "AI_TEST_MODEL_API_KEY": secret,
                    "AI_TEST_MODEL_PATH": path,
                    "AI_TEST_TAVILY_API_KEY": "fake-search-secret",
                },
            ),
            patch("harness.live_e2e.write_report") as saved,
        ):
            live_e2e._write_safe_report(PROJECT_DIR, report)
        persisted = saved.call_args.args[1]
        self.assertEqual(persisted["error"], "provider refused [redacted]")
        self.assertNotIn(secret, persisted["sse"][0]["detail"]["text"])
        self.assertNotIn(path, persisted["sse"][0]["detail"]["text"])
        self.assertIn("[redacted]", persisted["sse"][0]["detail"]["text"])
        self.assertIn(secret, report["error"])
        self.assertEqual(redact_report([secret], [secret]), ["[redacted]"])

    def test_acceptance_redacts_nested_report_before_write(self):
        secret = "fake-model-secret"

        def completed_steps(*args):
            args[2]["steps"]["provider"] = {"error": [f"bad {secret}"]}
            return 0

        with (
            patch.dict(os.environ, {"AI_TEST_MODEL_API_KEY": secret}),
            patch("harness.acceptance.create_run_dir", return_value=PROJECT_DIR),
            patch("harness.acceptance.write_report") as saved,
            patch("harness.acceptance.load_model_profile", return_value=None),
            patch("harness.acceptance.tavily_opt_in", return_value=False),
            patch("harness.acceptance._run_steps", side_effect=completed_steps),
        ):
            self.assertEqual(acceptance.run(ui=False, live=False), 0)
        self.assertEqual(
            saved.call_args.args[1]["steps"]["provider"]["error"],
            ["bad [redacted]"],
        )

    def test_remote_effort_proxy_may_use_loopback(self):
        values = self.remote_env()
        values["AI_TEST_MODEL_BASE_URL"] = "http://127.0.0.1:10999/v1"
        profile = load_model_profile(values)
        self.assertEqual(profile.name, "chosen-model")
        self.assertNotIn("fake-unit-key", repr(profile))
        self.assertNotIn("api_key", profile.public_details())

    def test_local_probe_rejects_another_advertised_model(self):
        profile = load_model_profile(self.local_env())
        with patch("harness.test_profile.qa_local_llm.probe", return_value=["qwen"]):
            self.assertFalse(probe_model(profile))

    def test_remote_preflight_requests_the_selected_model(self):
        profile = TestModelProfile(
            "remote", "https://models.example.test/v1", "chosen-model", "fake-unit-key"
        )
        client = Mock()
        client.post.return_value.json.return_value = {
            "choices": [{"message": {"content": "OK"}}]
        }
        context = MagicMock()
        context.__enter__.return_value = client
        with patch("harness.test_profile.httpx.Client", return_value=context):
            self.assertTrue(probe_model(profile))
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "chosen-model")
        self.assertFalse(payload["stream"])

    def test_remote_preflight_reports_only_http_status(self):
        profile = load_model_profile(self.remote_env())
        secret = "fake-unit-key"
        request = httpx.Request("POST", profile.base_url + "/chat/completions")
        response = httpx.Response(429, request=request, text=f"provider {secret}")
        client = Mock()
        client.post.return_value = response
        context = MagicMock()
        context.__enter__.return_value = client
        output = io.StringIO()
        with (
            patch("harness.test_profile.httpx.Client", return_value=context),
            redirect_stdout(output),
        ):
            self.assertFalse(probe_model(profile))
        self.assertIn("http_status=429", output.getvalue())
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("provider", output.getvalue())

    def test_remote_preflight_reports_network_error_type_without_exception_text(self):
        profile = load_model_profile(self.remote_env())
        client = Mock()
        client.post.side_effect = httpx.ConnectError("fake-unit-key in error")
        context = MagicMock()
        context.__enter__.return_value = client
        output = io.StringIO()
        with (
            patch("harness.test_profile.httpx.Client", return_value=context),
            redirect_stdout(output),
        ):
            self.assertFalse(probe_model(profile))
        self.assertIn("network_error=ConnectError", output.getvalue())
        self.assertNotIn("fake-unit-key", output.getvalue())

    def test_remote_preflight_reports_response_shape_without_provider_text(self):
        profile = load_model_profile(self.remote_env())
        client = Mock()
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": ""},
                    "finish_reason": "length",
                    "provider_text": "fake-unit-key in response",
                }
            ]
        }
        context = MagicMock()
        context.__enter__.return_value = client
        output = io.StringIO()
        with (
            patch("harness.test_profile.httpx.Client", return_value=context),
            redirect_stdout(output),
        ):
            self.assertFalse(probe_model(profile))
        self.assertIn(
            "http_status=200; response_shape=message_empty; finish_reason=length",
            output.getvalue(),
        )
        self.assertNotIn("fake-unit-key", output.getvalue())

    def test_tavily_requires_explicit_opt_in_and_key(self):
        self.assertFalse(tavily_opt_in({}))
        with self.assertRaises(PrerequisiteError):
            tavily_opt_in({"AI_TEST_TAVILY_ENABLED": "1"})
        self.assertTrue(
            tavily_opt_in(
                {"AI_TEST_TAVILY_ENABLED": "1", "AI_TEST_TAVILY_API_KEY": "fake"}
            )
        )

    def test_acceptance_never_launches_a_panel_owned_model(self):
        profile = load_model_profile(self.local_env())
        with (
            patch("harness.acceptance.create_run_dir", return_value=PROJECT_DIR),
            patch("harness.acceptance.write_report") as write_report_mock,
            patch("harness.acceptance.load_model_profile", return_value=profile),
            patch("harness.acceptance.tavily_opt_in", return_value=False),
            patch("harness.acceptance._run_steps", return_value=0) as steps,
            patch("harness.live_e2e.ensure_local_model") as legacy,
        ):
            self.assertEqual(acceptance.run(ui=False, live=True), 0)
        legacy.assert_not_called()
        self.assertEqual(steps.call_args.args[3].name, "local")
        self.assertEqual(write_report_mock.call_args.args[1]["status"], "pass")

    def test_acceptance_stops_one_owned_launcher_on_failure(self):
        launcher = Mock()

        def failed_steps(*args):
            args[-1]["launcher"] = launcher
            return 1

        with (
            patch("harness.acceptance.create_run_dir", return_value=PROJECT_DIR),
            patch("harness.acceptance.write_report"),
            patch("harness.acceptance.load_model_profile", return_value=None),
            patch("harness.acceptance.tavily_opt_in", return_value=False),
            patch("harness.acceptance._run_steps", side_effect=failed_steps),
        ):
            self.assertEqual(acceptance.run(ui=False, live=True), 1)
        launcher.stop.assert_called_once_with()

    def test_acceptance_reuses_a_single_default_launcher(self):
        launcher = Mock()
        owner = {"launcher": None}
        report = {"steps": {}}
        with patch(
            "harness.live_e2e.ensure_local_model",
            return_value=(SimpleNamespace(base_url="http://127.0.0.1/v1"), "qwen", launcher),
        ) as legacy:
            child_env = acceptance._prepare_live_model(None, PROJECT_DIR, report, owner)
        legacy.assert_called_once()
        self.assertEqual(child_env, {})
        self.assertIs(owner["launcher"], launcher)

    def test_remote_preflight_is_performed_once_by_parent(self):
        profile = TestModelProfile(
            "remote", "https://models.example.test/v1", "chosen-model", "fake-unit-key"
        )
        owner = {"launcher": None}
        with patch("harness.acceptance.probe_model", return_value=True) as probe:
            child_env = acceptance._prepare_live_model(
                profile, PROJECT_DIR, {"steps": {}}, owner
            )
        probe.assert_called_once_with(profile)
        self.assertEqual(child_env["AI_TEST_MODEL_PARENT_READY"], "1")
        self.assertIsNone(owner["launcher"])

    def test_standalone_remote_live_probes_selected_model(self):
        profile = load_model_profile(self.remote_env())
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"AI_TEST_MODEL_PARENT_READY": ""}),
            patch("harness.live_e2e.create_run_dir", return_value=PROJECT_DIR),
            patch("harness.live_e2e.load_model_profile", return_value=profile),
            patch("harness.live_e2e.require_free_ports"),
            patch(
                "harness.live_e2e.ensure_local_model",
                return_value=(SimpleNamespace(api_key=profile.api_key), profile.name, None),
            ),
            patch("harness.live_e2e.probe_model", return_value=False) as probe,
            patch("harness.live_e2e._write_safe_report"),
            redirect_stdout(output),
        ):
            self.assertEqual(live_e2e.run(scenario="arithmetic"), 2)
        probe.assert_called_once_with(profile)
        self.assertIn("selected remote test model", output.getvalue())
        self.assertNotIn("GGUF", output.getvalue())

    def test_parent_ready_remote_live_does_not_repeat_preflight(self):
        profile = load_model_profile(self.remote_env())
        process = Mock()
        with (
            patch.dict(os.environ, {"AI_TEST_MODEL_PARENT_READY": "1"}),
            patch("harness.live_e2e.create_run_dir", return_value=PROJECT_DIR),
            patch("harness.live_e2e.load_model_profile", return_value=profile),
            patch("harness.live_e2e.require_free_ports"),
            patch(
                "harness.live_e2e.ensure_local_model",
                return_value=(SimpleNamespace(api_key=profile.api_key), profile.name, None),
            ),
            patch("harness.live_e2e.probe_model") as probe,
            patch("harness.live_e2e.ManagedProcess") as managed,
            patch("harness.live_e2e.wait_tcp", return_value=False),
            patch("harness.live_e2e._write_safe_report"),
            redirect_stdout(io.StringIO()),
        ):
            managed.return_value.start.return_value = process
            self.assertEqual(live_e2e.run(scenario="arithmetic"), 1)
        probe.assert_not_called()

    def test_acceptance_invalid_profile_does_not_start_any_model(self):
        with (
            patch("harness.acceptance.create_run_dir", return_value=PROJECT_DIR),
            patch("harness.acceptance.write_report"),
            patch("harness.acceptance.load_model_profile", side_effect=PrerequisiteError("invalid")),
            patch("harness.acceptance.tavily_opt_in", return_value=False),
            patch("harness.acceptance._run_steps") as steps,
            patch("harness.live_e2e.ensure_local_model") as legacy,
        ):
            self.assertEqual(acceptance.run(ui=False, live=True), 2)
        steps.assert_not_called()
        legacy.assert_not_called()

    def test_real_result_urls_require_a_completed_search(self):
        records = [
            {"event": "tool_completed", "tool": "search_web", "ok": False,
             "result": {"results": [{"url": "https://wrong.example.test"}]}},
            {"event": "tool_completed", "tool": "search_web", "ok": True,
             "result": {"results": [{"url": "https://docs.python.org"}]}},
        ]
        self.assertEqual(search_result_urls(records), ["https://docs.python.org"])

    def test_test_backend_can_opt_out_of_dotenv(self):
        from agent.settings import load_settings

        with (
            patch.dict(os.environ, {"AGENT_LOAD_DOTENV": "0"}),
            patch("agent.settings.resolve_settings") as resolve,
        ):
            load_settings()
        resolve.assert_called_once_with(dotenv=False)


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


class FakeTelegramTest(unittest.TestCase):
    """The loopback Telegram fake records payloads and never leaves loopback."""

    def test_records_send_message_payload(self):
        import json
        import urllib.request

        with FakeTelegramServer(0) as telegram:
            url = f"{telegram.base_url}/bot123:abc/sendMessage"
            body = json.dumps(
                {"chat_id": "42", "text": "hello https://docs.example.test/1"}
            ).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["message_id"], 1)
            self.assertEqual(telegram.message_count, 1)
            self.assertIn("docs.example.test/1", telegram.messages[0])
            self.assertIn("/sendMessage", telegram.requests[0]["path"])
            stats = fetch_stats(telegram.base_url)
            self.assertEqual(stats["count"], 1)
            self.assertIn("docs.example.test/1", stats["messages"][0])

    def test_unknown_paths_are_not_recorded(self):
        import urllib.error
        import urllib.request

        with FakeTelegramServer(0) as telegram:
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(
                    f"{telegram.base_url}/not-the-bot-api", timeout=5.0
                )
            self.assertEqual(telegram.message_count, 0)


class McpProbeExpectationTest(unittest.TestCase):
    """The hub probes both servers, so one chat request opens two sessions."""

    def test_probe_expects_two_posts_per_configured_server(self):
        self.assertEqual(CONFIGURED_MCP_SERVERS, 2)
        self.assertEqual(EXPECTED_MCP_POSTS_PER_PROBE, 4)


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

    def test_real_search_accepts_redacted_trace_and_reports_answer_urls_only(self):
        records = self._search_records()
        self.assertEqual(search_result_urls(records), [])
        events = [
            ("tool_call", {"tool": "search_web"}),
            ("tool_result", {"tool": "search_web", "ok": True}),
            (
                "delta",
                {
                    "text": (
                        "[Python](https://docs.python.org/) and "
                        "[video](https://www.youtube.com/watch?v=abc)"
                    )
                },
            ),
            ("done", {"ok": True}),
        ]
        result = verify_real_search_sse(events, records)
        self.assertTrue(result["ok"], msg=result)
        self.assertEqual(result["search_result_count"], 2)
        self.assertEqual(result["answer_url_count"], 2)
        self.assertEqual(result["source_url_match"], "unverified_trace_redacted")
        self.assertNotIn("urls_found", result)

    def test_real_search_requires_results_and_answer_url(self):
        records = self._search_records()
        search = next(
            record
            for record in records
            if record.get("event") == "tool_completed"
            and record.get("tool") == "search_web"
        )
        search["result"]["count"] = 0
        events = [
            ("tool_call", {"tool": "search_web"}),
            ("tool_result", {"tool": "search_web", "ok": True}),
            ("delta", {"text": "No valid citation: https://"}),
            ("done", {"ok": True}),
        ]
        result = verify_real_search_sse(events, records)
        self.assertFalse(result["ok"])
        self.assertEqual(result["search_result_count"], 0)
        self.assertEqual(result["answer_urls"], [])

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


class NotificationsHarnessVerificationTest(unittest.TestCase):
    """The day-20 cross-server verifiers (D20-03/D20-09/D20-16/D20-20/D20-24)."""

    @staticmethod
    def _records() -> list:
        return [
            {"event": "request_start", "request_id": "r1", "trigger": "chat"},
            {"event": "mcp_connect", "request_id": "r1", "server": "A", "ok": True},
            {"event": "mcp_connect", "request_id": "r1", "server": "B", "ok": True},
            {
                "event": "mcp_list_tools",
                "request_id": "r1",
                "tools_count": 15,
                "per_server": {"A": 9, "B": 6},
            },
            {"event": "model_request", "request_id": "r1", "phase": "tool_selection"},
            {
                "event": "tool_selected",
                "request_id": "r1",
                "server": "A",
                "tool": "get_latest_search_run",
            },
            {
                "event": "tool_completed",
                "request_id": "r1",
                "server": "A",
                "tool": "get_latest_search_run",
                "ok": True,
                "result": {"status": "ok", "results": [{"title": "x", "url": "u"}]},
            },
            {
                "event": "tool_selected",
                "request_id": "r1",
                "server": "B",
                "tool": "evaluate_run",
            },
            {
                "event": "tool_completed",
                "request_id": "r1",
                "server": "B",
                "tool": "evaluate_run",
                "ok": True,
                "result": {"status": "ok", "should_notify": True, "new_items": []},
            },
            {
                "event": "tool_selected",
                "request_id": "r1",
                "server": "B",
                "tool": "send_notification",
            },
            {
                "event": "tool_completed",
                "request_id": "r1",
                "server": "B",
                "tool": "send_notification",
                "ok": True,
                "result": {"status": "sent", "delivery_id": "d1"},
            },
            {"event": "model_request", "request_id": "r1", "phase": "final_answer"},
            {"event": "request_done", "request_id": "r1", "ok": True},
        ]

    def test_both_servers_connected(self):
        self.assertTrue(verify_servers_connected(self._records(), "r1")["ok"])

    def test_one_server_missing_is_rejected(self):
        records = [r for r in self._records() if r.get("server") != "B"]
        self.assertFalse(verify_servers_connected(records, "r1")["ok"])

    def test_server_chain_requires_a_before_b(self):
        self.assertTrue(verify_server_chain(self._records(), "r1")["ok"])

    def test_server_chain_rejects_b_only(self):
        records = [r for r in self._records() if r.get("server") != "A"]
        self.assertFalse(verify_server_chain(records, "r1")["ok"])

    def test_servers_sse_requires_a_before_b(self):
        events = [
            (
                "tool_call",
                {
                    "tool": "get_latest_search_run",
                    "server": "A",
                    "arguments": {},
                    "round": 1,
                },
            ),
            (
                "tool_result",
                {
                    "tool": "get_latest_search_run",
                    "ok": True,
                    "summary": "s",
                    "duration_ms": 1,
                    "server": "A",
                },
            ),
            (
                "tool_call",
                {"tool": "send_notification", "server": "B", "arguments": {}, "round": 1},
            ),
            (
                "tool_result",
                {
                    "tool": "send_notification",
                    "ok": True,
                    "summary": "s",
                    "duration_ms": 1,
                    "server": "B",
                },
            ),
            ("done", {"ok": True}),
        ]
        self.assertTrue(verify_servers_sse(events)["ok"])
        self.assertFalse(verify_servers_sse(events[:1])["ok"])

    def test_delivery_requires_sent_and_telegram_url(self):
        url = "https://docs.example.test/1"
        records = self._records()
        sent = verify_notifications_delivery(
            records, "r1", [f"New item {url}"], require_sent=True
        )
        self.assertTrue(sent["ok"], msg=sent)
        without_url = verify_notifications_delivery(
            records, "r1", ["New item without a link"], require_sent=True
        )
        self.assertFalse(without_url["ok"])

    def test_delivery_rejects_a_bare_ok(self):
        records = self._records()
        for record in records:
            if record.get("tool") == "send_notification":
                record["result"] = {"delivery_id": "d1"}  # no status at all
        result = verify_notifications_delivery(
            records, "r1", ["https://docs.example.test/1"], require_sent=True
        )
        self.assertFalse(result["ok"])

    def test_delivery_accepts_an_honest_not_required(self):
        records = self._records()
        for record in records:
            if record.get("tool") == "evaluate_run":
                record["result"] = {"status": "ok", "should_notify": False}
            if record.get("tool") == "send_notification":
                record["result"] = {"status": "not_required"}
        result = verify_notifications_delivery(records, "r1", [])
        self.assertTrue(result["ok"], msg=result)

    def test_delivery_rejects_a_dishonest_not_required(self):
        records = self._records()
        for record in records:
            if record.get("tool") == "evaluate_run":
                record["result"] = {"status": "ok", "should_notify": True}
            if record.get("tool") == "send_notification":
                record["result"] = {"status": "not_required"}
        result = verify_notifications_delivery(records, "r1", [])
        self.assertFalse(result["ok"])

    def test_monitor_state_detects_baseline(self):
        records = [
            {
                "event": "request_start",
                "request_id": "m1",
                "trigger": "monitor",
                "watch_id": "w1",
            },
            {
                "event": "tool_completed",
                "request_id": "m1",
                "tool": "evaluate_run",
                "ok": True,
                "result": {
                    "status": "empty",
                    "is_baseline": True,
                    "should_notify": False,
                },
            },
            {"event": "request_done", "request_id": "m1", "ok": True},
        ]
        state = monitor_trace_state(records, "w1")
        self.assertTrue(state["baseline_seen"])
        self.assertFalse(state["success"])

    def test_monitor_state_ignores_incomplete_sent(self):
        records = [
            {
                "event": "request_start",
                "request_id": "m1",
                "trigger": "monitor",
                "watch_id": "w1",
            },
            {
                "event": "monitor_incomplete",
                "request_id": "m1",
                "watch_id": "w1",
                "reason": "send_notification_missing",
            },
            {
                "event": "tool_completed",
                "request_id": "m1",
                "tool": "send_notification",
                "ok": True,
                "result": {"status": "sent"},
            },
        ]
        state = monitor_trace_state(records, "w1")
        self.assertFalse(state["success"])
        self.assertIn("m1", state["incomplete_ids"])

    def test_monitor_run_scope_accepts_the_injected_empty_task_id(self):
        records = [
            {
                "event": "request_start",
                "request_id": "m1",
                "trigger": "monitor",
                "watch_id": "w1",
            },
            {
                "event": "tool_selected",
                "request_id": "m1",
                "tool": "get_latest_search_run",
                "arguments": {"task_id": ""},
            },
            {"event": "request_start", "request_id": "c1", "trigger": "chat"},
            {
                "event": "tool_selected",
                "request_id": "c1",
                "tool": "get_latest_search_run",
                "arguments": {"task_id": "t1"},
            },
        ]
        scope = monitor_run_scope(records, "w1")
        self.assertTrue(scope["ok"], msg=scope)
        self.assertEqual(scope["count"], 1)
        self.assertEqual(scope["reads"][0]["task_id"], "")

    def test_monitor_run_scope_rejects_the_watch_id_as_task_id(self):
        records = [
            {
                "event": "request_start",
                "request_id": "m1",
                "trigger": "monitor",
                "watch_id": "w1",
            },
            {
                "event": "tool_selected",
                "request_id": "m1",
                "tool": "get_latest_search_run",
                "arguments": {"task_id": "w1"},
            },
        ]
        self.assertFalse(monitor_run_scope(records, "w1")["ok"])
        self.assertFalse(monitor_run_scope(records, "other")["ok"])

    def test_monitor_schedule_state_accepts_a_moved_schedule(self):
        created = {"next_check_at": "2026-09-28T08:00:00Z"}
        baseline = {
            "last_check_at": "2026-09-28T08:00:30Z",
            "next_check_at": "2026-09-28T08:01:30Z",
            "seen_count": 0,
        }
        final = {
            "last_check_at": "2026-09-28T08:01:30Z",
            "next_check_at": "2026-09-28T08:02:30Z",
            "seen_count": 3,
            "last_delivery": {"status": "sent"},
        }
        state = monitor_schedule_state(created, baseline, final)
        self.assertTrue(state["ok"], msg=state)
        self.assertEqual(state["expected_seen_count"], 3)

    def test_monitor_schedule_state_rejects_a_frozen_watch(self):
        created = {"next_check_at": "2026-09-28T08:00:00Z"}
        frozen = {
            "last_check_at": None,
            "next_check_at": "2026-09-28T08:00:00Z",
            "seen_count": 0,
        }
        state = monitor_schedule_state(created, frozen, frozen)
        self.assertFalse(state["ok"])
        self.assertFalse(state["checks"]["baseline_last_check"])
        self.assertFalse(state["checks"]["baseline_next_moved"])


class _FakeWatchListClient:
    """A client whose ``call_tool`` returns a scripted watch list."""

    def __init__(self, watches):
        self._watches = list(watches)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            ok=True,
            structured={"count": len(self._watches), "watches": list(self._watches)},
        )


class NotificationWatchScopeTest(unittest.TestCase):
    """The notifications watch is identified by its result, never by its query.

    Regression: the LIVE scenario used to compare the free-text ``query`` with
    ``WATCH_QUERY``, so a differently worded query hid the created watch and the
    setup check falsely failed. The id must come from the tool result and the
    ``source_task_id`` from a read of that exact watch.
    """

    def _records(self, request_id, watch_id, query):
        return [
            {"event": "request_start", "request_id": request_id, "trigger": "chat"},
            {
                "event": "tool_completed",
                "request_id": request_id,
                "tool": "create_notification_watch",
                "ok": True,
                "result": {"watch_id": watch_id, "query": query, "status": "active"},
            },
        ]

    def test_created_watch_id_comes_from_the_tool_result_not_the_query(self):
        records = self._records("r1", "w-1", "official Python documentation")
        self.assertEqual(created_watch_id_from_trace(records, "r1"), "w-1")
        # Another request's completion is never used.
        self.assertEqual(created_watch_id_from_trace(records, "other"), "")

    def test_watch_by_id_selects_the_requested_watch_among_two(self):
        watches = [
            {
                "watch_id": "w-1",
                "query": "python documentation",
                "source_task_id": "t-1",
            },
            {
                "watch_id": "w-2",
                "query": "official Python documentation",
                "source_task_id": "t-2",
            },
        ]
        client = _FakeWatchListClient(watches)
        selected = _watch_by_id(client, "chat-1", "w-2")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["source_task_id"], "t-2")
        self.assertIsNone(_watch_by_id(client, "chat-1", "missing"))
        self.assertEqual(client.calls[0][0], "list_notification_watches")

    def test_resolve_notifications_watch_uses_the_created_watch(self):
        records = self._records("r1", "w-2", "official Python documentation")
        client = _FakeWatchListClient(
            [
                {
                    "watch_id": "w-1",
                    "query": "python documentation",
                    "source_task_id": "t-1",
                },
                {
                    "watch_id": "w-2",
                    "query": "official Python documentation",
                    "source_task_id": "t-2",
                },
            ]
        )
        scope = resolve_notifications_watch(records, "r1", client, "chat-1", "t-2")
        self.assertTrue(scope["ok"], msg=scope)
        self.assertEqual(scope["watch_id"], "w-2")
        self.assertEqual(scope["source_task_id"], "t-2")
        # A watch linked to a different task must fail, not pass by text.
        mismatch = resolve_notifications_watch(records, "r1", client, "chat-1", "t-1")
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["watch_id"], "w-2")

    def test_missing_watch_result_is_not_a_scope(self):
        scope = resolve_notifications_watch(
            [], None, _FakeWatchListClient([]), "chat-1", "t-1"
        )
        self.assertFalse(scope["ok"])
        self.assertEqual(scope["watch_id"], "")

    def test_chat_run_task_id_reads_the_last_selection_of_the_request(self):
        records = [
            {
                "event": "tool_selected",
                "request_id": "r1",
                "tool": "get_latest_search_run",
                "arguments": {"task_id": "t-1"},
            },
            {
                "event": "tool_selected",
                "request_id": "r1",
                "tool": "get_latest_search_run",
                "arguments": {"task_id": "t-2"},
            },
            {
                "event": "tool_selected",
                "request_id": "r2",
                "tool": "get_latest_search_run",
                "arguments": {"task_id": "other-request"},
            },
        ]
        self.assertEqual(chat_run_task_id(records, "r1"), "t-2")
        self.assertEqual(chat_run_task_id(records, "r2"), "other-request")
        self.assertEqual(chat_run_task_id(records, "missing"), "")


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

    def test_live_e2e_accepts_the_notifications_scenario_option(self):
        completed = self._run(
            "live_e2e.py", argv=("--scenario", "notifications", "--help")
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_live_e2e_accepts_the_monitor_scenario_option(self):
        completed = self._run(
            "live_e2e.py", argv=("--scenario", "notifications-monitor", "--help")
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def test_notifier_real_live_blocks_without_explicit_opt_in(self):
        completed = self._run(
            "notifier_real_live.py", extra_env={"NOTIFIER_REAL_ALLOW": ""}
        )
        self.assertNotIn("ModuleNotFoundError", completed.stderr)
        self.assertEqual(completed.returncode, 2, msg=completed.stderr)
        self.assertIn("NOTIFIER_REAL_STATUS: BLOCKED", completed.stdout)

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

    def test_notifier_restart_script_runs_from_a_file_path(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            completed = self._run(
                "notifier_restart.py",
                extra_env={
                    "NOTIFIER_RESTART_MCP_PORT": str(busy_port),
                    "NOTIFIER_RESTART_NOTIFIER_PORT": str(_free_port()),
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
