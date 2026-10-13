# D31 chat interface specification amendment

SOURCE: Owner requests chat as primary UI; preserve functionality; add create rename delete conversations; reorganize or hide auxiliary controls; rebuild and overwrite Word/video; commit and push.
DESIGN_STATUS: READY_FOR_INDEPENDENT_REVIEW

The original docs/specs/d31/SPEC.md and ACCEPTANCE.md remain authoritative and unchanged in scope. Original D01-D06 U01-U10 are not replaced. This amendment adds C01-C04.

C01 Persistent conversations: owner can create, name, rename, select and delete a chat in a selected project. Deletion archives visible conversation metadata while retaining task/report/schedule evidence. Active work and attached enabled/inflight schedules prevent archive until explicitly stopped/paused. Legacy saved runs and schedules migrate safely and idempotently. Chat boundaries persist across restart and cannot mix projects.

C02 Chat-first usability: primary screen is an accessible chronological user/assistant thread with persistent bottom composer, explicit provider and action controls, conversation sidebar and convenient collapsed workspace. No mandatory wall of sources/plan/log under the chat. Every existing capability remains discoverable and usable. Responsive narrow layout, keyboard send/newline and IME handling must be verified in actual UI.

C03 Preserve execution contract: genuine RAG/MCP, independent question semantics, provider routing, one worker, queue/Stop/retry, quality distinction, sources/version evidence, tools, schedules, Telegram dedup and project history remain unchanged. Asynchronous UI updates cannot cross project/chat selection. Archived task evidence remains readable. No new background model/web/send call is caused by chat CRUD.

C04 Replace deliverables: updated implementation-specific Word includes real redesigned UI screenshots and existing week-7 concepts plus conversation boundaries; all rendered pages checked. Wide readable real-app MP4 demonstrates official D31 and chat CRUD/drawer/scheduling/report flow; existing requested Word/MP4 destination names overwritten only after successful generation/verification. Code scoped, checked, committed and pushed; publication receipt verified.

Schedule consistency is part of C01 and C03. Scheduler migrates old schedules after creating its table, resolving mapped last_task_id or an active project default. Archive, initial task persistence/link and enabled schedule save use BEGIN IMMEDIATE with same-transaction validation. Re-enabling a paused schedule attached to an archived chat fails without changing due/state or creating work; it can be enabled after explicit restoration. No archive/start/enable check-then-write race may admit work into an archived chat.
