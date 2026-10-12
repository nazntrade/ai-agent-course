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
