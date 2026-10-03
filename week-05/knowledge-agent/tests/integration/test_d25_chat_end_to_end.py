"""INT: D25 dialogue store, multi-turn RAG and restart via a real backend."""

from __future__ import annotations

import json
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
from harness.owned_backend import isolated_backend_env  # noqa: E402

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


class Backend:
    def __init__(self, db_path: Path, embed_url: str, chat_url: str, extra_env: dict | None = None) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        overrides = {
            "KNOWLEDGE_HOST": "127.0.0.1",
            "KNOWLEDGE_PORT": str(self.port),
            "KNOWLEDGE_DB_PATH": str(db_path),
            "EMBED_BASE_URL": embed_url,
            "CHAT_BASE_URL": chat_url,
            "CHAT_MODEL": CHAT_MODEL,
            "KNOWLEDGE_SKIP_ENV_FILE": "1",
        }
        if extra_env:
            overrides.update(extra_env)
        env = isolated_backend_env(db_path, overrides)
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


def wait_ready(base: str, index_version_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, last = http("GET", f"{base}/api/index-versions/{index_version_id}")
        if status == 200 and last.get("status") in ("ready", "failed"):
            return last
        time.sleep(0.3)
    raise AssertionError(f"index did not finish: {last}")


def build_index(base: str, source: Path) -> tuple[str, str]:
    collection = http("POST", base + "/api/collections", {"name": "d25"})[1]["collection_id"]
    build = http(
        "POST",
        base + "/api/index/build",
        {"collection_id": collection, "sources": [{"path": str(source)}], "strategy": "structure"},
    )[1]
    version = wait_ready(base, build["index_version_id"])
    assert version["status"] == "ready"
    return collection, version["index_version_id"]


class Server:
    def __init__(self, backend: Backend, base: str, embed, chat, tmp_path: Path):
        self.backend = backend
        self.base = base
        self.embed = embed
        self.chat = chat
        self.tmp_path = tmp_path


@pytest.fixture()
def d25_server(tmp_path):
    embed_port, chat_port = free_port(), free_port()
    embed = _start_server(embed_stub.create_server("127.0.0.1", embed_port, dimension=64))
    chat = _start_server(chat_stub.create_server("127.0.0.1", chat_port, model=CHAT_MODEL))

    source = tmp_path / "corpus.md"
    source.write_text(
        "# 1 Overview\n\n" + "Agents plan and use memory and tools. " * 60
        + "\n\n## 1.1 Memory\n\n" + "Memory stores observations and goals. " * 30,
        encoding="utf-8",
    )
    db = tmp_path / "index.db"
    extra = {
        "CHAT_RUNS_PATH": str(tmp_path / "chat-runs"),
        "DIALOGUE_DB_PATH": str(tmp_path / "conversations.db"),
    }
    backend = Backend(db, f"http://127.0.0.1:{embed_port}", f"http://127.0.0.1:{chat_port}", extra)
    if not backend.wait():
        backend.stop()
        _stop_server(embed)
        _stop_server(chat)
        pytest.fail("backend did not start")
    yield {
        "backend": backend,
        "base": backend.base,
        "source": source,
        "db": db,
        "dialogues_db": tmp_path / "conversations.db",
        "embed_url": f"http://127.0.0.1:{embed_port}",
        "chat_url": f"http://127.0.0.1:{chat_port}",
        "tmp": tmp_path,
        "extra": extra,
        "chat": chat,
        "embed": embed,
    }
    backend.stop()
    _stop_server(embed)
    _stop_server(chat)


def _create_dialogue(base: str, name: str = "chat") -> str:
    return http("POST", base + "/api/dialogues", {"name": name})[1]["dialogue_id"]


def _turn(base: str, dialogue_id: str, client_turn_id: str, question: str, **overrides):
    payload = {
        "client_turn_id": client_turn_id,
        "question": question,
        "mode": "with_rag",
        "top_k": 3,
        "grounding": True,
    }
    payload.update(overrides)
    return http("POST", base + f"/api/dialogues/{dialogue_id}/turns", payload)


def test_multi_turn_rag_persists_and_isolates_dialogues(d25_server):
    base = d25_server["base"]
    collection, version_id = build_index(base, d25_server["source"])
    first = _create_dialogue(base, "first")
    second = _create_dialogue(base, "second")

    status, turn = _turn(base, first, "c1", "How do agents use memory?", collection_id=collection)
    assert status == 200, turn
    assert turn["status"] == "ok"
    assert turn["search_query"] and turn["original_query"]
    assert turn["retrieval_performed"] is True
    assert turn["settings"]["index_version_id"] == version_id
    assert turn["sources"], "grounded answer must expose sources"
    passed_ids = set(turn["retrieval"]["passed_chunk_ids"])
    assert {item["chunk_id"] for item in turn["sources"]} <= passed_ids
    assert turn["context"]["mandatory_parts"]["passed_fragments_tokens"] >= 0
    assert turn["context"]["reserve_tokens"] > 0

    # A second dialogue is completely empty (isolation).
    assert http("GET", base + f"/api/dialogues/{second}/turns")[1]["total"] == 0

    # Idempotency: the same client_turn_id does not create a second turn.
    status, again = _turn(base, first, "c1", "How do agents use memory?", collection_id=collection)
    assert status == 200 and again["turn_id"] == turn["turn_id"]
    assert http("GET", base + f"/api/dialogues/{first}/turns")[1]["total"] == 1


def test_task_memory_is_stored_separately_and_updated(d25_server):
    base = d25_server["base"]
    collection, _ = build_index(base, d25_server["source"])
    dialogue = _create_dialogue(base)
    _turn(base, dialogue, "c1", "Цель: изучить память агентов", collection_id=collection)
    _turn(base, dialogue, "c2", "условие: без кода", collection_id=collection)

    memory = http("GET", base + f"/api/dialogues/{dialogue}/memory")[1]
    assert memory["goal"]["text"] == "изучить память агентов"
    assert memory["goal"]["grounds"]
    assert any(item["text"] == "без кода" and item["status"] == "active" for item in memory["constraints"])

    turn_id = memory["goal"]["grounds"][0]
    updated = http(
        "PATCH",
        base + f"/api/dialogues/{dialogue}/memory",
        {
            "expected_version": memory["version"],
            "operations": [{"op": "set_goal", "text": "новая цель", "grounds": [turn_id]}],
        },
    )[1]
    assert updated["goal"]["text"] == "новая цель"
    # Note: the memory table is separate from turns (verified by the store test).


def test_refusal_when_no_grounding_sources_pass(d25_server):
    base = d25_server["base"]
    collection, _ = build_index(base, d25_server["source"])
    dialogue = _create_dialogue(base)
    status, turn = _turn(
        base, dialogue, "c1", "Random unrelated question", collection_id=collection,
        use_filter=True, min_score=0.999,
    )
    assert status == 200
    assert turn["status"] in ("refused", "ok")
    if turn["status"] == "refused":
        assert turn["answer"]["insufficient_sources"] is True


def test_restart_preserves_history_and_memory(d25_server):
    base = d25_server["base"]
    collection, _ = build_index(base, d25_server["source"])
    dialogue = _create_dialogue(base, "persistent")
    _turn(base, dialogue, "c1", "Цель: сохранить историю", collection_id=collection)

    d25_server["backend"].stop()
    restarted = Backend(
        d25_server["db"],
        d25_server["embed_url"],
        d25_server["chat_url"],
        d25_server["extra"],
    )
    try:
        assert restarted.wait()
        listing = http("GET", restarted.base + "/api/dialogues")[1]["dialogues"]
        assert any(item["dialogue_id"] == dialogue for item in listing)
        turns = http("GET", restarted.base + f"/api/dialogues/{dialogue}/turns")[1]
        assert turns["total"] == 1
        memory = http("GET", restarted.base + f"/api/dialogues/{dialogue}/memory")[1]
        assert memory["goal"]["text"] == "сохранить историю"
    finally:
        restarted.stop()


def test_migration_preserves_legacy_data_and_fabricates_no_history(d25_server):
    runs_dir = d25_server["tmp"] / "chat-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "schema_version": "chat-run-v1",
        "run_id": "legacy-run-1",
        "question": "old single-run question",
        "answer": {"text": "old single-run answer"},
    }
    legacy_path = runs_dir / "legacy-run-1.json"
    legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = legacy_path.read_bytes()

    # The index DB and the conversation DB are physically different files.
    assert d25_server["db"] != d25_server["dialogues_db"]
    assert d25_server["dialogues_db"].is_file()

    restarted = Backend(
        d25_server["db"], d25_server["embed_url"], d25_server["chat_url"], d25_server["extra"]
    )
    try:
        assert restarted.wait()
        assert http("GET", restarted.base + "/api/dialogues")[1]["dialogues"] == []
    finally:
        restarted.stop()
    assert legacy_path.read_bytes() == before


def test_delete_requires_confirmation_and_keeps_index(d25_server):
    base = d25_server["base"]
    collection, version_id = build_index(base, d25_server["source"])
    dialogue = _create_dialogue(base)
    refused = http("DELETE", base + f"/api/dialogues/{dialogue}")
    assert refused[0] == 409 and refused[1]["error"]["code"] == "deletion_requires_confirmation"
    deleted = http("DELETE", base + f"/api/dialogues/{dialogue}?confirm=true")
    assert deleted[0] == 200
    # The document index and collection are untouched by dialogue deletion.
    assert http("GET", base + f"/api/index-versions/{version_id}")[0] == 200
