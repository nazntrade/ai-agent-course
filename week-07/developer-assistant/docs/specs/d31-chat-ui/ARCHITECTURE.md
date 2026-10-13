# D31 chat-first architecture amendment

DESIGN_STATUS: READY_FOR_INDEPENDENT_REVIEW

## Scope and invariant

The owner requests a recognizable chat as the main interface, persistent create rename delete conversations, convenient hidden supporting controls, replaced tutorial/video, and commit/push. This amendment preserves every original D01-D06 and U01-U10 criterion. It does not replace the RAG/MCP/provider/tool/scheduler execution pipeline, authorize autonomous sending, or change model parameters. There is one existing worker. A chat groups saved runs, rather than adding a second execution or implicit conversation-memory system.

## Additive persistence

Add chats(id, project_id, title, created, updated, archived) and task_chats(task_id PRIMARY KEY, chat_id). Existing projects/tasks/documents/chunks/deliveries remain untouched. Migration is transactional and idempotent. For each project with unmapped task rows, create or select one named legacy/default chat and attach those rows without rewriting task payloads, answers, events, providers, elapsed values, quality, or reports. A new project gets a default active chat lazily or at project creation. Restart must not duplicate migration chats.

A new initial task is persisted and linked in a BEGIN IMMEDIATE transaction, with same-project and active-chat validation before INSERT. Subsequent task updates preserve the link. chat_id is optional for legacy callers: resolve an active default chat or create one when none exists. Never silently reopen an archived chat. Explicit unknown, archived, or cross-project chat_id is rejected before scheduling any work. Reply runs remain independent executions with existing bounded context. Visual adjacency is not evidence that entire historical text is sent to a model.

List active chats scoped by project; order by updated timestamp/id or creation plus latest task. Name is trimmed, length bounded, nonempty, sanitized for secrets and displayed as plain text. Rename affects only chat metadata. Archive is the user-visible Delete: hide from normal sidebar while retaining immutable run/report/schedule evidence. Unknown/previously archived ID gives a truthful stable response. No filesystem deletion occurs.

Delete rejects chats containing queued/running work or enabled/inflight schedules, with actionable Stop/Pause guidance; no hidden cancellation or schedule disablement. Store.archive uses BEGIN IMMEDIATE and checks eligibility and updates archive state in that same transaction. Initial task insertion/linking and enabled schedule persistence take the same immediate write lock, so whichever operation wins determines a truthful rejection of the other rather than a check-then-write race. New schedules store chat_id and launch runs there. Retry of a run resolves the original chat; if archived, fail clearly rather than reviving it silently. The existing project task-history endpoint remains compatible, including archived runs for evidence.

Store initializes and migrates chats/tasks before Scheduler is constructed; Store must not assume that the schedules table already exists. Scheduler first creates its own table, then migrates unscoped legacy schedules transactionally. Resolve the chat using the last_task_id mapping when present and belonging to the schedule's project; otherwise resolve an active project default. Preserve the original question, due, interval, delivery, state and last_task_id fields. Migrated enabled records must target an active same-project chat; a historical mapped chat that is archived cannot silently be reopened or receive enabled execution.

Scheduler.save uses BEGIN IMMEDIATE. Before persisting any enabled/create record, validate chat existence, same project and active state inside that transaction, then perform its INSERT/UPDATE without releasing the lock. toggle(enable) validates these conditions before changing enabled or due, preserving the old paused record on rejection. Pause then archive then enable therefore rejects without model calls or schedule time changes. A deliberately restored active chat can subsequently be enabled. Actual scheduled triggers still revalidate via initial task insertion; an archived target cannot launch a run even if stale scheduler memory survives.

## API boundaries

Add GET/POST /api/projects/{pid}/chats; POST /api/chats/{cid}/rename; POST /api/chats/{cid}/archive; GET /api/chats/{cid}/tasks. TaskBody adds optional chat_id, forwarded through Service.create and store initial persistence. ScheduleBody inherits chat_id. Scheduler.create captures the resolved chat and validates at creation. A scheduled trigger validates its chat again, preserving existing trigger claim/error semantics.

GET chat tasks returns chronological messages with a bounded latest page and an older-page cursor. Empty chats return an empty list. Existing project-wide history remains available in workspace details. An older archived scheduled result can still be inspected read-only without exposing a writable archived composer.

The report endpoint stays keyed to saved completed task ID and fixed Telegram owner. UI report buttons belong to each corresponding assistant message. No new send on rename, archive, select, retry, or schedule pause.

## Main interface

A desktop three-region arrangement: quiet left sidebar with project selector and named conversations plus New chat; central header with current title and rename/delete; spacious chronological conversation; fixed bottom composer; optional right workspace drawer. On narrow screens the sidebar and drawer collapse behind buttons, conversation takes the available width, and the composer remains reachable without horizontal page scrolling. Screen-reader labels, focus-visible states and reduced-motion preference apply. No blinking or sound.

Each run renders a user bubble and an assistant reply. Completed, failed, queued and cancelled states are visible in the relevant message, never replaced by a green generic success banner. Markdown may be represented safely or rendered through escaping; never interpret model/user/source HTML. Readability comes from restrained line width, whitespace, clear sender labels, and a subtle background/border rather than decorative dashboards.

Per-answer secondary row: model or Tavily route, elapsed time, status, then Sources count, Plan and Execution details collapsed by default. Keep quality NOT_ASSESSED and grounding labels available inside details, with a concise honest explanation. Source/version and full tool/error evidence remain inspectable. Show Stop for each running/queued row and Retry/report on eligible terminal rows.

Composer includes textarea, clear provider selection and Send, optional Help/Web mode, and advanced bounded model-tools option in an unobtrusive popover/details. Enter sends, Shift+Enter inserts newline, IME composition never triggers send. Empty input does not accidentally create a run. Multiple submissions still use the existing queue. Preserve explicit Local vs DeepSeek choice and no implicit fallback.

Workspace drawer groups Documentation (index/import/source manifest), Project (add project/live branch), System Tools, Schedules and project-wide run history. These operations retain current backend/API behavior and visible actual results. The drawer closes without losing a draft. Opening scheduled history selects its chat if active, or shows archived evidence read-only.

## Frontend isolation and scrolling

Maintain selected project_id and chat_id separately from immutable task IDs. Every asynchronous response verifies both captured identifiers before changing visible conversation/composer. Poll only selected chat's busy runs, and ignore stale responses after project/chat switches. Run progress must not jump the user from an older message into another run. Preserve expanded answer-detail state by task/source ID across refreshes. Auto-scroll only if near bottom or after owner sends; otherwise provide a New messages action. Project selection clears old messages/branch/tool state before rendering its new conversation.

## Review risks

A visual chat can imply memory that does not exist: tutorial states each /help turn uses its current question and project evidence. Frontend refactor can hide rather than retain Stop, sources, history and schedules: browser acceptance must exercise all controls. Migration can orphan legacy reports/schedules: isolated populated-database tests compare payloads and mappings byte-for-byte. UI can dispatch cross-project data through stale promises: delayed-result switching tests and actual browser switching required. Deleting active chats must not lose the owner ability to Stop. Caches must not serve old index HTML/CSS after restart; use simple cache-safe static version reference if needed, without changing API behavior.

## Suggested execution units

State and migration first; API/service/scheduler linkage next; chat layout and safe message rendering; drawer and existing operations; independent post-review and integrated browser checks; then regenerate and visually verify Word/MP4; finally scoped privacy check commit/push. No dependency beyond current stack is required.
