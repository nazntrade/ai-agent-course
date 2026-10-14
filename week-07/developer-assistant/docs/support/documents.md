# Documentation, sources and indexing

RAG retrieves relevant documentation snippets instead of treating the model's
memory as product authority. Each source has a relative path, line range and
content hash. Search is SQLite FTS5 with BM25 keyword ranking, not a guarantee
of semantic understanding. Документация, индекс, источники, старая версия.

## After changing documentation

In the main application, open Workspace, then Documents and use Index README
+ docs. Importing a file does not prove that an old index was updated. Inspect
the actual source excerpt and version. Deletion must remove the old source
after reindexing. A fabricated claim with a valid citation still is not correct.
Support has its own allowlisted FAQ corpus; its FAQ Reindex action does not
change the main application's project index or conversation history.

## Missing information

When no relevant documentation or diagnostic fact exists, the assistant should
say what is unknown and ask a small concrete follow-up question. It must not
invent a product guarantee, billing rule, account status or error code.
An unresolved problem can be escalated while retaining the ticket context.
