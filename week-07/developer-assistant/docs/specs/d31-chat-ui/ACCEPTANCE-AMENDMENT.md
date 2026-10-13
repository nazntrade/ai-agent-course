# D31 chat interface additional acceptance

Original D01-D06 U01-U10 are mandatory and unchanged. New criteria start NOT_ASSESSED. These checks supplement full original integrated acceptance.

| ID | Requirement | Sufficient independent verification |
|---|---|---|
| C01 | Persistent project-scoped chat CRUD and safe migration | OFFLINE initialized populated legacy DB: original task payloads unchanged; one project legacy group; repeated migration stable; schedules/delivery IDs preserved. API create rename archive, reject whitespace/oversize/cross-project/archived task targets and active/scheduled archive. Restart metadata persists. BROWSER create two chats, rename, switch, delete inactive one, retained other and source/task isolation. |
| C02 | Recognizable accessible responsive conversation | BROWSER desktop: user/assistant messages central, bottom composer, sidebar CRUD, auxiliary drawer/per-answer details collapsed but discoverable. Keyboard Enter/Shift+Enter and IME-safe implementation plus responsive narrow viewport no horizontal clipping. Sources/tool results/errors readable; legitimate scroll position preserved through polling. |
| C03 | Existing execution safety and all functionality preserved | Actual UI chat queued/running Stop and retry IDs; async late response cannot overwrite another chat/project. Source/details survive polling. Real D31 selected Local and DeepSeek structure/API answers, genuine branch, index/import, tool operations, Tavily, schedules and owner Telegram report dedup still work. Scheduled result belongs to original chat; archive rejection keeps control accessible. Existing history includes retained archived evidence. No model or send calls from CRUD. |
| C04 | Updated reviewed Word/video and code publication | New Word screenshot/content reflects chat UI and exact RAG/MCP/tool/run semantics; every rendered page inspected. Landscape MP4 decoded and sampled, all D31 + new CRUD/drawer feature coverage, no secrets. Requested output files replaced. Independent artifact review and scoped commit/push receipt. |

Use isolated fixture databases/projects for migration/negative tests. Actual owner conversation history is preserved. Transport completion is separate from semantic correctness. Existing scheduled Telegram remains fixed-recipient, persisted-report-only and deduplicated. No approval is required beyond already given authorization for scoped implementation and publishing.

## Schedule persistence and race checks

OFFLINE fresh Store initialization must succeed before the schedules table exists. Scheduler creates the table and only then migrates unscoped legacy records, taking the mapped chat of same-project last_task_id when available, otherwise an active default. Existing schedule and delivery fields remain intact; repeated initialization does not create duplicate groups.

OFFLINE create schedule then pause, archive its now-inactive chat, attempt enable: reject without changing enabled/due, creating tasks or invoking model/report operations. Explicitly restoring the chat to active in an isolated fixture permits enable again. Wrong-project and missing chat references are rejected identically. Creation of an enabled schedule into an archived chat is denied before persistence.

OFFLINE competing database connections verify serialized outcomes under BEGIN IMMEDIATE: archive first makes initial task creation and enabled schedule save fail; initial task/save first makes archive fail while work or enabled schedule remains. Checks and INSERT/UPDATE occur within one transaction in Store.archive, initial task persistence/link and Scheduler.save. Static post-review confirms toggle does not mutate the original record before validation; blocked enable preserves stored due and state.
