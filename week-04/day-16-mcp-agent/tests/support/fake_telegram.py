"""A deterministic loopback fake of the Telegram Bot API.

Server B is the only process that talks to Telegram, and its single network
boundary (``notifier_server/telegram.py``) builds the request URL from
``NOTIFIER_TELEGRAM_API_BASE_URL``. Integration tests and the restart harness
point that variable at this loopback server, so the real Telegram service is
never called: the fake binds an ephemeral ``127.0.0.1`` port, accepts
``/bot<token>/sendMessage`` requests and records the payload (including the bot
token path and the message text) for later inspection.

Only the loopback interface is used; there is no outbound traffic. The fake
answers the Telegram response shape (``{"ok": true, "result": {"message_id":
...}}``) that server B expects, so a delivery is reported as ``sent`` without a
real message.

``GET /__stats__`` exposes the recorded requests over the same loopback port so
a separate test process (the unittests started by the harness) can inspect the
deliveries the notifier process actually made.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The method name server B appends to the base URL, and the diagnostics path.
SEND_MESSAGE_METHOD = "sendMessage"
STATS_PATH = "/__stats__"


class FakeTelegramHandler(BaseHTTPRequestHandler):
    """The subset of the Telegram Bot API used by the notifier."""

    protocol_version = "HTTP/1.1"
    server_version = "fake-telegram/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Stay quiet; the harness captures nothing but errors."""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _requests(self) -> list:
        return getattr(self.server, "requests", None) or []

    def do_GET(self):  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != STATS_PATH:
            self._send_json(404, {"ok": False, "description": "not found"})
            return
        requests = self._requests()
        self._send_json(
            200,
            {
                "count": len(requests),
                "messages": [record["body"].get("text") for record in requests],
                "requests": [
                    {"path": record["path"], "body": record["body"]}
                    for record in requests
                ],
            },
        )

    def do_POST(self):  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.endswith("/" + SEND_MESSAGE_METHOD):
            self._send_json(404, {"ok": False, "description": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "description": "invalid json"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "description": "invalid json"})
            return
        requests = getattr(self.server, "requests", None)
        if requests is None:
            requests = []
            self.server.requests = requests
        requests.append({"path": self.path, "body": body})
        self._send_json(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": len(requests),
                    "chat": {"id": body.get("chat_id")},
                    "text": body.get("text"),
                },
            },
        )


class _FakeTelegramHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list = []


class FakeTelegramServer:
    """A fake Telegram Bot API bound to an ephemeral loopback port."""

    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self.host = host
        self._requested_port = int(port)
        self._httpd: _FakeTelegramHttpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._requested_port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def requests(self) -> list:
        if self._httpd is None:
            return []
        return [
            {"path": record["path"], "body": dict(record["body"])}
            for record in self._httpd.requests
        ]

    @property
    def messages(self) -> list:
        """The text of every recorded ``sendMessage`` request, in order."""
        return [record["body"].get("text") for record in self.requests]

    @property
    def message_count(self) -> int:
        return len(self._httpd.requests) if self._httpd is not None else 0

    def start(self) -> "FakeTelegramServer":
        self._httpd = _FakeTelegramHttpServer(
            (self.host, self._requested_port), FakeTelegramHandler
        )
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

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


def fetch_stats(base_url: str, timeout_s: float = 5.0) -> dict:
    """Read the recorded deliveries over loopback (for a separate test process)."""
    url = f"{str(base_url).rstrip('/')}{STATS_PATH}"
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def free_port() -> int:
    """Reserve a currently free loopback port (best effort, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv=None) -> int:
    """Run the fake server in the foreground on ``--port``."""
    parser = argparse.ArgumentParser(prog="fake_telegram")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = FakeTelegramServer(args.port, args.host).start()
    print(f"fake telegram listening on {server.base_url}", flush=True)
    try:
        while server._thread is not None and server._thread.is_alive():
            server._thread.join(timeout=1)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
