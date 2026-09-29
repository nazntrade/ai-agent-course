"""INT: real backend process + local stub over real HTTP (D21-05..D21-10)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent.parent
HARNESS_DIR = MODULE_DIR / "harness"
for extra in (str(MODULE_DIR), str(HARNESS_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import embed_stub  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (json.loads(body.decode("utf-8")) if body else {})


class Backend:
    def __init__(self, db_path: Path, embed_url: str, extra_env: dict | None = None) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env.update(
            {
                "KNOWLEDGE_HOST": "127.0.0.1",
                "KNOWLEDGE_PORT": str(self.port),
                "KNOWLEDGE_DB_PATH": str(db_path),
                "EMBED_BASE_URL": embed_url,
            }
        )
        if extra_env:
            env.update(extra_env)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "knowledge_agent"],
            cwd=str(MODULE_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait(self, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                status, _ = http("GET", self.base + "/api/health")
                if status == 200:
                    return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture()
def server(tmp_path):
    stub_port = free_port()
    stub = embed_stub.create_server("127.0.0.1", stub_port, dimension=64)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    source = tmp_path / "corpus.md"
    source.write_text(
        "# 1 Overview\n\n" + "Agents plan and use memory and tools. " * 60
        + "\n\n## 1.1 Memory\n\n" + "Memory stores observations. " * 30,
        encoding="utf-8",
    )
    other = tmp_path / "other.txt"
    other.write_text("Isolated second source. " * 40, encoding="utf-8")

    backend = Backend(tmp_path / "index.db", f"http://127.0.0.1:{stub_port}")
    if not backend.wait():
        backend.stop()
        stub.shutdown()
        stub.server_close()
        pytest.fail("backend did not start")
    yield {
        "base": backend.base,
        "source": source,
        "other": other,
        "db": tmp_path / "index.db",
        "stub_url": f"http://127.0.0.1:{stub_port}",
    }
    backend.stop()
    stub.shutdown()
    stub.server_close()


def wait_ready(base: str, index_version_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, last = http("GET", f"{base}/api/index-versions/{index_version_id}")
        if status == 200 and last.get("status") in ("ready", "failed"):
            return last
        time.sleep(0.3)
    raise AssertionError(f"index did not finish: {last}")


def build(base: str, collection_id: str, path: Path, strategy: str) -> dict:
    status, payload = http(
        "POST",
        f"{base}/api/index/build",
        {
            "collection_id": collection_id,
            "sources": [{"path": str(path)}],
            "strategy": strategy,
        },
    )
    assert status == 200, payload
    return payload


def test_full_cycle_two_collections_dedup_and_search(server):
    base = server["base"]
    collection_a = http("POST", base + "/api/collections", {"name": "a"})[1]["collection_id"]
    collection_b = http("POST", base + "/api/collections", {"name": "b"})[1]["collection_id"]

    fixed_a = wait_ready(base, build(base, collection_a, server["source"], "fixed")["index_version_id"])
    structure_a = wait_ready(
        base, build(base, collection_a, server["source"], "structure")["index_version_id"]
    )
    fixed_b = wait_ready(base, build(base, collection_b, server["other"], "fixed")["index_version_id"])
    assert fixed_a["status"] == "ready"
    assert structure_a["status"] == "ready"

    # Dedup: unchanged reimport reuses the ready index.
    rebuild = build(base, collection_a, server["source"], "fixed")
    assert rebuild["reused"] is True
    assert rebuild["index_version_id"] == fixed_a["index_version_id"]

    # Active index switching is only a parameter change.
    status, _ = http(
        "PUT",
        f"{base}/api/collections/{collection_a}/active-index",
        {"index_version_id": structure_a["index_version_id"]},
    )
    assert status == 200

    status, search = http(
        "POST",
        base + "/api/search",
        {"collection_id": collection_a, "query": "memory planning", "top_k": 3},
    )
    assert status == 200
    assert search["fragments"]
    assert search["index_version_id"] == structure_a["index_version_id"]

    status, search_b = http(
        "POST", base + "/api/search", {"collection_id": collection_b, "query": "isolated", "top_k": 2}
    )
    assert status == 200
    assert search_b["index_version_id"] == fixed_b["index_version_id"]

    status, chunks = http(
        "GET", f"{base}/api/index-versions/{structure_a['index_version_id']}/chunks?limit=1&offset=0"
    )
    assert status == 200 and len(chunks["items"]) == 1
    assert chunks["total"] >= 1
    assert chunks["limit"] == 1 and chunks["offset"] == 0

    status, compare = http("GET", f"{base}/api/compare?collection_id={collection_a}")
    assert status == 200
    assert {row["strategy"] for row in compare["strategies"]} == {"fixed", "structure"}


def test_bad_source_returns_422(server):
    base = server["base"]
    collection = http("POST", base + "/api/collections", {"name": "bad"})[1]["collection_id"]
    broken = Path(server["source"]).parent / "broken.pdf"
    broken.write_bytes(b"this is definitely not a pdf")
    status, payload = http(
        "POST",
        base + "/api/index/build",
        {
            "collection_id": collection,
            "sources": [{"path": str(broken)}],
            "strategy": "fixed",
        },
    )
    assert status == 422
    assert payload["error"]["code"] in {"source_invalid", "source_unreadable"}

    # The service is still alive.
    status, _ = http("GET", base + "/api/health")
    assert status == 200


def test_missing_strategy_index_returns_409(server):
    base = server["base"]
    collection = http("POST", base + "/api/collections", {"name": "empty"})[1]["collection_id"]
    status, payload = http(
        "POST",
        base + "/api/search",
        {"collection_id": collection, "strategy": "fixed", "query": "nothing"},
    )
    assert status == 409
    assert payload["error"]["code"] == "index_not_ready"


def test_incompatible_identity_after_restart_returns_409(server):
    base = server["base"]
    collection = http("POST", base + "/api/collections", {"name": "compat"})[1]["collection_id"]
    ready = wait_ready(base, build(base, collection, server["source"], "fixed")["index_version_id"])
    assert ready["status"] == "ready"

    # Restart the backend on the same database with a changed query prefix.
    restarted = Backend(
        server["db"],
        server["stub_url"],
        extra_env={"EMBED_QUERY_PREFIX": "q: "},
    )
    try:
        assert restarted.wait()
        status, payload = http(
            "POST",
            restarted.base + "/api/search",
            {"collection_id": collection, "query": "memory", "top_k": 2},
        )
        assert status == 409, payload
        assert payload["error"]["code"] == "index_incompatible"
        assert "expected" in payload["error"]["details"]
        assert "actual" in payload["error"]["details"]
    finally:
        restarted.stop()
