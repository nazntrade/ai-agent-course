"""INT: D23 filter/rewrite/four-mode backend over loopback stubs (no network)."""

from __future__ import annotations

import json
import re
import socket
import time

import pytest

from .test_chat_end_to_end import (
    Backend,
    CHAT_MODEL,
    _build,
    _start_server,
    _stop_server,
    free_port,
    http,
)

import chat_stub  # noqa: E402
import embed_stub  # noqa: E402


def _fixture_server(tmp_path, *, chat_delay_ms=0.0, extra_env=None):
    embed_port, chat_port = free_port(), free_port()
    embed = _start_server(embed_stub.create_server("127.0.0.1", embed_port, dimension=64))
    chat = _start_server(
        chat_stub.create_server("127.0.0.1", chat_port, model=CHAT_MODEL, delay_ms=chat_delay_ms)
    )
    source = tmp_path / "corpus.md"
    source.write_text(
        "# 1 Overview\n\n" + "Agents plan and use memory and tools. " * 60
        + "\n\n## 1.1 Memory\n\n" + "Memory stores observations. " * 30,
        encoding="utf-8",
    )
    # D24 pins the D23 regression to the flat rag-v1 path (SPEC D24 2.3).
    env = {"CHAT_RUNS_PATH": str(tmp_path / "chat-runs"), "RAG_GROUNDING_ENABLED": "0"}
    if extra_env:
        env.update(extra_env)
    backend = Backend(
        tmp_path / "index.db",
        f"http://127.0.0.1:{embed_port}",
        f"http://127.0.0.1:{chat_port}",
        extra_env=env,
    )
    if not backend.wait():
        backend.stop()
        _stop_server(embed)
        _stop_server(chat)
        pytest.fail("backend did not start")
    server = {
        "base": backend.base,
        "source": source,
        "embed_url": f"http://127.0.0.1:{embed_port}",
        "chat_url": f"http://127.0.0.1:{chat_port}",
    }
    yield server
    backend.stop()
    _stop_server(embed)
    _stop_server(chat)


@pytest.fixture()
def d23_server(tmp_path):
    yield from _fixture_server(tmp_path)


@pytest.fixture()
def slow_chat_server(tmp_path):
    yield from _fixture_server(tmp_path, chat_delay_ms=800.0,
                               extra_env={"RAG_REWRITE_TIMEOUT_SECONDS": "0.2",
                                          "CHAT_TIMEOUT_SECONDS": "60"})


def test_four_modes_and_rewrite_trace(d23_server):
    base = d23_server["base"]
    collection, _version = _build(d23_server, "d23")
    seen = {}
    for mode in ("A", "B", "C", "D"):
        status, record = http(
            "POST",
            base + "/api/chat",
            {
                "mode": "with_rag",
                "collection_id": collection,
                "question": "How do agents use memory?",
                "rag_mode": mode,
                "min_score": 0.0,
                "prefilter_top_k": 5,
                "postfilter_top_k": 3,
            },
        )
        assert status == 200, (mode, record)
        assert record["rag_mode"] == mode
        retrieval = record["retrieval"]
        assert retrieval["passed_count"] <= retrieval["selected_count"] <= retrieval["found_count"]
        assert set(retrieval["exclusion_reasons"]) == {"threshold", "top_k", "context_budget"}
        seen[mode] = record
    assert seen["C"]["rewrite"]["used"] is True
    assert seen["C"]["original_query"] != seen["C"]["search_query"]
    assert seen["D"]["use_filter"] is True and seen["D"]["use_rewrite"] is True
    assert seen["A"]["rewrite"]["attempted"] is False


def test_compare_modes_endpoint(d23_server):
    base = d23_server["base"]
    collection, version_id = _build(d23_server, "modes")
    status, record = http(
        "POST",
        base + "/api/chat/compare-modes",
        {"collection_id": collection, "question": "Memory?", "min_score": 0.0},
    )
    assert status == 200, record
    assert record["comparison_kind"] == "four_modes"
    assert [mode["id"] for mode in record["modes"]] == ["A", "B", "C", "D"]
    assert record["comparison"]["index_version_id"] == version_id
    assert record["comparison"]["same_model"] is True
    assert record["comparison"]["same_settings"] is True


