"""Recording proxy between the application and the local model.

The application always talks to this loopback proxy; the proxy forwards to the
real upstream (the local llama-server or the mock provider) and appends one JSON
line per ``/v1/chat/completions`` call to ``llm_calls.jsonl``. Only metadata and
the raw ``usage``/``timings`` are recorded: never prompts, never responses,
never headers and never keys. ``/v1/models`` is a readiness probe and is not
counted as an LLM call.

A request that is still in flight when the proxy stops (an interrupted live run)
is not silently dropped: ``flush_in_flight`` appends an explicitly incomplete
line (``incomplete: true``, ``error: "aborted"``, ``status: null``, no tokens),
so even an aborted run leaves evidence that a call was attempted.

The upstream is validated at construction time: a non-loopback upstream raises
``UpstreamError``, so the proxy can never forward model traffic off the machine.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from lib.config import is_loopback_url

MAX_HEADER_BYTES = 65536


class UpstreamError(RuntimeError):
    """Raised when a non-loopback upstream would break the isolation rule."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_upstream(base_url) -> tuple:
    """Return ``(host, port, base_path)`` of a loopback upstream URL."""
    split = urlsplit(str(base_url))
    if split.scheme != "http" or not is_loopback_url(base_url):
        raise UpstreamError(
            f"the recording proxy only forwards to a loopback http upstream, "
            f"got {base_url!r}"
        )
    host = split.hostname or "127.0.0.1"
    port = split.port or 80
    base_path = split.path.rstrip("/")
    return host, port, base_path


def _join_upstream_path(base_path, path) -> str:
    """Join the upstream base path and the client path without duplication.

    The application is pointed at ``proxy_base + "/v1"`` while the upstream may
    itself already carry a ``/v1`` prefix, so a naive concatenation would send
    ``/v1/v1/chat/completions``. A path that already starts with the base path
    is forwarded unchanged; an empty or root base path leaves the path as-is.
    The query string, if any, is preserved.
    """
    raw_path, _, query = str(path or "").partition("?")
    base = str(base_path or "").rstrip("/")
    if not base:
        target = raw_path
    elif raw_path == base or raw_path.startswith(base + "/"):
        target = raw_path
    elif raw_path in ("", "/"):
        target = base + "/"
    elif raw_path.startswith("/"):
        target = base + raw_path
    else:
        target = base + "/" + raw_path
    return f"{target}?{query}" if query else target


