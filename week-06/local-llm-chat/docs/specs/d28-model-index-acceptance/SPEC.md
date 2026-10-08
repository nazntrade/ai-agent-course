# SPEC: D28 — External Model Profile Priority & Week-05 Index Integration

## 1. Overview

Day 28 extends the `local-llm-chat` module (week-06) in two directions:

1. **External model profile priority** — when `AI_TEST_MODEL_*` environment variables form a complete external profile, that profile takes precedence over all local/network defaults. The app must never silently fall back to a local Gemma process. Partial or missing profiles must produce a clear error listing the missing parameter names (no secrets). When no external profile is present, the app continues to operate independently with its own GGUF model selection.
2. **Week-05 knowledge-agent index integration** — the app connects to the existing `week-05/knowledge-agent/local-data/index.db` as a read-only RAG source. The index contains the `agents-survey` collection (~59 chunks, 768-dimensional vectors, `embeddinggemma:300m`). The adapter must support schema inspection, vector retrieval via cosine similarity, and display of collection metadata (name, chunk count) in the UI.

All D26 capabilities are preserved and must pass existing unit/integration tests.

## 2. Scope

### In scope
- `app/config.py` — add `AI_TEST_MODEL_*` reading (KIND, BASE_URL, NAME, API_KEY, ID, LEASE_URL, LEASE_ID, PARENT_READY); expose external profile state to service and UI.
- `app/api/routes.py` — new endpoints: `GET /api/models` (list GGUF files), `GET /api/rag/collections` (return week-05 collection metadata).
- `app/rag/external_index.py` — read-only SQLite adapter for week-05 index.db with schema inspection and cosine-similarity retrieval.
- `app/service.py` — external profile priority logic: when full external profile is set, use it for generation and RAG query rewrite; block manual model selection; manage only own runtime.
- `app/ui/index.html` — model selection dropdown, external profile status badge, RAG collection info panel, lock when external active.
- `app/ui/app.js` — JS for model selection, profile status polling, collection info fetch, disabled state management.
- `app/ui/styles.css` — styles for new UI elements (model selector, external badge, collection info).
- `README.md` — D28 section (Russian): model selection, index connection, launch commands, measured results, readiness status.
- `local-data/acceptance/day-28/` — preflight.json, q-*.json (5+), stability.json, deepseek-comparison.json, other artifacts.

### Out of scope
- Modifying any files under `week-05/**`.
- Changing the existing D26 provider switching mechanism beyond the external profile overlay.
- Modifying `.env` contents or reading secrets.
- Creating new test infrastructure outside the acceptance harness.

## 3. Requirements

### R1. AI_TEST_MODEL_* Profile Alignment

**Statement:** The app must read the `AI_TEST_MODEL_KIND`, `AI_TEST_MODEL_BASE_URL`, `AI_TEST_MODEL_NAME`, `AI_TEST_MODEL_API_KEY`, and `AI_TEST_MODEL_ID` environment variables. When a complete profile is present (all required fields set), the app must use it for chat generation and RAG query rewrite. The hardcoded Gemma override (SPEC R2.3) must be removed — `AI_TEST_MODEL_*` variables must no longer be silently ignored by `config.py`.

**Priority:** Must

**Constraints:**
- The variable names must match the existing `AI_TEST_MODEL_` prefix used by `test-runtime-json`.
- Secret values (e.g., `AI_TEST_MODEL_API_KEY`) must never be echoed in errors, logs, or the browser.
- The profile KIND value determines the transport: `remote` for HTTP-based models, `local` for GGUF.

### R2. External Profile Priority

**Statement:** When a complete external profile is detected:
1. The external model must be used for all chat generation and RAG query rewrite.
2. No local `llama-server.exe` process may be started.
3. The UI must display the external model name and identity (provider, base_url, model_id).
4. Manual model selection controls must be disabled (grayed out) in the UI.

**Priority:** Must

**Constraints:**
- "Complete" means KIND, BASE_URL, NAME, and API_KEY are all non-empty.
- The external profile must not affect other consumers of local models (e.g., embedding).

### R3. Partial/Wrong Profile → Error, No Hidden Fallback

**Statement:** When `AI_TEST_MODEL_*` variables are present but incomplete:
1. The app must display a clear error message listing the missing parameter names (e.g., `AI_TEST_MODEL_API_KEY`).
2. No fallback to a local Gemma process or DeepSeek is permitted.
3. The UI must show the error state without starting any model process.

**Priority:** Must

**Constraints:**
- Missing parameter names must be listed as keys, never their values.
- The app must continue running (no crash); it enters a blocked state for model generation.

### R4. Standalone Operation Without External Profile

