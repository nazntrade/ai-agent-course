"""D21 provenance, processing identity, comparison and public-path regressions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app
from knowledge_agent.sources.text_source import TextSourceAdapter
from tests.helpers import make_service


def _build(service, collection, sources, strategy="fixed"):
    return service.build(collection, sources, strategy, wait=True)["index_version_id"]


def _sources(path, label=None):
    return [{"path": str(path), **({"label": label} if label else {})}]


def test_equal_bytes_keep_each_source_uri_and_collection_provenance(tmp_path):
    service, store = make_service(tmp_path)
    first = tmp_path / "first" / "corpus.md"
    second = tmp_path / "second" / "corpus.md"
    for path in (first, second):
        path.parent.mkdir()
        path.write_text("# Memory\n\nRemember observations. " * 20, encoding="utf-8")
    collection_a = service.create_collection("a")["collection_id"]
    collection_b = service.create_collection("b")["collection_id"]
    version_a = _build(service, collection_a, _sources(first, "first source"))
    version_b = _build(service, collection_b, _sources(second, "second source"))
    with sqlite3.connect(store.db_path) as connection:
        saved = connection.execute("SELECT uri, label FROM sources ORDER BY label").fetchall()
    assert saved == [(str(first.resolve()), "first source"), (str(second.resolve()), "second source")]
    chunks_a = service.list_chunks(version_a)["items"]
    chunks_b = service.list_chunks(version_b)["items"]
    assert {item["metadata"]["source_label"] for item in chunks_a} == {"first source"}
    assert {item["metadata"]["source_label"] for item in chunks_b} == {"second source"}
    assert {item["metadata"]["document_id"] for item in chunks_a}.isdisjoint(
        item["metadata"]["document_id"] for item in chunks_b
    )


def test_equal_bytes_txt_and_markdown_do_not_reuse_different_parses(tmp_path):
    service, store = make_service(tmp_path)
    plain = tmp_path / "corpus.txt"
    markdown = tmp_path / "corpus.md"
    text = "# First\n\nFirst body. " * 10 + "\n\n# Second\n\nSecond body. " * 10
    plain.write_text(text, encoding="utf-8")
    markdown.write_text(text, encoding="utf-8")
    collection = service.create_collection("formats")["collection_id"]
    plain_id = _build(service, collection, _sources(plain))
    result = service.build(collection, _sources(markdown), "fixed", wait=True)
    assert result["reused"] is False
    markdown_id = result["index_version_id"]
    assert markdown_id != plain_id
    assert {item["metadata"]["section_path"] for item in service.list_chunks(plain_id)["items"]} == {"Document"}
    assert any(item["metadata"]["section_path"] != "Document" for item in service.list_chunks(markdown_id)["items"])
    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute("SELECT document_id, extraction_version FROM documents").fetchall()
    assert len({row[0] for row in rows}) == 2
    assert {row[1] for row in rows} == {"text-v1:plain", "text-v1:markdown"}


def test_extraction_bump_records_new_documents_and_keeps_ready_index(tmp_path, monkeypatch):
    service, store = make_service(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("# Memory\n\nAgent memory stores observations. " * 20, encoding="utf-8")
    collection = service.create_collection("versions")["collection_id"]
    old_id = _build(service, collection, _sources(source))
    old_chunks = service.list_chunks(old_id)["items"]
    monkeypatch.setattr(TextSourceAdapter, "extraction_version", "text-v2")
    new_id = _build(service, collection, _sources(source))
    assert new_id != old_id
    assert service.get_index_version(new_id)["manifest"]["pipeline"]["extraction_versions"] == ["text-v2:markdown"]
    assert service.list_chunks(old_id)["items"] == old_chunks
    assert service.get_index_version(old_id)["status"] == "ready"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sources", "documents", "sections", "chunks", "index_versions")}
    assert service.build(collection, _sources(source), "fixed", wait=True)["reused"] is True
    with sqlite3.connect(store.db_path) as connection:
        after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    assert after == before


@pytest.mark.parametrize("label", [r"C:\private\source.md", "/private/source.md", r"\\server\private\source.md", "source\n.md"])
def test_public_label_is_sanitized_for_build_and_error_payloads(tmp_path, label):
    service, store = make_service(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("# Heading\n\nPublic source content. " * 10, encoding="utf-8")
    client = TestClient(create_app(service, ui_dir=tmp_path / "no-ui"))
    collection = service.create_collection("paths")["collection_id"]
    version = _build(service, collection, _sources(source, label))
    response = client.get(f"/api/index-versions/{version}")
    serialized = json.dumps(response.json())
    assert "private" not in serialized
    assert "server" not in serialized
    assert str(tmp_path) not in serialized
    assert "\\n" not in response.json()["manifest"]["sources"][0]["label"]
    missing = client.post("/api/index/build", json={
        "collection_id": collection, "sources": _sources(tmp_path / "absent.md", label), "strategy": "fixed",
    })
    assert missing.status_code == 422
    assert "private" not in missing.text and "server" not in missing.text
    store.close()


@pytest.mark.parametrize("change", ["extraction", "exclusions", "order"])
def test_compare_rejects_processing_or_order_changes(tmp_path, monkeypatch, change):
    service, _ = make_service(tmp_path)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n\nFirst text. " * 20, encoding="utf-8")
    second.write_text("# Second\n\nSecond text. " * 20, encoding="utf-8")
    sources = _sources(first) + _sources(second)
    collection = service.create_collection("comparison")["collection_id"]
    _build(service, collection, sources, "fixed")
    if change == "extraction":
        monkeypatch.setattr(TextSourceAdapter, "extraction_version", "text-v2")
    elif change == "exclusions":
        service.excluded_roles = ()
    else:
        sources.reverse()
    _build(service, collection, sources, "structure")
    result = service.compare(collection, ["fixed", "structure"])
    assert result["comparable"] is False


def test_equal_cleaned_corpus_with_different_public_labels_is_comparable(tmp_path):
    service, _ = make_service(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("# Memory\n\nMemory stores observations. " * 20, encoding="utf-8")
    collection = service.create_collection("labels")["collection_id"]
    _build(service, collection, _sources(source, "first label"), "fixed")
    _build(service, collection, _sources(source, "second label"), "structure")
    assert service.compare(collection, ["fixed", "structure"])["comparable"] is True


def test_changed_exclusion_policy_never_adds_sections_to_old_ready_document(tmp_path):
    service, store = make_service(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("# Body\n\nBody content. " * 10 + "\n\n# References\n\nCitations. " * 10, encoding="utf-8")
    collection = service.create_collection("policies")["collection_id"]
    old_id = _build(service, collection, _sources(source), "fixed")
    old_rows = store.count_rows(old_id)
    old_chunks = service.list_chunks(old_id)["items"]
    service.excluded_roles = ()
    new_id = _build(service, collection, _sources(source), "structure")
    assert store.count_rows(new_id)["sections"] > old_rows["sections"]
    assert store.count_rows(old_id) == old_rows
    assert service.list_chunks(old_id)["items"] == old_chunks


@pytest.mark.parametrize("legacy_path", [r"C:\private\retained.md", "/private/retained.md", r"\\private-server\private\retained.md"])
def test_legacy_ready_public_projection_is_safe_without_changing_database(tmp_path, legacy_path):
    service, store = make_service(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("# Memory\n\nRetained agent memory observations. " * 20, encoding="utf-8")
    collection = service.create_collection("retained")["collection_id"]
    version_id = _build(service, collection, _sources(source))
    original_chunks = service.list_chunks(version_id)["items"]
    chunk_ids = {chunk["chunk_id"] for chunk in original_chunks}

    # Emulate an old ready database whose prior build stored raw public labels.
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE sources SET public_uri = ?, label = ?", (legacy_path, legacy_path))
        connection.execute("UPDATE documents SET title = ?", (legacy_path,))
        metadata_rows = connection.execute("SELECT chunk_id, metadata_json FROM chunks").fetchall()
        for chunk_id, serialized in metadata_rows:
            metadata = json.loads(serialized)
            metadata.update(source_uri=legacy_path, source_label=legacy_path, title=legacy_path)
            connection.execute("UPDATE chunks SET metadata_json = ? WHERE chunk_id = ?",
                               (json.dumps(metadata), chunk_id))
        manifest = json.loads(connection.execute(
            "SELECT manifest_json FROM index_versions WHERE index_version_id = ?", (version_id,)
        ).fetchone()[0])
        manifest.pop("cleaned_corpus_sha256", None)
        manifest.pop("processing", None)
        for entry in manifest["sources"]:
            entry["label"] = legacy_path
        connection.execute("UPDATE index_versions SET manifest_json = ? WHERE index_version_id = ?",
                           (json.dumps(manifest), version_id))
    readonly_uri = f"file:{Path(store.db_path).as_posix()}?mode=ro"
    with sqlite3.connect(readonly_uri, uri=True) as connection:
        before = tuple(connection.iterdump())

    client = TestClient(create_app(service, ui_dir=tmp_path / "no-ui"))
    chunks = client.get(f"/api/index-versions/{version_id}/chunks")
    detail = client.get(f"/api/index-versions/{version_id}")
    versions = client.get(f"/api/collections/{collection}/index-versions")
    search = client.post("/api/search", json={
        "collection_id": collection, "index_version_id": version_id, "query": "memory", "top_k": 2,
    })
    for response in (chunks, detail, versions, search):
        assert response.status_code == 200
        assert "private" not in response.text
        assert "C:" not in response.text
    assert detail.json()["status"] == "ready"
    assert detail.json()["manifest"]["sources"][0]["label"] == "retained.md"
    assert versions.json()["index_versions"][0]["manifest"]["sources"][0]["label"] == "retained.md"
    assert {item["chunk_id"] for item in chunks.json()["items"]} == chunk_ids
    assert search.json()["index_version_id"] == version_id and search.json()["fragments"]
    for item in chunks.json()["items"] + search.json()["fragments"]:
        assert {item["metadata"][key] for key in ("source_uri", "source_label", "title")} == {"retained.md"}
    with sqlite3.connect(readonly_uri, uri=True) as connection:
        assert tuple(connection.iterdump()) == before
        assert connection.execute("SELECT uri FROM sources").fetchone()[0] == str(source.resolve())
        assert connection.execute("SELECT public_uri FROM sources").fetchone()[0] == legacy_path
    store.close()
