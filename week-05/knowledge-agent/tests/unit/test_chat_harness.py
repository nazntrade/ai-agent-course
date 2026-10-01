"""D22 harness modules compile and LIVE stays opt-in (never fake inference)."""

from __future__ import annotations

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import chat_stub  # noqa: E402
import live_chat  # noqa: E402
import rag_eval  # noqa: E402


def test_chat_harness_modules_import():
    assert chat_stub.create_server is not None
    assert rag_eval.main is not None
    assert live_chat.main is not None


def test_live_chat_requires_opt_in(monkeypatch, capsys):
    monkeypatch.delenv("RUN_CHAT_LIVE", raising=False)
    assert live_chat.main() == 3
    assert "BLOCKED" in capsys.readouterr().out


def test_chat_stub_answers_without_a_pinned_model():
    server = chat_stub.create_server("127.0.0.1", 0, model="")
    try:
        assert server.server_address[1] > 0
    finally:
        server.server_close()


def test_invalid_answer_reason_accepts_non_empty_answer():
    assert rag_eval._invalid_answer_reason({"answer": {"text": "grounded"}, "errors": []}) is None


def test_invalid_answer_reason_reports_empty_or_missing_text():
    assert rag_eval._invalid_answer_reason({"answer": {"text": "   ", "finish_reason": "length"}, "errors": []}) == (
        "empty answer text (finish_reason=length)"
    )
    assert rag_eval._invalid_answer_reason({"answer": None, "errors": []}) == (
        "empty answer text (finish_reason=None)"
    )


def test_invalid_answer_reason_reports_provider_errors():
    reason = rag_eval._invalid_answer_reason(
        {"answer": {"text": "ok"}, "errors": [{"code": "chat_invalid_response", "message": "empty"}]}
    )
    assert reason == "chat_invalid_response"
