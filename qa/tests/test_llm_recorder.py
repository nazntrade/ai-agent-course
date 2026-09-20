"""Unit tests of the recording proxy (``lib.llm_recorder``).

A local stub upstream stands in for the model; everything stays on loopback and
no prompt, response, header or key ever reaches the call log.
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lib.llm_recorder import RecordingProxy, UpstreamError

SECRET_PROMPT = "CONFIDENTIAL-PROMPT-MARKER"
SECRET_ANSWER = "confidential-answer-marker"
SECRET_KEY = "fixture-authorization-key"

# Authorization headers seen by the stub upstream, reset per test.
CAPTURED_AUTHORIZATION = []
# Request paths the stub upstream actually received, reset per test.
CAPTURED_PATHS = []

USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
TIMINGS = {
    "prompt_n": 11,
    "prompt_ms": 12.5,
    "predicted_n": 7,
    "predicted_ms": 21.0,
    "time_to_first_token_ms": 4.5,
}


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        CAPTURED_AUTHORIZATION.append(self.headers.get("Authorization"))
        CAPTURED_PATHS.append(self.path)
        body = json.dumps({"data": [{"id": "stub-model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        CAPTURED_AUTHORIZATION.append(self.headers.get("Authorization"))
        CAPTURED_PATHS.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": SECRET_ANSWER}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": USAGE, "timings": TIMINGS, "model": "stub-model"},
            ]
            for chunk in chunks:
                self.wfile.write(
                    ("data: " + json.dumps(chunk) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps(
            {
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": SECRET_ANSWER},
                        "finish_reason": "stop",
                    }
                ],
                "usage": USAGE,
                "timings": TIMINGS,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GatedHandler(BaseHTTPRequestHandler):
    """Stub upstream that only answers once ``gate`` is released."""

    protocol_version = "HTTP/1.1"
    gate = threading.Event()

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        _GatedHandler.gate.wait(timeout=10)
        body = json.dumps(
            {
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": SECRET_ANSWER},
                        "finish_reason": "stop",
                    }
                ],
                "usage": USAGE,
                "timings": TIMINGS,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RecorderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "llm_calls.jsonl"
        CAPTURED_AUTHORIZATION.clear()
        CAPTURED_PATHS.clear()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.upstream.daemon_threads = True
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.addCleanup(self._stop_upstream)
        self.proxy = RecordingProxy(
            f"http://127.0.0.1:{self.upstream.server_address[1]}/v1", self.log_path
        ).start()
        self.addCleanup(self.proxy.stop)

    def _stop_upstream(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)

    def _post(self, payload, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps(payload).encode("utf-8"),
                headers=headers or {"Content-Type": "application/json"},
            )
            return connection.getresponse()
        finally:
            pass

    def _records(self):
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _wait_records(self, count=1, timeout=5.0):
        """Wait for the proxy log to reach ``count`` lines (it writes async)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            records = self._records()
            if len(records) >= count:
                return records
            time.sleep(0.02)
        return self._records()


