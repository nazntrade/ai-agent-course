"""Loopback mock of an OpenAI-compatible chat provider.

The server binds ``127.0.0.1:0`` and answers ``GET /v1/models`` plus
``POST /v1/chat/completions`` (non-stream JSON and SSE stream). It reproduces
the reported Day 15 defects deterministically: the first planning reply of a
fresh task is an execution-incompatible plan and the first execution reply
contradicts the attached storage facts; the corrective retry of both returns the
good fixture. Every reply carries synthetic ``usage`` and ``timings``, so the
whole metrics parser is exercised without a real model.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_MODEL = "mock-local"

DEFAULT_PLAN_RETRY_MARKER = "Предыдущий план отклонён"
DEFAULT_STEP_RETRY_MARKER = "Предыдущий результат шага отклонён"


@dataclass
class MockFixtures:
    """Deterministic replies of the mock, one set per stage."""

    bad_plan: dict = field(default_factory=dict)
    good_plan: dict = field(default_factory=dict)
    bad_step_text: str = ""
    good_step_text: str = ""
    validation_payload: dict = field(
        default_factory=lambda: {"passed": True, "defects": [], "notes": "mock"}
    )
    planning_markers: tuple = ()
    execution_markers: tuple = ()
    validation_markers: tuple = ()
    plan_retry_marker: str = DEFAULT_PLAN_RETRY_MARKER
    step_retry_marker: str = DEFAULT_STEP_RETRY_MARKER
    model: str = MOCK_MODEL


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _token_count(text) -> int:
    body = str(text or "")
    return max(1, len(body) // 4)


def _synthetic_timings(text, prompt_text) -> tuple:
    """Return ``(usage, timings)`` derived deterministically from the sizes."""
    prompt_tokens = _token_count(prompt_text)
    completion_tokens = _token_count(text)
    prompt_ms = round(prompt_tokens * 1.5, 3)
    predicted_ms = round(completion_tokens * 2.0, 3)
    ttft_ms = round(prompt_ms * 0.5 + 1.0, 3)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    timings = {
        "prompt_n": prompt_tokens,
        "prompt_ms": prompt_ms,
        "prompt_per_second": round(prompt_tokens / (prompt_ms / 1000), 3),
        "predicted_n": completion_tokens,
        "predicted_ms": predicted_ms,
        "predicted_per_second": round(completion_tokens / (predicted_ms / 1000), 3),
        "time_to_first_token_ms": ttft_ms,
    }
    return usage, timings


class MockProvider:
    """Threaded loopback HTTP server implementing the provider contract."""

    def __init__(self, fixtures=None, *, host="127.0.0.1", port=0):
        self._fixtures = fixtures if fixtures is not None else MockFixtures()
        self._host = host
        self._port = port
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def fixtures(self) -> MockFixtures:
        """The deterministic fixtures this server answers with."""
        return self._fixtures

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """The bound port (known only after ``start``)."""
        return self._server.server_address[1] if self._server is not None else self._port

    @property
    def base_url(self) -> str:
        """The loopback ``/v1`` root of the running server."""
        return f"http://{self._host}:{self.port}/v1"

    @property
    def calls(self) -> int:
        """Number of ``/v1/chat/completions`` requests served."""
        with self._lock:
            return self._calls

    def start(self) -> "MockProvider":
        """Bind and serve in a daemon thread."""
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="mock-provider", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down and join its thread."""
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

    # --- Reply selection ---------------------------------------------------

    def _record_call(self) -> None:
        with self._lock:
            self._calls += 1

    def classify(self, messages) -> str:
        """Return ``planning``, ``execution`` or ``validation`` for the call."""
        system_text = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        )
        user_text = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        fixtures = self._fixtures
        if any(marker in system_text for marker in fixtures.planning_markers):
            return "planning"
        if any(marker in system_text for marker in fixtures.validation_markers):
            return "validation"
        if any(marker in system_text for marker in fixtures.execution_markers):
            return "execution"
        # The synthetic action message is the reliable fallback.
        if "run_planning" in user_text:
            return "planning"
        if "run_validation" in user_text:
            return "validation"
        return "execution"

    def reply_text(self, messages) -> str:
        """Return the deterministic assistant content for one request."""
        stage = self.classify(messages)
        body = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if isinstance(message, dict)
        )
        fixtures = self._fixtures
        if stage == "planning":
            retry = fixtures.plan_retry_marker in body
            plan = fixtures.good_plan if retry else fixtures.bad_plan
            return json.dumps(plan, ensure_ascii=False)
        if stage == "validation":
            return json.dumps(fixtures.validation_payload, ensure_ascii=False)
        retry = fixtures.step_retry_marker in body
        return fixtures.good_step_text if retry else fixtures.bad_step_text


def _sse_event(payload) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _make_handler(provider: MockProvider):
    """Build the request handler class bound to one provider instance."""

    class MockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # pragma: no cover - silence the server log
            return

        def _write_json(self, payload, status=200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.rstrip("/") == "/v1/models":
                self._write_json(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": provider.fixtures.model,
                                "object": "model",
                                "owned_by": "mock",
                            }
                        ],
                    }
                )
                return
            self._write_json({"error": {"message": "not found"}}, status=404)

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = _int(self.headers.get("Content-Length"), 0)
            raw = self.rfile.read(length) if length else b""
            try:
                request = json.loads(raw.decode("utf-8", "replace") or "{}")
            except ValueError:
                self._write_json({"error": {"message": "invalid json"}}, status=400)
                return
            # Exact match: a doubled prefix such as ``/v1/v1/chat/completions``
            # is a proxy bug and must not be accepted "helpfully".
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._write_json({"error": {"message": "not found"}}, status=404)
                return

            provider._record_call()
            messages = request.get("messages") or []
            text = provider.reply_text(messages)
            prompt_text = json.dumps(messages, ensure_ascii=False)
            usage, timings = _synthetic_timings(text, prompt_text)
            created = int(time.time())
            completion_id = f"mock-{provider.calls}"

            if request.get("stream"):
                self._stream_reply(provider, text, usage, timings, created, completion_id)
                return

            self._write_json(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": provider.fixtures.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                    "timings": timings,
                }
            )

        def _stream_reply(self, provider, text, usage, timings, created, completion_id):
            base = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": provider.fixtures.model,
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def chunk(delta, finish_reason=None, choices=True):
                return dict(
                    base,
                    choices=(
                        [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
                        if choices
                        else []
                    ),
                )

            events = [chunk({"role": "assistant", "content": ""})]
            for piece in _word_chunks(text):
                events.append(chunk({"content": piece}))
            events.append(chunk({}, finish_reason="stop"))
            events.append(
                dict(base, choices=[], usage=usage, timings=timings)
            )
            for event in events:
                self.wfile.write(_sse_event(event).encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return MockHandler


def _word_chunks(text, words_per_chunk=4) -> list:
    """Split ``text`` into small deterministic pieces for the SSE stream."""
    body = str(text or "")
    if not body:
        return [""]
    words = body.split(" ")
    pieces = []
    for start in range(0, len(words), words_per_chunk):
        piece = " ".join(words[start : start + words_per_chunk])
        if start + words_per_chunk < len(words):
            piece += " "
        pieces.append(piece)
    return pieces
