"""KnowledgeService: build/search/compare orchestration over abstract adapters.

The service knows the domain contracts only; it never imports SQLite, Ollama or
PDF libraries (SPEC 5).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..domain.contracts import (
    CORPUS_SCHEMA_VERSION,
    Chunker,
    Embedder,
    IndexStore,
    SourceResolver,
    Tokenizer,
    check_index_compatibility,
    identity_for_compatibility,
)
from ..domain.errors import (
    EmbeddingUnavailable,
    IndexBusy,
    IndexNotReady,
    KnowledgeError,
    SourceEmpty,
    SourceUnreadable,
)
from ..domain.models import Chunk, Document, SourceRef
from ..text.normalize import NORMALIZATION_DETAIL, NORMALIZATION_VERSION

MANIFEST_SCHEMA_VERSION = 1
EXCLUDED_ROLES = ("references",)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvalidStrategy(KnowledgeError):
    code = "invalid_strategy"
    http_status = 422


class KnowledgeService:
    def __init__(
        self,
        store: IndexStore,
        embedder: Embedder,
        tokenizer: Tokenizer,
        chunkers: Mapping[str, Chunker],
        resolver: SourceResolver,
        *,
        corpus_schema_version: str = CORPUS_SCHEMA_VERSION,
        excluded_roles: Sequence[str] = EXCLUDED_ROLES,
        normalization_version: str = NORMALIZATION_VERSION,
        batch_size: int = 16,
        embed_timeout_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._tokenizer = tokenizer
        self._chunkers = dict(chunkers)
        self._resolver = resolver
        self.corpus_schema_version = corpus_schema_version
        self.excluded_roles = tuple(excluded_roles)
        self.normalization_version = normalization_version
        self.batch_size = max(1, batch_size)
        self._build_lock = threading.Lock()

    # -- collections ------------------------------------------------------
    def list_collections(self) -> list[dict[str, Any]]:
        return self._store.list_collections()

    def create_collection(self, name: str) -> dict[str, Any]:
        return self._store.create_collection(name)

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        collection = self._store.get_collection(collection_id)
        if collection is None:
            raise KeyError(collection_id)
        return collection

    def set_active_index(self, collection_id: str, index_version_id: str) -> dict[str, Any]:
        if self._store.get_collection(collection_id) is None:
            raise KeyError(collection_id)
        return self._store.set_active_index(collection_id, index_version_id)

    def list_index_versions(self, collection_id: str) -> list[dict[str, Any]]:
        if self._store.get_collection(collection_id) is None:
            raise KeyError(collection_id)
        return [_public_index_version(version) for version in self._store.list_index_versions(collection_id)]

    def get_index_version(self, index_version_id: str) -> dict[str, Any]:
        version = self._store.get_index_version(index_version_id)
        if version is None:
            raise KeyError(index_version_id)
        return _public_index_version(version)

    def mark_stale_builds_failed(self, reason: str = "interrupted") -> int:
        return self._store.mark_stale_builds_failed(reason)

    # -- build ------------------------------------------------------------
    def build(
        self,
        collection_id: str,
        sources: Sequence[Mapping[str, Any]],
        strategy: str,
        *,
        wait: bool = False,
    ) -> dict[str, Any]:
        if self._store.get_collection(collection_id) is None:
            raise KeyError(collection_id)
        chunker = self._resolve_chunker(strategy)

        if not self._build_lock.acquire(blocking=False):
            raise IndexBusy("Another index build is already running.")
        handed_off = False
        released = False
        index_version_id: str | None = None
        try:
            prepared = self._prepare_sources(sources)
            identity = self._embedder.identity()
            fingerprint = self._fingerprint(prepared, chunker, identity)

            existing = self._store.find_ready_index(collection_id, fingerprint)
            if existing is not None:
                # First ready index of a collection becomes active automatically.
                collection = self._store.get_collection(collection_id)
                if collection and not collection.get("active_index_version_id"):
                    self._store.set_active_index(
                        collection_id, existing["index_version_id"]
                    )
                return {
                    "index_version_id": existing["index_version_id"],
                    "status": "ready",
                    "reused": True,
                    "strategy": strategy,
                    "counts": existing.get("counts"),
                }

            identity_dict = identity.to_dict()
            identity_dict["corpus_schema_version"] = self.corpus_schema_version
            identity_dict["normalization_version"] = self.normalization_version
            index_version_id = self._store.create_index_version(
                collection_id, strategy, fingerprint, identity_dict
            )
            self._store.update_progress(
                index_version_id, {"stage": "parsing", "percent": 5}
            )
            started = time.perf_counter()
            documents, chunks, excluded_roles = self._parse(
                prepared, chunker, index_version_id
            )
            parse_seconds = time.perf_counter() - started

            if wait:
                try:
                    self._finish_build(
                        collection_id,
                        index_version_id,
                        documents,
                        chunks,
                        parse_seconds,
                        excluded_roles,
                    )
                except Exception as exc:  # noqa: BLE001 - persisted as the index error code
                    self._mark_failed(index_version_id, exc)
                    raise
                finally:
                    self._build_lock.release()
                    released = True
                version = self._store.get_index_version(index_version_id)
                return {
                    "index_version_id": index_version_id,
                    "status": version["status"] if version else "building",
                    "reused": False,
                    "strategy": strategy,
                }

            worker = threading.Thread(
                target=self._finish_build_guarded,
                args=(
                    collection_id,
                    index_version_id,
                    documents,
                    chunks,
                    parse_seconds,
                    excluded_roles,
                ),
                name="knowledge-build",
                daemon=True,
            )
            worker.start()
            handed_off = True
            return {
                "index_version_id": index_version_id,
                "status": "building",
                "reused": False,
                "strategy": strategy,
            }
        except Exception as exc:  # noqa: BLE001 - translated for the API layer
            if index_version_id is not None and not released and not handed_off:
                self._mark_failed(index_version_id, exc)
            raise
        finally:
            if not handed_off and not released:
                self._build_lock.release()

    def _resolve_chunker(self, strategy: str) -> Chunker:
        chunker = self._chunkers.get(strategy)
        if chunker is None:
            raise InvalidStrategy(f"Unknown chunking strategy: {strategy!r}.")
        return chunker

    def _prepare_sources(
        self, sources: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if not sources:
            raise SourceEmpty("No sources were provided for indexing.")
        prepared: list[dict[str, Any]] = []
        for item in sources:
            path = item.get("path")
            if not path:
                raise SourceEmpty("A source entry is missing an explicit path.")
            file_path = Path(str(path)).resolve()
            label = _public_label(item.get("label") or str(path))
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                raise SourceUnreadable(
                    f"Source could not be read: {label}."
                ) from exc
            if not data:
                raise SourceEmpty(f"Source is empty: {label}.")
            adapter = self._resolver.resolve(str(file_path))
            content_sha256 = hashlib.sha256(data).hexdigest()
            source_ref = SourceRef(
                source_id=_digest({
                    "version": "source-v2",
                    "uri": os.path.normcase(str(file_path)),
                    "kind": adapter.kind,
                    "content_sha256": content_sha256,
                    "public_label": label,
                }),
                uri=str(file_path),
                public_uri=str(label),
                label=str(label),
                kind=adapter.kind,
                content_sha256=content_sha256,
                size_bytes=len(data),
            )
            prepared.append(
                {"adapter": adapter, "source_ref": source_ref, "label": str(label)}
            )
        return prepared

    def _parse(
        self,
        prepared: Sequence[Mapping[str, Any]],
        chunker: Chunker,
        index_version_id: str,
    ) -> tuple[list[Document], list[Chunk], list[str]]:
        documents: list[Document] = []
        chunks: list[Chunk] = []
        excluded: set[str] = set()
        total = len(prepared)
        for ordinal, item in enumerate(prepared):
            document = item["adapter"].extract(item["source_ref"])
            kept_sections = []
            for section in document.sections:
                role = (section.role or "body").lower()
                if role in self.excluded_roles:
                    excluded.add(role)
                    continue
                kept_sections.append(section)
            if len(kept_sections) != len(document.sections):
                document.sections = kept_sections
                document.extraction.warnings.append("excluded_roles:" + ",".join(sorted(excluded)))
            if not document.sections:
                raise SourceEmpty(
                    f"Source has no indexable sections after exclusions: {item['label']}."
                )
            # Parsed documents are immutable across normalization/exclusion policies.
            processing = {
                "normalization": self.normalization_version,
                "excluded_roles": sorted(self.excluded_roles),
            }
            document.document_id = _digest({
                "adapter_document_id": document.document_id,
                "processing": processing,
            })[:32]
            for section in document.sections:
                section.section_id = _digest({
                    "adapter_section_id": section.section_id,
                    "document_id": document.document_id,
                })[:32]
            documents.append(document)
            self._store.save_document(index_version_id, document, ordinal)
            chunks.extend(chunker.chunk(document))
            self._store.update_progress(
                index_version_id,
                {
                    "stage": "parsing",
                    "sources_total": total,
                    "sources_done": ordinal + 1,
                    "chunks_total": len(chunks),
                    "percent": 5 + int(35 * (ordinal + 1) / total),
                },
            )
        if not chunks:
            raise SourceEmpty("The selected sources produced no indexable text.")
        return documents, chunks, sorted(excluded)

    def _finish_build_guarded(
        self,
        collection_id: str,
        index_version_id: str,
        documents: list[Document],
        chunks: list[Chunk],
        parse_seconds: float,
        excluded_roles: list[str],
    ) -> None:
        try:
            self._finish_build(
                collection_id,
                index_version_id,
                documents,
                chunks,
                parse_seconds,
                excluded_roles,
            )
        except Exception as exc:  # noqa: BLE001 - persisted as the index error code
            self._mark_failed(index_version_id, exc)
        finally:
            self._build_lock.release()

    def _finish_build(
        self,
        collection_id: str,
        index_version_id: str,
        documents: list[Document],
        chunks: list[Chunk],
        parse_seconds: float,
        excluded_roles: list[str],
    ) -> None:
        version = self._store.get_index_version(index_version_id) or {}

        if not self._embedder.is_available():
            raise EmbeddingUnavailable(
                "The local embedding provider is not reachable."
            )

        total = len(chunks)
        self._store.update_progress(
            index_version_id,
            {"stage": "embedding", "chunks_total": total, "chunks_embedded": 0, "percent": 40},
        )
        started = time.perf_counter()
        latencies: list[float] = []
        input_tokens: int | None = 0
        embedded = 0
        for start in range(0, total, self.batch_size):
            batch = chunks[start : start + self.batch_size]
            result = self._embedder.embed_documents([c.text for c in batch])
            if result.input_tokens is None:
                input_tokens = None
            elif input_tokens is not None:
                input_tokens += result.input_tokens
            for chunk, vector in zip(batch, result.vectors):
                chunk.vector = vector
            if result.latency_ms is not None:
                latencies.append(result.latency_ms)
            latencies.extend(result.latencies_ms)
            embedded += len(batch)
            self._store.update_progress(
                index_version_id,
                {
                    "stage": "embedding",
                    "chunks_total": total,
                    "chunks_embedded": embedded,
                    "percent": 40 + int(50 * embedded / total),
                },
            )
        embed_seconds = time.perf_counter() - started

        # Identity after a successful embedding carries the observed dimension
        # and digest even if a later preflight probe fails.
        identity = self._embedder.identity()

        self._store.update_progress(
            index_version_id,
            {"stage": "storing", "chunks_total": total, "chunks_embedded": total, "percent": 92},
        )
        store_started = time.perf_counter()
        inserted = self._store.insert_chunks(index_version_id, chunks)
        store_seconds = time.perf_counter() - store_started

        counts = self._store.count_rows(index_version_id)
        build_seconds = parse_seconds + embed_seconds + store_seconds
        unique_tokens = sum(
            len(self._tokenizer.tokenize(section.text))
            for document in documents
            for section in document.sections
        )
        metrics = self._build_metrics(
            chunks=chunks,
            unique_tokens=unique_tokens,
            parse_seconds=parse_seconds,
            embed_seconds=embed_seconds,
            store_seconds=store_seconds,
            build_seconds=build_seconds,
            latencies=latencies,
            input_tokens=input_tokens,
        )
        metrics["db_size_bytes"] = self._store.db_size_bytes()
        dimension = identity.dimension or (len(chunks[0].vector) if chunks and chunks[0].vector else 0)
        metrics["vector_bytes"] = counts["chunks"] * dimension * 4

        manifest = self._build_manifest(
            collection_id=collection_id,
            index_version_id=index_version_id,
            version=version,
            identity=identity,
            documents=documents,
            counts=counts,
            metrics=metrics,
            chunks=chunks,
            excluded_roles=excluded_roles,
            finished_at=None,
        )
        finished_at = _utcnow()
        manifest["finished_at"] = finished_at
        identity_dict = identity.to_dict()
        identity_dict["corpus_schema_version"] = self.corpus_schema_version
        identity_dict["normalization_version"] = self.normalization_version
        self._store.finish_index(
            index_version_id,
            counts,
            metrics,
            manifest,
            finished_at=finished_at,
            identity=identity_dict,
        )
        # First ready index of a collection becomes active automatically.
        collection = self._store.get_collection(collection_id)
        if collection and not collection.get("active_index_version_id"):
            try:
                self._store.set_active_index(collection_id, index_version_id)
            except Exception:  # noqa: BLE001 - activation is best effort here
                pass

    def _mark_failed(self, index_version_id: str, exc: Exception) -> None:
        code = getattr(exc, "code", "internal_error")
        try:
            self._store.fail_index(index_version_id, code)
        except Exception:  # noqa: BLE001 - best effort while failing a build
            pass

    # -- search -----------------------------------------------------------
    def search(
        self,
        collection_id: str,
        query: str,
        top_k: int = 5,
        index_version_id: str | None = None,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        if self._store.get_collection(collection_id) is None:
            raise KeyError(collection_id)
        version = self._resolve_search_version(collection_id, index_version_id, strategy)
        if version is None or version.get("status") != "ready":
            raise IndexNotReady(
                "No ready index is available for this collection.",
                details={"collection_id": collection_id, "strategy": strategy},
            )

        if not self._embedder.is_available():
            raise EmbeddingUnavailable("The local embedding provider is not reachable.")

        expected = identity_for_compatibility(version)
        actual = self._embedder.identity().to_dict()
        actual["corpus_schema_version"] = self.corpus_schema_version
        actual["normalization_version"] = self.normalization_version
        check_index_compatibility(expected, actual)

        query_result = self._embedder.embed_query(query)
        query_vector = query_result.vectors[0]
        if len(query_vector) != (version.get("dimension") or len(query_vector)):
            raise KnowledgeError(
                "Query embedding dimension does not match the index.",
                details={
                    "expected": version.get("dimension"),
                    "actual": len(query_vector),
                },
            )

        rows = self._store.iter_chunk_vectors(version["index_version_id"])
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            score = _cosine(query_vector, row["vector"])
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[: max(1, top_k)]

        fragments = [
            {
                "rank": rank,
                "score": round(score, 6),
                "chunk_id": row["chunk_id"],
                "text": row["text"],
                "metadata": _public_metadata(row["metadata"]),
            }
            for rank, (score, row) in enumerate(top, start=1)
        ]
        return {
            "query": query,
            "collection_id": collection_id,
            "index_version_id": version["index_version_id"],
            "strategy": version["strategy"],
            "top_k": top_k,
            "fragments": fragments,
            "counts": {"indexed_chunks": len(rows), "returned": len(fragments)},
            "metrics": version.get("metrics"),
        }

    def _resolve_search_version(
        self, collection_id: str, index_version_id: str | None, strategy: str | None
    ) -> dict[str, Any] | None:
        if index_version_id:
            version = self._store.get_index_version(index_version_id)
            # Scoped isolation: a version from another collection is never used.
            if version is None or version.get("collection_id") != collection_id:
                return None
            return version
        active = self._store.get_active_index(collection_id)
        if strategy:
            # SPEC 11.1: prefer the collection active version when its strategy
            # matches; otherwise fall back to the latest ready version of it.
            if active and active.get("status") == "ready" and active.get("strategy") == strategy:
                return active
            return self._store.find_ready_index_by_strategy(collection_id, strategy)
        return active

    def list_chunks(
        self,
        index_version_id: str,
        offset: int = 0,
        limit: int = 50,
        document_id: str | None = None,
        section_path: str | None = None,
    ) -> dict[str, Any]:
        items, total = self._store.list_chunks(
            index_version_id, offset, limit, document_id, section_path
        )
        return {
            "items": [{**item, "metadata": _public_metadata(item["metadata"])} for item in items],
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": {"document_id": document_id, "section_path": section_path},
        }

    def compare(
        self,
        collection_id: str,
        strategies: Sequence[str],
        index_version_id: str | None = None,
    ) -> dict[str, Any]:
        if self._store.get_collection(collection_id) is None:
            raise KeyError(collection_id)
        versions: list[dict[str, Any]] = []
        if index_version_id:
            version = self._store.get_index_version(index_version_id)
            if version is None or version.get("collection_id") != collection_id:
                raise KeyError(index_version_id)
            # With an explicit version its real strategy is authoritative.
            versions = [version]
        else:
            for strategy in strategies:
                version = self._store.find_ready_index_by_strategy(collection_id, strategy)
                if version is not None:
                    versions.append(version)

        rows = [
            {
                "strategy": version["strategy"],
                "index_version_id": version["index_version_id"],
                "status": version["status"],
                "counts": version.get("counts"),
                "metrics": version.get("metrics"),
            }
            for version in versions
        ]
        comparable, note = self._comparison_status(versions)
        return {
            "collection_id": collection_id,
            "strategies": rows,
            "comparable": comparable,
            "note": note,
        }

    def _comparison_status(
        self, versions: Sequence[Mapping[str, Any]]
    ) -> tuple[bool, str | None]:
        """Compare only the same cleaned corpus and processing/embedding identity."""

        if not versions:
            return False, "no ready indexes to compare"
        signatures = [self._comparison_signature(version) for version in versions]
        if all(signature is not None and signature == signatures[0] for signature in signatures):
            return True, None
        return (
            False,
            "indexes use different cleaned corpora or processing/embedding identities,"
            " or lack comparison provenance; metrics are not directly comparable",
        )

    def _comparison_signature(self, version: Mapping[str, Any]) -> tuple[Any, ...] | None:
        manifest = version.get("manifest") or {}
        digest = manifest.get("cleaned_corpus_sha256")
        processing = manifest.get("processing")
        if not digest or not processing:
            return None
        pipeline = manifest.get("pipeline") or {}
        identity = identity_for_compatibility(version)
        return (
            digest,
            tuple(pipeline.get("extraction_versions") or []),
            pipeline.get("normalization"),
            tuple(processing.get("excluded_roles_policy") or []),
            tuple(sorted((key, str(value)) for key, value in identity.items())),
        )

    def health(self) -> dict[str, Any]:
        preflight = self._embedder.preflight()
        reachable = bool(preflight.get("reachable"))
        model_present = bool(preflight.get("model_present"))
        model = self._embedder.identity().model
        hint = None if reachable and model_present else f"ollama pull {model}"
        return {
            "status": "ok" if reachable and model_present else "degraded",
            "embedding": {
                "reachable": reachable,
                "model_present": model_present,
                "model": model,
                "digest": self._embedder.identity().digest,
                "dimension": self._embedder.identity().dimension,
                "hint": hint,
            },
        }

    # -- metrics/manifest -------------------------------------------------
    def _fingerprint(
        self, prepared: Sequence[Mapping[str, Any]], chunker: Chunker, identity: Any
    ) -> str:
        payload = {
            "corpus_schema_version": self.corpus_schema_version,
            "normalization_version": self.normalization_version,
            "excluded_roles": sorted(str(role) for role in self.excluded_roles),
            "sources": [
                {
                    "source_id": item["source_ref"].source_id,
                    "kind": item["source_ref"].kind,
                    "content_sha256": item["source_ref"].content_sha256,
                    "extraction_version": _extraction_version(
                        item["adapter"], item["source_ref"]
                    ),
                }
                for item in prepared
            ],
            "chunking": chunker.params,
            "embedding": {
                "model": identity.model,
                "digest": identity.digest,
                "dimension": identity.dimension,
                "normalization": identity.normalization,
                "dtype": identity.dtype,
                "document_prefix": identity.document_prefix,
                "query_prefix": identity.query_prefix,
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _build_metrics(
        self,
        *,
        chunks: Sequence[Chunk],
        unique_tokens: int,
        parse_seconds: float,
        embed_seconds: float,
        store_seconds: float,
        build_seconds: float,
        latencies: Sequence[float],
        input_tokens: int | None,
    ) -> dict[str, Any]:
        token_lengths = [chunk.token_count for chunk in chunks]
        char_lengths = [chunk.char_count for chunk in chunks]
        total_tokens = sum(token_lengths) or 0
        # Overlap is the extra token material beyond unique source content.
        overlap_overhead = (
            (total_tokens - unique_tokens) / unique_tokens if unique_tokens else 0.0
        )
        crossings = sum(1 for chunk in chunks if chunk.crosses_sections)
        crossing_ratio = crossings / len(chunks) if chunks else 0.0
        return {
            "chunk_tokens": _stats(token_lengths),
            "chunk_chars": _stats(char_lengths),
            "overlap_overhead": round(overlap_overhead, 6),
            "section_crossing_ratio": round(crossing_ratio, 6),
            "parse_seconds": round(parse_seconds, 6),
            "embed_seconds": round(embed_seconds, 6),
            "store_seconds": round(store_seconds, 6),
            "build_seconds": round(build_seconds, 6),
            "chunks_per_second": round(len(chunks) / build_seconds, 6) if build_seconds else None,
            "embed_latency_median_ms": _percentile(latencies, 0.5),
            "embed_latency_p95_ms": _percentile(latencies, 0.95),
            "input_tokens": input_tokens,
            "db_size_bytes": 0,
            "vector_bytes": 0,
        }

    def _build_manifest(
        self,
        *,
        collection_id: str,
        index_version_id: str,
        version: Mapping[str, Any],
        identity: Any,
        documents: Sequence[Document],
        counts: Mapping[str, Any],
        metrics: Mapping[str, Any],
        chunks: Sequence[Chunk],
        excluded_roles: Sequence[str],
        finished_at: str | None,
    ) -> dict[str, Any]:
        sources = []
        warnings: list[str] = []
        for document in documents:
            extraction = document.extraction
            sources.append(
                {
                    "source_id": document.source.source_id,
                    "extraction_version": extraction.extraction_version,
                    "label": document.source.label,
                    "kind": document.source.kind,
                    "content_sha256": document.source.content_sha256,
                    "page_count": extraction.page_count,
                    "useful_pages": extraction.useful_pages,
                    "useful_chars": extraction.useful_chars,
                    "language": extraction.language,
                }
            )
            warnings.extend(extraction.warnings)
        chunker_params = dict(
            self._resolve_chunker(version.get("strategy", "fixed")).params
        )
        chunker_params["corpus_schema_version"] = self.corpus_schema_version
        return {
            "cleaned_corpus_sha256": _digest([
                {
                    "kind": document.source.kind,
                    "extraction_version": document.extraction.extraction_version,
                    "sections": [
                        [section.section_path, section.role, section.page_start, section.page_end, section.text]
                        for section in document.sections
                    ],
                }
                for document in documents
            ]),
            "processing": {"excluded_roles_policy": sorted(self.excluded_roles)},
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "corpus_schema_version": self.corpus_schema_version,
            "index_version_id": index_version_id,
            "collection_id": collection_id,
            "status": "ready",
            "created_at": version.get("created_at"),
            "started_at": version.get("started_at"),
            "finished_at": finished_at or version.get("finished_at"),
            "pipeline": {
                "adapter": "multi",
                "extraction_version": documents[0].extraction.extraction_version
                if documents
                else None,
                "extraction_versions": sorted(
                    {document.extraction.extraction_version for document in documents}
                ),
                "normalization": self.normalization_version,
                "normalization_detail": NORMALIZATION_DETAIL,
            },
            "chunking": {
                "strategy": version.get("strategy"),
                "unit": self._tokenizer.unit,
                "tokenizer": getattr(self._tokenizer, "name", "lexical-v1"),
                "params": chunker_params,
            },
            "embedding": {
                **identity.to_dict(),
                "truncate": False,
                "batch_size": self.batch_size,
            },
            "sources": sources,
            "excluded_roles": list(excluded_roles),
            "counts": dict(counts),
            "metrics": dict(metrics),
            "warnings": sorted(set(warnings)),
        }


def _public_label(value: Any) -> str:
    """Strip path prefixes and controls for platform-independent public provenance."""

    label = re.sub(r"[\x00-\x1f\x7f]", "", str(value)).replace("\\", "/")
    label = label.rstrip("/").rsplit("/", 1)[-1]
    label = re.sub(r"^[A-Za-z]:", "", label).strip()
    return label[:200] or "source"


def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Project retained public fields safely without mutating stored provenance."""

    projected = dict(metadata)
    for key in ("source_uri", "source_label", "title"):
        if projected.get(key) is not None:
            projected[key] = _public_label(projected[key])
    return projected


def _public_index_version(version: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(version)
    manifest = version.get("manifest")
    if manifest is not None:
        projected["manifest"] = {
            **manifest,
            "sources": [
                {
                    **source,
                    **{key: _public_label(source[key])
                       for key in ("label", "public_uri")
                       if source.get(key) is not None},
                }
                for source in manifest.get("sources", [])
            ],
        }
    return projected


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _extraction_version(adapter: Any, source_ref: SourceRef) -> str:
    effective = getattr(adapter, "effective_extraction_version", None)
    return str(effective(source_ref) if effective else adapter.extraction_version)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _stats(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {"min": 0, "median": 0, "p95": 0, "max": 0}
    ordered = sorted(values)
    size = len(ordered)
    index = min(size - 1, int(round(0.95 * (size - 1))))
    return {
        "min": ordered[0],
        "median": ordered[size // 2],
        "p95": ordered[index],
        "max": ordered[-1],
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 3)
