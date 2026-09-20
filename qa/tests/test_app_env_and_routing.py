"""Unit tests of the child environment and the app process wiring.

No Streamlit process is started: only the environment and the command line are
inspected.
"""

from __future__ import annotations

import socket
import unittest

from lib.app_process import (
    API_KEY_ENV,
    BASE_URL_ENV,
    DB_PATH_ENV,
    PLACEHOLDER_API_KEY,
    AppProcess,
    build_child_env,
    port_is_free,
)


class ChildEnvTest(unittest.TestCase):
    def test_deepseek_variables_are_removed(self):
        env = build_child_env(
            base_url="http://127.0.0.1:1234/v1",
            db_path="C:/run/app.db",
            base_env={
                "DEEPSEEK_API_KEY": "sk-owner-secret",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "PATH": "C:/Windows",
            },
        )
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("DEEPSEEK_BASE_URL", env)
        self.assertEqual(env["PATH"], "C:/Windows")

    def test_loopback_routing_and_private_database_are_set(self):
        env = build_child_env(
            base_url="http://127.0.0.1:1234/v1",
            db_path="C:/run/app.db",
            base_env={},
        )
        self.assertEqual(env[BASE_URL_ENV], "http://127.0.0.1:1234/v1")
        self.assertEqual(env[DB_PATH_ENV], "C:/run/app.db")
        self.assertEqual(env[API_KEY_ENV], PLACEHOLDER_API_KEY)

    def test_streamlit_is_headless_and_does_not_gather_usage(self):
        env = build_child_env(
            base_url="http://127.0.0.1:1234/v1",
            db_path="C:/run/app.db",
            base_env={},
        )
        self.assertEqual(env["STREAMLIT_SERVER_HEADLESS"], "true")
        self.assertEqual(env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"], "false")

    def test_empty_api_key_falls_back_to_the_placeholder(self):
        env = build_child_env(
            base_url="http://127.0.0.1:1234/v1",
            db_path="C:/run/app.db",
            api_key="",
            base_env={},
        )
        self.assertEqual(env[API_KEY_ENV], PLACEHOLDER_API_KEY)

    def test_base_environment_is_not_mutated(self):
        base = {"DEEPSEEK_API_KEY": "sk-owner-secret"}
        build_child_env(
            base_url="http://127.0.0.1:1234/v1", db_path="C:/run/app.db", base_env=base
        )
        self.assertEqual(base, {"DEEPSEEK_API_KEY": "sk-owner-secret"})


class PortTest(unittest.TestCase):
    def test_free_port_is_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertTrue(port_is_free(port))

    def test_listening_port_is_not_free(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        self.assertFalse(port_is_free(port))


class AppProcessWiringTest(unittest.TestCase):
    def test_command_targets_the_app_on_the_private_port(self):
        process = AppProcess(
            port=8599,
            db_path="C:/run/app.db",
            base_url="http://127.0.0.1:1234/v1",
            python_exe="python.exe",
        )
        command = process.command
        self.assertEqual(command[0], "python.exe")
        self.assertIn("streamlit", command)
        self.assertIn("app.py", command)
        self.assertIn("8599", command)
        self.assertIn("127.0.0.1", command)

    def test_health_url_uses_the_private_port(self):
        process = AppProcess(
            port=8599, db_path="C:/run/app.db", base_url="http://127.0.0.1:1/v1"
        )
        self.assertEqual(process.health_url, "http://127.0.0.1:8599/_stcore/health")

    def test_a_real_local_base_url_is_used(self):
        process = AppProcess(
            port=1, db_path="C:/run/app.db", base_url="http://127.0.0.1:7777/v1"
        )
        env = build_child_env(
            base_url=process.base_url, db_path=process.db_path, base_env={}
        )
        self.assertTrue(env[BASE_URL_ENV].startswith("http://127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
