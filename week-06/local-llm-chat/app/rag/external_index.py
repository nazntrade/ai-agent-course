"""Read-only SQLite adapter for the week-05 knowledge-agent index.db (D28).

Connects to the existing week-05 index without modifying it.  Provides schema
inspection, collection metadata, and cosine-similarity vector search over
BLOB-stored vectors.
"""

from __future__ import annotations

import math
import hashlib
import json
import struct
import sqlite3
from pathlib import Path
from typing import Any, Sequence


class ExternalIndex:
    """Read-only bridge to the week-05 index.db."""

    def __init__(self, db_path: str | Path, reading_view_path: str | Path | None = None) -> None:
        self._db_path = str(db_path)
        # Open in read-only mode using URI.
        self._conn = sqlite3.connect(
            Path(self._db_path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._reading_views = {}
        if reading_view_path and Path(reading_view_path).is_file():
            view = json.loads(Path(reading_view_path).read_text(encoding="utf-8"))
            source = Path(view["source_path"])
            if hashlib.sha256(source.read_bytes()).hexdigest() != view["source_sha256"]:
                raise ValueError("Reading-view source hash mismatch; rebuild it from the original PDF")
            self._reading_views = view["views"]

    # -- schema inspection ------------------------------------------------

    def inspect_schema(self) -> dict[str, Any]:
        """Return a compact schema description of the week-05 index.

        Returns ``{tables, collections, dimension, chunks}``.
        """
        # Discover top-level table names.
        tables = [
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]

        collections: list[dict[str, Any]] = []
        dimension: int = 0

        # Get dimension from index_versions for agents-survey's active version.
        try:
            agent_row = self._conn.execute("""
                SELECT iv.dimension
                FROM collections c
                JOIN index_versions iv
                  ON c.active_index_version_id = iv.index_version_id
                WHERE c.name = 'agents-survey'
            """).fetchone()
            if agent_row and agent_row["dimension"] is not None:
                dimension = agent_row["dimension"]
        except sqlite3.Error as exc:
            raise RuntimeError(
                "Failed to get dimension for agents-survey from index_versions"
            ) from exc

        # Read collections — use the *actual* column names from the week-05 schema.
        try:
            rows = self._conn.execute(
                "SELECT c.name, c.active_index_version_id, iv.status "
                "FROM collections c "
                "LEFT JOIN index_versions iv "
                "  ON iv.index_version_id = c.active_index_version_id "
                "ORDER BY c.name"
            ).fetchall()
            for row in rows:
                collections.append({
                    "name": row["name"],
                    "index_version_id": row["active_index_version_id"],
                    "status": row["status"],
                })
        except sqlite3.Error as exc:
            raise RuntimeError("Failed to read collections") from exc
        except Exception as exc:
            raise RuntimeError("Unexpected error reading collections") from exc

        # Count chunks for agents-survey's active version.
        try:
            agent_version = self._conn.execute("""
                SELECT active_index_version_id
                FROM collections
                WHERE name = 'agents-survey'
            """).fetchone()
            if agent_version and agent_version["active_index_version_id"]:
                chunk_row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM chunks "
                    "WHERE index_version_id = ?",
                    (agent_version["active_index_version_id"],),
                ).fetchone()
                chunk_count = int(chunk_row["c"])
            else:
                chunk_count = 0
        except sqlite3.Error as exc:
            raise RuntimeError("Failed to count chunks for agents-survey") from exc
        except Exception as exc:
            raise RuntimeError("Unexpected error counting chunks for agents-survey") from exc

        return {
            "tables": tables,
            "collections": collections,
            "dimension": dimension,
            "chunks": chunk_count,
        }

    def list_collections(self) -> list[dict[str, Any]]:
        """Return collection metadata: name, chunk_count, dimension, model.

        Dimension is read from ``index_versions`` via the collection's active
        index version, NOT inferred from a random BLOB.
        """
        try:
            rows = self._conn.execute("""
                SELECT c.name, iv.dimension,
                       (SELECT COUNT(*) FROM chunks
                        WHERE chunks.index_version_id = iv.index_version_id) as chunk_count,
                       iv.model
                FROM collections c
                JOIN index_versions iv
                  ON c.active_index_version_id = iv.index_version_id
                WHERE iv.status = 'ready'
                ORDER BY c.created_at
            """).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("Failed to list collections") from exc
        except Exception as exc:
            raise RuntimeError("Unexpected error listing collections") from exc

        return [
            {
                "name": r["name"],
                "dimension": r["dimension"],
                "chunk_count": r["chunk_count"],
                "model": r["model"],
            }
            for r in rows
        ]

    def embedding_profile(self):
        row = self._conn.execute("SELECT iv.model, iv.digest, iv.dimension, iv.normalization, iv.dtype, iv.document_prefix, iv.query_prefix FROM index_versions iv JOIN collections c ON c.active_index_version_id=iv.index_version_id WHERE c.name='agents-survey' AND iv.status='ready'").fetchone()
        if row is None:
            raise ValueError("No ready active index for agents-survey")
        return dict(row)

    def validate_embedder(self, embedder):
        profile = self.embedding_profile()
        for key in ("model", "document_prefix", "query_prefix"):
            if profile[key] != getattr(embedder, key, None):
                raise ValueError(f"Embedding profile mismatch: {key}")
        if profile["normalization"] != "l2" or profile["dtype"] != "float32":
            raise ValueError("Unsupported stored vector format")
        return profile

    def verify_runtime_embedding(self, embedder):
        """A changed tag must reproduce every stored vector, not merely its size."""
        profile = self.validate_embedder(embedder)
        runtime = embedder.preflight()
        if not runtime.get("model_present"):
            raise ValueError("The configured embedding model is unavailable")
        digest = runtime.get("digest")
        cache_key = (profile["digest"], digest)
        if getattr(self, "_verified_embedding_key", None) == cache_key:
            return self.embedding_verification
        checked = 0
        max_difference = 0.0
        if digest != profile["digest"]:
            rows = self._conn.execute("SELECT text, vector FROM chunks WHERE index_version_id=(SELECT active_index_version_id FROM collections WHERE name='agents-survey') ORDER BY chunk_id").fetchall()
            if not rows:
                raise ValueError("Active embedding index is empty")
            for start in range(0, len(rows), 8):
                batch = rows[start:start+8]
                generated = embedder.embed_documents([row["text"] for row in batch])
                if len(generated) != len(batch):
                    raise ValueError("Embedding batch size mismatch")
                for row, vector in zip(batch, generated):
                    stored = self._decode_vector(row["vector"])
                    if len(vector) != len(stored):
                        raise ValueError("Embedding dimension mismatch")
                    difference = max(abs(a-b) for a,b in zip(stored,vector))
                    if not math.isfinite(difference) or difference > 1e-5:
                        raise ValueError("Installed embedding model is incompatible with the stored vectors")
                    max_difference = max(max_difference, difference)
                    checked += 1
        self.embedding_verification = {"stored_digest": profile["digest"], "runtime_digest": digest,
            "status": "digest_match" if digest == profile["digest"] else "all_vectors_reproduced",
            "vectors_checked": checked, "max_abs_difference": max_difference}
        self._verified_embedding_key = cache_key
        return self.embedding_verification

    # -- search -----------------------------------------------------------

    def search(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve chunks by cosine similarity against BLOB vectors.

        Dimension is taken from ``iv.dimension`` in the SQL result rather than
        from ``len(query_vector)``.  A mismatch between the query vector length
        and the stored dimension raises a clear error instead of silently
        returning empty results.
        """
        # Resolve the active index version for agents-survey and read its
        # declared dimension in one round-trip.
        version_row = self._conn.execute("""
            SELECT iv.index_version_id, iv.dimension, iv.model
            FROM collections c
            JOIN index_versions iv
              ON c.active_index_version_id = iv.index_version_id
            WHERE c.name = 'agents-survey' AND iv.status = 'ready'
        """).fetchone()

        if not version_row:
            raise ValueError("No ready active index for agents-survey")

        db_version_id = version_row["index_version_id"]
        db_dimension = version_row["dimension"]

        if db_dimension is not None and db_dimension != len(query_vector):
            raise ValueError(
                f"Dimension mismatch: query vector length={len(query_vector)}, "
                f"expected={db_dimension} (from index_versions)"
            )

        dim = len(query_vector)

        # Join chunks directly (index_version_id + chunk_id) — no index_documents
        # is needed since we already scoped to the correct version.
        rows = self._conn.execute(
            """
            SELECT c.chunk_id, c.text, c.metadata_json, c.vector,
                   c.ordinal, s.label, s.public_uri, iv.model, iv.dimension
            FROM chunks c
            JOIN documents d ON c.document_id = d.document_id
            JOIN sources s ON d.source_id = s.source_id
            JOIN index_versions iv ON c.index_version_id = iv.index_version_id
            WHERE c.index_version_id = ?
              AND c.vector IS NOT NULL
            ORDER BY c.ordinal
            """,
            (db_version_id,),
        ).fetchall()

        # Pre-normalise the query vector for cosine similarity.
        q_norm = math.sqrt(sum(v * v for v in query_vector))
        if q_norm == 0.0:
            return []

        scored: list[dict[str, Any]] = []
        for row in rows:
            try:
                vector = self._decode_vector(row["vector"])
            except (struct.error, TypeError, ValueError) as exc:
                raise ValueError("Malformed vector in active index") from exc
            if len(vector) != dim:
                raise ValueError("Stored vector dimension differs from active index profile")
            # Cosine similarity = dot / (norm_q * norm_v).
            dot = sum(qi * vi for qi, vi in zip(query_vector, vector))
            v_norm = math.sqrt(sum(v * v for v in vector))
            if v_norm == 0.0:
                continue
            score = dot / (q_norm * v_norm)
            reading = self._reading_views.get(row["chunk_id"])
            if reading and reading["original_text_sha256"] != hashlib.sha256(row["text"].encode()).hexdigest():
                raise ValueError("Reading view no longer matches original index text")
            scored.append({
                "chunk_id": row["chunk_id"],
                "text": row["text"],
                "reading_view": "\n\n".join(p["text"] for p in reading["passages"]) if reading else None,
                "reading_locations": [{"page": p["page"], "column": p["column"]} for p in reading["passages"]] if reading else [],
                "score": round(score, 6),
                "source": row["public_uri"] or row["label"],
                "label": row["label"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "index_version_id": db_version_id,
                "embedding_model": row["model"],
            })

        scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
        if min_score is not None:
            scored = [item for item in scored if item["score"] >= min_score]
        return scored[: max(0, top_k)]

    def compare(self, query_vector, *, top_k=3, min_score=None):
        unfiltered = self.search(query_vector, top_k=top_k)
        filtered = self.search(query_vector, top_k=top_k, min_score=min_score)
        return {"top_k": top_k, "min_score": min_score, "unfiltered": unfiltered, "filtered": filtered,
                "stable": [r["chunk_id"] for r in unfiltered] == [r["chunk_id"] for r in filtered]}

    def add_document(self, **kwargs):
        from ..errors import InvalidRequest
        raise InvalidRequest("The week-05 index is read-only. Add documents in the indexing application.")

    def count_chunks(self) -> int:
        """Count only the selected collection's active version."""
        row = self._conn.execute("SELECT COUNT(*) AS c FROM chunks WHERE index_version_id = (SELECT active_index_version_id FROM collections WHERE name = 'agents-survey')").fetchone()
        return int(row["c"])

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _decode_vector(blob: bytes) -> list[float]:
        """Decode a BLOB vector encoded with ``struct.pack('<Nf', ...)``."""
        size = len(blob)
        if size % 4 != 0:
            raise ValueError(f"Blob size {size} is not a multiple of 4")
        n = size // 4
        return list(struct.unpack("<" + str(n) + "f", blob))

    def close(self) -> None:
        self._conn.close()


# -- internal helpers --------------------------------------------------------


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity (duplicate of rag.embedder.cosine for read-only use)."""
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))


def _count_chunks_for_collection(
    conn: sqlite3.Connection, index_version_id: str | None
) -> int:
    """Count chunks belonging to a specific collection's active version."""
    if index_version_id is None:
        # Fallback: count all chunks.
        row = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()
        return int(row["c"])
    row = conn.execute(
        "SELECT COUNT(*) AS c "
        "FROM chunks "
        "WHERE index_version_id = ?",
        (index_version_id,),
    ).fetchone()
    return int(row["c"])