class RecordingProxy:
    """Threaded loopback proxy that logs call metadata to JSONL."""

    def __init__(
        self,
        upstream_base_url,
        log_path,
        *,
        host="127.0.0.1",
        port=0,
        timeout=600.0,
    ):
        self._upstream_host, self._upstream_port, self._base_path = _parse_upstream(
            upstream_base_url
        )
        self._upstream_base_url = str(upstream_base_url)
        self._log_path = Path(log_path)
        self._host = host
        self._port = port
        self._timeout = timeout
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self._seq = 0
        self._calls = 0
        self._blocked_external_calls = 0
        # Requests whose upstream response has not completed yet, keyed by seq.
        self._in_flight = {}
        # Seqs already written as incomplete: a late completion must not append a
        # second, contradictory line for the same call.
        self._aborted = set()

    @property
    def upstream_base_url(self) -> str:
        return self._upstream_base_url

    @property
    def base_url(self) -> str:
        """The loopback ``/v1`` root the application must be pointed at."""
        return f"http://{self._host}:{self.port}"

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server is not None else self._port

    @property
    def calls(self) -> int:
        """Number of recorded ``/v1/chat/completions`` calls."""
        with self._lock:
            return self._calls

    @property
    def blocked_external_calls(self) -> int:
        """Requests refused because they did not target the loopback proxy."""
        with self._lock:
            return self._blocked_external_calls

    @property
    def in_flight_calls(self) -> int:
        """LLM requests forwarded but not yet finished."""
        with self._lock:
            return len(self._in_flight)

    def start(self) -> "RecordingProxy":
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="llm-recorder", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        # Record any request that is still waiting for the upstream so an
        # interrupted run leaves evidence instead of an empty log.
        self.flush_in_flight()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    # --- Recording ---------------------------------------------------------

    def record_external_refusal(self) -> None:
        with self._lock:
            self._blocked_external_calls += 1

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def begin_call(self, seq, path, stream) -> None:
        """Remember a forwarded LLM request until its response completes."""
        with self._lock:
            self._in_flight[seq] = {
                "seq": seq,
                "path": path,
                "stream": bool(stream),
            }

    def log_call(self, entry: dict) -> bool:
        """Append one metadata line to the call log; metadata only.

        Returns ``False`` when the call was already flushed as aborted, so a
        response that arrives after the flush cannot add a second line.
        """
        line = dict(entry)
        line.setdefault("timestamp", _now_iso())
        with self._lock:
            seq = line.get("seq")
            if seq is not None:
                self._in_flight.pop(seq, None)
                if seq in self._aborted:
                    return False
            self._calls += 1
            self._write_line_locked(line)
        return True

    def flush_in_flight(self) -> int:
        """Write every unfinished request as an explicit incomplete call.

        Only routing metadata is written (``seq``, ``path``, ``stream``); no
        prompt, header or key is ever touched. The call is idempotent, so the
        runner can flush before building the report and ``stop`` can flush again.
        """
        with self._lock:
            pending = list(self._in_flight.values())
            for entry in pending:
                self._aborted.add(entry["seq"])
            self._in_flight.clear()
            for entry in pending:
                line = _incomplete_entry(entry)
                line.setdefault("timestamp", _now_iso())
                self._calls += 1
                self._write_line_locked(line)
        return len(pending)

    def _write_line_locked(self, line: dict) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            handle.flush()

    def forward(self, method, path, body, headers, *, stream):
        """Forward one request and return the raw upstream connection/response."""
        connection = http.client.HTTPConnection(
            self._upstream_host, self._upstream_port, timeout=self._timeout
        )
        target = _join_upstream_path(self._base_path, path)
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        return connection, response


