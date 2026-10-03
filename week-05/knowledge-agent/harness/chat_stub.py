"""Deterministic local chat stub for INT tests and smoke runs.

This is NOT inference. It speaks enough of the Ollama ``/api/chat`` surface for
the backend to exercise both modes, streaming and provider boundary cases
without a real model. ``X-Stub-Fail`` selects a failure/edge mode per request.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_CHUNK_RE = re.compile(r"\[chunk_id:\s*([0-9a-fA-F]{16,64})\]")


def count_input_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text or ""))


def _is_grounded(messages: list[dict[str, Any]]) -> bool:
    """Grounded-rag-v1 is selected by the JSON-answer marker in the system text."""

    for message in messages:
        if message.get("role") == "system":
            if "return exactly one json object" in str(message.get("content") or "").lower():
                return True
    return False


def _context_chunks(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract ``chunk_id`` and body text from every ``<context>`` block.

    The body is the chunk text after the three metadata lines rendered by
    ``chat/prompts.build_context_block`` (section_path/pages/source_label). A
    quote taken from this body is a deterministic substring of the chunk text,
    so ``quote_verbatim``/``source_exists`` are provably true for the happy path.
    """

    chunks: list[dict[str, str]] = []
    for message in messages:
        content = str(message.get("content") or "")
        if "<context>" not in content:
            continue
        current: dict[str, str] | None = None
        body: list[str] = []
        metadata_remaining = 0
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[chunk_id:"):
                if current is not None:
                    current["text"] = "\n".join(body).strip()
                    chunks.append(current)
                match = _CHUNK_RE.search(stripped)
                current = {"chunk_id": match.group(1) if match else ""}
                body = []
                metadata_remaining = 3
                continue
            if current is None:
                continue
            if stripped == "</context>":
                break
            if metadata_remaining > 0:
                metadata_remaining -= 1
                continue
            # D25 conversation context carries exact, selectable excerpts.
            # This stub must emulate that protocol rather than quote JSON markup.
            try:
                excerpt = json.loads(line)
            except ValueError:
                excerpt = None
            if isinstance(excerpt, dict) and isinstance(excerpt.get("text"), str) and (excerpt.get("quote_id") or excerpt.get("evidence_id")):
                if excerpt.get("evidence_id"):
                    current.setdefault("evidence_id", str(excerpt["evidence_id"]))
                elif excerpt.get("quote_id"):
                    current.setdefault("quote_id", str(excerpt["quote_id"]))
                body.append(excerpt["text"])
            else:
                body.append(line)
        if current is not None:
            current["text"] = "\n".join(body).strip()
            chunks.append(current)
    return chunks


def _grounded_response(messages: list[dict[str, Any]], fail: str | None) -> str:
    """Deterministic grounded-JSON answer (not inference)."""

    from knowledge_agent.chat.citations import normalize_whitespace

    question = ""
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "user" and "<context>" not in content and content.strip():
            question = content.strip()
            break
    chunks = _context_chunks(messages)
    first = chunks[0] if chunks else {"chunk_id": "", "text": ""}
    chunk_id = first["chunk_id"]
    normalized = normalize_whitespace(first["text"])
    quote = normalized[:160].strip() if normalized else ""
    answer = f"[stub] Grounded answer for: {question[:120]}"
    if chunk_id:
        answer += f" [{chunk_id}]"

    if fail == "grounded_bad_json":
        return '{"answer": "unterminated", "citations": ['
    if fail == "grounded_insufficient":
        return json.dumps(
            {
                "answer": "The information is not available in the provided context.",
                "citations": [],
                "insufficient": True,
                "limitation": None,
            }
        )
    if fail == "grounded_empty_citations":
        return json.dumps(
            {"answer": answer, "citations": [], "insufficient": False, "limitation": None}
        )
    if fail == "grounded_unknown_id":
        unknown = "f" * 64 if chunk_id != "f" * 64 else "0" * 64
        citations = [{"chunk_id": unknown, "quote": quote or "fabricated"}]
    elif fail == "grounded_fabricated_quote":
        citations = (
            [{"chunk_id": chunk_id, "quote": "This exact sentence is absent from the fragment."}]
            if chunk_id
            else []
        )
    elif fail == "grounded_translation":
        citations = (
            [{"chunk_id": chunk_id, "quote": quote, "translation": "Translated citation."}]
            if chunk_id and quote
            else []
        )
    elif fail == "grounded_limitation":
        citations = [{"chunk_id": chunk_id, "quote": quote}] if chunk_id and quote else []
        return json.dumps(
            {
                "answer": answer,
                "citations": citations,
                "insufficient": False,
                "limitation": "partial answer: the documents do not state everything asked.",
            }
        )
    else:
        if first.get("evidence_id"):
            citations = [{"evidence_id": first["evidence_id"]}]
            answer = answer.replace("[" + chunk_id + "]", "[" + first["evidence_id"] + "]")
        elif first.get("quote_id"):
            citations = [{"chunk_id": chunk_id, "quote_id": first["quote_id"]}]
        else:
            citations = [{"chunk_id": chunk_id, "quote": quote}] if chunk_id and quote else []
    return json.dumps(
        {
            "answer": answer,
            "citations": citations,
            "insufficient": False,
            "limitation": None,
        }
    )


def _response_text(messages: list[dict[str, Any]], fail: str | None = None) -> str:
    if _is_grounded(messages):
        return _grounded_response(messages, fail)

    question = ""
    chunk_id = None
    rewrite = False
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "system" and "rewrite" in content.lower():
            rewrite = True
        if message.get("role") == "user":
            match = _CHUNK_RE.search(content)
            if match and chunk_id is None:
                chunk_id = match.group(1)
            if "<context>" not in content and content.strip() and not question:
                question = content.strip()
    if rewrite:
        # Deterministic, single-line rewrite that differs from the question so
        # the D23 trace can show original_query != search_query (not inference).
        return "retrieval query: " + (question[:100] or "query")
    answer = f"[stub] Answer for: {question[:160]}"
    if chunk_id:
        answer += f" Source [{chunk_id}]."
    return answer


class StubState:
    def __init__(self, model: str, delay_ms: float, fail_mode: str | None) -> None:
        self.model = model
        self.delay_ms = delay_ms
        self.fail_mode = fail_mode


class ChatStubHandler(BaseHTTPRequestHandler):
    state: StubState
    server_version = "chat-stub/1.0"

    def log_message(self, *args: Any) -> None:  # keep smoke output clean
        return

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

    def _model_name(self) -> str:
        return self.state.model or "stub-chat"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/api/version"):
            self._send(200, {"version": "0.0.0-chat-stub"})
        elif self.path.startswith("/api/tags"):
            self._send(
                200,
                {
                    "models": [
                        {
                            "name": self._model_name(),
                            "model": self._model_name(),
                            "digest": "stub-chat-digest",
                        }
                    ]
                },
            )
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        fail = self.headers.get("X-Stub-Fail") or self.state.fail_mode
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
                    "digest": "stub-chat-digest",
                    "model_info": {"stub.context_length": 8192},
                },
            )
            return

        if not self.path.startswith("/api/chat"):
            self._send(404, {"error": "unknown endpoint"})
            return

        payload = self._read_json()
        messages = payload.get("messages") or []
        stream = bool(payload.get("stream"))

        if fail == "unavailable":
            self._send(503, {"error": "stub chat unavailable"})
            return
        if fail == "missing_model":
            self._send(404, {"error": "model 'x' not found, try pulling it first"})
            return
        if fail == "length":
            self._send(400, {"error": "input is too long for context length"})
            return
        if fail == "timeout":
            time.sleep(3.0)

        text = "" if fail == "empty" else _response_text(messages, fail)
        if fail == "length_done":
            done_reason = "length"
        else:
            done_reason = "stop"
        input_tokens = sum(count_input_tokens(str(m.get("content") or "")) for m in messages)
        output_tokens = count_input_tokens(text) or 5
        duration_ns = 500_000_000

        if stream:
            pieces = [text[i:i + 12] for i in range(0, len(text), 12)] or [""]
            body_lines = []
            for piece in pieces[:-1]:
                body_lines.append(
                    json.dumps(
                        {
                            "model": self._model_name(),
                            "message": {"role": "assistant", "content": piece},
                            "done": False,
                        }
                    )
                )
            final: dict[str, Any] = {
                "model": self._model_name(),
                "message": {"role": "assistant", "content": pieces[-1]},
                "done": True,
                "done_reason": done_reason,
                "prompt_eval_count": input_tokens,
                "eval_count": output_tokens,
                "eval_duration": duration_ns,
            }
            if fail == "no_usage":
                final.pop("prompt_eval_count", None)
                final.pop("eval_count", None)
                final.pop("eval_duration", None)
            elif fail == "partial_usage":
                final.pop("prompt_eval_count", None)
            body_lines.append(json.dumps(final))
            body = ("\n".join(body_lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        response: dict[str, Any] = {
            "model": self._model_name(),
            "message": {"role": "assistant", "content": text},
            "done": True,
            "done_reason": done_reason,
            "prompt_eval_count": input_tokens,
            "eval_count": output_tokens,
            "eval_duration": duration_ns,
        }
        if fail == "no_usage":
            response.pop("prompt_eval_count", None)
            response.pop("eval_count", None)
            response.pop("eval_duration", None)
        elif fail == "partial_usage":
            response.pop("prompt_eval_count", None)
        self._send(200, response)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8771,
    model: str = "",
    delay_ms: float = 0.0,
    fail_mode: str | None = None,
) -> ThreadingHTTPServer:
    state = StubState(model=model, delay_ms=delay_ms, fail_mode=fail_mode)

    class BoundHandler(ChatStubHandler):
        pass

    BoundHandler.state = state
    server = ThreadingHTTPServer((host, port), BoundHandler)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic chat stub (not inference).")
    parser.add_argument("--host", default=os.environ.get("CHAT_STUB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CHAT_STUB_PORT", "8771")))
    parser.add_argument("--model", default=os.environ.get("CHAT_STUB_MODEL", ""))
    parser.add_argument("--delay-ms", type=float, default=float(os.environ.get("CHAT_STUB_DELAY_MS", "0")))
    parser.add_argument("--fail-mode", default=os.environ.get("CHAT_STUB_FAIL_MODE"))
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port, args.model, args.delay_ms, args.fail_mode)
    print(
        f"chat_stub listening on http://{args.host}:{args.port} "
        f"(this is a stub, not inference)",
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
