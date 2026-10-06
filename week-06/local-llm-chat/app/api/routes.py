"""HTTP routes (SPEC 8.1)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field

from ..config import PROVIDER_LOCAL, PROVIDER_NETWORK
from ..errors import DialogueNotFound, InvalidRequest


class ProviderSelect(BaseModel):
    provider: Literal["local", "network"]
    dialogue_id: str | None = None


class DialogueCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)


class DialogueRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MemoryPatch(BaseModel):
    expected_version: int
    memory: dict[str, Any]


class AskRequest(BaseModel):
    dialogue_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    rag_enabled: bool = False
    provider: Literal["local", "network"] | None = None
    use_rewrite: bool = False
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1, le=50)


class DemoDocument(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100000)


def create_router(service: Any) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, Any]:
        return service.health()

    @router.get("/provider")
    def get_provider() -> dict[str, Any]:
        return service.provider_state()

    @router.post("/provider")
    def select_provider(body: ProviderSelect) -> dict[str, Any]:
        return service.select_provider(body.provider, dialogue_id=body.dialogue_id)

    @router.post("/local/load")
    def load_local() -> dict[str, Any]:
        return service.ensure_local_ready()

    @router.post("/local/unload")
    def unload_local() -> dict[str, Any]:
        return service.unload_local()

    @router.get("/dialogues")
    def list_dialogues() -> dict[str, Any]:
        return {"dialogues": service.dialogues.list_dialogues()}

    @router.post("/dialogues")
    def create_dialogue(body: DialogueCreate | None = None) -> dict[str, Any]:
        return service.dialogues.create_dialogue(body.name if body else None)

    @router.get("/dialogues/{dialogue_id}")
    def get_dialogue(dialogue_id: str) -> dict[str, Any]:
        dialogue = service.dialogues.get_dialogue(dialogue_id)
        dialogue["messages"] = service.dialogues.list_messages(dialogue_id)
        dialogue["memory"] = service.dialogues.get_memory(dialogue_id)
        return dialogue

    @router.patch("/dialogues/{dialogue_id}")
    def rename_dialogue(dialogue_id: str, body: DialogueRename) -> dict[str, Any]:
        return service.dialogues.rename_dialogue(dialogue_id, body.name)

    @router.delete("/dialogues/{dialogue_id}")
    def delete_dialogue(dialogue_id: str) -> dict[str, Any]:
        service.dialogues.delete_dialogue(dialogue_id)
        return {"deleted": dialogue_id}

    @router.get("/dialogues/{dialogue_id}/memory")
    def get_memory(dialogue_id: str) -> dict[str, Any]:
        return service.dialogues.get_memory(dialogue_id)

    @router.patch("/dialogues/{dialogue_id}/memory")
    def patch_memory(dialogue_id: str, body: MemoryPatch) -> dict[str, Any]:
        return service.dialogues.save_memory(
            dialogue_id, body.memory, expected_version=body.expected_version
        )

    @router.post("/ask")
    def ask(body: AskRequest) -> dict[str, Any]:
        return service.ask(
            body.dialogue_id,
            body.question,
            rag_enabled=body.rag_enabled,
            provider=body.provider,
            use_rewrite=body.use_rewrite,
            min_score=body.min_score,
            top_k=body.top_k,
        )

    @router.post("/rag/documents")
    def add_document(body: DemoDocument) -> dict[str, Any]:
        return service.add_document(body.label, body.text)

    @router.get("/rag/search")
    def rag_search(
        query: str = Query(min_length=1, max_length=2000),
        top_k: int = Query(default=5, ge=1, le=50),
        min_score: float | None = Query(default=None, ge=0.0, le=1.0),
        use_rewrite: bool = Query(default=False),
        provider: str | None = Query(default=None, pattern="^(local|network)$"),
    ) -> dict[str, Any]:
        return service.search(query, top_k=top_k, min_score=min_score, use_rewrite=use_rewrite, provider=provider)

    @router.get("/rag/compare")
    def rag_compare(
        query: str = Query(min_length=1, max_length=2000),
        top_k: int = Query(default=3, ge=1, le=50),
        min_score: float | None = Query(default=None, ge=0.0, le=1.0),
        use_rewrite: bool = Query(default=False),
        provider: str | None = Query(default=None, pattern="^(local|network)$"),
    ) -> dict[str, Any]:
        return service.compare(query, top_k=top_k, min_score=min_score, use_rewrite=use_rewrite, provider=provider)

    @router.get("/mcp/connection-point")
    def mcp_connection_point() -> dict[str, Any]:
        from ..mcp.connection_point import describe_connection_point

        return describe_connection_point()

    return router
