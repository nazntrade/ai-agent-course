"""INT: D24 grounded RAG (default RAG_GROUNDING_ENABLED=1) over loopback stubs.

The app runs as ``python -m knowledge_agent`` with the deterministic grounded
chat stub; no network and no ``.env`` are used.
"""

from __future__ import annotations

import pytest

from .test_chat_end_to_end import (
    Backend,
    CHAT_MODEL,
    _build,
    _start_server,
    _stop_server,
    free_port,
    http,
    post_stream,
)

import chat_stub  # noqa: E402
import embed_stub  # noqa: E402


def _grounded_server(tmp_path, *, chat_fail_mode=None, extra_env=None):
    embed_port, chat_port = free_port(), free_port()
    embed = _start_server(embed_stub.create_server("127.0.0.1", embed_port, dimension=64))
    chat = _start_server(
        chat_stub.create_server(
            "127.0.0.1", chat_port, model=CHAT_MODEL, fail_mode=chat_fail_mode
        )
    )
    source = tmp_path / "corpus.md"
    source.write_text(
        "# 1 Overview\n\n" + "Agents plan and use memory and tools. " * 60
        + "\n\n## 1.1 Memory\n\n" + "Memory stores observations. " * 30,
        encoding="utf-8",
    )
    env = {"CHAT_RUNS_PATH": str(tmp_path / "chat-runs")}
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
    yield {
        "base": backend.base,
        "source": source,
        "chat_url": f"http://127.0.0.1:{chat_port}",
    }
    backend.stop()
    _stop_server(embed)
    _stop_server(chat)


@pytest.fixture()
def d24_server(tmp_path):
    yield from _grounded_server(tmp_path)


@pytest.fixture()
def grounded_variant(tmp_path, request):
    yield from _grounded_server(tmp_path, chat_fail_mode=request.param)


def _grounding(record):
    return (record.get("answer") or {}).get("grounding") or {}


def test_grounded_chat_stream_compare_and_compare_modes(d24_server):
    base = d24_server["base"]
    collection, version_id = _build(d24_server, "d24")

    status, record = http(
        "POST",
        base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )
    assert status == 200, record
    assert record["prompt"]["template_id"] == "grounded-rag-v1"
    grounding = _grounding(record)
    assert grounding["status"] == "verified"
    assert grounding["meaning_check"] == "not_performed"
    passed_ids = [item["chunk_id"] for item in record["retrieval"]["passed"]]
    assert passed_ids, "the stub must be given at least one passed chunk"
    for citation in grounding["citations"]:
        assert citation["chunk_id"] in passed_ids
        assert citation["quote_verbatim"] is True
        assert citation["source"] and citation["section"]
        assert citation["meaning_supported"] is None

    events = post_stream(
        base + "/api/chat/stream",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )
    done = next(event for event in events if event["type"] == "done")
    assert done["answer"]["answer"]["grounding"]["status"] == "verified"

    modes = http(
        "POST",
        base + "/api/chat/compare-modes",
        {"collection_id": collection, "question": "Memory?", "min_score": 0.0},
    )[1]
    assert modes["comparison"]["prompt_templates"]["generation"] == "grounded-rag-v1"
    for mode in modes["modes"]:
        assert "grounding" in mode["branch"]["answer"]

    # POST /api/chat/compare is never grounded.
    compare = http(
        "POST",
        base + "/api/chat/compare",
        {"collection_id": collection, "question": "Memory?", "top_k": 3},
    )[1]
    assert compare["comparison"]["prompt_templates"]["with_rag"] == "rag-v1"
    assert "grounding" not in compare["branches"]["with_rag"]["answer"]


@pytest.mark.parametrize("grounded_variant", ["grounded_unknown_id"], indirect=True)
def test_grounded_unknown_chunk_id(grounded_variant):
    base = grounded_variant["base"]
    collection, _version = _build(grounded_variant, "unknown-id")
    record = http(
        "POST", base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )[1]
    grounding = _grounding(record)
    assert grounding["status"] == "failed"
    assert grounding["citations"][0]["status"] == "unknown_chunk_id"


@pytest.mark.parametrize("grounded_variant", ["grounded_fabricated_quote"], indirect=True)
def test_grounded_fabricated_quote(grounded_variant):
    base = grounded_variant["base"]
    collection, _version = _build(grounded_variant, "fabricated")
    record = http(
        "POST", base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )[1]
    grounding = _grounding(record)
    assert grounding["status"] != "verified"
    assert grounding["citations"][0]["quote_verbatim"] is False


@pytest.mark.parametrize("grounded_variant", ["grounded_insufficient"], indirect=True)
def test_grounded_model_insufficient(grounded_variant):
    base = grounded_variant["base"]
    collection, _version = _build(grounded_variant, "insufficient")
    record = http(
        "POST", base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )[1]
    assert record["answer"]["insufficient_sources"] is True
    grounding = _grounding(record)
    assert grounding["status"] == "refused"
    assert grounding["reason"] == "model_insufficient"


@pytest.mark.parametrize("grounded_variant", ["grounded_bad_json"], indirect=True)
def test_grounded_bad_json_is_a_format_error(grounded_variant):
    base = grounded_variant["base"]
    collection, _version = _build(grounded_variant, "bad-json")
    status, record = http(
        "POST", base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )
    assert status == 503, record
    assert record["error"]["code"] == "chat_invalid_response"
    assert record["error"]["details"]["format"] == "grounded_json"


@pytest.mark.parametrize("grounded_variant", ["length_done"], indirect=True)
def test_grounded_truncated_generation(grounded_variant):
    base = grounded_variant["base"]
    collection, _version = _build(grounded_variant, "truncated")
    record = http(
        "POST", base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )[1]
    answer = record["answer"]
    assert answer["truncated"] is True
    assert answer["insufficient_sources"] is False
    assert answer["grounding"]["status"] == "failed"
    assert answer["grounding"]["reason"] == "truncated_generation"


def test_chunk_id_filter_over_http(d24_server):
    base = d24_server["base"]
    collection, version_id = _build(d24_server, "chunk-filter")
    # Find a real chunk id through the D21 endpoint.
    first = http("GET", f"{base}/api/index-versions/{version_id}/chunks?limit=1")[1]
    chunk_id = first["items"][0]["chunk_id"]
    matched = http("GET", f"{base}/api/index-versions/{version_id}/chunks?chunk_id={chunk_id}")[1]
    assert matched["total"] == 1
    assert matched["items"][0]["chunk_id"] == chunk_id
    assert matched["filters"]["chunk_id"] == chunk_id
    missing = http("GET", f"{base}/api/index-versions/{version_id}/chunks?chunk_id={'f' * 64}")[1]
    assert missing["total"] == 0 and missing["items"] == []
