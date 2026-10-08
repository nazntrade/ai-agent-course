# ACCEPTANCE: D28 — External Model Profile Priority & Week-05 Index Integration

## Criteria Table

| ID | Level | Source | Requirement | Check Method | Artifacts |
|----|-------|--------|-------------|--------------|-----------|
| C01 | Must | D28 model alignment | AI_TEST_MODEL_* profile used for generation and RAG query rewrite; hardcoded Gemma override removed. | With AI_TEST_MODEL_KIND=remote, AI_TEST_MODEL_BASE_URL, AI_TEST_MODEL_NAME, AI_TEST_MODEL_API_KEY, AI_TEST_MODEL_ID set, app uses that profile for chat and RAG query rewrite. | None (LIVE) |
| C02 | Must | D28 priority | Full external profile priority. No silent local Gemma. | Complete external profile shows external model identity, no local llama-server started. | None (LIVE) |
| C03 | Must | D28 reject | Partial/wrong profile → clear error, no hidden fallback. | Partial profile shows error with missing parameter names. No fallback to Gemma. | None (LIVE) |
| C04 | Must | D28 standalone | Without external profile, app works independently. | No AI_TEST_MODEL_* vars → app uses own config, works with local GGUF. | None (LIVE) |
| C05 | Must | D28 options | GGUF model selection from configured directory + DeepSeek variants when no external profile. | UI offers GGUF selection and DeepSeek variants. | None (LIVE) |
| C06 | Must | D28 display | UI shows actual model name and config source. | UI shows factual model name and external vs local source. | None (LIVE) |
| C07 | Must | D28 lock | Manual model selection blocked when external profile active. | Model selector disabled when external profile active. | None (LIVE) |
| C08 | Must | D28 own runtime | Manual switch manages only own runtime. Unload button preserved. | Own process stopped/started on manual switch. Other consumers unaffected. | None (LIVE) |
| C09 | Must | D28 index | Week-05 index connected as read-only RAG source. | Connects to week-05/knowledge-agent/local-data/index.db. agents-survey (59 chunks, dim 768). Source DB never modified. | None (LIVE) |
| C10 | Must | D28 panel | RAG panel shows collection name and chunk count. | RAG panel shows collection name and chunk count. | None (LIVE) |
| C11 | Must | D28 preflight | Preflight: index → embedding → retrieval → answer. No second generator. | Real preflight with week-05 index. No second llama-server process. | preflight.json |
| C12 | Must | D28 questions | Five substantive questions about week-05 docs with citation support and timing. | 5 questions about agents-survey docs. At least one insufficient context. Each artifact includes quality_ok and citation_status. Record correctness, citations, times. | q-1.json … q-5.json |
| C13 | Must | D28 stability | Two questions repeated for stability. | Two questions repeated. Answers consistent. | stability.json |
| C14 | Must | D28 deepseek | DeepSeek comparison on same questions with strict pass/fail. Explicit branch only. | Same questions via DeepSeek with same fragments. ALL answers must succeed (non-error) for PASS. Compare results. | deepseek-comparison.json |
| C15 | Must | D28 artifacts | Complete answers, models, sources, parameters, times saved to local-data/acceptance/day-28/. | All artifacts under local-data/acceptance/day-28/. No secrets. | *.json files |
| C16 | Must | D28 UI | UI scenarios: model selection, external priority, regression. | UI model selection works. External priority. RAG regression with isolated DB. | None (LIVE) |
| C17 | Must | D28 README | README updated with D28 section. | README has D28 section: model selection, index, launch, results, readiness. | README.md |
| C18 | Must | D26 preservation | All D26 capabilities preserved. | Existing D26 unit+integration tests pass. | pytest results |

## Criterion Details

### C01 — Model Profile Alignment
- **Expectation:** When AI_TEST_MODEL_KIND=remote, AI_TEST_MODEL_BASE_URL, AI_TEST_MODEL_NAME, AI_TEST_MODEL_API_KEY, AI_TEST_MODEL_ID are all set, the application routes chat requests and RAG query rewrite through the external provider.
- **Evidence:** HTTP request logs or provider identity showing external model in use. Chat response contains external model's characteristic output style.
- **Not evidence:** Config file containing the values but not a live chat through the external provider.

### C02 — External Profile Priority
- **Expectation:** With a complete external profile, no `llama-server.exe` process is started. The UI shows the external model identity.
- **Evidence:** Process list check (no llama-server). API response showing provider="external".
- **Not evidence:** Only the config showing the external profile without verifying no local process was started.

### C03 — Partial Profile Error
- **Expectation:** With some AI_TEST_MODEL_* vars missing (e.g., API_KEY not set), the app displays a clear error listing the missing parameter names without falling back to Gemma.
- **Evidence:** API response with error message containing missing param names (e.g., `["DEEPSEEK_API_KEY"]`). No llama-server process started.
- **Not evidence:** A crash or silent failure.

### C04 — Standalone Operation
- **Expectation:** Without any AI_TEST_MODEL_* variables, the application operates normally with its own GGUF configuration.
- **Evidence:** Chat response from local Gemma model. D26 lifecycle works.
- **Not evidence:** Only that the app starts without error.

### C05 — GGUF Selection & DeepSeek
- **Expectation:** The `/api/models` endpoint lists GGUF files. UI dropdown is populated. DeepSeek variants are available when network config is complete.
- **Evidence:** GET /api/models returns list of .gguf files. UI dropdown populated.
- **Not evidence:** Hardcoded list in JS without server endpoint.