class RecorderNonStreamTest(RecorderTestCase):
    def test_non_stream_call_is_forwarded_and_recorded(self):
        response = self._post(
            {"model": "m", "messages": [{"role": "user", "content": SECRET_PROMPT}]}
        )
        body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], SECRET_ANSWER)

        records = self._wait_records(1)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["usage"], USAGE)
        self.assertEqual(record["timings"], TIMINGS)
        self.assertEqual(record["model"], "stub-model")
        self.assertFalse(record["stream"])
        self.assertTrue(record["is_llm_call"])
        self.assertIsNone(record["first_content_ms"])
        self.assertIsNone(record["error"])
        self.assertEqual(self.proxy.calls, 1)

    def test_log_never_contains_prompt_or_answer(self):
        self._post(
            {"messages": [{"role": "user", "content": SECRET_PROMPT}]}
        ).read()
        self._wait_records(1)
        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_PROMPT, text)
        self.assertNotIn(SECRET_ANSWER, text)

    def test_authorization_is_forwarded_and_never_logged(self):
        self._post(
            {"model": "m", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {SECRET_KEY}",
            },
        ).read()
        self._wait_records(1)
        self.assertIn(f"Bearer {SECRET_KEY}", CAPTURED_AUTHORIZATION)
        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_KEY, text)

    def test_models_probe_is_not_counted(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=5)
        connection.request("GET", "/v1/models")
        connection.getresponse().read()
        self.assertEqual(self.proxy.calls, 0)
        self.assertEqual(self._records(), [])

    def test_non_loopback_upstream_is_refused(self):
        with self.assertRaises(UpstreamError):
            RecordingProxy("https://api.deepseek.com/v1", self.log_path)

    def test_off_box_host_header_is_refused_and_counted(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=b"{}",
            headers={"Content-Type": "application/json", "Host": "evil.example"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        self.assertEqual(self.proxy.blocked_external_calls, 1)
        self.assertEqual(self.proxy.calls, 0)

    def test_upstream_connection_error_is_recorded_as_a_failed_call(self):
        dead_port = free_port()
        log_path = Path(self._tmp.name) / "dead.jsonl"
        proxy = RecordingProxy(f"http://127.0.0.1:{dead_port}/v1", log_path).start()
        self.addCleanup(proxy.stop)
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 502)
        response.read()
        deadline = time.time() + 5
        records = []
        while time.time() < deadline:
            if log_path.exists():
                records = [
                    json.loads(line)
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            if records:
                break
            time.sleep(0.02)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], 502)
        self.assertTrue(records[0]["error"])


