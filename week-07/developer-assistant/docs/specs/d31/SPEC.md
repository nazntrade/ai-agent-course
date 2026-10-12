# D31 — Developer Assistant Lite

DESIGN_STATUS: READY_FOR_INDEPENDENT_REVIEW

## Source and scope

The owner authorizes full D31 implementation, real application/provider checks, documentation, submission video, commit and push. Week 7 is an independent public educational module; no private control-center source, service dependency, settings or identifying paths enter published files. Existing `.env` is loaded only by the application; agents must not read it.

The official D31 assignment requires README and project docs in RAG, optional data/API descriptions, an actual MCP connection providing the current Git branch, and `/help` answers grounded in project documentation/context. Deliverables are video and code. Owner extensions are a lightweight control interface, one Local provider plus DeepSeek, project/task/plan/subtasks/Stop/history, System Tools, Tavily search and Telegram report to the configured owner. Extensions do not replace the official requirements.

## Components

Python FastAPI serves static English HTML/CSS/JS and a loopback API, default port 8791. SQLite persists project metadata, corpus manifest/FTS5 chunks, tasks, runs, results and sanitized tool events. No frontend build step, CDN, sounds or blinking status. One local language model; retrieval uses FTS5 BM25 rather than a second embedding model. DeepSeek is a selectable alternative, never an implicit fallback.

An independent `mcp==2.2.0` stdio project server implements branch/status/files/diff. The application uses the SDK v2 Client and actual discovery/calls, not an internal function masquerading as MCP. System Tools are separately registered capabilities, invoked through the same validated executor. Tavily uses genuine MCP. Telegram sends only a persisted report to the configured owner's chat via a restricted capability; generated text cannot select recipients.

## Project and retrieval contract

The owner selects a project root explicitly. The module itself is the default example. Canonical paths remain within that root; reject traversal, escaping symlinks, secret paths (`.env`, credentials, private keys), binary/unbounded files and excluded dependency/runtime directories. Documentation indexing includes README, docs and declared JSON/YAML API/data schemas. External article material is a distinct supplemental collection, never evidence for private project behavior.

Each chunk records source-relative path, SHA-256 source version, heading or JSON pointer, position and stable ID. Reindex replaces changed/deleted source versions atomically without duplicate accumulation. Retrieval returns bounded excerpts and visible citations. Indexed content is untrusted data, not executable instructions. No matching support leads to an explicit insufficient-context answer; contradictory sources are disclosed. A true citation object does not prove an answer's content. Acceptance independently compares claims to source text, including fabricated claims with real references.

## Execution and state

Every task stores original requirements, selected provider, a short plan and explicit subtask dependencies/checks. D31 question answering is a single unit; do not invent five-role loops for a simple question. Larger future work may use bounded sequential units while retaining the parent contract. Task state: queued, running, completed, failed, cancelled; verification is separate from execution completion. One worker/writer, no simultaneous mutating runs.

`/help` obtains live MCP branch, retrieves project context, calls the selected model and persists the complete answer, citations and actual tool evidence. Model failures remain visible. Stop cancels awaited provider/tool operations, prevents further actions, and terminates the task as cancelled; late results cannot overwrite newer or terminal runs. On restart, previously running jobs become interrupted/failed with a recoverable reason, never completed. Explicit retry creates a new run preserving history.

The bounded tool loop has at most six calls per run; at most three transient attempts per eligible operation. Permission denials and missing context are not endlessly retried. Validate tool name, schema, project scope, timeout and output bounds before execution. Persist real error/exit status. Provider reasoning/tool-call state follows the actual selected provider's protocol, including DeepSeek thinking continuation when required; private reasoning is not shown or published. Model temperature/output budget/context budget are explicit and validated. Private runtime configuration resides in ignored local config, not tracked machine paths.

## Capabilities

System tools: bounded project tree, text read, text search, and trusted test-suite execution. Tests use a predefined executable/entrypoint and enum of suites; no arbitrary command string. No generic shell, deletion, unrestricted writes or push tool is available to the assistant. Git branch supports detached/unborn/non-repository states accurately. Web results remain external untrusted sources. Telegram requires a saved result, fixed owner target, bounded/sanitized report and deduplication; failure is distinct from successful delivery.

## UI/API and privacy

UI shows project/provider, task/plan, actual state, answer/source excerpts, tools, history and Stop. API validates all request bodies and returns consistent typed errors; credentials are never returned, logged or rendered. Runtime/index/model/corpus data are ignored. Missing configuration yields an actionable safe error while UI remains available. Public docs use neutral examples and relative paths. Health proves module identity, not merely any listener on the port.

## Evidence and deliverables

Offline tests use isolated temporary databases/projects and fake credentials only. Required LIVE validates both providers, real stdio MCP branch and Tavily/Telegram integrations; browser checks exercise real buttons and source display. The full official D31 flow must work with the real project documents. Saved sanitized evidence includes request scenario, selected provider, outcome and source-content review. Submission video is real running UI in landscape MP4 with readable captions; learning Word document explains this implementation with actual screenshots and lesson connections. No mock or successful transport is semantic PASS.
