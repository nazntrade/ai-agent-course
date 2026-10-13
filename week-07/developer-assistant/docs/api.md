# HTTP API and data contract

## Application health
GET /api/health returns application=developer-assistant, version=1.0.0 and status=ok.
GET /api/config returns provider labels, configured booleans and public budgets, never keys.
The service binds loopback only and rejects cross-origin writes and non-loopback Host.

## Project and documentation endpoints
GET /api/projects lists registered owner-selected projects and default_id.
POST /api/projects accepts root and optional name to select another existing project folder.
POST /api/projects/{project_id}/index rebuilds README/docs corpus atomically.
GET /api/projects/{project_id}/index returns document_count, chunk_count and relative source hashes.
POST /api/projects/{project_id}/documents accepts name/content, creates a new docs/imported file
and returns needs_reindex=true. Existing filenames are not overwritten.
GET /api/projects/{project_id}/sources/{source_id} reads an indexed excerpt and its source version.
GET /api/projects/{project_id}/mcp performs genuine stdio discovery/call for git_branch.

## Task endpoints and state
POST /api/tasks accepts project_id, question, provider (local or deepseek), mode (help or web), and optional allow_tools (false by default).
It returns a queued task immediately. GET /api/tasks/{task_id} returns the live saved state.
GET /api/projects/{project_id}/tasks returns recent history for that project.
POST /api/tasks/{task_id}/stop cancels queued/running execution.
POST /api/tasks/{task_id}/retry creates a separate run after the original stops.
POST /api/tasks/{task_id}/telegram explicitly sends the saved completed result to the configured owner.

Task stores id, original question, selected provider, plan, status, answer, sources,
tool events, quality, grounding, elapsed_s and safe error. Quality starts NOT_ASSESSED.
Status completed means execution completed; semantic correctness needs independent source review.
Status failed retains the error, cancelled retains the stop reason.

## System Tools endpoint
POST /api/projects/{project_id}/tools accepts name and arguments.
project_tree takes an empty object; read_project_file takes path and optional start_line;
search_project_text takes query; run_project_tests takes suite=smoke or unit.
Tool schemas reject extra properties. Tests are trusted only for this example module.
Responses preserve actual exit_code; a failed test is not converted to success.

## Limits
Question maximum 4000 characters. Imported document maximum 300000 characters.
File reader maximum 512000 bytes, 160 returned lines by default, 12000 output characters.
Project tree at most 500 permitted text files. Search at most 30 matches.
Scope rejects traversal, linked paths, secrets and excluded folders.
Runtime budgets and context budget are validated by application configuration.

## Schedules

GET /api/projects/{project_id}/schedules lists persisted schedules.
POST /api/schedules accepts project_id, question, provider, mode, due (timezone required), interval_minutes (0 means once), telegram. Scheduled jobs use the default bounded workflow, without optional model System Tools. POST /api/schedules/{id} with enabled pauses or enables a schedule. Telegram always targets the configured owner; arbitrary recipients and message text are not accepted.

## Saved conversations

GET /api/projects/{project_id}/chats lists active chats; include_archived=true includes recoverable deleted chats. POST to the same path creates a chat with title (1 to 80 trimmed characters). GET /api/chats/{chat_id} reads metadata. POST /api/chats/{chat_id}/rename accepts title; /archive hides an inactive conversation; /restore makes it active again. Active or scheduled chats cannot be archived silently.

GET /api/chats/{chat_id}/tasks returns chronological latest tasks, next_before cursor, and run_order; optional before and limit (1 to 200) retrieve earlier pages. Existing project-wide history remains available, including archived run evidence. Task and schedule creation optionally accept chat_id; unknown, archived and other-project chat targets are rejected before execution. Legacy callers without chat_id resolve an active default conversation. Retry and scheduled results remain linked to the original chat. No chat operation invokes a model or sends a report.

## PR review routes (D32)

GET `/api/projects/{pid}/reviews/connection` returns repository and project scope.
POST `/api/projects/{pid}/reviews/connect` accepts `{repository, scope}`; repository is
`owner/name`, scope is a relative directory in it. It verifies public repository access
and persists project metadata, without model calls. GET `/reviews/pulls` under the same
project returns at most 30 open PRs. Refresh is explicit, with no background model calls.
An opt-in browser watcher reuses pulls and sync: at most five PRs per 60-second cycle,
no overlapping cycles and no model calls. It captures project/chat/repository and pauses
on context change/error. The browser page must stay open; server schedules are separate.

POST `/api/projects/{pid}/reviews/authenticate` is an explicit owner action without
credential arguments. It requires the connected repository to match the selected
project's trusted Git origin, uses fixed noninteractive Git credential lookup and
returns only authenticated/repository/scope metadata. The in-memory client is bound
to that repository. Credentials never become UI responses, model context or SQLite.

POST `/api/projects/{pid}/reviews` accepts number, provider=local|deepseek, optional
chat_id, telegram=false. It returns a queued ordinary task. GET `/api/tasks/{id}` reads
its status/report; POST `/api/tasks/{id}/stop` cancels it; retry uses a new run ID. Imported
CI tasks cannot silently re-run a model; choose the PR for a manual review instead.

POST `/api/projects/{pid}/reviews/sync` accepts number and optional chat_id. It reads
only the GitHub Actions bot's marked comment and validates its bounded JSON envelope,
repository, PR, project scope, revision, sources and finding coordinates. Same-head
imports are idempotent; old-head results are stale history. It does not invoke a model,
extract an arbitrary archive or repeat a CI Telegram notification.

POST `/api/tasks/{id}/telegram` for a manual saved review sends the short fixed-owner
report through the existing Telegram MCP with repository/PR/head dedup. For imported
CI results it returns the recorded CI delivery status without resending. Telegram error
or uncertainty never changes a saved analysis to a fake successful notification.

Review result status completed|partial is separate from task execution completed.
Review semantic quality is NOT_ASSESSED. Provider failures and invalid output fail the
task; missing coverage/context remains partial. Findings use only supplied source labels
and changed HEAD lines. The reviewed code is never executed by these routes.
