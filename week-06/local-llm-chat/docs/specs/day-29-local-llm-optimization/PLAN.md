
## Authoritative manual implementation amendment — 2026-10-09

The user explicitly transferred D29 implementation to primary Codex. The original six questions, substantive quality, frozen twelve criteria, real measurements, regressions and Video + Code remain binding. Earlier lease-only descriptions below describe the prior execution arrangement and are superseded for the standalone local path by this section.

The application owns its standalone llama-server descriptor. A generation profile contains temperature, max_tokens, context_window in actual model TOKENS, full prompt_template, and a separate RAG character budget. Baseline starts with 8192 tokens; the candidate uses 6144 tokens. Both use the same Qwen3.6 Q5 weights. The optimization screen exposes profile selection and editable temperature/output/context/template. Applying a changed token window waits for generation, stops ONLY the owned descriptor, starts with the requested -c, and verifies /props default_generation_settings.n_ctx. Failed starts retain a visible error, restore the previous configuration and do not falsely claim an applied profile. Busy benchmark/profile switching rejects conflicting mutations. Borrowed external servers remain owner-controlled: context changes are rejected, not simulated. Network generation and its configuration remain unchanged.

The browser acceptance must exercise baseline/candidate/custom controls and prove returned actual runtime context and per-inference parameters, including restart/loading, the same-model quantization inventory, complete answers, and the truncation signal. Lab comparisons reuse the production ContextBuilder, retrieval, provider and bounded citation-correction path; they create no owner chat messages and preserve all timestamped results. Semantic grading is an independent inspection of full answers against the actual evidence and expected facts, never a citation-presence or keyword-only PASS. Candidate selection requires no factual/negative regression and a measured improvement; otherwise baseline remains selected. The selected default is recorded with the review and follows the model identity.

Additional user deliverables: Word explanation in Current task, explaining our actual code/UI, temperature/output/context, weight and KV quantization distinctions, before/after measurements, safeguards and relation to 00 Local_LLM_Week_6_Detailed_Notes.docx. Include actual application screenshots; render and visually inspect every document page. Record actual running UI to 1920x1080 MP4 with explanatory subtitles and verify duration, streams and representative frames. These deliverables do not substitute for independent product verification.

# PLAN: Day 29 — Local LLM Optimization

Module: `week-06/local-llm-chat`. Companion to `SPEC.md` (requirements R1–R12, fixed baseline facts, artifact schemas) and `ACCEPTANCE.md` (acceptance rows per frozen criterion). All paths relative to the module root. Stage: PRODUCT / SPECIFICATION — this plan is prepared now, executed at IMPLEMENTATION.

## 0. Preconditions and order

- The app already works (D26/D28): Local/Network/External providers, optional RAG over the week-05 index, citation verification, SQLite history.
- The Day 29 model is constant between runs: `Qwen3.6-35B-A3B-UD-Q5_K_XL` via the external-profile / local-alias lease path (KIND=local, loopback; lease acquire/use/release in `finally`).
- `.env` is never read; the app loads its own `.env` at normal start. No model downloads; drive `E:` (model dir) read-only; no `git commit`/`push`.
- **Risky-boundary preflight first (IMPLEMENTATION step P0):** before any product change, a minimal **real** preflight on the selected model/provider: acquire the `AI_TEST_MODEL_*` lease, one short request through the app's external provider path, verify format + non-empty answer + lease release. Only then proceed to implementation steps. No stub configuration, no disabled validation.

## 1. Implementation steps

### P0. Real preflight of the LIVE boundary (before code changes)
- Acquire the local lease from `AI_TEST_MODEL_*` (KIND=local, loopback), send one minimal request through the existing external provider path against the running app, release the lease in `finally`.
- Verify: response format, non-empty answer, lease released, recorded `model`/`name` identity. This confirms the D29 model is reachable exactly as in Day 28.
- Map: `C_REPRODUCIBLE` (lease mechanics), `C_COMPARE` (model reachability). Method: **LOCAL** (real local inference, no mock).

### P1. Named profiles + template selection in the app
- `app/config.py`: add a named-generation-profile structure (`GenerationProfile { temperature, max_tokens, context_window (tokens), max_context_chars (chars, additional), prompt_template_id }` with named entries `baseline` and `optimized`) plus an "active profile" setting. Values follow SPEC section 3/R2 facts; `context_window` for the lease path is recorded from actual lease/server facts at run time (external fact), not hardcoded as a guess.
- `app/context/builder.py`: keep `SYSTEM_RAG` text as named template `default`; add the revised `optimized` template (SPEC R3: keeps strict `[n]` citation rule, sharpens insufficient-context/no-fabrication rules, keeps untrusted-data rule); select template by `template_id` (today: unconditional, `builder.py:50`).
- Verification (unit, offline): both templates exist and differ; template selection by id; character budget unchanged behavior when `default`.
- Map: `C_PARAMS`, `C_PROMPT`. Method: **STATIC** (code) + offline unit checks.

