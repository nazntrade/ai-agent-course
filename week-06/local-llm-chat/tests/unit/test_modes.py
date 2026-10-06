"""D26-03: default no-RAG, RAG attaches sources, no-RAG does not; embedding != answer."""

from __future__ import annotations

from app.config import load_settings
from app.context.builder import ContextBuilder


def test_no_rag_never_attaches_document_sources():
    builder = ContextBuilder()
    messages, trace = builder.build(
        question="What is the capital?",
        history=[],
        fragments=[{"label": "doc", "text": "secret doc text"}],
        rag_enabled=False,
    )
    assert trace["sources"] == []
    assert trace["rag_enabled"] is False
    joined = "\n".join(m.content for m in messages)
    assert "secret doc text" not in joined
    assert "[1]" not in joined


def test_rag_attaches_sources_with_labels():
    builder = ContextBuilder()
    messages, trace = builder.build(
        question="What is the capital?",
        history=[],
        fragments=[{"label": "geo.txt", "text": "The capital is Paris.", "chunk_id": "c1", "score": 0.9}],
        rag_enabled=True,
    )
    assert trace["rag_enabled"] is True
    assert len(trace["sources"]) == 1
    assert trace["sources"][0]["label"] == "geo.txt"
    joined = "\n".join(m.content for m in messages)
    assert "The capital is Paris." in joined


def test_no_rag_history_is_preserved():
    builder = ContextBuilder()
    messages, trace = builder.build(
        question="And its population?",
        history=[{"role": "user", "text": "Capital of France?"}, {"role": "assistant", "text": "Paris."}],
        rag_enabled=False,
    )
    assert trace["history_turns"] == 2
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]


def test_answer_model_is_independent_from_embedding_model():
    settings = load_settings({"GEMMA_MODEL_ID": "gemma.gguf", "EMBED_MODEL": "embeddinggemma:300m"})
    assert settings.gemma_model_id != settings.embed_model
