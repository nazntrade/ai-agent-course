"""HTTP surface matches SPEC 11 and returns the shared error shape."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app

from tests.helpers import make_service

EXPECTED = {
    "/api/health": {"get"},
    "/api/collections": {"get", "post"},
    "/api/collections/{collection_id}/active-index": {"put"},
    "/api/collections/{collection_id}/index-versions": {"get"},
    "/api/index-versions/{index_version_id}": {"get"},
    "/api/index/build": {"post"},
    "/api/index-versions/{index_version_id}/chunks": {"get"},
    "/api/search": {"post"},
    "/api/compare": {"get"},
}


def _client(tmp_path) -> TestClient:
    service, _ = make_service(tmp_path)
    app = create_app(service, ui_dir=tmp_path / "no-ui")
    return TestClient(app)


def test_openapi_is_31_and_contains_expected_paths(tmp_path):
    schema = _client(tmp_path).get("/openapi.json").json()
    assert schema["openapi"].startswith("3.1")
    for path, methods in EXPECTED.items():
        assert path in schema["paths"], path
        for method in methods:
            assert method in schema["paths"][path], (path, method)


def test_health_shape(tmp_path):
    response = _client(tmp_path).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["embedding"]["reachable"] is True
    assert body["embedding"]["model"] == "fake-model"


def test_unknown_collection_returns_404_error_shape(tmp_path):
    response = _client(tmp_path).get("/api/collections/missing/index-versions")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_search_on_empty_collection_returns_409(tmp_path):
    client = _client(tmp_path)
    collection = client.post("/api/collections", json={"name": "demo"}).json()
    response = client.post(
        "/api/search",
        json={"collection_id": collection["collection_id"], "query": "hello"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "index_not_ready"


def test_unknown_strategy_returns_422(tmp_path):
    client = _client(tmp_path)
    collection = client.post("/api/collections", json={"name": "demo"}).json()
    response = client.post(
        "/api/index/build",
        json={
            "collection_id": collection["collection_id"],
            "sources": [{"path": "whatever.txt"}],
            "strategy": "nope",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_strategy"


def test_missing_source_file_returns_422(tmp_path):
    client = _client(tmp_path)
    collection = client.post("/api/collections", json={"name": "demo"}).json()
    response = client.post(
        "/api/index/build",
        json={
            "collection_id": collection["collection_id"],
            "sources": [{"path": str(tmp_path / "absent.txt")}],
            "strategy": "fixed",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_unreadable"


def test_validation_error_does_not_leak_absolute_paths(tmp_path):
    client = _client(tmp_path)
    # ``strategy`` is missing, so FastAPI returns a validation error; the request
    # body contains an absolute path that must not be echoed back (invariant I7).
    response = client.post(
        "/api/index/build",
        json={
            "collection_id": "c",
            "sources": [{"path": r"C:\Users\private\secret-corpus.pdf"}],
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    text = response.text
    assert "C:\\" not in text
    assert "private" not in text
    assert "secret-corpus" not in text
    for error in body["error"]["details"]["errors"]:
        assert set(error) <= {"type", "loc", "msg"}
        assert "input" not in error
        assert "ctx" not in error


def test_openapi_snapshot_has_no_drift(tmp_path):
    schema = _client(tmp_path).get("/openapi.json").json()
    snapshot = Path(__file__).resolve().parents[2] / "knowledge_agent" / "api" / "openapi.json"
    if os.environ.get("UPDATE_KNOWLEDGE_OPENAPI") == "1":
        snapshot.write_text(json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    assert json.loads(snapshot.read_text(encoding="utf-8")) == schema


def test_every_success_response_matches_spec_shapes(tmp_path):
    service, store = make_service(tmp_path)
    client = TestClient(create_app(service, ui_dir=tmp_path / "no-ui"))
    source = tmp_path / "source.md"
    source.write_text("# Memory\n\nAgents store observations and use memory. " * 20, encoding="utf-8")
    collection = client.post("/api/collections", json={"name": "contract"}).json()
    assert {"collection_id", "name", "active_index_version_id", "counts"} <= collection.keys()
    assert collection["active_index_version_id"] is None
    assert set(collection["counts"]) == {"sources", "documents", "sections", "chunks"}
    collection_id = collection["collection_id"]
    listing = client.get("/api/collections").json()
    assert set(listing) == {"collections"} and listing["collections"][0] == collection

    built = client.post("/api/index/build", json={
        "collection_id": collection_id, "sources": [{"path": str(source)}], "strategy": "fixed",
    })
    assert built.status_code == 200
    build = built.json()
    assert {"index_version_id", "status", "reused"} <= build.keys()
    assert build["reused"] is False and build["status"] in {"building", "ready"}
    version_id = build["index_version_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        version = client.get(f"/api/index-versions/{version_id}").json()
        if version["status"] != "building":
            break
        time.sleep(0.01)
    assert version["status"] == "ready"
    summary_fields = {
        "index_version_id", "collection_id", "strategy", "status", "fingerprint",
        "created_at", "started_at", "finished_at", "counts", "metrics", "error",
    }
    assert summary_fields | {"progress", "manifest"} <= version.keys()
    versions = client.get(f"/api/collections/{collection_id}/index-versions").json()
    assert set(versions) == {"index_versions"}
    assert summary_fields <= versions["index_versions"][0].keys()
    active = client.put(f"/api/collections/{collection_id}/active-index", json={"index_version_id": version_id})
    assert active.status_code == 200
    assert {"collection_id", "active_index_version_id"} <= active.json().keys()

    chunks = client.get(f"/api/index-versions/{version_id}/chunks?offset=0&limit=2").json()
    assert {"items", "total", "offset", "limit", "filters"} <= chunks.keys()
    assert chunks["items"] and chunks["total"] >= len(chunks["items"])
    assert {"chunk_id", "index_version_id", "text", "token_count", "char_count", "metadata"} <= chunks["items"][0].keys()
    metadata_fields = {
        "source_uri", "source_label", "document_id", "title", "section_path",
        "page_start", "page_end", "language", "content_sha256", "content_version",
    }
    assert metadata_fields <= chunks["items"][0]["metadata"].keys()
    search = client.post("/api/search", json={"collection_id": collection_id, "query": "memory", "top_k": 2})
    assert search.status_code == 200
    response = search.json()
    assert {"query", "collection_id", "index_version_id", "strategy", "top_k", "fragments", "counts", "metrics"} <= response.keys()
    assert response["fragments"] and set(response["counts"]) == {"indexed_chunks", "returned"}
    assert {"rank", "score", "chunk_id", "text", "metadata"} <= response["fragments"][0].keys()
    assert metadata_fields <= response["fragments"][0]["metadata"].keys()
    compare = client.get(f"/api/compare?collection_id={collection_id}").json()
    assert {"collection_id", "strategies"} <= compare.keys()
    assert {"strategy", "index_version_id", "status", "counts", "metrics"} <= compare["strategies"][0].keys()
    repeated = client.post("/api/index/build", json={
        "collection_id": collection_id, "sources": [{"path": str(source)}], "strategy": "fixed",
    }).json()
    assert repeated["reused"] is True and repeated["status"] == "ready"
    assert repeated["index_version_id"] == version_id
    store.close()


def test_error_contract_for_404_409_422_503_500(tmp_path, monkeypatch):
    service, store = make_service(tmp_path)
    client = TestClient(create_app(service, ui_dir=tmp_path / "no-ui"), raise_server_exceptions=False)
    collection = service.create_collection("errors")["collection_id"]
    responses = [
        client.get("/api/index-versions/missing"),
        client.post("/api/search", json={"collection_id": collection, "query": "empty"}),
        client.post("/api/collections", json={"name": ""}),
    ]
    source = tmp_path / "source.txt"
    source.write_text("Agent memory observations. " * 20, encoding="utf-8")
    service.build(collection, [{"path": str(source)}], "fixed", wait=True)
    monkeypatch.setattr(service._embedder, "_available", False)
    responses.append(client.post("/api/search", json={"collection_id": collection, "query": "memory"}))
    monkeypatch.setattr(service, "list_collections", lambda: (_ for _ in ()).throw(RuntimeError("private diagnostic")))
    responses.append(client.get("/api/collections"))
    assert [response.status_code for response in responses] == [404, 409, 422, 503, 500]
    assert [response.json()["error"]["code"] for response in responses] == [
        "not_found", "index_not_ready", "invalid_request", "embedding_unavailable", "internal_error",
    ]
    for response in responses:
        assert set(response.json()) == {"error"}
        assert {"code", "message"} <= response.json()["error"].keys()
        assert "private diagnostic" not in response.text
    store.close()


def test_chunks_pagination_reaches_every_item_beyond_first_fifty(tmp_path):
    service, store = make_service(tmp_path, chunk_size=5, overlap=0)
    source = tmp_path / "many.txt"
    source.write_text("memory observations tool planning action " * 70, encoding="utf-8")
    collection = service.create_collection("pagination")["collection_id"]
    version = service.build(collection, [{"path": str(source)}], "fixed", wait=True)["index_version_id"]
    client = TestClient(create_app(service, ui_dir=tmp_path / "no-ui"))
    first = client.get(f"/api/index-versions/{version}/chunks?limit=50&offset=0").json()
    second = client.get(f"/api/index-versions/{version}/chunks?limit=50&offset=50").json()
    assert len(first["items"]) == 50 and second["items"]
    combined = first["items"] + second["items"]
    assert len(combined) == first["total"] == second["total"]
    assert len({item["chunk_id"] for item in combined}) == len(combined)
    assert {item["chunk_id"] for item in combined} == {
        item["chunk_id"] for item in store.iter_chunk_vectors(version)
    }
    store.close()


def test_health_distinguishes_available_provider_from_absent_model(tmp_path, monkeypatch):
    service, store = make_service(tmp_path)
    monkeypatch.setattr(service._embedder, "preflight", lambda: {"reachable": True, "model_present": False})
    client = TestClient(create_app(service, ui_dir=tmp_path / "no-ui"))
    response = client.get("/api/health")
    assert response.status_code == 200
    health = response.json()
    assert health["status"] == "degraded"
    assert health["embedding"]["reachable"] is True
    assert health["embedding"]["model_present"] is False
    assert health["embedding"]["hint"] == "ollama pull fake-model"
    store.close()