### P2. Parameter passthrough from the active profile
- `app/service.py`: `service.ask` passes the active profile's `temperature` / `max_output_tokens` as per-call `options` to `provider.chat` (already supported: `external_http.py:170-179`, `local_llama.py:122-126`), and drives `ContextBuilder(max_context_chars=profile.max_context_chars)`. The model token window is applied at server start (`-c`): by the app on the Gemma path (`GEMMA_CONTEXT_TOKENS` → `GemmaProcessManager`), by the lease owner on the D29 path (recorded as external fact).
- Verification (unit/integration, offline): profile values appear in the built request (mocked provider), `max_context_chars` reaches the builder, no behavior change for the default profile.
- Map: `C_PARAMS`. Method: offline **TEST** (mock allowed here — this is wiring, not the comparison).

### P3. Task + scenario definition
- Fix the 6-question RAG task (SPEC R1) and the baseline/optimized profiles (full template texts, model, parameters; `context_window` in tokens, `max_context_chars` in characters; `parameter_reasons` for any equal parameter) into `local-data/d29/scenario.json` (frozen schema).
- The anonymized version (task, questions, expected results, negative cases, both parameter sets) is fixed in the permanent `README.md` D29 section.
- Map: `C_TASK_SET`, `C_README`. Method: **STATIC**.

### P4. Quantization facts
- Enumerate `*.gguf` under `GGUF_DIR` via `GET /api/models` (name, size bytes); derive quant labels from file names; additionally read the model directory listing facts directly before declaring unavailability.
- If ≥2 comparable quants of `Qwen3.6-35B-A3B` exist: LOCAL comparison on the same question set (parameters per run recorded). If only one: concrete file facts + concrete explanation. Hardware flags (e.g. `n_gpu_layers=0`) recorded as facts only.
- Write `local-data/d29/quantization.json` (frozen schema).
- Map: `C_QUANT`. Method: **LOCAL** (real file facts + real local runs if compared).

### P5. Measurement harness: ДО/ПОСЛЕ runs
- New `harness/d29_optimize.py` + `tests/scenarios/d29-optimization.py` + declaration `tests/scenarios/d29-optimization.json` (kind `live`, mirrors `d28-acceptance.json`).
- Harness behavior (one command: `test.bat scenario d29-optimization`):
  1. validate `AI_TEST_MODEL_*`, acquire lease; isolated TEMP dialogue DB + timestamped out dir (user DB untouched);
  2. read model facts via `/api/models`;
  3. run the 6 questions under the **baseline** profile (ДО) → per-question `answers` (full actual texts), `latency_s` (wall clock, per question + total), `memory` (peak of the model process), `cpu` (model-process CPU consumption; method + units stated, e.g. Windows `Get-Process`/`Get-Counter` process counters — no new Python dependencies);
  4. run the same 6 questions under the **optimized** profile (ПОСЛЕ) + the truncation probe (one positive question at deliberately low `max_tokens`; record the surfaced `finish_reason == "length"` / length error);
  5. evaluate quality per question against `expected_facts` / `negative_cases` by factual reading of the **full** answers (length/`finish_reason`/citation counts/keywords are not quality evidence);
  6. apply the R6 rule → `selected_profile`; write `scenario.json`, `comparison.json` (frozen fields; baseline defects preserved in `runs[0]`);
  7. `finally`: release lease, write `run_log.json` (`command`, `exit_code`, `model`, `timestamp`).
- All comparison measurements are **real local-model runs, never mock**; no paid network calls.
- Map: `C_COMPARE`, `C_PARAMS`, `C_PROFILE`, `C_PROMPT`, `C_REPRODUCIBLE`. Method: **LOCAL**.

