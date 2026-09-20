"""Offline unit tests of the local-LLM launcher ownership and stop logic.

No real process is spawned and no real port is inspected: the process, the
listener lookup, the start time and the killer are all injected. A loopback stub
verifies that the readiness probe authenticates with the configured key.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lib.config import LocalLlmConfig
from lib.local_llm import (
    KEEP_SERVER_ENV,
    LAUNCHER_API_KEY_ENV,
    LAUNCHER_QA_API_KEY_ENV,
    OWNERSHIP_NOT_STARTED,
    OWNERSHIP_OWN_DETACHED,
    OWNERSHIP_OWN_LIVE,
    OWNERSHIP_UNPROVEN,
    LocalLlmLauncher,
    parse_netstat_listeners,
    probe,
)

FIXTURE_KEY = "fixture-local-key"

NETSTAT_SAMPLE = """
  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:5432         0.0.0.0:0              LISTENING       1111
  TCP    [::]:8080              [::]:0                 LISTENING       4243
  TCP    127.0.0.1:9000         127.0.0.1:50000        ESTABLISHED     5555
  TCP    127.0.0.1:8081         0.0.0.0:0              LISTENING       4242
"""


class FakeProcess:
    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0


def _config():
    return LocalLlmConfig(
        base_url="http://127.0.0.1:8080/v1",
        launch_command="start_qwen",
    )


def _launcher(*, process=None, killed=None, listeners=(), start_time=None):
    killed = [] if killed is None else killed
    launcher = LocalLlmLauncher(
        _config(),
        popen=lambda *args, **kwargs: process,
        kill_fn=lambda pid: killed.append(pid) or True,
        listeners_fn=lambda port: list(listeners),
        start_time_fn=start_time or (lambda pid: None),
    )
    launcher.start()
    return launcher, killed


class _AuthStubHandler(BaseHTTPRequestHandler):
    """Loopback stub that answers ``/v1/models`` only with the fixture key."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # pragma: no cover - silence the server log
        return

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if str(self.headers.get("Authorization") or "") != f"Bearer {FIXTURE_KEY}":
            self._send({"error": {"message": "Invalid API Key"}}, 401)
            return
        self._send({"data": [{"id": "stub-model"}]}, 200)

    def _send(self, payload, status) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ParseNetstatTest(unittest.TestCase):
    def test_only_listening_rows_of_the_port_are_returned(self):
        self.assertEqual(parse_netstat_listeners(NETSTAT_SAMPLE, 8080), [4242, 4243])
        self.assertEqual(parse_netstat_listeners(NETSTAT_SAMPLE, 5432), [1111])
        self.assertEqual(parse_netstat_listeners(NETSTAT_SAMPLE, 9999), [])
        self.assertEqual(parse_netstat_listeners("", 8080), [])


class ClaimOwnershipTest(unittest.TestCase):
    def test_not_started_without_a_process(self):
        launcher = LocalLlmLauncher(_config(), listeners_fn=lambda port: [])
        self.assertEqual(launcher.claim_ownership(), OWNERSHIP_NOT_STARTED)
        self.assertFalse(launcher.spawned)

    def test_live_process_is_owned(self):
        launcher, _ = _launcher(process=FakeProcess(pid=7, alive=True))
        self.assertEqual(launcher.claim_ownership(), OWNERSHIP_OWN_LIVE)

    def test_detached_listener_started_later_is_owned(self):
        launcher, _ = _launcher(
            process=FakeProcess(pid=7, alive=False),
            listeners=[9001],
            start_time=lambda pid: 2000.0,
        )
        launcher._started_at = 1000.0
        self.assertEqual(launcher.claim_ownership(), OWNERSHIP_OWN_DETACHED)

    def test_detached_listener_with_unknown_start_is_unproven(self):
        launcher, _ = _launcher(
            process=FakeProcess(pid=7, alive=False),
            listeners=[9001],
            start_time=lambda pid: None,
        )
        launcher._started_at = 1000.0
        self.assertEqual(launcher.claim_ownership(), OWNERSHIP_UNPROVEN)


