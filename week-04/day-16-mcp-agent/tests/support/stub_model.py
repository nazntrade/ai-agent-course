"""A local, deterministic OpenAI-compatible stub model server.

It is a *test double*: it exists so the integration and UI paths can be driven
without a real model, while the MCP server, the backend and the HTTP transport
stay real. It never talks to the network beyond loopback and holds no secret.

Behaviour of ``POST /v1/chat/completions`` with ``stream: true``:

* when the conversation already contains a ``tool`` message, answer with a short
  sentence that repeats the tool result (the "final answer" turn);
* otherwise, when the user message contains two numbers, ask for the
  ``calculate`` tool instead of answering directly;
* otherwise, answer with plain text and no tool call.

Run it with ``python -m tests.support.stub_model --port 8099``.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_ID = "stub-model"
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list:
    return NUMBER_RE.findall(str(text or ""))


def _operation(text: str) -> str:
    lowered = str(text or "").lower()
    if "multipl" in lowered or "times" in lowered:
        return "multiply"
    if "subtract" in lowered or "minus" in lowered or "difference" in lowered:
        return "subtract"
    if "divid" in lowered:
        return "divide"
    return "add"


def _tool_result_payload(messages: list) -> dict | None:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except ValueError:
                return None
            if isinstance(parsed, dict) and "result" in parsed:
                return parsed
    return None


def plan_response(messages: list) -> dict:
    """Decide the assistant turn: text, tool call or a final answer.

    ``finish_reason`` is ``tool_calls`` for a tool request and ``stop`` for a
    plain answer, matching the real provider contract.
    """
    messages = [message for message in messages if isinstance(message, dict)]
    tool_payload = _tool_result_payload(messages)
    if tool_payload is not None:
        result = tool_payload.get("result")
        return {
            "text": f"The result is {result}.",
            "tool_call": None,
            "finish_reason": "stop",
        }

    user_text = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            user_text = str(message.get("content") or "")
            break

    numbers = _numbers(user_text)
    if len(numbers) >= 2:
        arguments = {
            "operation": _operation(user_text),
            "a": float(numbers[0]),
            "b": float(numbers[1]),
        }
        return {
            "text": "",
            "tool_call": {"name": "calculate", "arguments": arguments},
            "finish_reason": "tool_calls",
        }

    return {
        "text": "I can answer plainly: this request needs no MCP tool.",
        "tool_call": None,
        "finish_reason": "stop",
    }


def _chunk(delta: dict, finish_reason: Any = None, usage: dict | None = None) -> str:
    payload = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n"


def stream_chunks(messages: list) -> list:
    """Build the SSE chunk list of one stubbed model response."""
    plan = plan_response(messages)
    chunks: list = []
    if plan["tool_call"] is None:
        for piece in [plan["text"][index : index + 8] for index in range(0, len(plan["text"]), 8)]:
            chunks.append(_chunk({"role": "assistant", "content": piece}))
        chunks.append(_chunk({}, finish_reason=plan["finish_reason"]))
    else:
        call = plan["tool_call"]
        chunks.append(
            _chunk(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_stub_1",
                            "type": "function",
                            "function": {"name": call["name"], "arguments": ""},
                        }
                    ],
                }
            )
        )
        serialized = json.dumps(call["arguments"], ensure_ascii=False)
        for index in range(0, len(serialized), 10):
            chunks.append(
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": serialized[index : index + 10]},
                            }
                        ]
                    }
                )
            )
        chunks.append(_chunk({}, finish_reason="tool_calls"))
    chunks.append(
        _chunk(
            {},
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
    )
    chunks.append("data: [DONE]\n\n")
    return chunks


class StubModelHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible surface used by the harness."""

    protocol_version = "HTTP/1.1"
    server_version = "stub-model/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Stay quiet; the harness captures nothing but errors."""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": MODEL_ID}]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802 - stdlib naming
        if not self.path.endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": {"message": "invalid JSON"}})
            return
        if not body.get("stream"):
            self._send_json(
                400, {"error": {"message": "the stub only supports streaming"}}
            )
            return
        messages = body.get("messages") or []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in stream_chunks(messages):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


class StubModelServer:
    """A stub model server bound to a loopback port."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port = int(port)
        self.host = host
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> "StubModelServer":
        self._httpd = ThreadingHTTPServer((self.host, self.port), StubModelHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="stub-model", daemon=True
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


def main(argv=None) -> int:
    """Run the stub in the foreground on ``--port``."""
    parser = argparse.ArgumentParser(prog="stub_model")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = StubModelServer(args.port, args.host).start()
    print(f"stub model listening on {server.base_url}", flush=True)
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