class RecorderUpstreamPathTest(RecorderTestCase):
    """The proxy must not duplicate the upstream ``/v1`` base path.

    The application is pointed at ``proxy_base + "/v1"`` while the upstream may
    already carry ``/v1``; the stub records ``self.path`` so a regression back to
    ``/v1/v1/chat/completions`` fails here instead of on the real llama-server.
    """

    def _proxy_for(self, upstream_base_url, log_name):
        proxy = RecordingProxy(
            upstream_base_url, Path(self._tmp.name) / log_name
        ).start()
        self.addCleanup(proxy.stop)
        return proxy

    def _post_path(self, proxy, path, payload=None):
        body = json.dumps(payload or {"model": "m", "messages": []}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        return response

    def test_upstream_v1_prefix_is_not_duplicated(self):
        response = self._post_path(self.proxy, "/v1/chat/completions")
        self.assertEqual(response.status, 200)
        self.assertEqual(CAPTURED_PATHS[-1], "/v1/chat/completions")
        self.assertNotIn("/v1/v1/chat/completions", CAPTURED_PATHS)

    def test_upstream_with_trailing_slash_is_not_duplicated(self):
        port = self.upstream.server_address[1]
        proxy = self._proxy_for(f"http://127.0.0.1:{port}/v1/", "slash.jsonl")
        response = self._post_path(proxy, "/v1/chat/completions")
        self.assertEqual(response.status, 200)
        self.assertEqual(CAPTURED_PATHS[-1], "/v1/chat/completions")

    def test_upstream_without_prefix_keeps_the_client_path(self):
        port = self.upstream.server_address[1]
        proxy = self._proxy_for(f"http://127.0.0.1:{port}", "noprefix.jsonl")
        response = self._post_path(proxy, "/v1/chat/completions")
        self.assertEqual(response.status, 200)
        self.assertEqual(CAPTURED_PATHS[-1], "/v1/chat/completions")

    def test_upstream_root_slash_keeps_the_client_path(self):
        port = self.upstream.server_address[1]
        proxy = self._proxy_for(f"http://127.0.0.1:{port}/", "root.jsonl")
        response = self._post_path(proxy, "/v1/chat/completions")
        self.assertEqual(response.status, 200)
        self.assertEqual(CAPTURED_PATHS[-1], "/v1/chat/completions")

    def test_query_string_is_preserved(self):
        response = self._post_path(
            self.proxy, "/v1/chat/completions?foo=bar&baz=1"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(CAPTURED_PATHS[-1], "/v1/chat/completions?foo=bar&baz=1")

    def test_missing_prefix_is_added_once(self):
        response = self._post_path(self.proxy, "/chat/completions")
        self.assertEqual(response.status, 200)
        self.assertEqual(CAPTURED_PATHS[-1], "/v1/chat/completions")


class RecorderInFlightTest(unittest.TestCase):
    """An interrupted request must still leave an incomplete evidence line."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "llm_calls.jsonl"
        _GatedHandler.gate = threading.Event()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _GatedHandler)
        self.upstream.daemon_threads = True
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.addCleanup(self._stop_upstream)
        self.proxy = RecordingProxy(
            f"http://127.0.0.1:{self.upstream.server_address[1]}/v1", self.log_path
        ).start()
        self.addCleanup(self.proxy.stop)

    def _stop_upstream(self):
        _GatedHandler.gate.set()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)

    def _records(self):
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _start_blocked_post(self):
        body = json.dumps(
            {
                "model": "m",
                "messages": [{"role": "user", "content": SECRET_PROMPT}],
            }
        ).encode("utf-8")
        thread = threading.Thread(
            target=self._post_body, args=(body,), daemon=True
        )
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and self.proxy.in_flight_calls < 1:
            time.sleep(0.02)
        return thread

    def _post_body(self, body):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.proxy.port, timeout=10
        )
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            connection.getresponse().read()
        except Exception:
            pass
        finally:
            connection.close()

    def test_flush_writes_an_incomplete_call_without_secrets(self):
        thread = self._start_blocked_post()
        self.assertEqual(self.proxy.in_flight_calls, 1)
        self.assertEqual(self.proxy.flush_in_flight(), 1)

        records = self._records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["incomplete"])
        self.assertEqual(record["error"], "aborted")
        self.assertIsNone(record["status"])
        self.assertIsNone(record["usage"])
        self.assertIsNone(record["timings"])
        self.assertIsNone(record["model"])
        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_PROMPT, text)
        self.assertNotIn(SECRET_ANSWER, text)

        # The late response must not append a second, completed line.
        _GatedHandler.gate.set()
        thread.join(timeout=5)
        self.assertEqual(len(self._records()), 1)
        self.assertEqual(self.proxy.in_flight_calls, 0)

    def test_flush_keeps_completed_records_and_is_idempotent(self):
        _GatedHandler.gate.set()
        self._post_body(
            json.dumps({"model": "m", "messages": []}).encode("utf-8")
        )
        deadline = time.time() + 5
        while time.time() < deadline and len(self._records()) < 1:
            time.sleep(0.02)
        before = self._records()
        self.assertEqual(len(before), 1)
        self.assertFalse(before[0].get("incomplete"))

        self.assertEqual(self.proxy.flush_in_flight(), 0)
        self.assertEqual(self.proxy.flush_in_flight(), 0)
        self.assertEqual(len(self._records()), 1)

    def test_stop_flushes_an_in_flight_call(self):
        thread = self._start_blocked_post()
        self.assertEqual(self.proxy.in_flight_calls, 1)
        self.proxy.stop()
        self.assertEqual(len(self._records()), 1)
        self.assertTrue(self._records()[0]["incomplete"])

        _GatedHandler.gate.set()
        thread.join(timeout=5)
        self.assertEqual(len(self._records()), 1)


class RecorderStreamTest(RecorderTestCase):
    def test_stream_call_is_relayed_and_recorded(self):
        response = self._post(
            {
                "stream": True,
                "messages": [{"role": "user", "content": SECRET_PROMPT}],
            }
        )
        text = response.read().decode("utf-8")
        self.assertIn(SECRET_ANSWER, text)
        self.assertIn("[DONE]", text)

        records = self._wait_records(1)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["stream"])
        self.assertEqual(record["usage"], USAGE)
        self.assertEqual(record["timings"], TIMINGS)
        self.assertIsNotNone(record["first_content_ms"])
        self.assertGreaterEqual(record["first_content_ms"], 0)

    def test_stream_log_never_contains_the_answer(self):
        self._post({"stream": True, "messages": [{"role": "user", "content": SECRET_PROMPT}]}).read()
        self._wait_records(1)
        text = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_ANSWER, text)


if __name__ == "__main__":
    unittest.main()
