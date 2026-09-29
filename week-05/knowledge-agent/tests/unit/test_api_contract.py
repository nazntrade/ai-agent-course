"""HTTP surface matches SPEC 11 and returns the shared error shape."""

from __future__ import annotations

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
