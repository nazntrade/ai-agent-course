# PLAN: D28 — External Model Profile Priority & Week-05 Index Integration

## Implementation Steps

### Step 1: config.py — Add AI_TEST_MODEL_* Reading

**File:** `app/config.py`

**Changes:**
1. Add a `TEST_MODEL_*` defaults dictionary containing keys: `KIND`, `BASE_URL`, `NAME`, `API_KEY`, `ID`, `LEASE_URL`, `LEASE_ID`, `PARENT_READY`.
2. Extend the `Settings` dataclass with fields:
   - `test_model_kind: str = ""`
   - `test_model_base_url: str = ""`
   - `test_model_name: str = ""`
   - `test_model_api_key: str = ""` (field with `repr=False`)
   - `test_model_id: str = ""`
   - `test_model_lease_url: str = ""`
   - `test_model_lease_id: str = ""`
   - `test_model_parent_ready: bool = False`
3. Add method `external_profile_complete(self) -> bool` — returns `True` when KIND, BASE_URL, NAME, and API_KEY are all non-empty.
4. Add method `external_profile_missing(self) -> list[str]` — returns names of missing parameters (e.g., `["DEEPSEEK_API_KEY"]`).
5. Modify `load_settings()` to merge `AI_TEST_MODEL_*` values into Settings (reading from the `source` mapping using the `AI_TEST_MODEL_` prefix, stripping the prefix to map to internal names).

**Dependencies:** None (standalone module change).

---

### Step 2: External Provider — Generic HTTP-based Answer Provider

**File:** `app/providers/external_http.py` (new)

**Purpose:** A generic OpenAI-compatible provider that reads configuration from the external profile fields in Settings. This provider is used when `AI_TEST_MODEL_*` variables are complete.

**Class:** `ExternalHttpProvider(AnswerProvider)`

**Constructor parameters:**
- `base_url: str` — from `test_model_base_url`
- `model_id: str` — from `test_model_name`
- `api_key: str` — from `test_model_api_key` (repr=False)
- `timeout: float = 120.0`
- `max_output_tokens: int = 2048`
- `temperature: float = 0.0`

**Behavior:**
- `name = "external"`
- `is_configured()` / `missing_config()` — mirrors `DeepSeekProvider` pattern (checks base_url, model_id, api_key).
- `identity()` — returns `{"provider": "external", "base_url": ..., "model": ...}`.
- `_request()` — same transport as `DeepSeekProvider` (urllib), including error mapping.
- `preflight()` — calls `/models` endpoint.
- `status()` — returns `ProviderStatus`.
- `chat()` — same payload format as `DeepSeekProvider`.

**Dependencies:** `app/config.py` (Settings fields), `app/providers/base.py`.

---

### Step 3: external_index.py — Read-Only Week-05 Index Adapter

**File:** `app/rag/external_index.py` (new)

**Purpose:** Read-only SQLite adapter for the week-05 index.db. Inspects schema, decodes BLOB vectors, performs cosine similarity search.

**Class:** `ExternalIndex(db_path: str | Path)`

**Methods:**
1. `inspect_schema() -> dict` — returns `{tables: [...], collections: [{name, index_version_id, status}], dimension: int, chunks: int}`.
2. `list_collections() -> list[dict]` — returns collection info: name, chunk_count, dimension, embedding_model.
3. `search(query_vector: Sequence[float], top_k: int = 5, min_score: float | None = None) -> list[dict]` — retrieves chunks from the active index version using cosine similarity.
4. `count_chunks() -> int` — returns total chunk count.
5. `_decode_vector(blob: bytes) -> list[float]` — uses `struct.unpack("<Nf", blob)`.
6. `close() -> None` — closes SQLite connection.

**Key implementation details:**
- Open database in read-only mode: `sqlite3.connect(db_path, uri=True)` or open with `PRAGMA read_only=1` (or use a copy). Since the week-05 path is outside the module, the adapter opens it read-only.
- The schema matches `week-05/knowledge-agent/knowledge_agent/storage/schema.py`: tables `collections`, `index_versions`, `chunks`, `index_documents`, `documents`, `sources`, `sections`.
- Vector search: join `chunks` with `index_documents` on `index_version_id` + `chunk_id`, join with `index_versions` to get active version, decode BLOB vectors, compute cosine similarity using `rag.embedder.cosine`.

**Dependencies:** `app/rag/embedder.py` (cosine function), `struct` module.

---

### Step 4: service.py — External Profile Priority

**File:** `app/service.py`

**Changes:**
1. Add `external_provider: AnswerProvider | None` parameter to `__init__`.
2. Add property `external_profile_active(self) -> bool` — checks `self.settings.external_profile_complete()`.
3. Add property `external_profile_info(self) -> dict` — returns profile metadata (kind, model, source).
4. In `ask()`: when `external_profile_active()` is True, use `self.external_provider` for generation. Set `name = "external"` in stored message.
5. In `provider_state()`: return external profile info when active.
6. In `select_provider()`: block switching when external profile is active (raise `ProviderError`).
7. In `select_provider("local")`: when external profile becomes inactive, revert to local.
8. New method `select_model(model_id: str)` — selects a GGUF model (only when no external profile active).

