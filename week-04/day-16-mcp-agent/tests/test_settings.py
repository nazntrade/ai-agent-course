"""Unit tests of the settings resolution (no environment file, no network)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import settings as settings_module
from agent.settings import (
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_MODEL_NAME,
    resolve_settings,
)
from mcp_server import config as mcp_config


class DefaultsTest(unittest.TestCase):
    """An empty environment resolves to loopback defaults."""

    def setUp(self):
        self.settings = resolve_settings(env={}, dotenv=False)

    def test_model_endpoint_defaults_to_loopback(self):
        self.assertEqual(self.settings.model_base_url, DEFAULT_MODEL_BASE_URL)
        self.assertTrue(self.settings.model_base_url.startswith("http://127.0.0.1"))

    def test_unlaunched_default_model_is_unchanged(self):
        self.assertEqual(self.settings.model_name, DEFAULT_MODEL_NAME)

    def test_mcp_endpoint_defaults_to_loopback(self):
        self.assertEqual(self.settings.mcp_server_host, "127.0.0.1")
        self.assertEqual(self.settings.mcp_server_port, 8765)
        self.assertIn("/mcp", self.settings.mcp_server_url)

    def test_backend_defaults(self):
        self.assertEqual(self.settings.backend_host, "127.0.0.1")
        self.assertEqual(self.settings.backend_port, 8600)
        self.assertEqual(self.settings.public_backend_url, "http://127.0.0.1:8600")

    def test_missing_key_means_the_model_is_not_configured(self):
        self.assertEqual(self.settings.api_key, "")
        self.assertFalse(self.settings.model_configured)
        self.assertTrue(self.settings.warnings)

    def test_no_placeholder_key_is_ever_used(self):
        self.assertNotEqual(self.settings.api_key, "local-e2e")
        self.assertNotIn("local-e2e", self.settings.api_key)

    def test_trace_path_is_resolved_inside_the_project(self):
        self.assertIsNotNone(self.settings.trace_path)
        self.assertTrue(str(self.settings.trace_path).endswith("trace.jsonl"))
        self.assertTrue(Path(self.settings.trace_path).is_absolute())

    def test_db_path_defaults_inside_the_project(self):
        self.assertTrue(Path(self.settings.db_path).is_absolute())
        self.assertTrue(str(self.settings.db_path).endswith("day18.sqlite3"))
        self.assertIn("data", str(self.settings.db_path))

    def test_chat_context_window_defaults_to_twenty(self):
        self.assertEqual(self.settings.chat_context_messages, 20)

    def test_tool_round_limit_defaults_to_five(self):
        self.assertEqual(self.settings.max_tool_rounds, 5)

    def test_notifier_defaults_to_the_loopback_endpoint(self):
        self.assertEqual(
            self.settings.mcp_notifier_url, "http://127.0.0.1:8766/mcp"
        )

    def test_notifier_monitor_defaults_on_with_a_thirty_second_tick(self):
        self.assertTrue(self.settings.notifier_monitor_enabled)
        self.assertEqual(self.settings.notifier_monitor_tick_seconds, 30)


class EnvironmentTest(unittest.TestCase):
    """Explicit environment variables win over the defaults."""

    def test_local_launcher_model_is_only_a_fallback(self):
        resolved = resolve_settings(
            env={"AGENT_LOCAL_MODEL_DEFAULT_NAME": "gemma-4-26B-A4B-it-UD-IQ4_XS"},
            dotenv=False,
        )
        self.assertEqual(resolved.model_name, "gemma-4-26B-A4B-it-UD-IQ4_XS")
        self.assertEqual(resolved.model_base_url, DEFAULT_MODEL_BASE_URL)

    def test_local_launcher_sets_the_gemma_fallback(self):
        launcher = (settings_module.PROJECT_ROOT / "run_app.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'set "AGENT_LOCAL_MODEL_DEFAULT_NAME=gemma-4-26B-A4B-it-UD-IQ4_XS"',
            launcher,
        )

    def test_explicit_model_name_overrides_local_launcher_fallback(self):
        resolved = resolve_settings(
            env={
                "AGENT_MODEL_NAME": "explicit-model",
                "AGENT_LOCAL_MODEL_DEFAULT_NAME": "gemma-4-26B-A4B-it-UD-IQ4_XS",
            },
            dotenv=False,
        )
        self.assertEqual(resolved.model_name, "explicit-model")

    def test_model_name_loaded_from_dotenv_overrides_launcher_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENT_LOCAL_MODEL_DEFAULT_NAME": "gemma-4-26B-A4B-it-UD-IQ4_XS"},
                    clear=True,
                ),
                mock.patch.object(
                    settings_module,
                    "load_dotenv_if_present",
                    side_effect=lambda _path: os.environ.__setitem__(
                        "AGENT_MODEL_NAME", "from-dotenv"
                    ),
                ),
            ):
                resolved = resolve_settings(env_path=Path(temporary) / "synthetic.env")
        self.assertEqual(resolved.model_name, "from-dotenv")

    def test_key_is_read_from_the_named_variable(self):
        environment = {
            "AGENT_MODEL_API_KEY_ENV": "MY_LOCAL_KEY",
            "MY_LOCAL_KEY": "secret-value",
        }
        resolved = resolve_settings(env=environment, dotenv=False)
        self.assertEqual(resolved.model_api_key, "secret-value")
        self.assertTrue(resolved.model_configured)
        self.assertFalse(resolved.warnings)

    def test_repr_never_contains_the_key(self):
        environment = {
            "AGENT_MODEL_API_KEY_ENV": "MY_LOCAL_KEY",
            "MY_LOCAL_KEY": "super-secret-value",
        }
        resolved = resolve_settings(env=environment, dotenv=False)
        self.assertNotIn("super-secret-value", repr(resolved))

    def test_ports_are_parsed(self):
        environment = {
            "MCP_SERVER_PORT": "9999",
            "BACKEND_PORT": "7000",
            "BACKEND_HOST": "localhost",
        }
        resolved = resolve_settings(env=environment, dotenv=False)
        self.assertEqual(resolved.mcp_server_port, 9999)
        self.assertEqual(resolved.backend_port, 7000)
        self.assertEqual(resolved.backend_host, "localhost")

    def test_invalid_port_falls_back_to_default(self):
        resolved = resolve_settings(env={"BACKEND_PORT": "not-a-port"}, dotenv=False)
        self.assertEqual(resolved.backend_port, 8600)
        self.assertEqual(
            resolve_settings(env={"BACKEND_PORT": "0"}, dotenv=False).backend_port, 8600
        )
        self.assertEqual(
            resolve_settings(env={"BACKEND_PORT": "70000"}, dotenv=False).backend_port,
            8600,
        )

    def test_invalid_timeout_falls_back_to_default(self):
        resolved = resolve_settings(
            env={"AGENT_MODEL_TIMEOUT_SECONDS": "-5"}, dotenv=False
        )
        self.assertEqual(
            resolved.model_timeout_seconds,
            settings_module.DEFAULT_MODEL_TIMEOUT_SECONDS,
        )

    def test_public_urls_are_derived_from_the_backend_host_and_port(self):
        resolved = resolve_settings(
            env={"BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": "8700"}, dotenv=False
        )
        self.assertEqual(resolved.public_frontend_url, "http://127.0.0.1:8700")

    def test_db_path_is_resolved_from_the_environment(self):
        resolved = resolve_settings(
            env={"AGENT_DB_PATH": "runs/test.sqlite3"}, dotenv=False
        )
        self.assertTrue(Path(resolved.db_path).is_absolute())
        self.assertTrue(str(resolved.db_path).endswith("test.sqlite3"))

    def test_chat_context_messages_is_clamped(self):
        self.assertEqual(
            resolve_settings(env={"AGENT_CHAT_CONTEXT_MESSAGES": "1"}, dotenv=False)
            .chat_context_messages,
            2,
        )
        self.assertEqual(
            resolve_settings(env={"AGENT_CHAT_CONTEXT_MESSAGES": "500"}, dotenv=False)
            .chat_context_messages,
            100,
        )
        self.assertEqual(
            resolve_settings(env={"AGENT_CHAT_CONTEXT_MESSAGES": "nope"}, dotenv=False)
            .chat_context_messages,
            20,
        )

    def test_max_tool_rounds_is_clamped(self):
        self.assertEqual(
            resolve_settings(env={"AGENT_MAX_TOOL_ROUNDS": "1"}, dotenv=False)
            .max_tool_rounds,
            2,
        )
        self.assertEqual(
            resolve_settings(env={"AGENT_MAX_TOOL_ROUNDS": "99"}, dotenv=False)
            .max_tool_rounds,
            10,
        )
        self.assertEqual(
            resolve_settings(env={"AGENT_MAX_TOOL_ROUNDS": "nope"}, dotenv=False)
            .max_tool_rounds,
            5,
        )
        self.assertEqual(
            resolve_settings(env={"AGENT_MAX_TOOL_ROUNDS": "7"}, dotenv=False)
            .max_tool_rounds,
            7,
        )

    def test_empty_notifier_url_disables_server_b(self):
        resolved = resolve_settings(env={"MCP_NOTIFIER_URL": ""}, dotenv=False)
        self.assertEqual(resolved.mcp_notifier_url, "")

    def test_explicit_notifier_url_is_kept(self):
        resolved = resolve_settings(
            env={"MCP_NOTIFIER_URL": "http://127.0.0.1:9999/mcp"}, dotenv=False
        )
        self.assertEqual(resolved.mcp_notifier_url, "http://127.0.0.1:9999/mcp")

    def test_notifier_monitor_flag_can_be_disabled(self):
        self.assertFalse(
            resolve_settings(
                env={"NOTIFIER_MONITOR_ENABLED": "0"}, dotenv=False
            ).notifier_monitor_enabled
        )
        self.assertTrue(
            resolve_settings(
                env={"NOTIFIER_MONITOR_ENABLED": "1"}, dotenv=False
            ).notifier_monitor_enabled
        )

    def test_notifier_monitor_tick_is_clamped(self):
        self.assertEqual(
            resolve_settings(
                env={"NOTIFIER_MONITOR_TICK_SECONDS": "0"}, dotenv=False
            ).notifier_monitor_tick_seconds,
            1,
        )
        self.assertEqual(
            resolve_settings(
                env={"NOTIFIER_MONITOR_TICK_SECONDS": "99999"}, dotenv=False
            ).notifier_monitor_tick_seconds,
            3600,
        )
        self.assertEqual(
            resolve_settings(
                env={"NOTIFIER_MONITOR_TICK_SECONDS": "nope"}, dotenv=False
            ).notifier_monitor_tick_seconds,
            30,
        )


class NotifierSettingsSourceGuardTest(unittest.TestCase):
    """``agent/settings.py`` must never read a Telegram secret or B's dotenv flag."""

    def test_no_telegram_secret_is_read_in_the_backend_settings(self):
        source = Path(settings_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("TELEGRAM", source)
        self.assertNotIn("NOTIFIER_LOAD_DOTENV", source)


class McpConfigDotenvTest(unittest.TestCase):
    """The MCP server reads ``AGENT_DB_PATH``/tick from the same ``.env``.

    Regression for the SPEC §5.1 gap: the MCP process used to resolve the
    database path and the tick before ``.env`` was loaded, so a value set only
    in a local ``.env`` was seen by the backend but not by the MCP server, and
    the two processes opened different files (or ignored the configured tick).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_path = Path(self._tmp.name) / ".env"

    def _write_env(self, body: str) -> None:
        self.env_path.write_text(body, encoding="utf-8")

    def test_db_path_is_read_from_dotenv_when_allowed(self):
        target = Path(self._tmp.name) / "from-env.sqlite3"
        self._write_env(f"AGENT_DB_PATH={target.as_posix()}\n")
        with mock.patch.dict(os.environ, {"MCP_LOAD_DOTENV": "1"}, clear=False):
            os.environ.pop("AGENT_DB_PATH", None)
            resolved = mcp_config.resolve_db_path(env_path=self.env_path)
        self.assertEqual(resolved, target)

    def test_db_path_ignores_dotenv_when_disabled(self):
        self._write_env(
            f"AGENT_DB_PATH={(Path(self._tmp.name) / 'ignored.sqlite3').as_posix()}\n"
        )
        with mock.patch.dict(os.environ, {"MCP_LOAD_DOTENV": "0"}, clear=False):
            os.environ.pop("AGENT_DB_PATH", None)
            resolved = mcp_config.resolve_db_path(env_path=self.env_path)
        self.assertEqual(
            resolved,
            mcp_config.PROJECT_ROOT / "data" / mcp_config.DEFAULT_DB_FILENAME,
        )

    def test_db_path_default_matches_the_backend_default(self):
        # ``env={}`` never touches a file, so this stays isolated from the real
        # .env; both processes must compute the same default.
        self.assertEqual(
            mcp_config.resolve_db_path(env={}),
            resolve_settings(env={}, dotenv=False).db_path,
        )
        self.assertEqual(
            mcp_config.resolve_db_path(env={}),
            mcp_config.PROJECT_ROOT / "data" / "day18.sqlite3",
        )

    def test_task_tick_is_read_from_dotenv_when_allowed(self):
        self._write_env("MCP_TASK_TICK_SECONDS=7.5\n")
        with mock.patch.dict(os.environ, {"MCP_LOAD_DOTENV": "1"}, clear=False):
            os.environ.pop("MCP_TASK_TICK_SECONDS", None)
            resolved = mcp_config.resolve_task_tick_seconds(env_path=self.env_path)
        self.assertEqual(resolved, 7.5)

    def test_task_tick_ignores_dotenv_when_disabled(self):
        self._write_env("MCP_TASK_TICK_SECONDS=7.5\n")
        with mock.patch.dict(os.environ, {"MCP_LOAD_DOTENV": "0"}, clear=False):
            os.environ.pop("MCP_TASK_TICK_SECONDS", None)
            resolved = mcp_config.resolve_task_tick_seconds(env_path=self.env_path)
        self.assertEqual(resolved, mcp_config.DEFAULT_TASK_TICK_SECONDS)

    def test_explicit_environment_never_reads_the_file(self):
        self._write_env("AGENT_DB_PATH=/should/not/be/read.sqlite3\n")
        with mock.patch.dict(os.environ, {"MCP_LOAD_DOTENV": "1"}, clear=False):
            resolved = mcp_config.resolve_db_path(
                env={"AGENT_DB_PATH": "explicit.sqlite3"}
            )
        self.assertEqual(resolved, mcp_config.PROJECT_ROOT / "explicit.sqlite3")


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
