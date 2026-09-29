"""KnowledgeService end-to-end logic on fakes (D21-04, D21-06, D21-08)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_agent.domain.errors import IndexBusy, IndexIncompatible, IndexNotReady
from knowledge_agent.service.knowledge_service import InvalidStrategy

from tests.helpers import FakeEmbedder, make_service

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _source(path: Path, label: str | None = None):
    entry = {"path": str(path)}
    if label:
        entry["label"] = label
    return entry


def test_build_and_search_returns_fragments(tmp_path):
    service, store = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]

    result = service.build(
        collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True
    )
    version = service.get_index_version(result["index_version_id"])
    assert version["status"] == "ready"
    assert version["counts"]["chunks"] > 0
    assert version["counts"]["sources"] == 1

    service.set_active_index(collection, result["index_version_id"])
    search = service.search(collection, "agent memory", top_k=3)
    assert search["fragments"]
    assert search["index_version_id"] == result["index_version_id"]
    metadata = search["fragments"][0]["metadata"]
    for field in (
        "source_uri",
        "source_label",
        "document_id",
        "section_path",
        "language",
        "content_sha256",
    ):
        assert field in metadata
    assert metadata["source_uri"] == "sample.txt"
    assert ":" not in metadata["source_uri"]


def test_reimport_is_reused_without_new_rows(tmp_path):
    service, store = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    first = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    before = store.count_rows(first["index_version_id"])["chunks"]

    second = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    assert second["reused"] is True
    assert second["status"] == "ready"
    assert second["index_version_id"] == first["index_version_id"]
    assert store.count_rows(first["index_version_id"])["chunks"] == before


def test_changed_bytes_create_new_index(tmp_path):
    service, _ = make_service(tmp_path)
    source = tmp_path / "note.txt"
    source.write_text("first version of the corpus", encoding="utf-8")
    collection = service.create_collection("demo")["collection_id"]
    first = service.build(collection, [_source(source)], "fixed", wait=True)

    source.write_text("completely different second version", encoding="utf-8")
    second = service.build(collection, [_source(source)], "fixed", wait=True)
    assert second["reused"] is False
    assert second["index_version_id"] != first["index_version_id"]


def test_search_before_build_is_not_ready(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    with pytest.raises(IndexNotReady):
        service.search(collection, "anything")


def test_incompatible_embedder_blocks_before_query_embedding(tmp_path):
    service, store = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    built = service.build(
        collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True
    )
    service.set_active_index(collection, built["index_version_id"])

    original_calls = service._embedder.calls
    service._embedder = FakeEmbedder(model="other-model", digest="other", dimension=16)
    with pytest.raises(IndexIncompatible):
        service.search(collection, "agent memory")
    assert service._embedder.calls == 0
    assert original_calls >= 1


def test_second_build_while_busy_raises_index_busy(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    assert service._build_lock.acquire(blocking=False)
    try:
        with pytest.raises(IndexBusy):
            service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed")
    finally:
        service._build_lock.release()


def test_unknown_collection_and_strategy(tmp_path):
    service, _ = make_service(tmp_path)
    with pytest.raises(KeyError):
        service.build("missing", [_source(FIXTURES / "sample.txt")], "fixed")
    collection = service.create_collection("demo")["collection_id"]
    with pytest.raises(InvalidStrategy):
        service.build(collection, [_source(FIXTURES / "sample.txt")], "unknown")


def test_manifest_contains_sources_and_chunking(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    result = service.build(collection, [_source(FIXTURES / "sample.txt")], "structure", wait=True)
    version = service.get_index_version(result["index_version_id"])
    manifest = version["manifest"]
    assert manifest["sources"][0]["content_sha256"]
    assert manifest["chunking"]["strategy"] == "structure"
    assert manifest["embedding"]["model"] == "fake-model"
    assert manifest["counts"]["chunks"] == version["counts"]["chunks"]
    assert "chunk_tokens" in manifest["metrics"]


def test_manifest_finished_at_matches_summary(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    result = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    version = service.get_index_version(result["index_version_id"])
    assert version["finished_at"] is not None
    assert version["manifest"]["finished_at"] == version["finished_at"]


def test_references_sections_are_excluded_and_recorded(tmp_path):
    source = tmp_path / "doc.md"
    source.write_text(
        "# 1 Introduction\n\nAgent memory text about planning.\n\n"
        "# References\n\n[1] Smith J. A very citable reference.\n",
        encoding="utf-8",
    )
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    result = service.build(collection, [_source(source)], "structure", wait=True)
    version = service.get_index_version(result["index_version_id"])
    assert version["manifest"]["excluded_roles"] == ["references"]

    chunks = service.list_chunks(result["index_version_id"])
    assert chunks["items"]
    assert all(
        item["metadata"]["section_path"] != "References" for item in chunks["items"]
    )
    assert all("citable reference" not in item["text"] for item in chunks["items"])


def test_search_prefers_active_index_for_matching_strategy(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    fixed = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    structure = service.build(
        collection, [_source(FIXTURES / "sample.txt")], "structure", wait=True
    )
    service.set_active_index(collection, structure["index_version_id"])

    # No strategy: the active index is used.
    assert service.search(collection, "memory")["index_version_id"] == structure["index_version_id"]
    # Strategy matching the active index: same version.
    assert (
        service.search(collection, "memory", strategy="structure")["index_version_id"]
        == structure["index_version_id"]
    )
    # Strategy not matching active: fall back to the ready version of that strategy.
    assert (
        service.search(collection, "memory", strategy="fixed")["index_version_id"]
        == fixed["index_version_id"]
    )


def test_search_rejects_index_version_from_another_collection(tmp_path):
    service, _ = make_service(tmp_path)
    collection_a = service.create_collection("a")["collection_id"]
    collection_b = service.create_collection("b")["collection_id"]
    build_a = service.build(
        collection_a, [_source(FIXTURES / "sample.txt")], "fixed", wait=True
    )
    build_b = service.build(
        collection_b, [_source(FIXTURES / "sample.txt")], "fixed", wait=True
    )
    with pytest.raises(IndexNotReady):
        service.search(
            collection_a, "memory", index_version_id=build_b["index_version_id"]
        )
    assert (
        service.search(
            collection_a, "memory", index_version_id=build_a["index_version_id"]
        )["index_version_id"]
        == build_a["index_version_id"]
    )


def test_compare_uses_actual_strategy_and_flags_comparability(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    structure = service.build(
        collection, [_source(FIXTURES / "sample.txt")], "structure", wait=True
    )

    result = service.compare(collection, ["fixed", "structure"])
    assert result["comparable"] is True
    assert {row["strategy"] for row in result["strategies"]} == {"fixed", "structure"}

    explicit = service.compare(
        collection, ["fixed", "structure"], index_version_id=structure["index_version_id"]
    )
    assert len(explicit["strategies"]) == 1
    assert explicit["strategies"][0]["strategy"] == "structure"
    assert explicit["strategies"][0]["index_version_id"] == structure["index_version_id"]


def test_compare_flags_different_sources(tmp_path):
    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    service.build(collection, [_source(FIXTURES / "sample.md")], "structure", wait=True)
    result = service.compare(collection, ["fixed", "structure"])
    assert result["comparable"] is False
    assert result["note"]


class _LateDimensionEmbedder(FakeEmbedder):
    """Reports no dimension on the first identity() call, like a flaky preflight."""

    def __init__(self) -> None:
        super().__init__(dimension=8)
        self.identity_calls = 0

    def identity(self):
        self.identity_calls += 1
        identity = super().identity()
        if self.identity_calls == 1:
            return replace(identity, dimension=None, digest=None)
        return identity


def test_identity_observed_after_embedding_is_persisted(tmp_path):
    embedder = _LateDimensionEmbedder()
    service, _ = make_service(tmp_path, embedder=embedder)
    collection = service.create_collection("demo")["collection_id"]
    result = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    version = service.get_index_version(result["index_version_id"])
    assert version["dimension"] == 8
    assert version["manifest"]["embedding"]["dimension"] == 8


def test_fingerprint_changes_with_extraction_version(tmp_path, monkeypatch):
    from knowledge_agent.sources.text_source import TextSourceAdapter

    service, _ = make_service(tmp_path)
    prepared = service._prepare_sources([_source(FIXTURES / "sample.txt")])
    chunker = service._resolve_chunker("fixed")
    identity = service._embedder.identity()
    baseline = service._fingerprint(prepared, chunker, identity)

    monkeypatch.setattr(TextSourceAdapter, "extraction_version", "text-v2")
    assert service._fingerprint(prepared, chunker, identity) != baseline


def test_fingerprint_changes_with_excluded_roles_policy(tmp_path):
    service_default, _ = make_service(tmp_path / "a")
    service_none, _ = make_service(tmp_path / "b", excluded_roles=())
    prepared = service_default._prepare_sources([_source(FIXTURES / "sample.txt")])
    chunker = service_default._resolve_chunker("fixed")
    identity = service_default._embedder.identity()
    assert service_default._fingerprint(
        prepared, chunker, identity
    ) != service_none._fingerprint(prepared, chunker, identity)


def test_changed_extraction_version_creates_new_index_version(tmp_path, monkeypatch):
    from knowledge_agent.sources.text_source import TextSourceAdapter

    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]
    first = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)

    monkeypatch.setattr(TextSourceAdapter, "extraction_version", "text-v2")
    second = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    assert second["reused"] is False
    assert second["index_version_id"] != first["index_version_id"]


def test_old_corpus_version_index_is_incompatible_and_not_reused(tmp_path):
    service, store = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]

    identity = service._embedder.identity().to_dict()
    identity["corpus_schema_version"] = "corpus-v1"
    old_id = store.create_index_version(
        collection, "fixed", "legacy-fingerprint", identity
    )
    store.finish_index(
        old_id,
        {"sources": 0, "documents": 0, "sections": 0, "chunks": 0},
        {},
        {},
        identity=identity,
    )
    service.set_active_index(collection, old_id)

    with pytest.raises(IndexIncompatible):
        service.search(collection, "memory")

    fresh = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    assert fresh["reused"] is False
    assert fresh["index_version_id"] != old_id


def test_fingerprint_changes_with_normalization_version(tmp_path):
    service, _ = make_service(tmp_path)
    prepared = service._prepare_sources([_source(FIXTURES / "sample.txt")])
    chunker = service._resolve_chunker("fixed")
    identity = service._embedder.identity()
    baseline = service._fingerprint(prepared, chunker, identity)

    service.normalization_version = "norm-v2"
    assert service._fingerprint(prepared, chunker, identity) != baseline


def test_processing_version_change_invalidates_dedup(tmp_path, monkeypatch):
    """E2E: bytes unchanged, processing version changes, dedup must not reuse."""
    from knowledge_agent.sources.text_source import TextSourceAdapter

    service, _ = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]

    first = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    reused = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    assert reused["reused"] is True
    assert reused["index_version_id"] == first["index_version_id"]

    # Old index built with the previous processing version.
    previous_version_index = first["index_version_id"]

    # Processing version changes (normalization + extraction), source bytes stay.
    monkeypatch.setattr(service, "normalization_version", "norm-v2")
    monkeypatch.setattr(TextSourceAdapter, "extraction_version", "text-v2")
    upgraded = service.build(
        collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True
    )
    assert upgraded["reused"] is False
    assert upgraded["index_version_id"] != previous_version_index

    # The same new processing version is now idempotent.
    stable = service.build(collection, [_source(FIXTURES / "sample.txt")], "fixed", wait=True)
    assert stable["reused"] is True
    assert stable["index_version_id"] == upgraded["index_version_id"]


def test_index_with_old_normalization_version_is_incompatible(tmp_path):
    service, store = make_service(tmp_path)
    collection = service.create_collection("demo")["collection_id"]

    identity = service._embedder.identity().to_dict()
    identity["corpus_schema_version"] = service.corpus_schema_version
    identity["normalization_version"] = "norm-v0"
    old_id = store.create_index_version(
        collection, "fixed", "old-normalization-fingerprint", identity
    )
    store.finish_index(
        old_id,
        {"sources": 0, "documents": 0, "sections": 0, "chunks": 0},
        {},
        {},
        identity=identity,
    )
    service.set_active_index(collection, old_id)

    with pytest.raises(IndexIncompatible):
        service.search(collection, "memory")
