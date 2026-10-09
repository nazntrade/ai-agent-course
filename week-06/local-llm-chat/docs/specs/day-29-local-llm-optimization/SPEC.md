
## Authoritative manual implementation amendment — 2026-10-09

The user explicitly transferred D29 implementation to primary Codex. The original six questions, substantive quality, frozen twelve criteria, real measurements, regressions and Video + Code remain binding. Earlier lease-only descriptions below describe the prior execution arrangement and are superseded for the standalone local path by this section.

The application owns its standalone llama-server descriptor. A generation profile contains temperature, max_tokens, context_window in actual model TOKENS, full prompt_template, and a separate RAG character budget. Baseline starts with 8192 tokens; the candidate uses 6144 tokens. Both use the same Qwen3.6 Q5 weights. The optimization screen exposes profile selection and editable temperature/output/context/template. Applying a changed token window waits for generation, stops ONLY the owned descriptor, starts with the requested -c, and verifies /props default_generation_settings.n_ctx. Failed starts retain a visible error, restore the previous configuration and do not falsely claim an applied profile. Busy benchmark/profile switching rejects conflicting mutations. Borrowed external servers remain owner-controlled: context changes are rejected, not simulated. Network generation and its configuration remain unchanged.

The browser acceptance must exercise baseline/candidate/custom controls and prove returned actual runtime context and per-inference parameters, including restart/loading, the same-model quantization inventory, complete answers, and the truncation signal. Lab comparisons reuse the production ContextBuilder, retrieval, provider and bounded citation-correction path; they create no owner chat messages and preserve all timestamped results. Semantic grading is an independent inspection of full answers against the actual evidence and expected facts, never a citation-presence or keyword-only PASS. Candidate selection requires no factual/negative regression and a measured improvement; otherwise baseline remains selected. The selected default is recorded with the review and follows the model identity.

Additional user deliverables: Word explanation in Current task, explaining our actual code/UI, temperature/output/context, weight and KV quantization distinctions, before/after measurements, safeguards and relation to 00 Local_LLM_Week_6_Detailed_Notes.docx. Include actual application screenshots; render and visually inspect every document page. Record actual running UI to 1920x1080 MP4 with explanatory subtitles and verify duration, streams and representative frames. These deliverables do not substitute for independent product verification.

# SPEC: Day 29 — Local LLM Optimization

Module: `week-06/local-llm-chat`. Stage: PRODUCT / SPECIFICATION. This document is self-sufficient: a reader who has not seen the conversation can implement and verify Day 29 from it together with `PLAN.md` and `ACCEPTANCE.md`. All artifact paths are relative to the module root (`week-06/local-llm-chat/`).

## 1. Goal

Day 29 optimizes the **already-working** local LLM chat module. Starting from a fixed baseline profile, it (a) defines a concrete reproducible task with checkable facts and negative cases, (b) measures quality, speed, and resources "ДО" (baseline) and "ПОСЛЕ" (optimized) with the **same task and the same model**, (c) tunes three parameters (temperature, max_tokens, context_window) and the prompt template, (d) compares available quants of the chosen model or records verified unavailability, (e) selects an optimized profile by an explicit rule and fixes it as the app's active default, and (f) proves nothing else broke (regression, English UI in a real browser, demo video 1920×1080, reproducibility through the trusted `test.bat`).

The module already implements Local/Network/External providers, an optional RAG mode over the week-05 index, citation verification, and SQLite dialogue history (D26/D28). Day 29 **preserves** all of that and adds only the profile, measurement, and evidence-capture capabilities it needs.

## 2. Scope

