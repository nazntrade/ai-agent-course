"""Unit tests of the notifier configuration (no environment file, no network).

Every configuration test passes an explicit environment and ``dotenv=False``, so
the real ``.env`` is never read.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notifier_server import config as notifier_config
from notifier_server.config import (
    DEFAULT_TELEGRAM_API_BASE_URL,
    TelegramConfig,
    load_dotenv_if_present,
    resolve_db_path,
    resolve_telegram_config,
)

TOKEN = "unit-test-bot-token"
RECIPIENT = "123456789"


class TelegramConfigTest(unittest.TestCase):
    """The Telegram configuration resolves from the environment safely."""

    def test_defaults_without_a_token(self):
        config = resolve_telegram_config(env={}, dotenv=False)
        self.assertFalse(config.configured)
        self.assertEqual(config.token, "")
        self.assertEqual(config.recipient, "")
        self.assertEqual(config.base_url, DEFAULT_TELEGRAM_API_BASE_URL)

    def test_environment_overrides(self):
        env = {
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_CHAT_ID": RECIPIENT,
            "NOTIFIER_TELEGRAM_API_BASE_URL": "http://127.0.0.1:9999/",
        }
        config = resolve_telegram_config(env=env, dotenv=False)
        self.assertTrue(config.configured)
        self.assertEqual(config.token, TOKEN)
        self.assertEqual(config.recipient, RECIPIENT)
        self.assertEqual(config.base_url, "http://127.0.0.1:9999")

    def test_token_without_recipient_is_not_configured(self):
        config = resolve_telegram_config(env={"TELEGRAM_BOT_TOKEN": TOKEN}, dotenv=False)
        self.assertFalse(config.configured)

    def test_base_url_must_be_http_or_https(self):
        for value in ("ftp://example.test", "example.test", "http:", ""):
            with self.subTest(value=value):
                config = resolve_telegram_config(
                    env={"NOTIFIER_TELEGRAM_API_BASE_URL": value}, dotenv=False
                )
                self.assertEqual(config.base_url, DEFAULT_TELEGRAM_API_BASE_URL)

    def test_repr_never_contains_the_token_or_recipient(self):
        config = resolve_telegram_config(
            env={
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_CHAT_ID": RECIPIENT,
            },
            dotenv=False,
        )
        rendered = repr(config)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn(RECIPIENT, rendered)
        self.assertNotIn("token=", rendered)
        self.assertNotIn("recipient=", rendered)

    def test_explicit_environment_never_reads_a_file(self):
        config = resolve_telegram_config(env={"TELEGRAM_BOT_TOKEN": TOKEN}, dotenv=False)
        self.assertEqual(config.token, TOKEN)


class DbPathTest(unittest.TestCase):
    """``NOTIFIER_DB_PATH`` resolves inside the project by default."""

    def test_default_is_inside_the_project(self):
        path = resolve_db_path(env={})
        self.assertTrue(path.is_absolute())
        self.assertTrue(str(path).endswith("day20-notifier.sqlite3"))
        self.assertIn("data", str(path))

    def test_environment_overrides(self):
        path = resolve_db_path(env={"NOTIFIER_DB_PATH": "runs/custom.sqlite3"})
        self.assertTrue(path.is_absolute())
        self.assertTrue(str(path).endswith("custom.sqlite3"))

    def test_absolute_path_is_kept(self):
        target = Path(tempfile.gettempdir()) / "absolute-notifier.sqlite3"
        self.assertEqual(
            resolve_db_path(env={"NOTIFIER_DB_PATH": str(target)}), target
        )


class LoadDotenvTest(unittest.TestCase):
    """``NOTIFIER_LOAD_DOTENV=0`` stops the notifier from reading ``.env``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_path = Path(self._tmp.name) / ".env"
        self.env_path.write_text(
            "NOTIFIER_TEST_DOTENV_VALUE=from-file\n", encoding="utf-8"
        )
        self._saved = {
            "NOTIFIER_LOAD_DOTENV": os.environ.pop("NOTIFIER_LOAD_DOTENV", None),
            "NOTIFIER_TEST_DOTENV_VALUE": os.environ.pop(
                "NOTIFIER_TEST_DOTENV_VALUE", None
            ),
        }
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.pop("NOTIFIER_TEST_DOTENV_VALUE", None)

    def test_disabled_load_reads_nothing(self):
        os.environ["NOTIFIER_LOAD_DOTENV"] = "0"
        loaded = load_dotenv_if_present(env_path=self.env_path)
        self.assertFalse(loaded)
        self.assertNotIn("NOTIFIER_TEST_DOTENV_VALUE", os.environ)

    def test_enabled_load_reads_the_file(self):
        os.environ["NOTIFIER_LOAD_DOTENV"] = "1"
        loaded = load_dotenv_if_present(env_path=self.env_path)
        self.assertTrue(loaded)
        self.assertEqual(os.environ.get("NOTIFIER_TEST_DOTENV_VALUE"), "from-file")

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(
            load_dotenv_if_present(env_path=Path(self._tmp.name) / "missing.env")
        )

    def test_db_path_ignores_dotenv_when_disabled(self):
        target = Path(self._tmp.name) / "ignored.sqlite3"
        self.env_path.write_text(f"NOTIFIER_DB_PATH={target.as_posix()}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"NOTIFIER_LOAD_DOTENV": "0"}, clear=False):
            os.environ.pop("NOTIFIER_DB_PATH", None)
            resolved = resolve_db_path(env_path=self.env_path)
        self.assertEqual(
            resolved,
            notifier_config.PROJECT_ROOT / "data" / notifier_config.DEFAULT_DB_FILENAME,
        )

    def test_explicit_environment_never_reads_the_file(self):
        resolved = resolve_db_path(env={"NOTIFIER_DB_PATH": "explicit.sqlite3"})
        self.assertTrue(str(resolved).endswith("explicit.sqlite3"))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