class StopTest(unittest.TestCase):
    def test_live_process_is_killed_by_pid(self):
        launcher, killed = _launcher(process=FakeProcess(pid=777, alive=True))
        result = launcher.stop(keep=False, env={})
        self.assertTrue(result["stopped"])
        self.assertFalse(result["left_running"])
        self.assertEqual(killed, [777])
        self.assertFalse(launcher.spawned)

    def test_detached_proven_listener_is_killed(self):
        launcher, killed = _launcher(
            process=FakeProcess(pid=7, alive=False),
            listeners=[9001],
            start_time=lambda pid: 2000.0,
        )
        launcher._started_at = 1000.0
        result = launcher.stop(keep=False, env={})
        self.assertTrue(result["stopped"])
        self.assertEqual(killed, [9001])

    def test_unproven_listener_is_left_running(self):
        launcher, killed = _launcher(
            process=FakeProcess(pid=7, alive=False),
            listeners=[9001],
            start_time=lambda pid: 500.0,
        )
        launcher._started_at = 1000.0
        result = launcher.stop(keep=False, env={})
        self.assertFalse(result["stopped"])
        self.assertTrue(result["left_running"])
        self.assertIn("unproven", result["reason"])
        self.assertEqual(killed, [])

    def test_unknown_start_time_is_left_running(self):
        launcher, killed = _launcher(
            process=FakeProcess(pid=7, alive=False),
            listeners=[9001],
            start_time=lambda pid: None,
        )
        launcher._started_at = 1000.0
        result = launcher.stop(keep=False, env={})
        self.assertTrue(result["left_running"])
        self.assertEqual(killed, [])

    def test_nothing_to_stop_without_a_process(self):
        launcher = LocalLlmLauncher(_config(), listeners_fn=lambda port: [])
        result = launcher.stop(keep=False, env={})
        self.assertFalse(result["stopped"])
        self.assertFalse(result["left_running"])
        self.assertEqual(result["reason"], "nothing to stop")

    def test_keep_leaves_a_live_process_running(self):
        launcher, killed = _launcher(process=FakeProcess(pid=777, alive=True))
        result = launcher.stop(keep=True)
        self.assertTrue(result["left_running"])
        self.assertFalse(result["stopped"])
        self.assertEqual(killed, [])

    def test_keep_can_come_from_the_environment(self):
        launcher, killed = _launcher(process=FakeProcess(pid=777, alive=True))
        result = launcher.stop(env={KEEP_SERVER_ENV: "1"})
        self.assertTrue(result["left_running"])
        self.assertEqual(killed, [])


class ProbeAuthTest(unittest.TestCase):
    """Readiness must authenticate, or an auth-protected server answers 401."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthStubHandler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def test_probe_without_a_key_is_refused(self):
        self.assertIsNone(probe(self.base_url))

    def test_probe_with_a_wrong_key_is_refused(self):
        self.assertIsNone(probe(self.base_url, api_key="wrong-key"))

    def test_probe_with_the_configured_key_reads_the_models(self):
        self.assertEqual(probe(self.base_url, api_key=FIXTURE_KEY), ["stub-model"])

    def test_wait_ready_uses_the_config_key(self):
        launcher = LocalLlmLauncher(
            LocalLlmConfig(base_url=self.base_url, api_key=FIXTURE_KEY)
        )
        self.assertTrue(launcher.wait_ready(timeout=1, interval=0.05))

    def test_wait_ready_fails_without_the_matching_key(self):
        launcher = LocalLlmLauncher(
            LocalLlmConfig(base_url=self.base_url, api_key="wrong-key")
        )
        self.assertFalse(launcher.wait_ready(timeout=1, interval=0.05))


class LaunchEnvironmentTest(unittest.TestCase):
    def test_start_exports_the_key_to_the_child_launcher(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return FakeProcess()

        config = LocalLlmConfig(
            base_url="http://127.0.0.1:8080/v1",
            launch_command="start_qwen",
            api_key=FIXTURE_KEY,
        )
        launcher = LocalLlmLauncher(config, popen=fake_popen)
        launcher.start()
        self.assertEqual(captured["env"][LAUNCHER_API_KEY_ENV], FIXTURE_KEY)
        self.assertEqual(captured["env"][LAUNCHER_QA_API_KEY_ENV], FIXTURE_KEY)
        # The key must never end up in the command line itself.
        self.assertNotIn(FIXTURE_KEY, captured["command"])


if __name__ == "__main__":
    unittest.main()
