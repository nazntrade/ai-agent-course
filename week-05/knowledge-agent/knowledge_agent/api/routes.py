"""HTTP routes. Paths and payloads mirror SPEC 11 (the single source of truth)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
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


def create_router(service: KnowledgeService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, Any]:
        return service.health()

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

    return router


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
