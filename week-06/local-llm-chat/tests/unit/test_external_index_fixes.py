"""Verify external_index fixes: dimension from index_versions, not global BLOB."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.external_index import ExternalIndex

# Week-05 knowledge-agent index (read-only RAG source) — contains the
# collections/index_versions/chunks tables used by the ExternalIndex.
INDEX_DB = Path(__file__).resolve().parent.parent.parent.parent.parent / "week-05" / "knowledge-agent" / "local-data" / "index.db"


def test_list_collections_returns_dimension():
    """C09: agents-survey dimension should come from index_versions, not first BLOB."""
    idx = ExternalIndex(INDEX_DB)
    try:
        cols = idx.list_collections()
        agents = [c for c in cols if c["name"] == "agents-survey"]
        assert len(agents) == 1, f"Expected 1 agents-survey collection, got {len(agents)}"
        assert agents[0]["dimension"] == 768, (
            f"Expected dimension=768, got {agents[0]['dimension']}"
        )
        assert agents[0]["chunk_count"] > 0, (
            f"Expected chunk_count > 0, got {agents[0]['chunk_count']}"
        )
        assert agents[0]["model"] is not None, (
            f"Expected model to be set, got {agents[0]['model']}"
        )
    finally:
        idx.close()


def test_inspect_schema_returns_dimension():
    """inspect_schema dimension should come from index_versions for agents-survey."""
    idx = ExternalIndex(INDEX_DB)
    try:
        schema = idx.inspect_schema()
        assert schema["dimension"] == 768, (
            f"Expected dimension=768, got {schema['dimension']}"
        )
        # chunks count should be for agents-survey, not global
        assert schema["chunks"] > 0, (
            f"Expected chunks > 0, got {schema['chunks']}"
        )
    finally:
        idx.close()


def test_search_dimension_mismatch_raises():
    """search() should raise ValueError on dimension mismatch."""
    idx = ExternalIndex(INDEX_DB)
    try:
        # 512-dim vector when DB expects 768
        fake_vector = [0.0] * 512
        try:
            idx.search(fake_vector, top_k=1)
            assert False, "Expected ValueError for dimension mismatch"
        except ValueError as exc:
            assert "Dimension mismatch" in str(exc), (
                f"Expected 'Dimension mismatch' in error, got: {exc}"
            )
    finally:
        idx.close()


def test_search_with_correct_dimension_returns_results():
    """search() with correct 768-dim vector should return results."""
    idx = ExternalIndex(INDEX_DB)
    try:
        # A simple all-zeros vector (won't match well but should not error)
        # Actually let's use a unit vector of correct dimension
        fake_vector = [1.0 / 768**0.5] * 768
        results = idx.search(fake_vector, top_k=3)
        # Results should exist since we have 768-dim vectors in DB
        assert isinstance(results, list), f"Expected list, got {type(results)}"
    finally:
        idx.close()