### In scope
- `app/config.py` — profile parameters `temperature`, `max_tokens`, `context_window` explicitly settable per named profile; the optimized profile is the active default.
- `app/context/builder.py` — system prompt template selectable by named template id (baseline `SYSTEM_RAG` text preserved as the named template `default`; an optimized template added as a separate named template); `max_context_chars` (the app-side RAG **character** budget, a separate additional parameter) driven by the active profile; the model token `context_window` is recorded per run as an external fact (R2).
- `app/service.py` / `app/providers/*` — the active profile's parameters are actually applied to the request (providers already accept per-call `options`; the service must pass the profile values instead of relying on hardcoded constructor values only).
- `harness/d29_optimize.py` (new) + `tests/scenarios/d29-optimization.py` (new) + `tests/scenarios/d29-optimization.json` (new) — the reproducible scenario that produces every `local-data/d29/` artifact.
- `local-data/d29/` — all evidence artifacts (section 7).
- `README.md` — Day 29 section in Russian (anonymized).

### Out of scope
- Migrating or replacing the stack; adding new dependencies (see section 8).
- Modifying any `week-05/**` file or the week-05 index (read-only source).
- Reading `.env` or outputting secrets; downloading or re-quantizing models.
- Modifying protected rule/role/permission files or the trusted `*.bat` entry points (Configurator-owned).
- Changing the frozen acceptance criteria or D26/D28 behavior.
- Running unrelated third-party models to manufacture a quant comparison.

## 3. Fixed baseline facts (non-secret, from the repository)

These facts pin the baseline and must not be guessed during implementation. If implementation discovers the actually-applied value differs, record the **real value and its source**; never substitute a guess.

| Fact | Value | Source |
|---|---|---|
| App model actually exercised in the prior LIVE acceptance (Day 28) | `Qwen3.6-35B-A3B-UD-Q5_K_XL`, quant label **Q5_K_XL** | `README.md` Day 28 section, run `run-20261008T173122Z` (external profile, API alias `local`, KIND=local loopback lease) |
| Baseline temperature (app model path) | **0.0** — hardcoded when constructing `ExternalHttpProvider` | `app/__main__.py:79` |
| Baseline max_tokens | runtime-applied value read from `ChatResult.parameters.max_output_tokens` during the ДО run; config default **1024** (`GEMMA_MAX_OUTPUT_TOKENS`). A local `.env` may override it and is never read here | `app/providers/external_http.py:170-201` (request payload + recorded parameters); `app/config.py` default |
| Baseline context_window (model, **tokens** — the canonical profile parameter) | The D29 model (`Qwen3.6-35B-A3B-UD-Q5_K_XL`) is served by a **leased** llama-server: its token-level context (`-c`) is set by the lease owner at server start, outside the app. The app's chat request payload contains **no** context-window field (`external_http.py:173-179` sends only `temperature`/`max_tokens`), so the token window cannot be changed per request from the app. The effective value is an **external fact**: at implementation it is read from the actual lease/server facts and recorded with its source; it is never guessed. The D28 demo run's "48000" was `RAG_MAX_CONTEXT_CHARS` (a character budget), **not** a token window — do not conflate them (`README.md` Day 28 section). | `app/providers/external_http.py:173-179`; `README.md` Day 28 section; lease facts at run time |
| App-side RAG character budget (separate **additional** parameter, **characters**) | **12000** characters — `RAG_MAX_CONTEXT_CHARS` → `ContextBuilder.max_context_chars`: the prompt-construction text budget (how many characters of history+fragments the app sends). It is a different quantity from the model token window: changing it does not configure the model window. It stays explicitly tunable per profile and is recorded as the additional field `profile.max_context_chars`, but it does **not** replace the canonical `context_window`. | `app/config.py:45`; `app/context/builder.py:38,64,119` |
| Model token window on the app-owned path (fact for configurability) | `GEMMA_CONTEXT_TOKENS` (default **8192** tokens) → `GemmaProcessManager.context_tokens` → llama-server start arg `-c`. This is the only path where the app itself sets the model token window, at server start (not per request). The standalone Gemma is **not** the D29 model; this fact documents where a token window is actually configurable/applied. | `app/config.py:25`; `app/local_process/gemma_manager.py:41,142` |
| Baseline prompt template (RAG) | full text of module constant `SYSTEM_RAG` (strict `[n]` citation rules, insufficient-context honesty rule, untrusted-data rule) | `app/context/builder.py:20-34` |
| Parameter application today | `service.ask` calls `provider.chat(messages)` **without** options; providers fall back to constructor defaults (`temperature=0.0`, `max_output_tokens` from settings). Per-call options (`max_output_tokens`, `temperature`) are already supported by `ExternalHttpProvider` and `LocalLlamaProvider` | `app/service.py:374`; `app/providers/external_http.py:170-179`; `app/providers/local_llama.py:122-126` |
| Model enumeration | `GET /api/models` recursively lists `*.gguf` under `GGUF_DIR`, returns `{name (relative posix), path, size (bytes), source: "gguf"}`, skips `mmproj*` | `app/api/routes.py:164-187` |
| Standalone local model (not the D29 model) | `gemma-4-12b-it-qat-q4_0.gguf` (QAT Q4_0), context 8192 tokens via `-c` | `app/config.py`; `app/local_process/gemma_manager.py:142` |
| RAG index (read-only) | week-05 `knowledge-agent/local-data/index.db`, collection `agents-survey` | `app/config.py`; D28 record |