### P6. Fix the selected profile as the app default
- The `selected_profile` (all parameters + template id) becomes the app's active default in code/config: `run_app.bat` starts the app with it **without manual changes**; the `baseline` profile remains selectable for measurement. Reversibility: profile selection, not a hardcoded replacement.
- Verification: app start via `run_app.bat` uses the selected profile (observed in the app's profile state / request parameters); baseline still reproducible.
- Map: `C_PROFILE`. Method: **LOCAL** (real app run).

### P7. Regression of preserved behaviors
- Execute via trusted `test.bat` / `project_run`: existing unit + integration suites **offline** (mock allowed for the isolated regression) + the **local LIVE** scenario for the local path.
- Five checks, each backed by at least one executed scenario: `chat` (no-RAG round-trip, non-empty answer), `local_provider` (app model via lease path), `network_provider` (DeepSeek path **offline with mock** — request building + switching; no real network call required), `history` (SQLite: create dialogue, ask twice, second turn sees first), `rag` (index state, search, citations verified).
- Write `local-data/d29/regression.json` (frozen schema, string values `"pass"`/actual state).
- Map: `C_REGRESSION`. Method: **TEST**.

### P8. Real browser UI check
- Start the app via trusted `run_app.bat`; a **real browser** performs ≥3 interactions: send message → receive answer; switch Local/Network; open/view history. UI language English. Code/HTML or API calls do not count.
- Save `local-data/d29/browser_test.json` (frozen schema) + screenshots under `local-data/d29/`.
- Map: `C_UI_BROWSER`. Method: **UI**.

### P9. Demo video + manifest
- Real recording of the running application, **1920×1080**, understandable subtitles, covering: baseline profile, parameter/template changes, actual answers + ДО/ПОСЛЕ comparison, quality before/after, speed/resources (memory + CPU), final selected profile.
- `local-data/d29/demo.mp4`; facts measured **from the file** (dimensions, duration, size) into `local-data/d29/manifest.json` (frozen schema; `coverage` ≥6). The independent check inspects the video itself, not only the manifest.
- Map: `C_VIDEO`. Method: **UI**.

### P10. README Day 29 section (Russian, anonymized)
- Permanent `README.md` gains the D29 section (SPEC R11): task/scenario, both parameter sets (with units: `context_window` tokens vs `max_context_chars` characters), actual results (quality/speed/memory/CPU before-after), quantization decision, final profile + how to run, how to reproduce. No absolute local paths, owner data, or secrets.
- Map: `C_README`. Method: **STATIC**.

### P11. Reproducibility proof
- One command: `test.bat scenario d29-optimization` (via `project_run`) reproduces the measurement; isolated TEMP data; no downloads; no `E:` modifications; no git operations. `run_log.json` written by the harness (`exit_code = 0`).
- Map: `C_REPRODUCIBLE`. Method: **LOCAL**.

### P12. Specification documents (this stage)
- `SPEC.md`, `PLAN.md`, `ACCEPTANCE.md` complete and self-sufficient; all 12 frozen criteria mapped in each document.
- Map: `C_SPEC_DOCS`. Method: **STATIC**.

## 2. Step ↔ criterion ↔ artifact ↔ method

| Step | Criteria | Artifacts (declared) | Method |
|---|---|---|---|
| P0 | `C_REPRODUCIBLE`, `C_COMPARE` | (preflight record in run log context) | LOCAL (real) |
| P1 | `C_PARAMS`, `C_PROMPT` | code (`config.py`, `builder.py`) | STATIC + offline unit |
| P2 | `C_PARAMS` | code (`service.py`) | offline TEST (mock OK) |
| P3 | `C_TASK_SET`, `C_README` | `scenario.json`, README | STATIC |
| P4 | `C_QUANT` | `quantization.json` | LOCAL |
| P5 | `C_COMPARE`, `C_PARAMS`, `C_PROFILE`, `C_PROMPT`, `C_REPRODUCIBLE` | `comparison.json`, `run_log.json`, `scenario.json` | LOCAL |
| P6 | `C_PROFILE` | code/config + `comparison.json.selected_profile` | LOCAL |
| P7 | `C_REGRESSION` | `regression.json` | TEST |
| P8 | `C_UI_BROWSER` | `browser_test.json` + screenshots | UI |
| P9 | `C_VIDEO` | `demo.mp4`, `manifest.json` | UI |
| P10 | `C_README` | README D29 section | STATIC |
| P11 | `C_REPRODUCIBLE` | `run_log.json` | LOCAL |
| P12 | `C_SPEC_DOCS` | SPEC/PLAN/ACCEPTANCE.md | STATIC |

## 3. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Lease model facts differ from Day 28 (file/quant/window) | Record the **real** facts at run time with sources; never guess; artifacts reflect the actual values |
| `context_window` (tokens) not verifiable from app side | It is lease-owned; record the lease/server fact + source; keep the field with the actual value; the character budget stays a separate tunable parameter |
| CPU/memory counters unavailable or ambiguous | Use Windows process counters available without new dependencies; state the exact method + units in the artifact; if truly unavailable, record the real limitation (not a fabricated number) |
| Truncation probe not reproducible | It is a controlled measurement on one positive question; the surfaced signal is recorded as observed, not assumed |
| Video toolchain gap | Reuse the D28 browser-recording approach; if a new CLI is strictly required, justify it here at implementation, not silently |
| Profile change breaks D26/D28 behavior | P7 regression gates completion; offline suites must pass; baseline profile stays selectable |

## 4. Dependencies

- No new Python dependencies beyond the existing `requirements.txt`. CPU/memory measurement uses OS process counters (PowerShell `Get-Process`/`Get-Counter` or equivalent) invoked from the harness.
- Video measurement uses a tool already available to the course toolchain; any strictly required new CLI is justified at implementation.
- week-05 index is a read-only input (hash verified before/after).

## 5. Execution constraints (carried from SPEC)

- Order: P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8 → P9 → P10 → P11 (P12 is this stage).
- After a failed step: verify the cause, form a new checked hypothesis, do not loop the same failing run and do not auto-escalate to the user.
- Tester independence: the Developer does not accept its own implementation; evidence artifacts are declared here and saved by the roles per the frozen contract.