**Statement:** When no `AI_TEST_MODEL_*` variables are set:
1. The app must continue to work independently with its own configuration.
2. GGUF model selection must be available from the configured model directory (`D:\AI\Models\LLM\`).
3. DeepSeek variants must be available when the app's own network configuration is complete.
4. The existing D26 behavior is fully preserved.

**Priority:** Must

### R5. GGUF Model Selection & DeepSeek Variants

**Statement:** The app must:
1. Expose a `GET /api/models` endpoint listing `.gguf` files from `D:\AI\Models\LLM\`.
2. Present GGUF files as selectable options in the UI model selector.
3. Offer DeepSeek model variants (e.g., `deepseek-chat`, `deepseek-coder`) when network configuration is available.
4. Allow manual model switching only when no external profile is active.

**Priority:** Must

### R6. Model Information Display

**Statement:** The UI must show:
1. The actual model name currently in use (from provider identity, not a static label).
2. Whether the source is external (env-driven), local (GGUF), or network (DeepSeek).
3. A model info badge in the header that updates on provider/model changes.

**Priority:** Must

### R7. Manual Selection Blocked When External Active

**Statement:** When an external profile is active:
1. The model selector dropdown must be visually disabled.
2. Clicking the selector must produce a tooltip or message explaining that the external profile controls model selection.
3. The disable state must update immediately when the external profile state changes.

**Priority:** Must

### R8. Own Runtime Management

**Statement:** Manual model switching must:
1. Stop only the process owned by this application (Gemma PID tracked by `GemmaProcessManager`).
2. Start only a new process for the selected model.
3. Leave other consumers (embedding, external services) unaffected.
4. Preserve the unload button functionality regardless of active provider.

**Priority:** Must

### R9. Week-05 Index Read-Only Connection

**Statement:** The app must connect to `week-05/knowledge-agent/local-data/index.db` as a read-only RAG source:
1. Perform schema inspection to discover collections and their index versions.
2. Support vector search via cosine similarity on stored vectors (stored as BLOB in SQLite, encoded via `struct.pack("<Nf", ...)`).
3. Never modify the source database.
4. Return collection metadata: name, chunk count, dimension, embedding model.

**Priority:** Must

**Constraints:**
- The week-05 index uses a different schema than the D26 `RagStore` (it has `collections`, `index_versions`, and BLOB-stored vectors).
- The adapter must handle the `vector` BLOB decoding using `struct.unpack("<Nf", ...)`.
- The `agents-survey` collection has approximately 59 chunks with 768-dimensional vectors.

### R10. RAG Panel Collection Info

**Statement:** The RAG panel must display:
1. The collection name being used (e.g., "agents-survey").
2. The chunk count for the active index version.
3. The dimension and embedding model information.

**Priority:** Must

### R11. Preflight: index → embedding → retrieval → answer

**Statement:** The acceptance preflight must perform a complete chain:
1. Inspect the week-05 index schema and verify collection availability.
2. Use the existing `OllamaEmbedder` to embed a test query (no second `llama-server` process).
3. Retrieve relevant chunks from the external index via cosine similarity.
4. Generate an answer using the configured answer model (external, local, or network).
5. Record `provider`, `model`, `retrieval_count` (≥1) in the preflight artifact.

**Priority:** Must

### R12. Five Substantive Questions with Citations

**Statement:** Acceptance testing must include five questions about the `agents-survey` documents in the week-05 index:
1. Each question must be answerable from the indexed documents.
2. At least one question must produce an "insufficient context" response (demonstrating proper RAG fallback).
3. Answers must include citations referencing the retrieved chunks.
4. Timing information must be recorded for each question.
5. Each question artifact (`q-*.json`) must include `quality_ok` (boolean) and `citation_status` fields:
   - `quality_ok` is `True` when: answer text is non-empty, `finish_reason == "stop"`, and either the question topic is `"insufficient-context"` OR the answer has sources/citations (`len(sources) > 0` and citation status is `"source_only"`, `"verified"`, or `"partial"`).
   - `citation_status` is extracted from `record["rag"]["citations"]["status"]` (or `"unknown"` if not a dict).

**Priority:** Must

### R13. Stability: Two Questions Repeated

**Statement:** Two questions must be asked twice in sequence:
1. The answers must be consistent (same text, same model, same timing within acceptable variance).
2. Results must be saved to `stability.json` with `question`, `run1`, `run2`, and `consistent` fields.

**Priority:** Must

### R14. DeepSeek Comparison

**Statement:** The same questions from R12 must be answered via the DeepSeek provider:
1. Results must be compared side-by-side with the primary provider.
2. Comparison must be saved to `deepseek-comparison.json` with at least one question entry.
3. **Strict pass/fail:** All DeepSeek answers must be successful (non-error) to satisfy this criterion. If any answer fails, the comparison cannot be performed and the criterion FAILs.
4. The comparison artifact must record `successful_count`, `total_count`, and a list of any errors that occurred.

**Priority:** Must

### R14b. DeepSeek Comparison Strictness (Defect Fix)

**Statement:** The acceptance harness must enforce strict pass/fail for C14: `results["deepseek"]` is `True` only when `len(successful_answers) == len(questions)` (i.e., all DeepSeek answers succeed). A single failure makes the criterion FAIL. The `deepseek-comparison.json` artifact must include:
- `successful_count`: number of non-error DeepSeek answers
- `total_count`: total number of DeepSeek attempts
- `errors`: list of error entries for failed questions

**Priority:** Must

### R15. Artifact Storage

**Statement:** All acceptance artifacts must be saved under `local-data/acceptance/day-28/`:
1. `preflight.json` — preflight result with provider, model, retrieval_count.
2. `q-*.json` — five or more question-answer files with question, answer, citations.
3. `stability.json` — two-run consistency check.
4. `deepseek-comparison.json` — DeepSeek comparison results.
5. Each artifact must contain a `model` field.
6. No secrets (API keys, environment values) in any artifact.

**Priority:** Must

### R16. UI Scenarios

**Statement:** The following UI scenarios must work end-to-end:
1. Model selection from GGUF files when no external profile is active.
2. External profile priority override when `AI_TEST_MODEL_*` is set.
3. RAG regression: connecting to the week-05 index produces retrievable results using an isolated test database for dialogues.

**Priority:** Must

### R17. README Update

**Statement:** `README.md` must include a D28 section documenting:
1. Model selection mechanism (dropdown, GGUF listing).
2. External profile configuration (`AI_TEST_MODEL_*` variables).
3. Week-05 index connection (path, schema, read-only).
4. Launch commands and expected behavior.
5. Measured results from acceptance testing.
6. Readiness status (what works, what is environment-dependent).

**Priority:** Must

### R18. D26 Capability Preservation

**Statement:** All D26 capabilities must continue to function:
1. Local Gemma chat with GGUF model.
2. Network DeepSeek chat.
3. Provider switching with process lifecycle management.
4. RAG/no-RAG modes with citation verification.
5. Dialogue management (create, rename, delete, memory).
6. Query rewrite with safe fallback.

All existing unit and integration tests must pass.

**Priority:** Must

## 4. Architecture

### 4.1 External Profile Detection Flow

```
AI_TEST_MODEL_* vars present
├─ Complete (KIND, BASE_URL, NAME, API_KEY set)
│  └─ ExternalProvider created → used for chat + RAG rewrite
│     → model selector disabled in UI
│     → no local process started
├─ Partial (some vars missing)
│  └─ Error displayed with missing param names
│     → no fallback, no process started
└─ Absent
   └─ Normal D26 behavior: GGUF selection + DeepSeek
```

### 4.2 ExternalIndex Adapter

The `external_index.py` module provides a read-only bridge to the week-05 index.db:

```
ExternalIndex(db_path)
├─ inspect_schema() → {tables, collections, dimensions}
├─ list_collections() → [{name, chunk_count, dimension, model}]
├─ search(query_vector, top_k=5, min_score=None) → [{chunk_id, text, score, source, label}]
└─ _decode_vector(blob) → list[float]  (struct.unpack)
```

The `search` method:
1. Joins `chunks` with `index_documents` and `index_versions` to find active version.
2. Decodes BLOB vectors using `struct.unpack("<Nf", ...)`.
3. Computes cosine similarity (reuse existing `rag.embedder.cosine`).
4. Returns top-k scored chunks.

### 4.3 Service Integration

The `ChatService` is extended with:
- `external_profile_active` — property returning boolean.
- `external_profile_info` — property returning dict with profile metadata.
- `external_provider` — optional `AnswerProvider` instance when external profile is set.
- `select_external_profile()` — activates the external profile (called on startup if detected).

When external profile is active, `ask()` uses `external_provider` instead of `local_provider` or `network_provider`.

### 4.4 UI Changes

New elements in `index.html`:
- Model selector dropdown (outside header, in chat area).
- External profile badge in header (replaces or augments state-badge).
- RAG panel section for collection info.

New elements in `app.js`:
- `refreshModels()` — fetches from `/api/models`.
- `refreshProfile()` — checks `/api/provider` + new `/api/external-profile` endpoint.
- `refreshCollections()` — fetches from `/api/rag/collections`.
- `selectModel(modelId)` — posts to `/api/models/select`.
- `updateModelSelector()` — populates dropdown, disables when external active.

## 5. Non-Functional Requirements

- **Security:** Never read or output `.env` contents. Never include API keys in error messages, logs, or artifacts.
- **Read-only index:** The week-05 index.db is never modified; opened in read-only mode.
- **Backward compatibility:** All D26 endpoints and behaviors are preserved.
- **Code comments:** English. README: Russian.
- **No new dependencies:** Implementation uses existing packages (sqlite3, struct, os).
- **Process isolation:** Only the application's own GGUF process is managed; no cross-process interference.

### 5.1 Context Budget Truncation Behavior (Q4)

The `ContextBuilder` enforces `max_context_chars` (default 12000) by:
1. Adding system prompt, dialogue history turns, RAG fragments, and user question.
2. If `used > max_context_chars`, removing oldest non-system history turns until the budget fits.
3. If the budget still exceeds after all history is removed, raising `InvalidRequest` — fragment text is **never** silently truncated.

This means: with `top_k=3` and accumulated dialogue history across 5+ questions, the builder first trims history; if fragments alone exceed budget, it raises an error rather than dropping fragment content. This is the correct behavior for C11/C12.
