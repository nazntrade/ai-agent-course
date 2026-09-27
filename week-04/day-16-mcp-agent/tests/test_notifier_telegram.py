"""Unit tests of the Telegram boundary (loopback fake, no real network).

The fake Telegram server is bound to an ephemeral loopback port; the client's
base URL points at it. The tests prove the honest ``not_configured`` path, the
successful send, and that every failure is sanitized: neither the token nor the
request URL, headers or response body can leak into a result.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from notifier_server.config import TelegramConfig
from notifier_server.telegram import (
    INVALID_RESPONSE_MESSAGE,
    NOT_CONFIGURED_MESSAGE,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SENT,
    UNREACHABLE_MESSAGE,
    TelegramClient,
)

TOKEN = "123456:unit-test-secret-token"
RECIPIENT = "987654321"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FakeTelegramHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-telegram/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Stay quiet."""

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        self.server.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        status, payload = self.server.response
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _FakeTelegramHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list = []
        self.response = (200, {"ok": True, "result": {"message_id": 42}})


class FakeTelegramServer:
    """A fake Telegram API bound to an ephemeral loopback port."""

    def __init__(self, response=(200, {"ok": True, "result": {"message_id": 42}})):
        self._httpd: _FakeTelegramHttpServer | None = None
        self._thread: threading.Thread | None = None
        self.response = response

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{int(self._httpd.server_address[1])}"

    @property
    def request_count(self) -> int:
        return len(self._httpd.requests) if self._httpd is not None else 0

    @property
    def requests(self) -> list:
        return list(self._httpd.requests) if self._httpd is not None else []

    def set_response(self, status, payload) -> None:
        self.response = (status, payload)
        if self._httpd is not None:
            self._httpd.response = self.response

    def start(self) -> "FakeTelegramServer":
        self._httpd = _FakeTelegramHttpServer(
            ("127.0.0.1", 0), _FakeTelegramHandler
        )
        self._httpd.response = self.response
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fake-telegram", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _client(server: FakeTelegramServer, *, token=TOKEN, recipient=RECIPIENT) -> TelegramClient:
    return TelegramClient(
        TelegramConfig(token=token, recipient=recipient, base_url=server.base_url)
    )


class NotConfiguredTest(unittest.TestCase):
    """An empty token is honest and never touches the network."""

    def test_empty_token_returns_not_configured_without_a_call(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        result = _client(server, token="").send_message("hello")
        self.assertEqual(result.status, STATUS_NOT_CONFIGURED)
        self.assertEqual(result.error, NOT_CONFIGURED_MESSAGE)
        self.assertEqual(server.request_count, 0)

    def test_empty_recipient_is_not_configured_without_a_call(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        result = _client(server, recipient="").send_message("hello")
        self.assertEqual(result.status, STATUS_NOT_CONFIGURED)
        self.assertEqual(server.request_count, 0)


class SendSuccessTest(unittest.TestCase):
    """A successful send posts to the documented endpoint."""

    def test_message_is_posted_and_message_id_returned(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        result = _client(server).send_message("Xbox: 1 new item(s)")
        self.assertEqual(result.status, STATUS_SENT)
        self.assertEqual(result.message_id, "42")
        self.assertIsNone(result.error)
        self.assertEqual(server.request_count, 1)

        request = server.requests[0]
        self.assertEqual(request["path"], f"/bot{TOKEN}/sendMessage")
        self.assertEqual(request["body"]["chat_id"], RECIPIENT)
        self.assertEqual(request["body"]["text"], "Xbox: 1 new item(s)")

    def test_empty_text_is_a_failed_result_without_a_call(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        result = _client(server).send_message("   ")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(server.request_count, 0)


class SanitizationTest(unittest.TestCase):
    """Failures never carry the token, the URL, headers or the body."""

    def _assert_clean(self, result):
        rendered = f"{result.status} {result.error} {result.message_id}"
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn("sendMessage", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_rejected_status_is_sanitized(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        server.set_response(403, {"ok": False, "description": TOKEN})
        result = _client(server).send_message("hello")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("HTTP 403", result.error)
        self._assert_clean(result)

    def test_invalid_json_is_sanitized(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        server.set_response(200, b"<<not json>>")
        result = _client(server).send_message("hello")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.error, INVALID_RESPONSE_MESSAGE)
        self._assert_clean(result)

    def test_not_ok_payload_is_sanitized(self):
        server = FakeTelegramServer().start()
        self.addCleanup(server.stop)
        server.set_response(200, {"ok": False, "description": "bad token"})
        result = _client(server).send_message("hello")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.error, INVALID_RESPONSE_MESSAGE)
        self._assert_clean(result)

    def test_unreachable_is_sanitized(self):
        client = TelegramClient(
            TelegramConfig(
                token=TOKEN,
                recipient=RECIPIENT,
                base_url=f"http://127.0.0.1:{_free_port()}",
            )
        )
        result = client.send_message("hello")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.error, UNREACHABLE_MESSAGE)
        self._assert_clean(result)


class ReprTest(unittest.TestCase):
    """The token never appears in a repr of the client or its config."""

    def test_repr_hides_the_token(self):
        config = TelegramConfig(token=TOKEN, recipient=RECIPIENT)
        client = TelegramClient(config)
        self.assertNotIn(TOKEN, repr(config))
        self.assertNotIn(TOKEN, repr(client))
        self.assertNotIn(RECIPIENT, repr(config))


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
