"""Deterministic local embedding stub for INT tests and smoke runs.

This is NOT inference: vectors are derived from ``sha256(text)`` and the module
is always labeled as a stub. It speaks enough of the Ollama HTTP surface for the
backend to run without a real model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def stub_vector(text: str, dimension: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        block = hashlib.sha256(digest + str(counter).encode("ascii")).digest()
        for index in range(0, len(block) - 1, 2):
            raw = (block[index] << 8) | block[index + 1]
            values.append((raw / 65535.0) - 0.5)
            if len(values) == dimension:
                break
        counter += 1
    return values


def count_input_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


class StubState:
    def __init__(self, dimension: int, delay_ms: float, fail_mode: str | None) -> None:
        self.dimension = dimension
        self.delay_ms = delay_ms
        self.fail_mode = fail_mode


def _fail_for(header_value: str | None, state: StubState) -> str | None:
    return header_value or state.fail_mode


class StubHandler(BaseHTTPRequestHandler):
    state: StubState
    server_version = "embed-stub/1.0"

    def log_message(self, *args: Any) -> None:  # keep smoke output clean
        return

    # -- helpers ----------------------------------------------------------
    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/api/version"):
            self._send(200, {"version": "0.0.0-stub"})
        elif self.path.startswith("/api/tags"):
            self._send(
                200,
                {"models": [{"name": "embeddinggemma:300m", "model": "embeddinggemma:300m"}]},
            )
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        fail = _fail_for(self.headers.get("X-Stub-Fail"), self.state)
        if self.state.delay_ms:
            time.sleep(self.state.delay_ms / 1000.0)

        if self.path.startswith("/api/show"):
            self._read_json()
            if fail == "missing_model":
                self._send(404, {"error": "model not found, try pulling it"})
                return
            self._send(
                200,
                {
                    "digest": "stub-digest",
                    "model_info": {"embeddinggemma.embedding_length": self.state.dimension},
                },
            )
            return

        if self.path.startswith("/api/embed") or self.path.startswith("/api/embeddings"):
            payload = self._read_json()
            texts = payload.get("input")
            if texts is None:
                texts = [payload.get("prompt", "")]
            if isinstance(texts, str):
                texts = [texts]

            if fail == "unavailable":
                self._send(503, {"error": "stub unavailable"})
                return
            if fail == "missing_model":
                self._send(404, {"error": "model not found, try pulling it"})
                return
            if fail == "length" and any(len(text) > 1000 for text in texts):
                self._send(400, {"error": "input is too long for context length"})
                return

            vectors = [stub_vector(text, self.state.dimension) for text in texts]
            if fail == "bad_count" and len(vectors) > 1:
                vectors = vectors[:-1]
            if fail == "bad_dim":
                vectors = [vector[:-1] for vector in vectors]
            if fail == "nonfinite":
                vectors = [[float("nan")] + vector[1:] for vector in vectors]

            if self.path.startswith("/api/embeddings"):
                self._send(
                    200,
                    {"embedding": vectors[0], "prompt_eval_count": count_input_tokens(texts[0])},
                )
                return
            self._send(
                200,
                {
                    "embeddings": vectors,
                    "prompt_eval_count": sum(count_input_tokens(text) for text in texts),
                    "truncate": False,
                },
            )
            return

        self._send(404, {"error": "unknown endpoint"})


def create_server(
    host: str = "127.0.0.1",
    port: int = 8769,
    dimension: int = 64,
    delay_ms: float = 0.0,
    fail_mode: str | None = None,
) -> ThreadingHTTPServer:
    state = StubState(dimension=dimension, delay_ms=delay_ms, fail_mode=fail_mode)

    class BoundHandler(StubHandler):
        pass

    BoundHandler.state = state
    server = ThreadingHTTPServer((host, port), BoundHandler)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic embedding stub (not inference).")
    parser.add_argument("--host", default=os.environ.get("EMBED_STUB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EMBED_STUB_PORT", "8769")))
    parser.add_argument("--dimension", type=int, default=int(os.environ.get("STUB_DIMENSION", "64")))
    parser.add_argument("--delay-ms", type=float, default=float(os.environ.get("STUB_DELAY_MS", "0")))
    parser.add_argument("--fail-mode", default=os.environ.get("STUB_FAIL_MODE"))
    args = parser.parse_args(argv)

    server = create_server(
        args.host, args.port, args.dimension, args.delay_ms, args.fail_mode
    )
    print(
        f"embed_stub listening on http://{args.host}:{args.port} "
        f"(dimension={args.dimension}, this is a stub, not inference)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
