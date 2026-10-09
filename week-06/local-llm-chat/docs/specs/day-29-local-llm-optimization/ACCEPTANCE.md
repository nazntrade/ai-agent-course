
## Authoritative manual implementation amendment — 2026-10-09

The user explicitly transferred D29 implementation to primary Codex. The original six questions, substantive quality, frozen twelve criteria, real measurements, regressions and Video + Code remain binding. Earlier lease-only descriptions below describe the prior execution arrangement and are superseded for the standalone local path by this section.

The application owns its standalone llama-server descriptor. A generation profile contains temperature, max_tokens, context_window in actual model TOKENS, full prompt_template, and a separate RAG character budget. Baseline starts with 8192 tokens; the candidate uses 6144 tokens. Both use the same Qwen3.6 Q5 weights. The optimization screen exposes profile selection and editable temperature/output/context/template. Applying a changed token window waits for generation, stops ONLY the owned descriptor, starts with the requested -c, and verifies /props default_generation_settings.n_ctx. Failed starts retain a visible error, restore the previous configuration and do not falsely claim an applied profile. Busy benchmark/profile switching rejects conflicting mutations. Borrowed external servers remain owner-controlled: context changes are rejected, not simulated. Network generation and its configuration remain unchanged.

The browser acceptance must exercise baseline/candidate/custom controls and prove returned actual runtime context and per-inference parameters, including restart/loading, the same-model quantization inventory, complete answers, and the truncation signal. Lab comparisons reuse the production ContextBuilder, retrieval, provider and bounded citation-correction path; they create no owner chat messages and preserve all timestamped results. Semantic grading is an independent inspection of full answers against the actual evidence and expected facts, never a citation-presence or keyword-only PASS. Candidate selection requires no factual/negative regression and a measured improvement; otherwise baseline remains selected. The selected default is recorded with the review and follows the model identity.

Additional user deliverables: Word explanation in Current task, explaining our actual code/UI, temperature/output/context, weight and KV quantization distinctions, before/after measurements, safeguards and relation to 00 Local_LLM_Week_6_Detailed_Notes.docx. Include actual application screenshots; render and visually inspect every document page. Record actual running UI to 1920x1080 MP4 with explanatory subtitles and verify duration, streams and representative frames. These deliverables do not substitute for independent product verification.

# ACCEPTANCE: Day 29 — Local LLM Optimization

Module: `week-06/local-llm-chat`. Companion to `SPEC.md` (requirements, facts, artifact schemas) and `PLAN.md` (steps P0–P12). This document defines, per frozen criterion, the concrete check method, declared artifacts, and PASS/FAIL conditions. Acceptance is issued only by the independent Tester; the Developer does not accept its own implementation. Paths relative to the module root.

## 1. Acceptance criteria (per frozen criterion ID)

### A1. `C_TASK_SET` — concrete reproducible task
- **Requirement:** fixed task with a reproducible set of ≥3 questions, each with checkable `expected_facts` and `negative_cases` (invented answer with external citation, insufficient context when a document is found, wrong/truncated answer), plus the pre-optimization baseline profile (model, temperature, max_tokens, context_window, prompt template).
- **Method:** STATIC.
- **Declared artifact:** `local-data/d29/scenario.json` — `task` ≥10 chars; `questions` ≥3 (each: `question`, `expected_facts` ≥1); `baseline_profile.{temperature,max_tokens,context_window,prompt_template≥10}`; `optimized_profile.{temperature,max_tokens,context_window,prompt_template≥10}`.
- **PASS:** schema satisfied; questions cover positive + negative types (invention, insufficient-context, truncation probe); baseline profile matches SPEC section 3 facts (`temperature 0.0`; `max_tokens` = runtime-applied value; `context_window` in **tokens**, external lease fact with source; full `SYSTEM_RAG` template text); anonymized version present in permanent README.
- **FAIL:** guessed values without source; negative types missing; template id instead of full text; README missing the scenario.

### A2. `C_PARAMS` — three parameters tunable, applied, recorded
- **Requirement:** `temperature`, `max_tokens`, `context_window` explicitly settable in app/harness and actually applied; differ between baseline and optimized profiles **or** equal with a fixed recorded reason; each run records its full parameter set.
- **Method:** STATIC (code) + LOCAL (recorded per run).
- **Declared artifact:** `local-data/d29/comparison.json` — `runs` ≥2; `runs[0].profile.{temperature,max_tokens,context_window}`, `runs[0].model`, `runs[0].prompt_template`, `runs[0].metrics.{latency_s,memory}`.
- **PASS:** profile values are applied to the actual request/context (not only stored); `context_window` is in **tokens** and its source is recorded (lease/server fact); `max_context_chars` (characters, additional) is clearly separated and not conflated with the token window; each run carries the full profile; any equal parameter has a fixed recorded reason (`parameter_reasons`).
- **FAIL:** profile stored but not applied; character budget presented as the model context window; missing per-run parameters.

