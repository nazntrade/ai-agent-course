"""Unit tests of the settings resolution (no environment file, no network)."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent import settings as settings_module
from agent.settings import (
    DEFAULT_MODEL_BASE_URL,
    resolve_settings,
)


class DefaultsTest(unittest.TestCase):
    """An empty environment resolves to loopback defaults."""

    def setUp(self):
        self.settings = resolve_settings(env={}, dotenv=False)

    def test_model_endpoint_defaults_to_loopback(self):
        self.assertEqual(self.settings.model_base_url, DEFAULT_MODEL_BASE_URL)
        self.assertTrue(self.settings.model_base_url.startswith("http://127.0.0.1"))

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


class EnvironmentTest(unittest.TestCase):
    """Explicit environment variables win over the defaults."""

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


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
