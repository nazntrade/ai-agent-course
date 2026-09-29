"""SQLite-backed :class:`IndexStore` (SPEC 9)."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..domain.contracts import CORPUS_SCHEMA_VERSION, IndexStore
from ..domain.errors import IndexNotReady, StoreSchemaUnsupported
from ..domain.models import Chunk, Document
from .schema import DDL, SCHEMA_VERSION


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_vector(vector: Iterable[float]) -> bytes:
    values = list(vector)
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector(blob: bytes) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


class SqliteIndexStore(IndexStore):
    def __init__(self, db_path: str | Path, schema_version: int = SCHEMA_VERSION) -> None:
        self.db_path = str(db_path)
        self.schema_version = schema_version
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.ensure_schema()

    # -- schema -----------------------------------------------------------
    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(DDL)
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(index_versions)")
            }
            if "normalization_version" not in columns:
                # Lightweight in-place upgrade for databases created before the
                # processing-version field existed (no manual DB removal needed).
                self._conn.execute(
                    "ALTER TABLE index_versions ADD COLUMN normalization_version TEXT"
                )
                self._conn.commit()
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(self.schema_version),),
                )
                self._conn.commit()
            else:
                raw_value = row["value"]
                try:
                    actual_version = int(raw_value)
                except (TypeError, ValueError):
                    actual_version = None
                if actual_version != self.schema_version:
                    self._conn.close()
                    raise StoreSchemaUnsupported(
                        "The local index schema version is not supported.",
                        details={
                            "expected": self.schema_version,
                            "actual": actual_version if actual_version is not None else raw_value,
                        },
                    )

    # -- collections ------------------------------------------------------
    def create_collection(self, name: str) -> dict[str, Any]:
        collection_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO collections(collection_id, name, active_index_version_id, created_at)"
                " VALUES (?, ?, NULL, ?)",
                (collection_id, name, _utcnow()),
            )
            self._conn.commit()
        return {
            "collection_id": collection_id,
            "name": name,
            "active_index_version_id": None,
            "counts": _zero_counts(),
        }

    def get_collection(self, collection_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM collections WHERE collection_id = ?", (collection_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "collection_id": row["collection_id"],
            "name": row["name"],
            "active_index_version_id": row["active_index_version_id"],
            "counts": self._counts_for_active(row["active_index_version_id"]),
        }

    def list_collections(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM collections ORDER BY created_at"
            ).fetchall()
        return [
            {
                "collection_id": row["collection_id"],
                "name": row["name"],
                "active_index_version_id": row["active_index_version_id"],
                "counts": self._counts_for_active(row["active_index_version_id"]),
            }
            for row in rows
        ]

    def _counts_for_active(self, index_version_id: str | None) -> dict[str, int]:
        if not index_version_id:
            return _zero_counts()
        version = self.get_index_version(index_version_id)
        if not version:
            return _zero_counts()
        return version.get("counts") or _zero_counts()

    # -- index versions ---------------------------------------------------
    def find_ready_index(self, collection_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM index_versions WHERE collection_id = ? AND fingerprint = ?"
                " AND status = 'ready'",
                (collection_id, fingerprint),
            ).fetchone()
        return _index_row(row) if row else None

    def find_ready_index_by_strategy(
        self, collection_id: str, strategy: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM index_versions WHERE collection_id = ? AND strategy = ?"
                " AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
                (collection_id, strategy),
            ).fetchone()
        return _index_row(row) if row else None

    def create_index_version(
        self,
        collection_id: str,
        strategy: str,
        fingerprint: str,
        identity: Mapping[str, Any],
    ) -> str:
        index_version_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO index_versions(
                    index_version_id, collection_id, strategy, status, fingerprint,
                    corpus_schema_version, provider, base_url, endpoint_version, api,
                    model, digest, dimension, dtype, normalization,
                    normalization_version, document_prefix, query_prefix,
                    progress_json, created_at, started_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    index_version_id,
                    collection_id,
                    strategy,
                    "building",
                    fingerprint,
                    identity.get("corpus_schema_version", CORPUS_SCHEMA_VERSION),
                    identity.get("provider", "ollama"),
                    identity.get("base_url", ""),
                    identity.get("endpoint_version"),
                    identity.get("api", "/api/embed"),
                    identity.get("model", ""),
                    identity.get("digest"),
                    identity.get("dimension"),
                    identity.get("dtype", "float32"),
                    identity.get("normalization", "l2"),
                    identity.get("normalization_version"),
                    identity.get("document_prefix", ""),
                    identity.get("query_prefix", ""),
                    json.dumps({"stage": "starting", "percent": 0}),
                    _utcnow(),
                    _utcnow(),
                ),
            )
            self._conn.commit()
        return index_version_id

    def update_progress(self, index_version_id: str, progress: Mapping[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE index_versions SET progress_json = ? WHERE index_version_id = ?",
                (json.dumps(dict(progress)), index_version_id),
            )
            self._conn.commit()

    def finish_index(
        self,
        index_version_id: str,
        counts: Mapping[str, Any],
        metrics: Mapping[str, Any],
        manifest: Mapping[str, Any],
        finished_at: str | None = None,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        finished = finished_at or _utcnow()
        with self._lock:
            if identity is not None:
                # Persist the identity actually used for embedding, so a later
                # transient preflight failure cannot cause false incompatibility.
                self._conn.execute(
                    "UPDATE index_versions SET status='ready', counts_json=?, metrics_json=?,"
                    " manifest_json=?, finished_at=?, progress_json=?, error=NULL,"
                    " provider=?, base_url=?, endpoint_version=?, api=?, model=?, digest=?,"
                    " dimension=?, dtype=?, normalization=?, normalization_version=?,"
                    " document_prefix=?, query_prefix=?,"
                    " corpus_schema_version=? WHERE index_version_id=?",
                    (
                        json.dumps(dict(counts)),
                        json.dumps(dict(metrics)),
                        json.dumps(dict(manifest)),
                        finished,
                        json.dumps({"stage": "ready", "percent": 100}),
                        identity.get("provider", "ollama"),
                        identity.get("base_url", ""),
                        identity.get("endpoint_version"),
                        identity.get("api", "/api/embed"),
                        identity.get("model", ""),
                        identity.get("digest"),
                        identity.get("dimension"),
                        identity.get("dtype", "float32"),
                        identity.get("normalization", "l2"),
                        identity.get("normalization_version"),
                        identity.get("document_prefix", ""),
                        identity.get("query_prefix", ""),
                        identity.get("corpus_schema_version", CORPUS_SCHEMA_VERSION),
                        index_version_id,
                    ),
                )
                self._conn.commit()
                return
            self._conn.execute(
                "UPDATE index_versions SET status='ready', counts_json=?, metrics_json=?,"
                " manifest_json=?, finished_at=?, progress_json=?, error=NULL"
                " WHERE index_version_id=?",
                (
                    json.dumps(dict(counts)),
                    json.dumps(dict(metrics)),
                    json.dumps(dict(manifest)),
                    finished,
                    json.dumps({"stage": "ready", "percent": 100}),
                    index_version_id,
                ),
            )
            self._conn.commit()

    def fail_index(self, index_version_id: str, error: str) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM chunks WHERE index_version_id = ?", (index_version_id,)
                )
                self._conn.execute(
                    "UPDATE index_versions SET status='failed', error=?, finished_at=?,"
                    " progress_json=? WHERE index_version_id=?",
                    (
                        error,
                        _utcnow(),
                        json.dumps({"stage": "failed", "percent": 0}),
                        index_version_id,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def mark_stale_builds_failed(self, reason: str) -> int:
        """Fail hanging ``building`` versions without touching ``ready``/active."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT index_version_id FROM index_versions WHERE status = 'building'"
            ).fetchall()
            ids = [row["index_version_id"] for row in rows]
            if not ids:
                return 0
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for index_version_id in ids:
                    self._conn.execute(
                        "DELETE FROM chunks WHERE index_version_id = ?", (index_version_id,)
                    )
                    self._conn.execute(
                        "UPDATE index_versions SET status='failed', error=?, finished_at=?,"
                        " progress_json=? WHERE index_version_id=?",
                        (
                            reason,
                            _utcnow(),
                            json.dumps({"stage": "failed", "percent": 0}),
                            index_version_id,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return len(ids)

    def set_active_index(self, collection_id: str, index_version_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                collection = self._conn.execute(
                    "SELECT * FROM collections WHERE collection_id = ?", (collection_id,)
                ).fetchone()
                if collection is None:
                    raise KeyError("collection")
                version = self._conn.execute(
                    "SELECT * FROM index_versions WHERE index_version_id = ?",
                    (index_version_id,),
                ).fetchone()
                if version is None or version["collection_id"] != collection_id:
                    raise KeyError("index_version")
                if version["status"] != "ready":
                    raise IndexNotReady(
                        "The selected index version is not ready.",
                        details={"index_version_id": index_version_id},
                    )
                self._conn.execute(
                    "UPDATE collections SET active_index_version_id = ? WHERE collection_id = ?",
                    (index_version_id, collection_id),
                )
                self._conn.commit()
            except IndexNotReady:
                self._conn.rollback()
                raise
            except KeyError:
                self._conn.rollback()
                raise
            except Exception:
                self._conn.rollback()
                raise
        return {"collection_id": collection_id, "active_index_version_id": index_version_id}

    def get_active_index(
        self, collection_id: str, strategy: str | None = None
    ) -> dict[str, Any] | None:
        if strategy:
            return self.find_ready_index_by_strategy(collection_id, strategy)
        collection = self.get_collection(collection_id)
        if not collection or not collection["active_index_version_id"]:
            return None
        return self.get_index_version(collection["active_index_version_id"])

    def get_index_version(self, index_version_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM index_versions WHERE index_version_id = ?",
                (index_version_id,),
            ).fetchone()
        return _index_row(row) if row else None

    def list_index_versions(self, collection_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM index_versions WHERE collection_id = ? ORDER BY created_at DESC",
                (collection_id,),
            ).fetchall()
        return [_index_row(row) for row in rows]

    # -- documents/sections/chunks ---------------------------------------
    def save_document(self, index_version_id: str, document: Document, ordinal: int) -> None:
        source = document.source
        extraction = document.extraction
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sources(source_id, uri, public_uri, label, kind,"
                " content_sha256, size_bytes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    source.source_id,
                    source.uri,
                    source.public_uri,
                    source.label,
                    source.kind,
                    source.content_sha256,
                    source.size_bytes,
                    _utcnow(),
                ),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO documents(document_id, source_id, extraction_version,"
                " adapter, page_count, useful_pages, useful_chars, language, title, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    document.document_id,
                    source.source_id,
                    extraction.extraction_version,
                    extraction.adapter,
                    extraction.page_count,
                    extraction.useful_pages,
                    extraction.useful_chars,
                    extraction.language,
                    source.label,
                    _utcnow(),
                ),
            )
            for section in document.sections:
                self._conn.execute(
                    "INSERT OR IGNORE INTO sections(section_id, document_id, section_path,"
                    " level, role, start_bbox, page_start, page_end, text)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        section.section_id,
                        document.document_id,
                        section.section_path,
                        section.level,
                        section.role,
                        json.dumps(section.start_bbox) if section.start_bbox else None,
                        section.page_start,
                        section.page_end,
                        section.text,
                    ),
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO index_documents(index_version_id, document_id,"
                " source_id, ordinal) VALUES (?,?,?,?)",
                (index_version_id, document.document_id, source.source_id, ordinal),
            )
            self._conn.commit()

    def insert_chunks(
        self, index_version_id: str, chunks: Iterable[Chunk], ordinal_start: int = 0
    ) -> int:
        rows = []
        for offset, chunk in enumerate(chunks):
            vector = chunk.vector or []
            rows.append(
                (
                    index_version_id,
                    chunk.chunk_id,
                    chunk.metadata.document_id,
                    ordinal_start + offset,
                    chunk.text,
                    chunk.token_count,
                    chunk.char_count,
                    chunk.metadata.model_dump_json(),
                    encode_vector(vector),
                )
            )
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO chunks(index_version_id, chunk_id, document_id, ordinal,"
                " text, token_count, char_count, metadata_json, vector)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def list_chunks(
        self,
        index_version_id: str,
        offset: int = 0,
        limit: int = 50,
        document_id: str | None = None,
        section_path: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["index_version_id = ?"]
        params: list[Any] = [index_version_id]
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT chunk_id, text, token_count, char_count, metadata_json"
                f" FROM chunks WHERE {where} ORDER BY ordinal",
                params,
            ).fetchall()
        items = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            if section_path and metadata.get("section_path") != section_path:
                continue
            items.append(
                {
                    "chunk_id": row["chunk_id"],
                    "text": row["text"],
                    "token_count": row["token_count"],
                    "char_count": row["char_count"],
                    "metadata": metadata,
                }
            )
        total = len(items)
        return items[offset : offset + limit], total

    def iter_chunk_vectors(self, index_version_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chunk_id, text, metadata_json, vector FROM chunks"
                " WHERE index_version_id = ? ORDER BY ordinal",
                (index_version_id,),
            ).fetchall()
        return [
            {
                "chunk_id": row["chunk_id"],
                "text": row["text"],
                "metadata": json.loads(row["metadata_json"]),
                "vector": decode_vector(row["vector"]) if row["vector"] else [],
            }
            for row in rows
        ]

    def count_rows(self, index_version_id: str) -> dict[str, int]:
        with self._lock:
            chunks = self._conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE index_version_id = ?",
                (index_version_id,),
            ).fetchone()["c"]
            documents = self._conn.execute(
                "SELECT COUNT(*) AS c FROM index_documents WHERE index_version_id = ?",
                (index_version_id,),
            ).fetchone()["c"]
            sources = self._conn.execute(
                "SELECT COUNT(DISTINCT source_id) AS c FROM index_documents"
                " WHERE index_version_id = ?",
                (index_version_id,),
            ).fetchone()["c"]
            sections = self._conn.execute(
                "SELECT COUNT(*) AS c FROM sections s JOIN index_documents d"
                " ON s.document_id = d.document_id WHERE d.index_version_id = ?",
                (index_version_id,),
            ).fetchone()["c"]
        return {
            "sources": sources,
            "documents": documents,
            "sections": sections,
            "chunks": chunks,
        }

    def db_size_bytes(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _zero_counts() -> dict[str, int]:
    return {"sources": 0, "documents": 0, "sections": 0, "chunks": 0}


def _index_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "index_version_id": row["index_version_id"],
        "collection_id": row["collection_id"],
        "strategy": row["strategy"],
        "status": row["status"],
        "fingerprint": row["fingerprint"],
        "corpus_schema_version": row["corpus_schema_version"],
        "provider": row["provider"],
        "base_url": row["base_url"],
        "endpoint_version": row["endpoint_version"],
        "api": row["api"],
        "model": row["model"],
        "digest": row["digest"],
        "dimension": row["dimension"],
        "dtype": row["dtype"],
        "normalization": row["normalization"],
        "normalization_version": row["normalization_version"],
        "document_prefix": row["document_prefix"],
        "query_prefix": row["query_prefix"],
        "manifest": json.loads(row["manifest_json"]) if row["manifest_json"] else None,
        "counts": json.loads(row["counts_json"]) if row["counts_json"] else _zero_counts(),
        "metrics": json.loads(row["metrics_json"]) if row["metrics_json"] else None,
        "progress": json.loads(row["progress_json"]) if row["progress_json"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