**Dependencies:** `app/config.py` (external profile methods), `app/providers/base.py`.

---

### Step 5: routes.py — New API Endpoints

**File:** `app/api/routes.py`

**New endpoints:**
1. `GET /api/models` — lists `.gguf` files from `D:\AI\Models\LLM\`. Returns `{"models": [{"name": ..., "path": ..., "size": ...}, ...]}`.
2. `POST /api/models/select` — selects a model by name. Validates it exists. Only allowed when no external profile active. Returns `{"selected": model_name}`.
3. `GET /api/rag/collections` — returns collection info from `ExternalIndex`. Returns `{"collections": [{name, chunk_count, dimension, model}, ...]}`.
4. `GET /api/external-profile` — returns external profile status. Returns `{"active": bool, "missing": [...], "info": {...}}`.

**Dependencies:** `app/config.py`, `app/rag/external_index.py`, `app/service.py`.

---

### Step 6: index.html — UI Additions

**File:** `app/ui/index.html`

**Changes:**
1. Add model selector dropdown in header: `<select id="model-selector" class="model-select"><option value="">Select model...</option></select>`.
2. Add external profile badge next to state-badge: `<span id="external-badge" class="badge external" hidden>external</span>`.
3. Add model info display in header: `<span id="model-info" class="badge model-info">gemma-4-12b-it</span>`.
4. In RAG panel, add collection info section: `<div id="collection-info"><span id="collection-name"></span> · <span id="chunk-count"></span> chunks</div>`.
5. Add disabled-state tooltip for model selector when external active.

**Dependencies:** None (static HTML change).

---

### Step 7: app.js — JS Logic

**File:** `app/ui/app.js`

**New functions:**
1. `async function refreshModels()` — fetches `/api/models`, populates dropdown.
2. `async function refreshExternalProfile()` — fetches `/api/external-profile`, updates badge and model selector state.
3. `async function refreshCollections()` — fetches `/api/rag/collections`, updates collection info panel.
4. `async function selectModel(modelName)` — posts to `/api/models/select`, refreshes.
5. `function updateModelSelector()` — enables/disables dropdown, shows/hides tooltip.
6. Modify `refreshProvider()` to also check external profile and update model info badge.
7. Modify `wire()` to add event listeners for model selector change.

**Dependencies:** None (existing `api()` function is reused).

---

### Step 8: styles.css — New Styles

**File:** `app/ui/styles.css`

**New styles:**
1. `.model-select` — styled dropdown, disabled state styling.
2. `.badge.external` — different color for external profile indicator.
3. `.badge.model-info` — model name display.
4. `.collection-info` — RAG panel collection metadata display.
5. Disabled state styling for model selector (opacity, cursor).

**Dependencies:** None.

---

### Step 9: __main__.py — Service Build Update

**File:** `app/__main__.py`

**Changes:**
1. In `build_service()`: check `settings.external_profile_complete()`. If True, create `ExternalHttpProvider` and pass as `external_provider` parameter.
2. Import `ExternalHttpProvider` from new module.

**Dependencies:** `app/config.py`, `app/providers/external_http.py`.

---

### Step 10: README.md — D28 Section

**File:** `README.md`

**New section:** `## Этап D28 — Выбор модели и индекс знаний`

**Contents (Russian):**
1. Механизм выбора модели (выпадающий список, GGUF файлы).
2. Внешний профиль (`AI_TEST_MODEL_*` переменные).
3. Подключение индекса week-05 (путь, схема, только для чтения).
4. Команды запуска и ожидаемое поведение.
5. Измеренные результаты тестирования.
6. Статус готовности (что работает, что зависит от окружения).

**Dependencies:** None.

---

### Step 11: Acceptance Artifacts

**Directory:** `local-data/acceptance/day-28/`

**Artifacts to create:**
1. `preflight.json` — provider, model, retrieval_count (≥1).
2. `q-1.json` through `q-5.json` (or more) — question, answer, citations, timing.
3. `stability.json` — two-run consistency check.
4. `deepseek-comparison.json` — comparison results.

**Format:** Each JSON file follows the acceptance-contract assertions.

**Dependencies:** Steps 1–9 must be complete. Requires running the application.

---

## Dependencies Between Steps

```
Step 1 (config.py) ──→ Step 2 (external_http.py)
                          ↓
Step 1 ──→ Step 3 (external_index.py)
          ↓
Step 1 ──→ Step 4 (service.py) ──→ Step 5 (routes.py)
          ↓                             ↓
Step 9 (__main__.py) ←────────────────┘
          ↓
Step 6 (index.html) ──→ Step 7 (app.js) ──→ Step 8 (styles.css)
          ↓
Step 10 (README.md)
          ↓
Step 11 (acceptance artifacts)
```

