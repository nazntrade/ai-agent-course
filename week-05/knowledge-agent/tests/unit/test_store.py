"""SqliteIndexStore: schema, WAL, activation, dedup, failure paths (SPEC 9)."""

from __future__ import annotations

import pytest

from knowledge_agent.chunking.fixed import FixedChunker
from knowledge_agent.domain.contracts import CORPUS_SCHEMA_VERSION
from knowledge_agent.domain.errors import IndexNotReady, StoreSchemaUnsupported
from knowledge_agent.storage.sqlite_store import SqliteIndexStore
from knowledge_agent.text.normalize import NORMALIZATION_VERSION
from knowledge_agent.text.tokenizer import LexicalTokenizer

from tests.helpers import make_document


def _chunks(count: int = 3):
    document = make_document(" ".join(f"word{i}" for i in range(count * 20)))
    return FixedChunker(LexicalTokenizer(), chunk_size=20, overlap=5).chunk(document)


def _identity():
    return {
        "provider": "fake",
        "base_url": "http://fake",
        "endpoint_version": "0",
        "api": "/api/embed",
        "model": "fake-model",
        "digest": "fake-digest",
        "dimension": 8,
        "dtype": "float32",
        "normalization": "l2",
        "normalization_version": NORMALIZATION_VERSION,
        "document_prefix": "",
        "query_prefix": "",
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
    }


def _ready(store: SqliteIndexStore, collection_id: str, fingerprint: str = "fp-1") -> str:
    index_version_id = store.create_index_version(
        collection_id, "fixed", fingerprint, _identity()
    )
    store.insert_chunks(index_version_id, _chunks())
    store.finish_index(
        index_version_id,
        {"sources": 1, "documents": 1, "sections": 1, "chunks": len(_chunks())},
        {"build_seconds": 0.1},
        {"manifest_schema_version": 1},
    )
    return index_version_id


def test_wal_and_foreign_keys_enabled(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    foreign_keys = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert journal.lower() == "wal"
    assert foreign_keys == 1


def test_collection_and_active_index_lifecycle(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    collection = store.create_collection("demo")
    collection_id = collection["collection_id"]
    index_version_id = _ready(store, collection_id)

    with pytest.raises(IndexNotReady):
        building = store.create_index_version(collection_id, "fixed", "fp-2", _identity())
        store.set_active_index(collection_id, building)

    store.set_active_index(collection_id, index_version_id)
    active = store.get_active_index(collection_id)
    assert active["index_version_id"] == index_version_id
    assert store.get_collection(collection_id)["counts"]["chunks"] == len(_chunks())


def test_insert_or_ignore_deduplicates_chunks(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    collection_id = store.create_collection("demo")["collection_id"]
    index_version_id = store.create_index_version(collection_id, "fixed", "fp", _identity())
    chunks = _chunks()
    first = store.insert_chunks(index_version_id, chunks)
    second = store.insert_chunks(index_version_id, chunks)
    assert first == len(chunks)
    assert second == 0
    assert store.count_rows(index_version_id)["chunks"] == len(chunks)


def test_fail_index_removes_partial_chunks(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    collection_id = store.create_collection("demo")["collection_id"]
    index_version_id = store.create_index_version(collection_id, "fixed", "fp", _identity())
    store.insert_chunks(index_version_id, _chunks())
    store.fail_index(index_version_id, "embedding_unavailable")
    version = store.get_index_version(index_version_id)
    assert version["status"] == "failed"
    assert version["error"] == "embedding_unavailable"
    assert store.count_rows(index_version_id)["chunks"] == 0


def test_mark_stale_builds_failed_keeps_ready(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    collection_id = store.create_collection("demo")["collection_id"]
    ready = _ready(store, collection_id)
    store.set_active_index(collection_id, ready)
    stale = store.create_index_version(collection_id, "fixed", "stale", _identity())
    store.insert_chunks(stale, _chunks(1))

    count = store.mark_stale_builds_failed("interrupted")
    assert count == 1
    assert store.get_index_version(stale)["status"] == "failed"
    assert store.get_index_version(stale)["error"] == "interrupted"
    assert store.count_rows(stale)["chunks"] == 0
    assert store.get_index_version(ready)["status"] == "ready"
    assert store.get_collection(collection_id)["active_index_version_id"] == ready


def test_unknown_schema_version_is_rejected(tmp_path):
    path = tmp_path / "index.db"
    SqliteIndexStore(path)
    with pytest.raises(StoreSchemaUnsupported):
        SqliteIndexStore(path, schema_version=999)


def test_garbage_schema_version_is_rejected_not_internal_error(tmp_path):
    path = tmp_path / "index.db"
    store = SqliteIndexStore(path)
    store._conn.execute(
        "UPDATE schema_meta SET value = 'not-a-number' WHERE key = 'schema_version'"
    )
    store._conn.commit()
    store.close()
    with pytest.raises(StoreSchemaUnsupported):
        SqliteIndexStore(path)


def test_existing_db_without_normalization_version_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    store = SqliteIndexStore(path)
    store._conn.execute("ALTER TABLE index_versions DROP COLUMN normalization_version")
    store._conn.commit()
    store.close()

    upgraded = SqliteIndexStore(path)  # no manual DB removal required
    columns = {
        row["name"]
        for row in upgraded._conn.execute("PRAGMA table_info(index_versions)")
    }
    assert "normalization_version" in columns


def test_find_ready_index_and_list(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    collection_id = store.create_collection("demo")["collection_id"]
    ready = _ready(store, collection_id, "fp-x")
    assert store.find_ready_index(collection_id, "fp-x")["index_version_id"] == ready
    assert store.find_ready_index(collection_id, "missing") is None
    assert len(store.list_index_versions(collection_id)) == 1


def test_list_chunks_pagination_and_section_filter(tmp_path):
    store = SqliteIndexStore(tmp_path / "index.db")
    collection_id = store.create_collection("demo")["collection_id"]
    index_version_id = _ready(store, collection_id)
    items, total = store.list_chunks(index_version_id, offset=0, limit=2)
    assert total >= 2
    assert len(items) == 2
    filtered, filtered_total = store.list_chunks(
        index_version_id, section_path="Document"
    )
    assert filtered_total == total
    none_items, none_total = store.list_chunks(index_version_id, section_path="Nope")
    assert none_total == 0 and none_items == []