def _make_handler(proxy: RecordingProxy):
    class RecorderHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # pragma: no cover - silence the server log
            return

        def _refuse_offbox(self) -> bool:
            """Refuse a request that does not address the loopback proxy."""
            host = str(self.headers.get("Host") or "")
            if host and not is_loopback_url(f"http://{host}"):
                proxy.record_external_refusal()
                self._send_json({"error": {"message": "off-box forwarding refused"}}, 403)
                return True
            return False

        def _send_json(self, payload, status=200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self._refuse_offbox():
                return
            try:
                connection, response = proxy.forward(
                    "GET", self.path, None, self._forward_headers(), stream=False
                )
            except Exception as exc:
                self._send_json({"error": {"message": str(exc)}}, 502)
                return
            try:
                body = response.read()
                self._write_raw(response.status, response.getheader("Content-Type"), body)
            finally:
                connection.close()

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self._refuse_offbox():
                return
            length = _int(self.headers.get("Content-Length"), 0)
            if length > MAX_HEADER_BYTES * 64:
                self._send_json({"error": {"message": "request too large"}}, 413)
                return
            body = self.rfile.read(length) if length else b""
            stream = _wants_stream(body)
            is_llm_call = self.path.rstrip("/").endswith("/chat/completions")
            started = time.monotonic()
            seq = proxy.next_seq() if is_llm_call else None
            if is_llm_call:
                proxy.begin_call(seq, self.path, stream)

            try:
                connection, response = proxy.forward(
                    "POST", self.path, body, self._forward_headers(), stream=stream
                )
            except Exception as exc:
                if is_llm_call:
                    proxy.log_call(
                        {
                            "seq": seq,
                            "path": self.path,
                            "method": "POST",
                            "stream": stream,
                            "status": 502,
                            "duration_ms": round((time.monotonic() - started) * 1000, 3),
                            "first_content_ms": None,
                            "model": None,
                            "usage": None,
                            "timings": None,
                            "is_llm_call": True,
                            "error": type(exc).__name__,
                        }
                    )
                self._send_json({"error": {"message": str(exc)}}, 502)
                return

            try:
                if stream:
                    self._relay_stream(connection, response, is_llm_call, seq, started)
                else:
                    self._relay_json(connection, response, is_llm_call, seq, started, stream)
            finally:
                connection.close()

        def _forward_headers(self) -> dict:
            headers = {"Content-Type": "application/json", "Accept": "*/*"}
            for name in ("Authorization",):
                value = self.headers.get(name)
                if value:
                    headers[name] = value
            return headers

        def _write_raw(self, status, content_type, body) -> None:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _relay_json(self, connection, response, is_llm_call, seq, started, stream):
            body = response.read()
            # Log before the body is handed back: for a non-stream reply the
            # client may finish as soon as the framed body arrives, so logging
            # afterwards would race the caller.
            if is_llm_call:
                payload = _parse_json(body)
                proxy.log_call(
                    _entry(
                        seq,
                        self.path,
                        stream,
                        response.status,
                        started,
                        payload if isinstance(payload, dict) else {},
                        first_content_ms=None,
                    )
                )
            self._write_raw(response.status, response.getheader("Content-Type"), body)

        def _relay_stream(self, connection, response, is_llm_call, seq, started):
            self.send_response(response.status)
            content_type = response.getheader("Content-Type") or "text/event-stream"
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            first_content_ms = None
            collected = {}
            try:
                for line in response:
                    self.wfile.write(line)
                    self.wfile.flush()
                    payload = _parse_sse_line(line)
                    if payload is None:
                        continue
                    if first_content_ms is None and _has_content(payload):
                        first_content_ms = round(
                            (time.monotonic() - started) * 1000, 3
                        )
                    if isinstance(payload.get("usage"), dict):
                        collected["usage"] = payload["usage"]
                    if isinstance(payload.get("timings"), dict):
                        collected["timings"] = payload["timings"]
                    if payload.get("model"):
                        collected["model"] = payload["model"]
            finally:
                if is_llm_call:
                    proxy.log_call(
                        _entry(
                            seq,
                            self.path,
                            True,
                            response.status,
                            started,
                            collected,
                            first_content_ms=first_content_ms,
                        )
                    )

    return RecorderHandler


def _entry(seq, path, stream, status, started, payload, *, first_content_ms):
    """Build one metadata-only log line."""
    return {
        "seq": seq,
        "path": path,
        "method": "POST",
        "stream": bool(stream),
        "status": status,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "first_content_ms": first_content_ms,
        "model": payload.get("model"),
        "usage": payload.get("usage"),
        "timings": payload.get("timings"),
        "is_llm_call": True,
        "error": None,
    }


def _incomplete_entry(pending) -> dict:
    """Build the metadata-only line of a request that never finished.

    No tokens are invented: ``usage``/``timings`` stay ``None`` so the metrics
    report them as "no data", and only routing metadata is carried over.
    """
    return {
        "seq": pending.get("seq"),
        "path": pending.get("path"),
        "method": "POST",
        "stream": bool(pending.get("stream")),
        "status": None,
        "duration_ms": None,
        "first_content_ms": None,
        "model": None,
        "usage": None,
        "timings": None,
        "is_llm_call": True,
        "error": "aborted",
        "incomplete": True,
    }


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _wants_stream(body) -> bool:
    try:
        payload = json.loads((body or b"").decode("utf-8", "replace") or "{}")
    except ValueError:
        return False
    return bool(payload.get("stream")) if isinstance(payload, dict) else False


def _parse_json(body):
    try:
        return json.loads((body or b"").decode("utf-8", "replace") or "{}")
    except ValueError:
        return None


def _parse_sse_line(line):
    """Parse one SSE ``data:`` line into a payload, or return ``None``."""
    try:
        text = line.decode("utf-8", "replace").strip()
    except AttributeError:
        return None
    if not text.startswith("data:"):
        return None
    body = text[len("data:") :].strip()
    if not body or body == "[DONE]":
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _has_content(payload) -> bool:
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and str(delta.get("content") or ""):
            return True
    return False
