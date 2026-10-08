"""HTTP routes (SPEC 8.1)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field

from ..config import PROVIDER_EXTERNAL, PROVIDER_LOCAL, PROVIDER_NETWORK
from ..errors import DialogueNotFound, InvalidRequest, ProviderError


class ProviderSelect(BaseModel):
    provider: Literal["local", "network", "external"]
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
    provider: Literal["local", "network", "external"] | None = None
    use_rewrite: bool = False
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1, le=50)


class DemoDocument(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100000)


class ModelSelect(BaseModel):
    model_id: str = Field(min_length=1, max_length=500)


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
        provider: str | None = Query(default=None, pattern="^(local|network|external)$"),
    ) -> dict[str, Any]:
        return service.search(query, top_k=top_k, min_score=min_score, use_rewrite=use_rewrite, provider=provider)

    @router.get("/rag/compare")
    def rag_compare(
        query: str = Query(min_length=1, max_length=2000),
        top_k: int = Query(default=3, ge=1, le=50),
        min_score: float | None = Query(default=None, ge=0.0, le=1.0),
        use_rewrite: bool = Query(default=False),
        provider: str | None = Query(default=None, pattern="^(local|network|external)$"),
    ) -> dict[str, Any]:
        return service.compare(query, top_k=top_k, min_score=min_score, use_rewrite=use_rewrite, provider=provider)

    @router.get("/mcp/connection-point")
    def mcp_connection_point() -> dict[str, Any]:
        from ..mcp.connection_point import describe_connection_point

        return describe_connection_point()

    # -- D28: external profile & model selection -------------------------

    @router.get("/external-profile")
    def external_profile() -> dict[str, Any]:
        active = service.external_profile_active
        info = service.external_profile_info if active else {}
        return {
            "active": active,
            "missing": service.settings.external_profile_errors(),
            "info": info,
        }

    @router.get("/models")
    def list_models() -> dict[str, Any]:
        """List .gguf files from the configured model directory (D28).

        When network (DeepSeek) configuration is available, DeepSeek model
        variants are appended so the UI can offer them.
        """
        model_dir = Path(service.settings.gguf_dir)
        models: list[dict[str, Any]] = []
        if model_dir.is_dir():
            for p in sorted(model_dir.rglob("*.gguf")):
                if p.name.lower().startswith("mmproj"):
                    continue
                models.append({
                    "name": p.relative_to(model_dir).as_posix(),
                    "path": str(p),
                    "size": p.stat().st_size,
                    "source": "gguf",
                })
        # C05: add DeepSeek variants when network config is available.
        if not service.settings.missing_network_config():
            for name in service.settings.deepseek_models or (service.settings.deepseek_model_id,):
                models.append({"name": name, "source": "deepseek", "size": 0})
        return {"models": models}

    @router.post("/models/select")
    def select_model_api(body: ModelSelect) -> dict[str, Any]:
        """Select a GGUF model by name (only when no external profile active)."""
        if service.external_profile_active:
            raise ProviderError(
                "Model selection is controlled by the external profile.",
            )
        return service.select_model(body.model_id)

    @router.get("/rag/collections")
    def rag_collections() -> dict[str, Any]:
        """Return collection info from the week-05 index (D28)."""
        index_path = service.settings.week05_index_path
        try:
            from ..rag.external_index import ExternalIndex
            idx = ExternalIndex(index_path)
            collections = idx.list_collections()
            schema = idx.inspect_schema()
            idx.close()
            return {
                "collections": collections,
                "dimension": schema.get("dimension", 0),
                "total_chunks": schema.get("chunks", 0),
            }
        except Exception as exc:  # noqa: BLE001
            return {"collections": [], "error": str(exc), "dimension": 0, "total_chunks": 0}

    return router