**Model to use for Day 29 (baseline model).** The model is **constant** between the "ДО" and "ПОСЛЕ" runs: **`Qwen3.6-35B-A3B-UD-Q5_K_XL`**, reached through the same external-profile / local-alias lease path used in Day 28 (KIND=local, loopback, lease acquire/use/release). This is a fact from the Day 28 acceptance record, not a guess. During implementation the exact GGUF file facts (file name, quant label, size) are re-read via `/api/models` and written into the artifacts; if the file set differs, the real file facts are recorded.

> **Do not confuse the agent model with the app model.** The LLM running this task is not the app model and must never appear in any artifact. `.env` is never read.

## 4. Requirements

Each requirement lists the frozen criterion ID(s) it satisfies. All are **Priority: Must**.

### R1. Concrete, reproducible task — `C_TASK_SET`

The Day 29 task: **RAG-grounded Q&A over the week-05 `agents-survey` index** (the module's RAG mode). RAG gives stable, checkable expected facts and clean negative cases, so the task is reproducible across runs and machines.

- Canonical question set: **6 questions** (contract requires ≥3): 4 positive, 2 negative probes. Texts may be reworded; the expected facts and negative types must stay.
  1. (positive, architecture) "What roles do the profile, memory, planning, and action modules play in the architecture of LLM-based autonomous agents, according to the survey?" — expected facts: the four modules are named and their distinct roles are described.
  2. (positive, memory) "According to the survey, how do short-term and long-term memory differ, and how are memory reading, writing, and reflection used?" — expected facts: short-term vs long-term distinction; read/write/reflection usage.
  3. (positive, planning) "According to the survey, how does planning with feedback differ from planning without feedback?" — expected facts: with-feedback planning iterates/revises; without-feedback does not.
  4. (positive, evaluation) "According to the survey, what approaches evaluate LLM-based autonomous agents, and what limitations do they have?" — expected facts: named evaluation approaches; at least one stated limitation.
  5. (negative, **insufficient-context**) "What is the exact MD5 checksum of the original agents-survey.pdf?" — expected facts: the fragments do **not** state any checksum. Negative case: the answer must say the evidence does not establish it and must **not** invent a value.
  6. (negative, **hallucination**) "What is the author's full postal address, as stated in the survey?" — expected facts: no postal address exists in the index. Negative case: the answer must **not** fabricate an address; with RAG it should report no supporting source.
- A **truncation** probe is a controlled measurement (not a fixed question): one positive question (e.g. #1) re-run under a deliberately low `max_tokens`; the app must surface a truncation signal (`finish_reason == "length"` or a length error) instead of silently passing a cut answer. Recorded in `comparison.json` (additional field `truncation_probe`).

**Artifact `local-data/d29/scenario.json`** (exact frozen schema; additional fields allowed):
```json
{
  "task": "<string, ≥10 chars — the task definition>",
  "questions": [
    { "question": "...", "expected_facts": ["...", "..."], "negative_cases": ["..."] }
  ],
  "baseline_profile": {
    "model": "Qwen3.6-35B-A3B-UD-Q5_K_XL",
    "temperature": 0.0,
    "max_tokens": "<int — actual runtime-applied value read from ChatResult.parameters during the DO run; config default 1024>",
    "context_window": "<int, TOKENS — model token window of the leased server, external fact read at run time with its source; never guessed>",
    "max_context_chars": 12000,
    "prompt_template": "<full baseline template text, ≥10 chars>"
  },
  "optimized_profile": {
    "model": "Qwen3.6-35B-A3B-UD-Q5_K_XL",
    "temperature": "<chosen per R6>",
    "max_tokens": "<chosen per R6>",
    "context_window": "<int, TOKENS — lease-owned; equal to baseline with the fixed recorded reason (R2)>",
    "max_context_chars": "<int, chosen per R6 — app-side character budget>",
    "prompt_template": "<full optimized template text, ≥10 chars>"
  }
}
```
- `questions` has ≥3 entries; each has `question` (string), `expected_facts` (array of strings, ≥1), `negative_cases` (array of strings; empty for pure-positive questions).
- Both profiles carry the **full template text** (not just an id) and the model name; `prompt_template` strings must be ≥10 chars.
- `context_window` is an integer in **tokens** (the model token window). `max_context_chars` is an integer in **characters** (the app-side RAG budget, additional field). The two are different quantities and must not be conflated.
- The anonymized version of task + parameters + scenario is fixed in the permanent `README.md` (R11).

### R2. Three explicitly tunable parameters, applied and recorded — `C_PARAMS`

`temperature`, `max_tokens`, `context_window` must be **explicitly settable per profile in the app/harness** and **actually change what is sent to / recorded for the model**; a profile that is only stored is not applied. The canonical `context_window` is the **model token window (tokens)**; the app-side RAG **character** budget is a separate additional parameter (`max_context_chars`, characters) and never replaces it.

- **Baseline profile** (from section 3 facts): `temperature = 0.0`; `max_tokens` = the runtime-applied value read from `ChatResult.parameters` during the ДО run (config default 1024); `context_window` = the leased server's token window — an **external fact** read from the actual lease/server facts at run time and recorded with its source (never guessed; the D28 "48000" was the character budget, not this value); `prompt_template` = full `SYSTEM_RAG` text; additional `max_context_chars` = 12000 (characters).
- **Optimized profile**: each of the three canonical parameters is either (a) different from baseline or (b) equal to baseline **with a fixed, recorded reason** (additional field in `scenario.json`, e.g. `parameter_reasons`). Intended starting point, refined by the R6 rule: `temperature = 0.2`, `max_tokens = 2048`. `context_window` (tokens) is **lease-owned**: the token window is set by the lease owner at llama-server start (`-c`) and the app's chat payload has no context-window field, so it is not changeable per request from the app without a lease restart — when its value is therefore identical in both runs, that fixed reason is recorded (the contract's "обоснованно одинаков с зафиксированной причиной" branch). The app-side character budget is tuned: `max_context_chars` = 24000 (characters, additional field).
- Application mechanics (current code supports it; the service must use it): providers already accept per-call `options` with `max_output_tokens`/`temperature` (`external_http.py:170-179`, `local_llama.py:122-126`); `service.ask` currently calls `provider.chat(messages)` with **no** options (`service.py:374`) — Day 29 passes the active profile's values as options. The model token `context_window` is applied at llama-server start via `-c` — by the app itself on the app-owned Gemma path (`GEMMA_CONTEXT_TOKENS` → `GemmaProcessManager`, `gemma_manager.py:142`) and by the lease owner on the D29 lease path (external fact, recorded as such, not faked). The character budget `max_context_chars` is applied as `ContextBuilder.max_context_chars`.
- Every run records its **full** profile (all three parameters + model + prompt template) in `comparison.json → runs[].profile` / `runs[].model` / `runs[].prompt_template`. Changing several factors together is acknowledged: attribution is per-run full-profile, not per-factor ablation (the task does not require single-factor isolation).

### R3. Prompt template changed for the case — `C_PROMPT`

- Baseline template = full text of `SYSTEM_RAG` (`builder.py:20-34`), preserved in code as the named template `default`.
- Optimized template = a **revised** template stored in code as a separate named template (e.g. `optimized`), which (a) keeps the strict `[n]` citation rule, (b) sharpens the insufficient-context and no-fabrication instructions (targeting questions 5–6), (c) keeps the untrusted-data rule and consistent structure. The two template texts must differ and both remain in code (anonymized).
- `service.ask`/`ContextBuilder` must select the template by the active profile's named template id (today the template is chosen unconditionally: `builder.py:50`).
- Each run in `comparison.json` records its used template (full text in `runs[].prompt_template`); the comparison shows the template's effect (improvements and/or trade-offs; baseline errors preserved, not erased).
- The optimized template must not weaken the existing citation/verification logic (RAG citation checks stay on).

### R4. Quantization: compare available quants or record verified unavailability — `C_QUANT`

Scope: **comparable GGUF quant variants of the chosen model** (`Qwen3.6-35B-A3B`).

- Enumerate via `GET /api/models` (recursive `*.gguf` under `GGUF_DIR`; returns name, size in bytes; the quant label is derived from the file name, e.g. `Q5_K_XL`). Before declaring a second quant unavailable, read the model directory listing facts directly (file name, quant tag, size) — an empty search result alone is not proof of unavailability.
- If ≥2 comparable quants of the chosen model exist: run a **LOCAL** comparison of the available quants on the same question set and record each run's parameters (additional fields in `quantization.json`).
- If only one quant of the chosen model exists: record the **concrete file facts** (file name, quant label, size) and a concrete explanation of why a comparison is unavailable. Do **not** run unrelated third-party models to manufacture a comparison.
- Hardware parameters are recorded as **facts**: do not present `n_gpu_layers=0` (or similar) as proof of full weight residency in RAM.

**Artifact `local-data/d29/quantization.json`** (exact frozen schema; additional fields allowed):
```json
{
  "available_models": [
    { "file": "<relative gguf file name>", "quant": "<quant label>", "size_mb": 1234.5 }
  ],
  "decision": "compared | unavailable",
  "explanation": "<string, ≥10 chars>"
}
```
`available_models` has ≥1 entry with `file`, `quant`, `size_mb`; `decision` is exactly `"compared"` or `"unavailable"`; `explanation` ≥10 chars. When `compared`, additional fields (tested quants, per-quant measurements) carry the comparison data.

### R5. Measured comparison "ДО" vs "ПОСЛЕ" on one question set — `C_COMPARE`

For the **same model and same questions**, run the baseline profile (ДО, `runs[0]`) and the optimized profile (ПОСЛЕ, `runs[1]`):

- **Quality** — judged against `expected_facts` and `negative_cases` **only**. Length, `finish_reason`, citation counts, or keyword overlap are **not** quality evidence; a valid JSON answer or a saved field does not prove meaning. Each run's `quality` records, per question, which expected facts were actually present in the answer (factual reading of the full answer) and whether each negative case was violated. Explicit negative outcomes:
  - a fabricated answer containing an external/invented citation → fail;
  - an insufficient-context question answered with an invented value instead of "not established by the evidence" → fail;
  - a truncated answer presented as complete → fail.
- **Speed** — per-run total and per-question `latency_s` (real wall-clock measurements of local inference).
- **Resources** — per-run `memory` (peak memory of the model process during the run, measured, with the measurement method stated) **and** per-run `cpu` (additional field `metrics.cpu` on top of the canonical `memory`): actual CPU consumption of the model process for the run, with the real measurement method, units (e.g. cumulative CPU seconds and/or % of one core), and storage location stated in the artifact. The measurement must use tools already available to the course toolchain without new Python dependencies (e.g. the Windows process CPU counter via PowerShell `Get-Counter`/`Get-Process`, or an equivalent); if the exact counter is not available, record the actual method used, never a fabricated number.
- **Baseline errors preserved**: every baseline defect (wrong answer, invented value, truncation) stays visible in `runs[0]`; the comparison reports what the optimized profile fixes and what it does not.
- All measurements are **real local-model runs, never mock**. No paid network calls are required or used for the comparison.

**Artifact `local-data/d29/comparison.json`** (exact frozen fields; additional fields allowed):
```json
{
  "runs": [
    {
      "profile": { "temperature": 0.0, "max_tokens": 1024, "context_window": "<int, tokens — leased server token window, external fact>", "max_context_chars": 12000 },
      "model": "Qwen3.6-35B-A3B-UD-Q5_K_XL",
      "prompt_template": "<full baseline template text>",
      "answers": [ { "question": "...", "text": "<full actual answer text>" } ],
      "quality": {
        "per_question": [ { "question": "...", "expected_facts_found": ["..."], "negative_cases_violated": ["..."] } ],
        "summary": "<factual quality assessment>"
      },
      "metrics": {
        "latency_s": 12.3,
        "memory": { "peak_mb": 8192, "method": "<how measured>" },
        "cpu": { "value": 0.0, "units": "<e.g. cumulative CPU seconds | % of one core>", "method": "<how measured>" }
      }
    },
    { "<same shape for the optimized profile run>": "" }
  ],
  "selected_profile": { "temperature": 0.2, "max_tokens": 2048, "context_window": "<int, tokens — lease-owned value, equal to baseline with the fixed recorded reason>", "max_context_chars": 24000 },
  "truncation_probe": { "max_tokens": 64, "finish_reason": "length", "observed": "<what the app surfaced>" }
}
```
Frozen assertions: `runs` ≥2; each of `runs[0]` and `runs[1]` has `answers` (≥1, actual texts), `quality`, `metrics.latency_s`, `metrics.memory`; `runs[0]` additionally has `profile.temperature`, `profile.max_tokens`, `profile.context_window`, `model`, `prompt_template`; `runs[1].prompt_template` exists; `selected_profile` exists with `temperature`, `max_tokens`, `context_window`.

### R6. Optimized profile selected by an explicit rule and fixed as the app default — `C_PROFILE`

The optimized profile is **selected**, not asserted. Rule (fixed before measurement):
1. Run the task under the baseline profile (ДО).
2. Run the task under the optimized candidate (ПОСЛЕ).
3. The optimized profile is selected when it (a) does not increase negative-case violations, (b) does not regress factual correctness on the positive questions, and (c) improves at least one measured axis (a baseline item that failed, `latency_s`, or `memory`), with trade-offs stated. If no candidate satisfies the rule, the baseline remains the selected profile and the artifacts say so with the measured evidence (in which case R2's "differ or justified-equal" is satisfied by the recorded reasons).
4. `selected_profile` (all three parameters) is written to `comparison.json` and fixed **in code/config as the app's active default profile**: after Day 29, `run_app.bat` starts the app with the selected profile active **without any manual changes**; the baseline profile remains selectable for measurement.

### R7. Regression: preserved behaviors still work — `C_REGRESSION`

After all Day 29 changes, the preserved behaviors must still work:
- **chat** — a normal no-RAG chat round-trip returns a non-empty answer.
- **local_provider** — the local provider path (app model via the external-profile/lease path) serves a question.
- **network_provider** — the Network (DeepSeek) provider path is checked **offline with a mock** (no real network call is required; request building and provider switching are verified against the mock).
- **history** — a dialogue's history persists in SQLite (create dialogue, ask twice, the second turn sees the first).
- **rag** — RAG mode works end to end: index state reachable, search returns fragments, citations verified against supplied fragments.

Method: the trusted `test.bat` scenarios via `project_run` — unit/integration **offline** (mock allowed for the isolated regression) plus the **local LIVE** scenario for the local path. Each of the five checks is backed by at least one executed scenario, and the existing unit + integration suites must still pass.

**Artifact `local-data/d29/regression.json`** (exact frozen schema — string values, not booleans):
```json
{ "chat": "pass", "local_provider": "pass", "network_provider": "pass", "history": "pass", "rag": "pass" }
```
A failing check writes its actual state (e.g. `"fail"`) with a sibling detail field, and the criterion is not satisfied.

### R8. English UI verified in a real browser — `C_UI_BROWSER`

- The app is started via the trusted `run_app.bat`; a **real browser** performs the interactions (code/HTML inspection or API calls do **not** count).
- At least **3** real interactions, covering at least: send a message and receive an answer; switch Local/Network provider; open/view dialogue history. The UI language is **English**.
- **Artifact `local-data/d29/browser_test.json`** (exact frozen schema):
```json
{ "actions": [ { "action": "...", "target": "...", "observed": "..." } ], "result": "pass" }
```
`actions` ≥3 with the real observed results; `result` is exactly `"pass"` only if every recorded interaction succeeded. Saved screenshots (under `local-data/d29/`) are additional evidence referenced by `actions` entries.

### R9. Honest demo video + manifest — `C_VIDEO`

- `local-data/d29/demo.mp4` — **real recording** of the running application, **1920×1080**, understandable subtitles. Coverage (visible in the video): the baseline profile, the parameter/template changes, actual answers and the ДО/ПОСЛЕ comparison, quality before/after, speed/resources, the final selected profile.
- Video facts are **measured from the file** (dimensions, duration, size), not hand-typed.

**Artifact `local-data/d29/manifest.json`** (exact frozen schema):
```json
{
  "video_path": "local-data/d29/demo.mp4",
  "size_bytes": 1234567,
  "duration_s": 95,
  "width": 1920,
  "height": 1080,
  "coverage": ["baseline profile", "parameter changes", "prompt template change", "actual answers / DO-POSLE comparison", "quality before/after", "speed and resources", "final selected profile"]
}
```
`video_path` is a relative path (≥5 chars); `size_bytes` ≥100000; `duration_s` ≥30; `width` = 1920; `height` = 1080; `coverage` ≥6 strings. The independent check inspects the video itself, not only the manifest.

### R10. Reproducibility through the trusted entry point — `C_REPRODUCIBLE`

- One command reproduces the whole measurement: `test.bat scenario d29-optimization` (declaration `tests/scenarios/d29-optimization.json`, kind `live`; implementation `tests/scenarios/d29-optimization.py`; runner `harness/d29_optimize.py`).
- The LIVE run uses `AI_TEST_MODEL_*` from the environment (selected local model, KIND=local loopback lease): **lease acquire → use → release in `finally`**; a borrowed/foreign model is never stopped.
- **Isolated TEMP data**: an isolated TEMP dialogue DB and a timestamped output directory; the user database is never touched.
- **No model downloads**, no modifying/deleting/moving anything on drive `E:` (the model directory is read-only), no `git commit`/`push`.
- **Artifact `local-data/d29/run_log.json`** (exact frozen schema):
```json
{ "command": "test.bat scenario d29-optimization", "exit_code": 0, "model": "Qwen3.6-35B-A3B-UD-Q5_K_XL", "timestamp": "2026-10-09T..." }
```
`command` ≥5 chars; `exit_code` = 0; `model` and `timestamp` present.

### R11. README — Russian Day 29 section, anonymized — `C_README`

The permanent `README.md` (not only under `local-data/`) gains a **Day 29** section in **Russian** containing:
- the anonymized task and scenario (questions, expected results, negative cases);
- the anonymized **baseline** and **optimized** parameter sets (temperature, max_tokens, context_window in **tokens**, prompt template ids; the app-side character budget `max_context_chars` labeled as characters, a separate parameter);
- the **actual** comparison results: quality (expected facts / negative cases), speed (latency), resources (memory **and CPU**) — before and after;
- the quantization decision (compared or unavailable + file facts);
- the final selected profile and how to run the app with it via `run_app.bat`;
- how to reproduce via `test.bat scenario d29-optimization`.

De-anonymization rules: no local absolute paths, no owner data, no secrets, no private infrastructure details. The application UI stays **English**.

### R12. Self-sufficient specification documents — `C_SPEC_DOCS`

`SPEC.md` (this file), `PLAN.md`, and `ACCEPTANCE.md` are complete and self-sufficient (exactly 3 `.md` files in this directory): every one of the 12 frozen criteria is mapped to at least one requirement here, at least one implementation step in `PLAN.md`, and at least one acceptance row with a concrete check method and declared artifacts in `ACCEPTANCE.md`.

## 5. Non-functional requirements

- **No new dependencies** beyond the module's existing `requirements.txt`. Video measurement uses a tool already available to the course toolchain; if a new CLI is strictly required it must be justified in `PLAN.md` and added at implementation stage, not assumed here.
- **Security:** never read or print `.env`/keys; no secrets in any artifact, log, or browser session.
- **Read-only week-05 index:** never modified; verify its hash is unchanged before/after (reuse the D28 `source_hashes` approach).
- **Language:** code/comments/docstrings in English; `README.md` and user communication in Russian; user-facing UI in English.
- **Reversibility:** the optimized profile is a selectable profile, not a hardcoded replacement; the baseline profile remains available so "ДО" can be reproduced.
- **Backward compatibility:** all D26/D28 endpoints and behaviors preserved; existing unit + integration tests pass.

## 6. Architecture (Day 29 additions)

```
GenerationProfile { temperature, max_tokens, context_window (tokens, canonical),
                    max_context_chars (characters, additional), prompt_template_id }
   ├─ "baseline"  = (0.0, <runtime max_tokens from ChatResult.parameters>,
   │                 <lease token window — external fact>, 12000, "default")
   └─ "optimized" = (T*, M*, <lease token window — same, fixed recorded reason>,
                     C* chars, "optimized")                  <- chosen by R6 rule, fixed as active default

service.ask(question, rag_enabled, ...)
   └─ ContextBuilder(max_context_chars=profile.max_context_chars, template_id=profile.prompt_template_id)
      └─ provider.chat(messages, options={temperature, max_output_tokens from profile})
         └─ leased llama-server (KIND=local loopback; token window -c set by lease owner;
            lease acquire/use/release in finally)

harness/d29_optimize.py (invoked by tests/scenarios/d29-optimization.py)
   1. validate live profile (AI_TEST_MODEL_*), acquire lease; isolated TEMP db; timestamped out dir
   2. enumerate model quants via /api/models  -> quantization.json
   3. run task under baseline profile (ДО)    -> per-question records (answers, latency, memory, CPU)
   4. run task under optimized profile (ПОСЛЕ)+ truncation probe
   5. evaluate quality vs expected_facts / negative_cases; apply R6 rule
   6. write scenario.json, comparison.json (selected_profile)
   7. finally: release lease, write run_log.json
   (browser and video are separate controlled steps) -> browser_test.json, demo.mp4, manifest.json
```

Prompt template selection lives in `app/context/builder.py`; parameter passthrough lives in `app/service.py` → provider `options` (already supported by `external_http.py` / `local_llama.py`); nothing in the RAG retrieval/citation path is weakened.

## 7. Acceptance artifacts (declared)

All under `local-data/d29/` (exact schemas in this SPEC, section 4; steps in `PLAN.md`):

| Artifact | Frozen criterion |
|---|---|
| `scenario.json` | `C_TASK_SET` |
| `comparison.json` | `C_COMPARE`, `C_PARAMS`, `C_PROFILE`, `C_PROMPT` |
| `quantization.json` | `C_QUANT` |
| `regression.json` | `C_REGRESSION` |
| `browser_test.json` (+ screenshots) | `C_UI_BROWSER` |
| `demo.mp4`, `manifest.json` | `C_VIDEO` |
| `run_log.json` | `C_REPRODUCIBLE` |
| `README.md` Day 29 section | `C_README` |
| `docs/specs/day-29-local-llm-optimization/{SPEC,PLAN,ACCEPTANCE}.md` | `C_SPEC_DOCS` |

No secrets in any artifact.

## 8. Out-of-scope clarifications

- SPEC acceptance (these documents) is **not** product acceptance; the 12 product criteria are closed only by independent implementation + testing evidence.
- This SPEC does not run preflight, app tests, or LIVE; those belong to IMPLEMENTATION.
- The agent model is never an app model and never appears in any artifact.
- `n_gpu_layers=0` and similar hardware flags are recorded as facts only; they are not evidence of memory residency.