### A3. `C_PROFILE` — optimized profile selected by rule and fixed as default
- **Requirement:** a working optimized profile for the concrete task, fixed in code/config as the app's active default, justified by actual `comparison.json` results; reproducible via `run_app.bat` without manual changes.
- **Method:** LOCAL.
- **Declared artifact:** `local-data/d29/comparison.json` — `selected_profile.{temperature,max_tokens,context_window}`.
- **PASS:** the SPEC R6 rule is applied to measured data (no new negative-case violations, no factual regression, ≥1 improved axis or recorded reason for baseline retention); `selected_profile` matches the fixed default in code/config; `run_app.bat` starts the app with it active; baseline remains selectable.
- **FAIL:** asserted profile without measured justification; default not applied at start; baseline no longer reproducible.

### A4. `C_PROMPT` — prompt template changed for the case
- **Requirement:** old and new templates stored in code (anonymized); each run records its used template; the template's effect is visible in the comparison (improvements and/or trade-offs; baseline errors preserved).
- **Method:** STATIC.
- **Declared artifact:** `local-data/d29/comparison.json` — `runs[0].prompt_template`, `runs[1].prompt_template` exist.
- **PASS:** both full template texts in code and differ; selection by the active profile's template id; per-run template recorded; the comparison shows the template's measurable effect on the task's negative/positive questions.
- **FAIL:** templates identical or one removed; template effect not visible in the comparison; citation rules weakened.

### A5. `C_COMPARE` — honest ДО/ПОСЛЕ comparison
- **Requirement:** comparison on the **same** question set: quality (per expected facts and negative cases — not length/`finish_reason`/citations/keywords), speed (latency), resources (memory **and CPU**). Each run: `answers[]` with actual texts, `quality`, `metrics.{latency_s, memory}`; baseline errors preserved; real local measurements (not mock); full profile + prompt_template per run.
- **Method:** LOCAL.
- **Declared artifact:** `local-data/d29/comparison.json` — `runs` ≥2; per `runs[0]`/`runs[1]`: `answers` ≥1, `quality`, `metrics.latency_s`, `metrics.memory`; additional `metrics.cpu` (value, units, method) present for each run.
- **PASS:** both runs on the same model + same questions; quality assessed by factual reading of full answers against `expected_facts`/`negative_cases`; latency is real wall-clock; memory + CPU are measured with stated methods and units; baseline defects visible in `runs[0]`; no mock in the comparison.
- **FAIL:** quality judged by length/keywords/`finish_reason`; mock or network runs in the comparison; CPU missing or fabricated; baseline errors erased.

### A6. `C_QUANT` — quantization compared or verified-unavailable
- **Requirement:** if files of the chosen model in different quants are available on disk — real comparison (LOCAL runs, same question set, parameters per run); otherwise a concrete checked explanation based on real file facts (file name, quant label, size). Hardware flags recorded as facts, not residency proof.
- **Method:** LOCAL.
- **Declared artifact:** `local-data/d29/quantization.json` — `available_models` ≥1 (`file`, `quant`, `size_mb`); `decision` ∈ {"compared","unavailable"} (≥3 chars); `explanation` ≥10 chars.
- **PASS:** file facts read from the actual model directory/`/api/models` (not from an empty search alone); decision consistent with the facts; when `compared` — real per-quant measurements recorded.
- **FAIL:** unavailability declared without file facts; unrelated models run to manufacture a comparison; `n_gpu_layers=0` presented as full RAM residency.

### A7. `C_README` — permanent README, Russian D29 section, anonymized
- **Requirement:** the permanent `README.md` (not only `local-data/`) contains a Russian D29 section: anonymized parameters (baseline and optimized), scenario (questions/expected results), actual comparison results (quality/speed/resources before-after), quantization decision, final profile. App UI stays English. No absolute local paths, owner data, or secrets.
- **Method:** STATIC.
- **Declared artifact:** none (file content checked directly: `README.md`).
- **PASS:** all items present in Russian in the permanent README; units distinguished (`context_window` tokens vs `max_context_chars` characters); resources include memory and CPU; no de-anonymized data.
- **FAIL:** section only under `local-data/`; secrets/paths/owner data present; results not the actual measured ones.

### A8. `C_REGRESSION` — preserved behaviors still work
- **Requirement:** chat (send/receive), Local/Network switch, dialogue history (SQLite), optional RAG (index, search, citations) preserved. Checked via trusted `test.bat scenario` (`project_run`): unit/integration offline (mock allowed for the isolated regression) + local LIVE scenario. No paid network calls; Network provider checked offline (mock) — real network call not required.
- **Method:** TEST.
- **Declared artifact:** `local-data/d29/regression.json` — `chat`, `local_provider`, `network_provider`, `history`, `rag` all exactly `"pass"`.
- **PASS:** all five checks `"pass"`, each backed by at least one executed scenario; existing unit + integration suites pass; LIVE part uses the local model, not mock.
- **FAIL:** any check not `"pass"`; check passed without an executed scenario; user DB touched.

