"""A deterministic Tavily-compatible search server for tests.

It is a *test double* for the external search API: the MCP server, its transport
and the MCP protocol stay real, while the paid external service is replaced by a
loopback HTTP server. The server demands the expected ``Authorization: Bearer``
header on ``POST /search`` and answers the Tavily response shape.

Markers inside the query drive the edge cases:

* ``__test_empty__`` — HTTP 200 with no results;
* ``__test_many__`` — HTTP 200 with six results (more than the default page);
* ``__test_duplicates__`` — HTTP 200 with four results that share two URLs
  (fragment, host case and trailing slash);
* ``__test_noisy__`` — HTTP 200 with two navigation/boilerplate results and two
  clean ones, so the digest filtering can be proven end to end;
* ``__test_401__`` — HTTP 401;
* ``__test_429__`` — HTTP 429;
* ``__test_timeout__`` — accepts the request and never answers in time.

Run it with ``python -m tests.support.fake_search`` (the harness starts it
in-process instead).
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_API_KEY = "fake-search-key"

EMPTY_MARKER = "__test_empty__"
MANY_MARKER = "__test_many__"
DUPLICATES_MARKER = "__test_duplicates__"
NOISY_MARKER = "__test_noisy__"
UNAUTHORIZED_MARKER = "__test_401__"
RATE_LIMIT_MARKER = "__test_429__"
TIMEOUT_MARKER = "__test_timeout__"

SEARCH_PATH = "/search"
# Loopback-only diagnostics endpoint the integration tests poll to prove that no
# new search call was started after a chat with a task was deleted (D18-09).
STATS_PATH = "/__stats__"
STALL_SECONDS = 8.0

RESULTS = (
    {
        "title": "Python Official Documentation",
        "url": "https://docs.example.test/1",
        "content": "Official documentation home of the Python programming language.",
        "score": 0.98,
    },
    {
        "title": "The Python Tutorial",
        "url": "https://docs.example.test/2",
        "content": "Official Python tutorial for new and experienced programmers.",
        "score": 0.91,
    },
    {
        "title": "Python Standard Library",
        "url": "https://docs.example.test/3",
        "content": "Official reference for the Python standard library.",
        "score": 0.87,
    },
)

RESULT_URLS = tuple(item["url"] for item in RESULTS)

# Six results, more than the default page size of five: the ``__test_many__``
# marker lets a test prove that a stored per-task limit is applied.
MANY_RESULTS = tuple(
    {
        "title": f"Python Reference {index}",
        "url": f"https://docs.example.test/reference/{index}",
        "content": f"Reference page number {index}.",
        "score": 0.9 - index / 100,
    }
    for index in range(1, 7)
)

# Anonymized search output with the shape that reached a real digest: the first
# result carries a navigation menu in its snippet and the third is a boilerplate
# page label. Both must be filtered; the two clean results survive. The
# ``__test_noisy__`` marker lets a test prove the filtering and the cap.
NOISY_RESULTS = (
    {
        "title": "News : Example Blog | Example Org",
        "url": "https://docs.example.test/noisy/1",
        "content": (
            "#### IDEs #### Plugins & Services #### Team Tools #### "
            ".NET & Visual Studio #### Company Example | Products | Support"
        ),
        "score": 0.95,
    },
    {
        "title": "Kotlin 2.0 released",
        "url": "https://docs.example.test/noisy/2",
        "content": "The Kotlin team announced a new stable release.",
        "score": 0.9,
    },
    {
        "title": "Menu",
        "url": "https://docs.example.test/noisy/3",
        "content": "Skip to content Menu Sign in Cookies Privacy",
        "score": 0.8,
    },
    {
        "title": "Kotlin Multiplatform update",
        "url": "https://docs.example.test/noisy/4",
        "content": "Cross-platform development with Kotlin.",
        "score": 0.7,
    },
)

# Four results that collapse to two canonical URLs: one fragment duplicate, one
# host-case + trailing-slash duplicate and one unique page. The
# ``__test_duplicates__`` marker lets a test prove the digest deduplicates them.
DUPLICATE_RESULTS = (
    {
        "title": "Guide page one",
        "url": "https://docs.example.test/dup/1",
        "content": "First copy of the guide.",
        "score": 0.9,
    },
    {
        "title": "Guide page one (anchor)",
        "url": "https://docs.example.test/dup/1#section",
        "content": "Same page with a fragment.",
        "score": 0.8,
    },
    {
        "title": "Guide page one (host case)",
        "url": "https://Docs.Example.Test/dup/1/",
        "content": "Same page with a different host case and a trailing slash.",
        "score": 0.7,
    },
    {
        "title": "Guide page two",
        "url": "https://docs.example.test/dup/2",
        "content": "A genuinely different page.",
        "score": 0.6,
    },
)


class FakeSearchHandler(BaseHTTPRequestHandler):
    """Tavily-compatible subset used by the web-search tests."""

    protocol_version = "HTTP/1.1"
    server_version = "fake-search/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Stay quiet; the harness captures nothing but errors."""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, params: dict) -> None:
        requests = getattr(self.server, "requests", None)
        if requests is not None:
            requests.append(params)

    def _response_payload(self, query: str, results: list) -> dict:
        return {
            "query": query,
            "results": results,
        }

    def do_GET(self):  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != STATS_PATH:
            self._send_json(404, {"error": "not found"})
            return
        recorded = getattr(self.server, "requests", None) or []
        self._send_json(200, {"requests": len(recorded)})

    def do_POST(self):  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != SEARCH_PATH:
            self._send_json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {FAKE_API_KEY}":
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "invalid json"})
            return
        self._record(body)

        query = str(body.get("query") or "")
        if TIMEOUT_MARKER in query:
            time.sleep(STALL_SECONDS)
            return
        if UNAUTHORIZED_MARKER in query:
            self._send_json(401, {"error": "unauthorized"})
            return
        if RATE_LIMIT_MARKER in query:
            self._send_json(429, {"error": "rate limited"})
            return
        if EMPTY_MARKER in query:
            self._send_json(200, self._response_payload(query, []))
            return
        if MANY_MARKER in query:
            self._send_json(200, self._response_payload(query, list(MANY_RESULTS)))
            return
        if DUPLICATES_MARKER in query:
            self._send_json(200, self._response_payload(query, list(DUPLICATE_RESULTS)))
            return
        if NOISY_MARKER in query:
            self._send_json(200, self._response_payload(query, list(NOISY_RESULTS)))
            return
        self._send_json(200, self._response_payload(query, list(RESULTS)))


class _FakeSearchHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list = []


class FakeSearchServer:
    """A fake search API bound to an ephemeral loopback port."""

    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self.host = host
        self._requested_port = int(port)
        self._httpd: _FakeSearchHttpServer | None = None
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
        return list(self._httpd.requests) if self._httpd is not None else []

    @property
    def request_count(self) -> int:
        """How many search requests the fake API has served so far."""
        return len(self._httpd.requests) if self._httpd is not None else 0

    def start(self) -> "FakeSearchServer":
        self._httpd = _FakeSearchHttpServer(
            (self.host, self._requested_port), FakeSearchHandler
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fake-search", daemon=True
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


def free_port() -> int:
    """Reserve a currently free loopback port (best effort, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv=None) -> int:
    """Run the fake server in the foreground on ``--port``."""
    parser = argparse.ArgumentParser(prog="fake_search")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = FakeSearchServer(args.port, args.host).start()
    print(f"fake search listening on {server.base_url}", flush=True)
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
