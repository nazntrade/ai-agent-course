"""SQLite schema DDL and version marker (SPEC 9.1)."""

from __future__ import annotations

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active_index_version_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    uri TEXT NOT NULL,
    public_uri TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    extraction_version TEXT NOT NULL,
    adapter TEXT NOT NULL,
    page_count INTEGER,
    useful_pages INTEGER,
    useful_chars INTEGER NOT NULL,
    language TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    section_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    section_path TEXT NOT NULL,
    level INTEGER NOT NULL,
    role TEXT NOT NULL,
    start_bbox TEXT,
    page_start INTEGER,
    page_end INTEGER,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_versions (
    index_version_id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collections(collection_id),
    strategy TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    fingerprint TEXT NOT NULL,
    corpus_schema_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    endpoint_version TEXT,
    api TEXT NOT NULL,
    model TEXT NOT NULL,
    digest TEXT,
    dimension INTEGER,
    dtype TEXT NOT NULL,
    normalization TEXT NOT NULL,
    normalization_version TEXT,
    document_prefix TEXT NOT NULL,
    query_prefix TEXT NOT NULL,
    manifest_json TEXT,
    counts_json TEXT,
    metrics_json TEXT,
    progress_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ready_index
    ON index_versions(collection_id, fingerprint)
    WHERE status = 'ready';

CREATE TABLE IF NOT EXISTS index_documents (
    index_version_id TEXT NOT NULL REFERENCES index_versions(index_version_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    source_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (index_version_id, document_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    index_version_id TEXT NOT NULL REFERENCES index_versions(index_version_id),
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    vector BLOB,
    PRIMARY KEY (index_version_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(index_version_id, document_id);
CREATE INDEX IF NOT EXISTS ix_index_versions_collection
    ON index_versions(collection_id, strategy, status);
"""
