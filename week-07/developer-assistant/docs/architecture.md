# Architecture and execution flow

## Components and responsibilities

Developer Assistant is an independent Python FastAPI application. The static browser
UI selects a project and a provider and shows tasks, their plan, result, sources and logs.
SQLite stores projects, tasks and a versioned documentation corpus; it is not a language model.
Only one language model handles a request: Local or DeepSeek. There is no automatic fallback.

## RAG indexing and retrieval

Indexing reads README and supported text files in docs (.md, .txt, .json, .yaml, .yml).
Secret paths, .git, .venv, node_modules, local-data and linked paths are excluded.
Text is split into heading-aware chunks. Each chunk records relative path, start/end lines,
source SHA-256 and a stable ID. A SQLite FTS5 index ranks matching chunks with BM25.
Reindex atomically replaces a project's corpus; changed and deleted sources cannot
accumulate as old duplicate chunks. Imported docs require explicit reindex.
An optional external reference article is supplementary; it does not describe this project.

## MCP project context

For /help the application creates a real SDK v2 Client connected to a separate stdio
project MCP server. tools/list discovers git_branch, project_files and git_status.
tools/call obtains the real branch before the selected model answers. Detached HEAD,
unborn branches and nonrepository folders have explicit different states.
MCP is a protocol boundary for capabilities, while RAG supplies documentation knowledge.
Neither MCP nor RAG by themselves determine answer correctness.

## Plan and bounded task execution

A /help task has three dependent steps: context (live branch and retrieval), answer
(selected provider and bounded System Tools), result (save response and evidence).
One worker runs tasks sequentially; additional runs wait queued. Original question,
provider and dependency/check fields persist. Simple /help is one unit, not a five-role loop.
Ordinary /help answers directly from retrieved documentation and the mandatory live MCP
result, without asking the model to choose unnecessary tools. The owner can explicitly enable
the advanced model System Tools mode when additional inspection is needed.
In that mode the model may request at most four System Tool calls after the two mandatory context
operations, with at most five provider rounds. An identical repeated call stops the run.
No hidden reattempt of a failed answer occurs. Provider errors or output truncation fail the task.
Private reasoning stays in memory for protocol continuation and is never displayed or stored.

## Stop and history

Stop cancels an asyncio task, including queued waiting or in-flight HTTP/tool operations.
A cancelled state cannot be replaced by a late partial or answer. Retry creates a new ID.
Application shutdown cancels jobs and releases its owned local process; borrowed servers remain.
After an unexpected restart queued/running records become failed, marked interrupted.
Completed execution is distinct from verification; quality remains NOT_ASSESSED.

## Tool registry and scope

Executor checks registered name, JSON schema, project scope, output bounds and timeout.
Tree/read/search are bounded read-only actions. Trusted tests select smoke/unit commands
for this example only; arbitrary project commands or shell strings are never accepted.
Retrieved text and tool results are untrusted data, not instructions granting permissions.
Search web uses genuine Tavily MCP separately. Telegram accepts only a saved result and
uses a fixed owner destination from environment; a model cannot choose a recipient.
Notification is a separate real stdio MCP server exposing only send_self_report(report_id).
System Tool evidence uses T1/T2 references; MCP references only the actual branch result.
RAG excludes internal specification/evidence folders from the answer corpus.

## Provider parameters

Runtime configuration is private local-data/settings.json. App defaults: temperature 0.6,
max output 8192 tokens, evidence context budget 18000 characters. Model server context
window is separate from the per-question RAG evidence budget. Local working settings can
provide a larger actual context window without inserting every document into every request.
DeepSeek uses explicit thinking mode; its temperature does not control the thinking path.
Both providers stream final-answer content; private reasoning does not appear in the UI.

## Scheduled work

Scheduler persists one-time and recurring questions in SQLite. Due work waits for the single worker to be idle. A recurring schedule calculates its next due time after completion, so there are no catch-up bursts or overlapping runs. The app must stay open. An interrupted trigger is paused after restart for owner inspection rather than blindly replayed. Pausing prevents future launches; Stop cancels the already launched task separately. Completed scheduled results can be sent through the restricted Telegram MCP to the configured owner. Failed or cancelled tasks are not sent as completed reports. Uncertain delivery is never automatically retried.

## Chat interface and retained runs

The sidebar selects project-scoped saved chats. The main thread presents each run as a user message and assistant reply, with a bottom composer. Sources, plan and full execution evidence expand per reply. Workspace tabs retain documents, live branch, System Tools, schedules and all project run history. The interface uses safe text nodes for Markdown and search excerpts; model or document HTML is never executed.

Chats and task_chats are additive SQLite metadata. Legacy runs are linked idempotently without rewriting their original payloads or delivery IDs. Delete archives an inactive chat; restore is available in History. Atomic BEGIN IMMEDIATE validation prevents a chat archive racing a task start or enabled schedule save. Active/queued work and enabled/inflight schedules must be explicitly stopped or paused first. Retry stays in its original chat. A paused schedule cannot re-enable while that chat is archived. Each model call still uses its current question and bounded project evidence, not an implicit full-history context.

Polling ignores stale project/chat requests and older snapshots cannot revive a terminal run. Expanded sources and reading position remain intact. Loaded active runs outside the latest page are refreshed in bounded rotating batches. The mobile sidebar traps focus and restores it on close; Workspace uses a native modal dialog with tabbed sections.