### C06 — Model Info Display
- **Expectation:** The UI shows the factual model name and whether it is external, local, or network-sourced.
- **Evidence:** Header badge shows model name. Badge text changes when provider changes.
- **Not evidence:** Static label that doesn't change with provider.

### C07 — Model Selector Locked When External Active
- **Expectation:** When external profile is active, the model selector dropdown is disabled. Clicking it shows an explanation.
- **Evidence:** HTML `disabled` attribute on select element. Tooltip or message explaining external profile control.
- **Not evidence:** Only CSS hiding without the disabled state.

### C08 — Own Runtime Management
- **Expectation:** Manual model switching stops only the application's own GGUF process. Unload button works regardless of active provider.
- **Evidence:** Process lifecycle logs showing own PID stopped/started. Other processes (embedding, etc.) unaffected.
- **Not evidence:** Only that the UI responds without error.

### C09 — Week-05 Index Read-Only
- **Expectation:** The app connects to `week-05/knowledge-agent/local-data/index.db` in read-only mode. The `agents-survey` collection is discoverable with ~59 chunks and 768-dimensional vectors.
- **Evidence:** GET /api/rag/collections returns collection info. Schema inspection confirms tables and dimensions. The source DB file is not modified (check modification timestamp before and after).
- **Not evidence:** A successful connection without verifying read-only access.

### C10 — RAG Panel Collection Info
- **Expectation:** The RAG panel displays the collection name and chunk count.
- **Evidence:** UI renders collection name and chunk count in the RAG panel.
- **Not evidence:** Console log only, not visible in the rendered UI.

### C11 — Preflight Chain
- **Expectation:** A complete preflight chain runs: schema inspection → embedding → retrieval → answer. At least one chunk is retrieved. No second llama-server process is started.
- **Evidence:** `preflight.json` exists with `provider`, `model`, and `retrieval_count` (≥1, as an array with at least 1 item). Process list confirms only one llama-server (or none).
- **Not evidence:** A health check without the full chain. Missing `provider` or `model` fields in the artifact.

### C12 — Five Substantive Questions
- **Expectation:** Five questions about agents-survey documents are answered with citations. At least one question produces an "insufficient context" response.
- **Evidence:** Five JSON files (`q-1.json` through `q-5.json`), each containing `question`, `answer`, `citations`, `quality_ok`, and `citation_status` fields. Each file has:
  - `quality_ok`: `True` when answer is non-empty, finish_reason is "stop", and either the topic is "insufficient-context" or sources are present.
  - `citation_status`: extracted from `record["rag"]["citations"]["status"]` (or "unknown").
  - At least one file has an insufficient context indicator and `quality_ok: true` for that specific question.
- **Not evidence:** Only one question file, or files without citations. Files with `quality_ok: false` for non-insufficient-context questions.

### C13 — Stability
- **Expectation:** Two questions are asked twice. Answers are consistent between runs.
- **Evidence:** `stability.json` with `question`, `run1` (answer, model, timing), `run2` (answer, model, timing), and `consistent` (boolean).
- **Not evidence:** Only one run per question.

### C14 — DeepSeek Comparison
- **Expectation:** The same questions are answered via DeepSeek. ALL answers must be successful (non-error) for the criterion to PASS. If any answer fails, the comparison cannot be performed and the criterion FAILs. Results are compared side-by-side with primary provider answers.
- **Evidence:** `deepseek-comparison.json` with at least one question entry containing results from both providers. The file must include `successful_count`, `total_count`, and `errors` fields. `successful_count` must equal `total_count` (which must equal the number of questions) for PASS.
- **Not evidence:** Comparison with mocked data. A result where `successful_count < total_count`. Only the presence of `deepseek_answers` array without success/failure counting.

### C15 — Artifact Storage
- **Expectation:** All artifacts are saved under `local-data/acceptance/day-28/`. Each JSON file contains a `model` field. No secrets are stored.
- **Evidence:** Files exist at the expected paths. JSON content inspected for model field and absence of secrets.
- **Not evidence:** Only that the directory exists with files of the expected names.

### C16 — UI Scenarios
- **Expectation:** Model selection from GGUF files works when no external profile is active. External profile overrides model selection. RAG connects to week-05 index with isolated test DB.
- **Evidence:** UI interactions performed end-to-end. GGUF model selected and used. External profile blocks selection. RAG query returns results from week-05 index.
- **Not evidence:** Only that the endpoints return data without UI interaction.

### C17 — README Update
- **Expectation:** README.md contains a D28 section covering model selection, index connection, launch commands, measured results, and readiness.
- **Evidence:** README.md file contains a section with header "D28" or "День 28" covering the specified topics.
- **Not evidence:** A TODO or placeholder section.

### C18 — D26 Preservation
- **Expectation:** All existing D26 unit and integration tests pass.
- **Evidence:** `pytest unit` and `pytest integration` reports showing all tests passed. No regressions introduced.
- **Not evidence:** Only that the app starts without error.

## Test Runtime Configuration

```test-runtime-json
{
  "schemaVersion": 1,
  "applicability": "ai",
  "profileEnvironment": "AI_TEST_MODEL_",
  "profileRole": "chat",
  "provider": "openai-compatible",
  "absentProfile": "explicit-config",
  "partialProfile": "reject",
  "embeddings": "independent",
  "lifecycle": "lease-finally"
}
```

This is identical to the D26 runtime configuration; D28 extends it with external profile priority.

## Acceptance Contract Reference

This ACCEPTANCE.md maps 1-to-1 with the frozen acceptance-contract provided by the Controller. Each criterion ID (C01–C18) corresponds to the same ID in the contract. The check methods here describe the actual verification procedure; artifacts are declared per the contract.
