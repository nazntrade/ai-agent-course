"""D26-12: retrieval regression — index, search, filter, citations preserved."""

from __future__ import annotations

from app.rag.store import RagStore, chunk_text


def test_chunk_text_overlaps_and_covers():
    text = "abcdefghij" * 60
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    assert chunks
    assert "".join(chunks)[:10] == text[:10]


def test_index_search_and_filter_returns_sources(tmp_path):
    store = RagStore(str(tmp_path / "index.db"))
    docs = [
        (["The capital of France is Paris."], [[1.0, 0.0, 0.0]]),
        (["Bananas are yellow fruit."], [[0.0, 1.0, 0.0]]),
    ]
    for chunks, vectors in docs:
        store.add_document(label=chunks[0][:12], public_uri=chunks[0][:12], chunks=chunks, vectors=vectors)

    results = store.search([1.0, 0.0, 0.0], top_k=2)
    assert results
    assert "Paris" in results[0]["text"]
    assert results[0]["source"]
    assert results[0]["score"] >= results[1]["score"]

    filtered = store.search([1.0, 0.0, 0.0], top_k=2, min_score=0.5)
    assert all(item["score"] >= 0.5 for item in filtered)
    store.close()


def test_compare_reports_stability(tmp_path):
    store = RagStore(str(tmp_path / "index.db"))
    store.add_document(
        label="geo", public_uri="geo", chunks=["Paris is the capital."], vectors=[[1.0, 0.0]]
    )
    comparison = store.compare([1.0, 0.0], top_k=1)
    assert comparison["unfiltered"]
    assert comparison["stable"] is True
    store.close()
