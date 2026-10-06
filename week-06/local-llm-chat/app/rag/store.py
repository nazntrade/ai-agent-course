"""Retrieval service: indexing, search, filtering, comparison (SPEC R1.3, R5.2).

Documents are split into overlapping character chunks; vectors are stored in
SQLite. Search applies a top-k selection and an optional minimum-score filter,
returning stable source/citation metadata for RAG answers. This preserves the
week-05 retrieval contract in a compact, testable form.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .embedder import cosine

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    public_uri TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    section_path TEXT,
    text TEXT NOT NULL,
    vector_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id, ordinal);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunk_text(text: str, *, chunk_size: int = 500, overlap: int = 75) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    overlap = max(0, min(overlap, chunk_size - 1))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


class RagStore:
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
        self._conn.executescript(DDL)
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(self.schema_version),),
            )
            self._conn.commit()

    def add_document(
        self, *, label: str, public_uri: str, chunks: Sequence[str], vectors: Sequence[Sequence[float]]
    ) -> dict[str, Any]:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        document_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents(document_id, label, public_uri, created_at) VALUES (?,?,?,?)",
                (document_id, label, public_uri, _utcnow()),
            )
            for ordinal, (text, vector) in enumerate(zip(chunks, vectors)):
                self._conn.execute(
                    "INSERT INTO chunks(chunk_id, document_id, ordinal, section_path, text, vector_json)"
                    " VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, document_id, ordinal, None, text, json.dumps(list(vector))),
                )
            self._conn.commit()
        return {"document_id": document_id, "label": label, "chunks": len(chunks)}

    def count_chunks(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()
        return int(row["c"])

    def list_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT document_id, label, public_uri, created_at FROM documents ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self, query_vector: Sequence[float], *, top_k: int = 5, min_score: float | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.chunk_id, c.text, c.vector_json, c.section_path, d.label, d.public_uri"
                " FROM chunks c JOIN documents d ON d.document_id = c.document_id"
            ).fetchall()
        scored = []
        for position, row in enumerate(rows):
            vector = json.loads(row["vector_json"])
            score = cosine(query_vector, vector)
            scored.append(
                {
                    "chunk_id": row["chunk_id"],
                    "text": row["text"],
                    "score": round(score, 6),
                    "source": row["public_uri"],
                    "label": row["label"],
                    "section_path": row["section_path"],
                    "_position": position,
                }
            )
        scored.sort(key=lambda item: (-item["score"], item["_position"]))
        if min_score is not None:
            scored = [item for item in scored if item["score"] >= min_score]
        for item in scored:
            item.pop("_position", None)
        return scored[: max(0, top_k)]

    def compare(
        self, query_vector: Sequence[float], *, top_k: int = 3, min_score: float | None = None
    ) -> dict[str, Any]:
        unfiltered = self.search(query_vector, top_k=top_k)
        filtered = self.search(query_vector, top_k=top_k, min_score=min_score)
        return {
            "top_k": top_k,
            "min_score": min_score,
            "unfiltered": unfiltered,
            "filtered": filtered,
            "stable": [item["chunk_id"] for item in unfiltered] == [item["chunk_id"] for item in filtered],
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