### A9. `C_REPRODUCIBLE` — reproduction via trusted `test.bat`
- **Requirement:** `test.bat scenario d29-optimization` (one command, via `project_run`) reproduces the comparison; uses `AI_TEST_MODEL_*` from the environment (lease acquire/use/release in `finally`); isolated TEMP data (not the user DB); no model downloads; no modify/delete/move on `E:`; no git commit/push.
- **Method:** LOCAL.
- **Declared artifact:** `local-data/d29/run_log.json` — `command` ≥5 chars; `exit_code` = 0; `model`; `timestamp`.
- **PASS:** the single command exits 0 and regenerates the comparison artifacts; lease released; TEMP isolation verified; no `E:` modification; no git operations.
- **FAIL:** non-zero exit; missing lease release; user DB used; downloads or `E:` changes; multi-command reproduction.

### A10. `C_UI_BROWSER` — UI verified in a real browser
- **Requirement:** the running app (via `run_app.bat`) is driven by a **real browser**: send message → receive answer, switch Local/Network, view history (≥3 interactions); UI in English. Code/HTML does not replace the browser.
- **Method:** UI.
- **Declared artifact:** `local-data/d29/browser_test.json` — `actions` ≥3 (with real observed results); `result` exactly `"pass"`.
- **PASS:** every recorded interaction actually performed in the browser with observed results; screenshots saved and referenced; `result` = `"pass"` only if all succeeded.
- **FAIL:** API/HTML-based "check"; fewer than 3 real interactions; `result` = `"pass"` with a failed interaction.

### A11. `C_VIDEO` — real demo MP4 1920×1080 + manifest
- **Requirement:** real recording of the running application, 1920×1080, understandable subtitles, covering: baseline profile, parameter/template changes, actual answers/comparison, quality before/after, speed/resources, final profile. Facts measured from the file.
- **Method:** UI.
- **Declared artifact:** `local-data/d29/manifest.json` — `video_path` (relative, ≥5 chars); `size_bytes` ≥100000; `duration_s` ≥30; `width` = 1920; `height` = 1080; `coverage` ≥6; plus `local-data/d29/demo.mp4`.
- **PASS:** the Tester inspects the **video itself** (not only the manifest): resolution 1920×1080, subtitles present, coverage items visibly covered, measured facts match the file.
- **FAIL:** manifest-only check; wrong resolution; no subtitles; coverage not visible in the video.

### A12. `C_SPEC_DOCS` — self-sufficient specification documents
- **Requirement:** `SPEC.md`, `PLAN.md`, `ACCEPTANCE.md` exist and are self-sufficient, covering **all** D29 requirements: concrete task with reproducible questions/expected results (facts + negative cases), pre-optimization baseline profile, the three parameters, quantization (comparison or checked unavailability), prompt template change, honest quality/speed/resources comparison on one set, optimized profile selection, preserved chat/Local-Network/history/RAG, browser UI check, demo video 1920×1080 with subtitles + manifest, Russian D29 README section, anonymization, no model downloads / no `E:` changes, reproduction via `test.bat`.
- **Method:** STATIC.
- **Declared artifact:** `docs/specs/day-29-local-llm-optimization/*.md` — exactly 3 files.
- **PASS:** exactly 3 non-empty `.md` files; every frozen criterion ID appears in all three documents (SPEC: requirement; PLAN: implementation step; ACCEPTANCE: acceptance row) with consistent artifact paths/fields/methods; a reader can implement and verify without the conversation.
- **FAIL:** missing/incomplete document; a criterion unmapped in any of the three; schema drift between documents.

## 2. General acceptance rules

- **Independence:** acceptance is issued only by the independent Tester on declared artifacts; the Developer's self-checks are not acceptance.
- **Evidence:** each PASS requires actual independent evidence (executed scenario / measured file / read code); saved state or found sources do not imply PASS. Default for unassessed items: `NOT_ASSESSED`.
- **No weakening:** criteria are not relaxed, ownership/profiles not changed to reach a green result; mock is allowed only where this document explicitly says so (P2 wiring, `network_provider` regression) — never in the ДО/ПОСЛЕ comparison, quantization comparison, or LIVE local path.
- **Units and facts:** `context_window` = model token window (tokens); `max_context_chars` = app-side RAG character budget (characters). Conflating them is a FAIL of `C_PARAMS`.
- **Anonymization:** no secrets, absolute local paths, owner data, or private infrastructure in any artifact or the README.
- **Scope guard:** no requirements beyond the delegated task are added to acceptance; the agent model never appears in app artifacts.