Steps 6, 7, 8, 10 are independent of the server-side chain but depend on the API endpoints from Step 5.

## Defect Fixes (Pre-Implementation Review)

### F1: C14 — DeepSeek comparison passes on all-errors (CORRECTNESS BUG)

**File:** `tests/scenarios/d28-acceptance.py` (lines 345–368)
**Problem:** `results["deepseek"] = len(ds_answers) >= 1` is always true when the loop runs (5 iterations), even if all 5 answers are errors. The condition passes despite no successful answers being received.
**Fix:** Count only successful (non-error) answers and enforce strict all-pass:
```python
successful_answers = [a for a in ds_answers if "error" not in a]
results["deepseek"] = len(successful_answers) == len(questions)  # STRICT: all 5 must succeed
emit("D28_DEEPSEEK", "PASS" if results["deepseek"] else "FAIL")
```
Also save in `deepseek-comparison.json`:
```python
deepseek_comp["successful_count"] = len(successful_answers)
deepseek_comp["total_count"] = len(ds_answers)
deepseek_comp["errors"] = [a for a in ds_answers if "error" in a]
```

### F2: C12 — Question answers lack quality verification

**File:** `tests/scenarios/d28-acceptance.py` (lines 237–258)
**Problem:** Current check `answer_text.strip() and record["answer"]["finish_reason"] == "stop"` only proves text is non-empty — not that it is correct, substantive, or citation-supported.

**Fix:** Add quality checks after a successful answer:
```python
citation_status = citations.get("status", "unknown") if isinstance(citations, dict) else "unknown"
is_insufficient_context = q.get("topic") == "insufficient-context"
has_sources = len(sources) > 0
citations_valid = (citation_status in ["source_only", "verified", "partial"]) if isinstance(citations, dict) else False

quality_ok = (
    len(answer_text.strip()) > 0  # Non-empty
    and record["answer"]["finish_reason"] == "stop"
    and (is_insufficient_context or has_sources)  # Must have sources for normal questions
)
results[f"q{i + 1}"] = quality_ok
```

Save `quality_ok` and `citation_status` in `q-*.json`:
```python
answers_data.append({
    "question": q["question"],
    "topic": q["topic"],
    "answer": answer_text,
    "citations": citations,
    "sources": sources,
    "latency_ms": latency_ms,
    "model": record["answer"]["model"],
    "provider": record["provider"],
    "quality_ok": quality_ok,
    "citation_status": citation_status,
})
```

### F3: Q4 — Context budget with accumulated history (NO FIX NEEDED)

**File:** `app/context/builder.py` (lines 78–89)
**Analysis:** The single `dialogue_id` in the scenario accumulates history across Q1–Q5. However, `ContextBuilder` already handles truncation: when `used > max_context_chars` (default 12000), it removes oldest non-system turns until the budget fits or raises `InvalidRequest`. The `top_k=3` retrieval per question is bounded by this budget.
**Verdict:** No additional fix required. The ContextBuilder truncation mechanism covers accumulated history.

### F4: Q1/Q3 — Empty response error handling (ALREADY ADEQUATE)

**File:** `tests/scenarios/d28-acceptance.py` (lines 41–58, `build_q_error_artifact`)
**Analysis:** Error artifacts already capture `type`, `message`, `status_code`, `details`, and `response_body`. The `ProviderInvalidResponse` in `external_http.py` (lines 183–186) is raised when the response has no choices, empty content, or malformed JSON.
**Verdict:** Error handling is adequate. Adding `citations_status` to the success path (see F2) provides additional debugging context.

## Risk Assessment

1. **Schema compatibility:** The week-05 index uses BLOB-stored vectors; the adapter must decode them correctly with `struct.unpack("<Nf", ...)`. If the schema differs, the adapter's schema inspection must adapt.
2. **Read-only access:** Opening the week-05 index.db from a different Python process may encounter WAL locks. The adapter must use `PRAGMA journal_mode=WAL` and handle `database is locked` errors gracefully.
3. **External profile detection timing:** `config.py` must read `AI_TEST_MODEL_*` before any provider instantiation; the order of `load_settings()` calls must not conflict with existing `load_env_file()` behavior.
4. **UI state consistency:** The model selector must correctly reflect the external profile state on every poll; race conditions between profile detection and UI rendering must be handled.

## Regression Strategy

After implementation:
1. Run existing unit tests (pytest unit) — must pass all 32 unit + 25 integration = 57 total tests.
2. Run existing integration tests (pytest integration) — must pass all 32 unit + 25 integration = 57 total tests.
3. Run smoke test — backend must respond on loopback.
4. Test D26 scenarios (local chat, network chat, switching, RAG) to confirm preservation.
5. Then proceed to D28 acceptance artifacts.
