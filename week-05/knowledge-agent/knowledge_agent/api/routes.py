"""HTTP routes. Paths and payloads mirror SPEC 11 (the single source of truth)."""

from __future__ import annotations

import json
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Body, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..service.knowledge_service import KnowledgeService


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ActiveIndexBody(BaseModel):
    index_version_id: str


class SourceItem(BaseModel):
    path: str
    label: str | None = None


class BuildRequest(BaseModel):
    collection_id: str
    sources: list[SourceItem]
    strategy: str


class SearchRequest(BaseModel):
    collection_id: str
    index_version_id: str | None = None
    strategy: str | None = None
    query: str
    top_k: int = Field(default=5, ge=1, le=50)


class ChatRequest(BaseModel):
    collection_id: str | None = None
    index_version_id: str | None = None
    strategy: str | None = None
    mode: Literal["with_rag", "without_rag"]
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    max_context_tokens: int | None = Field(default=None, ge=1)
    save_run: bool = True


class CompareRequest(BaseModel):
    collection_id: str
    index_version_id: str | None = None
    strategy: str | None = None
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    max_context_tokens: int | None = Field(default=None, ge=1)
    save_run: bool = True


def create_router(service: KnowledgeService, chat_service: Any | None = None) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, Any]:
        payload = service.health()
        if chat_service is not None:
            payload = {**payload, "chat": chat_service.health()}
        else:
            payload = {
                **payload,
                "chat": {
                    "reachable": False,
                    "model_present": False,
                    "model": None,
                    "digest": None,
                    "context_length": None,
                    "hint": "chat is not configured",
                },
            }
        return payload

    @router.get("/collections")
    def list_collections() -> dict[str, Any]:
        return {"collections": service.list_collections()}

    @router.post("/collections")
    def create_collection(body: CollectionCreate) -> dict[str, Any]:
        return service.create_collection(body.name)

    @router.put("/collections/{collection_id}/active-index")
    def set_active_index(collection_id: str, body: ActiveIndexBody) -> dict[str, Any]:
        return service.set_active_index(collection_id, body.index_version_id)

    @router.get("/collections/{collection_id}/index-versions")
    def list_index_versions(collection_id: str) -> dict[str, Any]:
        return {"index_versions": service.list_index_versions(collection_id)}

    @router.get("/index-versions/{index_version_id}")
    def get_index_version(index_version_id: str) -> dict[str, Any]:
        version = service.get_index_version(index_version_id)
        return _summary(version) | {
            "progress": version.get("progress"),
            "manifest": version.get("manifest"),
        }

    @router.post("/index/build")
    def build_index(body: BuildRequest) -> dict[str, Any]:
        return service.build(
            body.collection_id,
            [item.model_dump() for item in body.sources],
            body.strategy,
        )

    @router.get("/index-versions/{index_version_id}/chunks")
    def list_chunks(
        index_version_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        document_id: str | None = None,
        section_path: str | None = None,
    ) -> dict[str, Any]:
        result = service.list_chunks(
            index_version_id, offset, limit, document_id, section_path
        )
        result["items"] = [
            {**item, "index_version_id": index_version_id} for item in result["items"]
        ]
        return result

    @router.post("/search")
    def search(body: SearchRequest) -> dict[str, Any]:
        return service.search(
            body.collection_id,
            body.query,
            top_k=body.top_k,
            index_version_id=body.index_version_id,
            strategy=body.strategy,
        )

    @router.get("/compare")
    def compare(
        collection_id: str,
        index_version_id: str | None = None,
        strategies: str = "fixed,structure",
    ) -> dict[str, Any]:
        wanted = [item.strip() for item in strategies.split(",") if item.strip()]
        return service.compare(collection_id, wanted, index_version_id)

    # -- D22 chat ---------------------------------------------------------
    @router.post("/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        return _require_chat(chat_service).chat(_chat_payload(body))

    @router.post("/chat/stream")
    def chat_stream(body: ChatRequest) -> StreamingResponse:
        chat = _require_chat(chat_service)
        # Resolve index/context before the response starts so typed errors keep
        # their HTTP status instead of becoming a mid-stream 200.
        plan = chat.prepare(_chat_payload(body))
        events = chat.stream_events(plan, body.save_run)
        return StreamingResponse(_sse(events), media_type="text/event-stream")

    @router.post("/chat/compare")
    def chat_compare(body: CompareRequest) -> dict[str, Any]:
        return _require_chat(chat_service).compare(_compare_payload(body))

    @router.get("/chat-runs")
    def list_chat_runs(
        limit: int = Query(default=20, ge=1, le=200),
        kind: Literal["single", "compare"] | None = None,
        mode: Literal["with_rag", "without_rag", "compare"] | None = None,
    ) -> dict[str, Any]:
        return _require_chat(chat_service).list_runs(limit=limit, kind=kind, mode=mode)

    @router.get("/chat-runs/{run_id}")
    def get_chat_run(run_id: str) -> dict[str, Any]:
        return _require_chat(chat_service).get_run(run_id)

    @router.get("/chat-runs/{run_id}/evaluation")
    def get_chat_evaluation(run_id: str) -> dict[str, Any]:
        return _require_chat(chat_service).get_evaluation(run_id)

    @router.put("/chat-runs/{run_id}/evaluation")
    def put_chat_evaluation(
        run_id: str, body: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        return _require_chat(chat_service).save_evaluation(run_id, body)

    return router


def _require_chat(chat_service: Any | None) -> Any:
    if chat_service is None:
        from ..domain.errors import ChatUnavailable

        raise ChatUnavailable("Chat generation is not configured.")
    return chat_service


def _chat_payload(body: ChatRequest) -> dict[str, Any]:
    return {
        "collection_id": body.collection_id,
        "index_version_id": body.index_version_id,
        "strategy": body.strategy,
        "mode": body.mode,
        "question": body.question,
        "top_k": body.top_k,
        "max_context_tokens": body.max_context_tokens,
        "save_run": body.save_run,
    }


def _compare_payload(body: CompareRequest) -> dict[str, Any]:
    return {
        "collection_id": body.collection_id,
        "index_version_id": body.index_version_id,
        "strategy": body.strategy,
        "question": body.question,
        "top_k": body.top_k,
        "max_context_tokens": body.max_context_tokens,
        "save_run": body.save_run,
    }


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        payload = json.dumps(event, ensure_ascii=False)
        yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"


def _summary(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "index_version_id": version.get("index_version_id"),
        "collection_id": version.get("collection_id"),
        "strategy": version.get("strategy"),
        "status": version.get("status"),
        "fingerprint": version.get("fingerprint"),
        "created_at": version.get("created_at"),
        "started_at": version.get("started_at"),
        "finished_at": version.get("finished_at"),
        "counts": version.get("counts"),
        "metrics": version.get("metrics"),
        "error": version.get("error"),
    }
