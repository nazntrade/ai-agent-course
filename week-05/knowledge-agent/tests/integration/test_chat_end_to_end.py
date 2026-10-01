"""INT: real backend + embed/chat stubs over loopback HTTP (D22-01..14)."""

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

import chat_stub  # noqa: E402
import embed_stub  # noqa: E402

CHAT_MODEL = "stub-chat:latest"


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
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (json.loads(body.decode("utf-8")) if body else {})


def post_stream(url: str, payload: dict) -> list[dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8", "replace")
    events = []
    for frame in body.split("\n\n"):
        line = next((item for item in frame.splitlines() if item.startswith("data:")), None)
        if line:
            events.append(json.loads(line[5:].strip()))
    return events


class Backend:
    def __init__(self, db_path: Path, embed_url: str, chat_url: str, extra_env: dict | None = None) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env.update(
            {
                "KNOWLEDGE_HOST": "127.0.0.1",
                "KNOWLEDGE_PORT": str(self.port),
                "KNOWLEDGE_DB_PATH": str(db_path),
                "EMBED_BASE_URL": embed_url,
                "CHAT_BASE_URL": chat_url,
                "CHAT_MODEL": CHAT_MODEL,
                "KNOWLEDGE_SKIP_ENV_FILE": "1",
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
            self.process.wait(timeout=10)


def _start_server(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop_server(server):
    server.shutdown()
    server.server_close()


@pytest.fixture()
def chat_server(tmp_path):
    embed_port, chat_port = free_port(), free_port()
    embed = _start_server(embed_stub.create_server("127.0.0.1", embed_port, dimension=64))
    chat = _start_server(chat_stub.create_server("127.0.0.1", chat_port, model=CHAT_MODEL))

    source = tmp_path / "corpus.md"
    source.write_text(
        "# 1 Overview\n\n" + "Agents plan and use memory and tools. " * 60
        + "\n\n## 1.1 Memory\n\n" + "Memory stores observations. " * 30,
        encoding="utf-8",
    )
    db = tmp_path / "index.db"
    backend = Backend(
        db,
        f"http://127.0.0.1:{embed_port}",
        f"http://127.0.0.1:{chat_port}",
        extra_env={"CHAT_RUNS_PATH": str(tmp_path / "chat-runs")},
    )
    if not backend.wait():
        backend.stop()
        _stop_server(embed)
        _stop_server(chat)
        pytest.fail("backend did not start")
    yield {
        "base": backend.base,
        "source": source,
        "db": db,
        "embed_url": f"http://127.0.0.1:{embed_port}",
        "chat_url": f"http://127.0.0.1:{chat_port}",
    }
    backend.stop()
    _stop_server(embed)
    _stop_server(chat)


def wait_ready(base: str, index_version_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, last = http("GET", f"{base}/api/index-versions/{index_version_id}")
        if status == 200 and last.get("status") in ("ready", "failed"):
            return last
        time.sleep(0.3)
    raise AssertionError(f"index did not finish: {last}")


def _build(server, name: str) -> tuple[str, str]:
    base = server["base"]
    collection = http("POST", base + "/api/collections", {"name": name})[1]["collection_id"]
    build = http(
        "POST",
        base + "/api/index/build",
        {"collection_id": collection, "sources": [{"path": str(server["source"])}], "strategy": "fixed"},
    )[1]
    version = wait_ready(base, build["index_version_id"])
    assert version["status"] == "ready"
    return collection, version["index_version_id"]


def test_without_and_with_rag_stream_and_runs(chat_server):
    base = chat_server["base"]
    collection, version_id = _build(chat_server, "chat")

    status, plain = http(
        "POST", base + "/api/chat", {"mode": "without_rag", "question": "Memory?", "top_k": 3}
    )
    assert status == 200, plain
    assert plain["answer"]["text"]
    assert plain["retrieval"] is None and plain["index"] is None

    status, rag = http(
        "POST",
        base + "/api/chat",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory and planning?", "top_k": 3},
    )
    assert status == 200, rag
    assert rag["index"]["index_version_id"] == version_id
    assert rag["retrieval"]["passed_count"] <= rag["retrieval"]["found_count"]
    assert rag["answer"]["citations"]["valid"], "the stub cites the passed chunk id"

    events = post_stream(
        base + "/api/chat/stream",
        {"mode": "with_rag", "collection_id": collection, "question": "Memory?", "top_k": 3},
    )
    kinds = [event["type"] for event in events]
    assert kinds[0] == "start" and "sources" in kinds and kinds[-1] == "done"
    done = next(event for event in events if event["type"] == "done")
    assert done["answer"]["run_id"] == events[0]["run_id"]

    status, runs = http("GET", base + "/api/chat-runs?limit=10")
    assert status == 200 and runs["runs"]
    run_id = done["answer"]["run_id"]
    status, saved = http("GET", base + f"/api/chat-runs/{run_id}")
    assert status == 200 and saved["run_id"] == run_id


def test_compare_pins_one_index_and_keeps_histories_separate(chat_server):
    base = chat_server["base"]
    collection, version_id = _build(chat_server, "compare")
    status, record = http(
        "POST", base + "/api/chat/compare", {"collection_id": collection, "question": "Memory?", "top_k": 3}
    )
    assert status == 200, record
    assert set(record["branches"]) == {"with_rag", "without_rag"}
    assert record["comparison"]["index_version_id"] == version_id
    assert record["comparison"]["same_model"] is True
    assert record["comparison"]["prompt_templates"]["with_rag"] == "rag-v1"
    assert record["branches"]["with_rag"]["retrieval"] is not None
    assert record["branches"]["without_rag"]["retrieval"] is None


def test_with_rag_without_ready_index_returns_409(chat_server):
    base = chat_server["base"]
    empty = http("POST", base + "/api/collections", {"name": "empty"})[1]["collection_id"]
    status, payload = http(
        "POST", base + "/api/chat", {"mode": "with_rag", "collection_id": empty, "question": "q"}
    )
    assert status == 409
    assert payload["error"]["code"] == "index_not_ready"
    status, payload = http(
        "POST", base + "/api/chat/compare", {"collection_id": empty, "question": "q"}
    )
    assert status == 409


def test_without_rag_works_with_empty_database(chat_server):
    base = chat_server["base"]
    status, record = http("POST", base + "/api/chat", {"mode": "without_rag", "question": "anything"})
    assert status == 200 and record["answer"]["text"]


def test_incompatible_embedding_identity_returns_409_before_chat(chat_server):
    base = chat_server["base"]
    collection, _version = _build(chat_server, "compat")
    restarted = Backend(
        chat_server["db"],
        chat_server["embed_url"],
        chat_server["chat_url"],
        extra_env={"EMBED_QUERY_PREFIX": "q: "},
    )
    try:
        assert restarted.wait()
        status, payload = http(
            "POST",
            restarted.base + "/api/chat",
            {"mode": "with_rag", "collection_id": collection, "question": "memory"},
        )
        assert status == 409, payload
        assert payload["error"]["code"] == "index_incompatible"
    finally:
        restarted.stop()
        base = base


def test_missing_chat_model_from_stub_returns_503(tmp_path):
    embed_port, chat_port = free_port(), free_port()
    embed = _start_server(embed_stub.create_server("127.0.0.1", embed_port, dimension=64))
    chat = _start_server(
        chat_stub.create_server("127.0.0.1", chat_port, model="absent:latest", fail_mode="missing_model")
    )
    backend = Backend(
        tmp_path / "index.db",
        f"http://127.0.0.1:{embed_port}",
        f"http://127.0.0.1:{chat_port}",
        extra_env={"CHAT_MODEL": "absent:latest", "CHAT_RUNS_PATH": str(tmp_path / "runs")},
    )
    try:
        assert backend.wait()
        status, payload = http(
            "POST", backend.base + "/api/chat", {"mode": "without_rag", "question": "q"}
        )
        assert status == 503
        assert payload["error"]["code"] == "chat_model_missing"
        assert payload["error"]["details"]["hint"] == "ollama pull absent:latest"
    finally:
        backend.stop()
        _stop_server(embed)
        _stop_server(chat)
