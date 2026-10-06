"""D26-16: API paths for rewrite, min_score filter, compare and citations."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import load_settings
from app.dialogues.store import SqliteDialogueStore
from app.providers.base import AnswerProvider, ChatResult, ChatUsage, ProviderStatus
from app.rag.store import RagStore
from app.service import ChatService


class StubEmbedder:
    """Deterministic bag-of-keywords embedding; no network."""

    def identity(self):
        return {"provider": "stub", "model": "stub-embed"}

    def _vector(self, text):
        text = text.lower()
        vector = [0.0, 0.0, 0.0]
        if "paris" in text or "france" in text or "capital" in text:
            vector[0] = 1.0
        if "banana" in text or "fruit" in text:
            vector[1] = 1.0
        if "orion" in text or "code" in text:
            vector[2] = 1.0
        if not any(vector):
            vector[0] = 1.0
        norm = sum(v * v for v in vector) ** 0.5
        return [v / norm for v in vector]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


class StubProvider(AnswerProvider):
    name = "local"

    def __init__(self, text="answer [1]"):
        self._text = text

    def identity(self):
        return {"provider": self.name, "model": "gemma.gguf"}

    def is_configured(self):
        return True

    def missing_config(self):
        return []

    def status(self):
        return ProviderStatus(provider=self.name, reachable=True, model="gemma.gguf")

    def chat(self, messages, options=None):
        return ChatResult(text=self._text, model="gemma.gguf", finish_reason="stop", usage=ChatUsage(1, 1, 2), latency_ms=1.0)


class StubRewriter:
    def __init__(self, search_query="paris capital france"):
        self._search_query = search_query

    def rewrite(self, question):
        from app.rag.rewrite import RewriteResult

        return RewriteResult(
            original_query=question,
            search_query=self._search_query,
            attempted=True,
            used=True,
            fallback=False,
        )


class StubGemma:
    state = "unloaded"

    def ensure_started(self):
        self.state = "ready"
        return self.state_snapshot()

    def stop(self):
        self.state = "unloaded"
        return self.state_snapshot()

    unload = stop

    def shutdown(self):
        pass

    def set_generating(self, value):
        pass

    def state_snapshot(self):
        return {"state": self.state, "pid": None, "port": 8791, "model": "gemma.gguf", "error": None}


def make_client(tmp_path):
    settings = load_settings({"DIALOGUE_DB_PATH": str(tmp_path / "conversations.db")})
    store = SqliteDialogueStore(settings.dialogue_db_path)
    rag = RagStore(str(tmp_path / "index.db"))
    service = ChatService(
        settings,
        local_provider=StubProvider("Answer [1] The capital of France is Paris."),
        network_provider=StubProvider("network answer"),
        gemma_manager=StubGemma(),
        dialogue_store=store,
        rag_store=rag,
        embedder=StubEmbedder(),
        query_rewriter=StubRewriter(),
    )
    client = TestClient(create_app(service))
    # Index two distinct documents.
    client.post("/api/rag/documents", json={"label": "geo.txt", "text": "The capital of France is Paris."})
    client.post("/api/rag/documents", json={"label": "fruit.txt", "text": "Bananas are yellow fruit."})
    return client, service


def test_search_endpoint_returns_results_with_scores(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.get("/api/rag/search", params={"query": "capital of France"})
    assert response.status_code == 200
    body = response.json()
    assert body["results"]
    assert body["results"][0]["score"] >= body["results"][-1]["score"]
    assert "geo.txt" in body["results"][0]["label"]


def test_min_score_filter_excludes_low_scores(tmp_path):
    client, _ = make_client(tmp_path)
    loose = client.get("/api/rag/search", params={"query": "capital of France", "top_k": 5}).json()["results"]
    strict = client.get("/api/rag/search", params={"query": "capital of France", "top_k": 5, "min_score": 0.9}).json()["results"]
    assert all(item["score"] >= 0.9 for item in strict)
    assert len(strict) <= len(loose)


def test_rewrite_endpoint_changes_search_query(tmp_path):
    client, _ = make_client(tmp_path)
    body = client.get("/api/rag/search", params={"query": "Where?", "use_rewrite": "true"}).json()
    assert body["search_query"] == "paris capital france"
    assert body["rewrite"]["used"] is True


def test_compare_endpoint_returns_filtered_and_unfiltered(tmp_path):
    client, _ = make_client(tmp_path)
    body = client.get("/api/rag/compare", params={"query": "capital of France", "top_k": 5, "min_score": 0.9}).json()
    assert "unfiltered" in body and "filtered" in body
    assert body["min_score"] == 0.9
    assert all(item["score"] >= 0.9 for item in body["filtered"])


def test_ask_rag_returns_rewrite_and_citation_report(tmp_path):
    client, service = make_client(tmp_path)
    dialogue = client.post("/api/dialogues", json={}).json()
    response = client.post("/api/ask", json={
        "dialogue_id": dialogue["dialogue_id"],
        "question": "What is the capital of France?",
        "rag_enabled": True,
        "use_rewrite": True,
        "min_score": 0.5,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["rag"]["enabled"] is True
    assert body["rag"]["sources"]
    assert body["rag"]["rewrite"]["used"] is True
    assert body["rag"]["citations"] is not None
    # The inline `[1]` maps to the first passed fragment (geo.txt).
    assert body["rag"]["citations"]["inline_unsupported"] == []


def test_ask_no_rag_returns_no_sources_or_citations(tmp_path):
    client, _ = make_client(tmp_path)
    dialogue = client.post("/api/dialogues", json={}).json()
    body = client.post("/api/ask", json={
        "dialogue_id": dialogue["dialogue_id"],
        "question": "What is the capital of France?",
        "rag_enabled": False,
    }).json()
    assert body["rag"]["sources"] == []
    assert body["rag"]["citations"] is None