def test_legacy_top_k_keeps_found_count_and_flat_usage(d23_server):
    base = d23_server["base"]
    collection, _version = _build(d23_server, "legacy")
    status, record = http(
        "POST",
        base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "agents", "top_k": 2},
    )
    assert status == 200, record
    assert record["retrieval"]["found_count"] == 2
    assert record["prefilter_top_k"] == 2 and record["postfilter_top_k"] == 2
    assert record["rag_mode"] == "A"
    assert record["usage"]["input_tokens"] is not None
    assert record["latency_ms"]["chat"] is not None


def test_empty_filter_is_deterministic(d23_server):
    base = d23_server["base"]
    collection, _version = _build(d23_server, "empty-filter")
    status, record = http(
        "POST",
        base + "/api/chat",
        {
            "mode": "with_rag",
            "collection_id": collection,
            "question": "agents",
            "use_filter": True,
            "min_score": 1.0,
        },
    )
    assert status == 200, record
    assert record["retrieval"]["passed"] == []
    assert record["answer"]["insufficient_sources"] is True
    assert record["answer"]["finish_reason"] is None
    assert record["usage"] is None
    assert "[stub]" not in (record["answer"]["text"] or "")
    assert record["retrieval"]["exclusion_reasons"]["threshold"]


def test_rewrite_timeout_falls_back_but_generation_answers(slow_chat_server):
    base = slow_chat_server["base"]
    collection, _version = _build(slow_chat_server, "rewrite-timeout")
    status, record = http(
        "POST",
        base + "/api/chat",
        {
            "mode": "with_rag",
            "collection_id": collection,
            "question": "Memory?",
            "rag_mode": "C",
            "prefilter_top_k": 3,
            "postfilter_top_k": 2,
        },
    )
    assert status == 200, record
    assert record["rewrite"]["fallback"] is True
    assert record["rewrite"]["reason"] == "chat_timeout"
    assert record["search_query"] == record["original_query"]
    assert record["answer"]["text"]


def _open_stream(base: str, payload: dict):
    host, port = "127.0.0.1", int(base.rsplit(":", 1)[1])
    sock = socket.create_connection((host, port), timeout=10)
    body = json.dumps(payload).encode("utf-8")
    request = (
        f"POST /api/chat/stream HTTP/1.1\r\nHost: {host}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("utf-8")
    sock.sendall(request + body)
    data = b""
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
        if b'"type": "start"' in data:
            break
    return sock, data.decode("utf-8", "replace")


def test_live_runner_projection_works_against_the_stub(d23_server):
    from harness import d23_eval, d23_live

    base = d23_server["base"]
    collection, version_id = _build(d23_server, "live-projection")
    answers, record, trace = d23_live.run_comparison(base, collection, version_id, 0.0)
    assert len(answers) == 40
    assert record is not None and record["rag_mode"] == "A"
    # Regression: the saved trace must come from a filtered branch (B/D), never A.
    assert trace is not None and trace["use_filter"] is True
    assert trace["use_rewrite"] is False
    assert trace["rag_mode"] in ("B", "D")
    sample = d23_eval.trace_sample(trace)
    assert isinstance(sample["exclusion_reasons"], dict)
    # The protocol table must be derivable from, and consistent with, the saved answers.
    table = d23_eval.passed_count_table(answers)
    assert len(table) == 10
    assert all(set(modes) == {"A", "B", "C", "D"} for modes in table.values())
    d23_eval.assert_passed_count_table(answers, table)
    with pytest.raises(AssertionError):
        d23_eval.assert_passed_count_table(answers, {next(iter(table)): {"D": -1}})
    summary = d23_live._summarize(answers)
    assert summary["answers_total"] == 40


def test_client_disconnect_keeps_the_server_healthy(d23_server):
    base = d23_server["base"]
    collection, _version = _build(d23_server, "cancel")
    sock, text = _open_stream(
        base,
        {
            "mode": "with_rag",
            "collection_id": collection,
            "question": "Memory?",
            "prefilter_top_k": 3,
            "postfilter_top_k": 2,
        },
    )
    match = re.search(r'"run_id":\s*"([^"]+)"', text)
    sock.close()
    time.sleep(0.5)
    assert match is not None, text
    run_id = match.group(1)
    status, health = http("GET", base + "/api/health")
    assert status == 200 and health["status"] in ("ok", "degraded")
    status, _record = http(
        "POST", base + "/api/chat", {"mode": "without_rag", "question": "after disconnect"}
    )
    assert status == 200
    status, aborted = http("GET", base + f"/api/chat-runs/{run_id}")
    if status == 200:
        assert aborted.get("answer") is None or aborted.get("errors")
