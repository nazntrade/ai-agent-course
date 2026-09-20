"""Unit tests of the local-LLM configuration (``lib.config``).

No real environment variable and no real local config file is read: every test
passes an explicit ``env`` and a temporary ``config_path``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.config import (
    DEFAULT_API_KEY,
    DEFAULT_LIVE_TIMEOUT_SECONDS,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MODEL,
    LOCAL_API_KEY_ENV,
    LOCAL_BASE_URL_ENV,
    LOCAL_CONFIG_ENV,
    LOCAL_LAUNCH_COMMAND_ENV,
    LOCAL_LIVE_TIMEOUT_ENV,
    LOCAL_MODEL_ENV,
    is_loopback_url,
    load_config,
    load_example_config,
    looks_like_placeholder,
    resolve_config_path,
)


class LoopbackTest(unittest.TestCase):
    def test_loopback_hosts_are_accepted(self):
        for url in (
            "http://127.0.0.1:8080/v1",
            "http://localhost:8080/v1",
            "http://[::1]:8080/v1",
        ):
            self.assertTrue(is_loopback_url(url), url)

    def test_remote_or_malformed_hosts_are_rejected(self):
        for url in (
            "https://api.deepseek.com",
            "http://192.168.1.10:8080/v1",
            "not a url",
            "",
            None,
        ):
            self.assertFalse(is_loopback_url(url), url)


class PlaceholderTest(unittest.TestCase):
    def test_placeholder_values_are_detected(self):
        for value in ("<your-model>", "your-model-id", "example-model", "CHANGEME"):
            self.assertTrue(looks_like_placeholder(value), value)

    def test_real_or_loopback_values_are_not_placeholders(self):
        for value in ("", "qwen-local", "http://127.0.0.1:8080/v1"):
            self.assertFalse(looks_like_placeholder(value), value)


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.missing = str(Path(self._tmp.name) / "missing.local.json")

    def test_defaults_without_any_configuration(self):
        config = load_config(env={}, config_path=self.missing)
        self.assertEqual(config.base_url, DEFAULT_LOCAL_BASE_URL)
        self.assertEqual(config.model, "")
        self.assertFalse(config.has_local_config)
        self.assertEqual(config.source, "default")
        self.assertTrue(config.is_loopback)
        self.assertFalse(config.can_launch)

    def test_environment_values_win_and_are_marked(self):
        config = load_config(
            env={
                LOCAL_BASE_URL_ENV: "http://127.0.0.1:9100/v1",
                LOCAL_MODEL_ENV: "local-model",
                LOCAL_LAUNCH_COMMAND_ENV: "start-server --port 9100",
            },
            config_path=self.missing,
        )
        self.assertEqual(config.base_url, "http://127.0.0.1:9100/v1")
        self.assertEqual(config.model, "local-model")
        self.assertTrue(config.has_local_config)
        self.assertEqual(config.source, "env")
        self.assertTrue(config.can_launch)

    def test_file_values_are_read_and_marked(self):
        path = Path(self._tmp.name) / "local.llm.local.json"
        path.write_text(
            json.dumps(
                {
                    "base_url": "http://127.0.0.1:9200/v1",
                    "model": "file-model",
                }
            ),
            encoding="utf-8",
        )
        config = load_config(env={}, config_path=str(path))
        self.assertEqual(config.base_url, "http://127.0.0.1:9200/v1")
        self.assertEqual(config.model, "file-model")
        self.assertTrue(config.has_local_config)
        self.assertEqual(config.source, "file")

    def test_environment_overrides_the_file(self):
        path = Path(self._tmp.name) / "local.llm.local.json"
        path.write_text(
            json.dumps({"base_url": "http://127.0.0.1:9200/v1", "model": "file-model"}),
            encoding="utf-8",
        )
        config = load_config(
            env={LOCAL_MODEL_ENV: "env-model"}, config_path=str(path)
        )
        self.assertEqual(config.model, "env-model")
        self.assertEqual(config.base_url, "http://127.0.0.1:9200/v1")
        self.assertEqual(config.source, "env")

    def test_api_key_can_come_from_the_environment_and_wins_over_the_file(self):
        path = Path(self._tmp.name) / "local.llm.local.json"
        path.write_text(json.dumps({"api_key": "file-key"}), encoding="utf-8")
        config = load_config(env={LOCAL_API_KEY_ENV: "env-key"}, config_path=str(path))
        self.assertEqual(config.api_key, "env-key")
        file_only = load_config(env={}, config_path=str(path))
        self.assertEqual(file_only.api_key, "file-key")
        self.assertEqual(
            load_config(env={}, config_path=self.missing).api_key, DEFAULT_API_KEY
        )

    def test_placeholder_model_is_ignored_with_a_warning(self):
        path = Path(self._tmp.name) / "local.llm.local.json"
        path.write_text(
            json.dumps({"model": "<your-local-model-id>", "launch_command": "<cmd>"}),
            encoding="utf-8",
        )
        config = load_config(env={}, config_path=str(path))
        self.assertEqual(config.model, "")
        self.assertEqual(config.launch_command, "")
        self.assertFalse(config.can_launch)
        self.assertTrue(config.has_local_config)
        self.assertTrue(config.warnings)

    def test_corrupt_config_file_falls_back_to_defaults(self):
        path = Path(self._tmp.name) / "local.llm.local.json"
        path.write_text("{ not json", encoding="utf-8")
        config = load_config(env={}, config_path=str(path))
        self.assertEqual(config.base_url, DEFAULT_LOCAL_BASE_URL)
        self.assertTrue(config.has_local_config)

    def test_config_path_can_come_from_the_environment(self):
        path = Path(self._tmp.name) / "explicit.json"
        path.write_text(json.dumps({"model": "explicit-model"}), encoding="utf-8")
        resolved = resolve_config_path({LOCAL_CONFIG_ENV: str(path)}, None)
        self.assertEqual(resolved, path)
        config = load_config(env={LOCAL_CONFIG_ENV: str(path)})
        self.assertEqual(config.model, "explicit-model")


class LiveTimeoutTest(unittest.TestCase):
    """The live scenario budget must come from config, not the recipe default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.missing = str(Path(self._tmp.name) / "missing.local.json")

    def test_default_is_generous(self):
        config = load_config(env={}, config_path=self.missing)
        self.assertEqual(config.live_timeout_seconds, DEFAULT_LIVE_TIMEOUT_SECONDS)
        # A 1049-token prefill alone took ~72 s on the real endpoint; the budget
        # must cover prefill plus generation plus one corrective retry.
        self.assertGreaterEqual(config.live_timeout_seconds, 600)

    def test_environment_overrides_the_live_timeout(self):
        config = load_config(
            env={LOCAL_LIVE_TIMEOUT_ENV: "2400"}, config_path=self.missing
        )
        self.assertEqual(config.live_timeout_seconds, 2400)

    def test_invalid_live_timeout_falls_back_to_the_default(self):
        config = load_config(
            env={LOCAL_LIVE_TIMEOUT_ENV: "not-a-number"}, config_path=self.missing
        )
        self.assertEqual(config.live_timeout_seconds, DEFAULT_LIVE_TIMEOUT_SECONDS)
        self.assertTrue(config.warnings)


class DefaultModelTest(unittest.TestCase):
    def test_default_model_is_a_concrete_fallback(self):
        self.assertTrue(DEFAULT_LOCAL_MODEL)
        self.assertFalse(looks_like_placeholder(DEFAULT_LOCAL_MODEL))


class ExampleConfigTest(unittest.TestCase):
    def test_example_file_has_placeholders_only(self):
        example = load_example_config()
        self.assertTrue(example)
        self.assertTrue(looks_like_placeholder(example.get("model")))
        self.assertTrue(looks_like_placeholder(example.get("launch_command")))
        self.assertEqual(example.get("base_url"), DEFAULT_LOCAL_BASE_URL)
        # No real local path or command is committed.
        for value in example.values():
            if isinstance(value, str):
                self.assertNotIn(":\\", value)


if __name__ == "__main__":
    unittest.main()
