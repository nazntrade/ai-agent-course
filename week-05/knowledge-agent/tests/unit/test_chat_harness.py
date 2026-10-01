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
