# D31 acceptance

All criteria start NOT_ASSESSED. PASS requires independent evidence of the stated behavior; implementation author's READY and test count are insufficient. No weakening on infrastructure failure.

| ID | Requirement | Sufficient verification |
|---|---|---|
| D01 | README and docs are in RAG | Index real module documentation; inspect manifest/chunks, ask README and docs-specific questions, review supported answers and relative citations. |
| D02 | API/data descriptions are usable | Index declared schema/API docs; ask an endpoint/data-field question; verify answer against actual source, not generic knowledge. |
| D03 | Genuine MCP current git branch | LIVE stdio SDK v2 discovery and tools/call on actual repository; compare returned branch with read-only git. Test detached/nonrepo handling offline. |
| D04 | `/help` understands project structure | BROWSER enter `/help` structure question; actual selected-provider LIVE answer includes project structure supported by retrieved docs and live branch. |
| D05 | Grounding and honest limitations | Review complete answers; unknown claim, irrelevant source, fabricated claim with real citation, contradictory docs and prompt injection do not pass as supported facts. |
| D06 | Video plus code | Real UI landscape MP4 shows index, `/help`, current branch, sources and limitations; code/reproduction instructions present and authorized commit/push verified. |
| U01 | Lite interface/state | BROWSER select project/provider, create task, inspect short plan/subtasks, run, Stop, history and result. No sound/blink effects. |
| U02 | One Local plus DeepSeek | LIVE same representative grounded question on each explicitly selected provider; correct actual model route, complete supported response. No mock or hidden fallback. |
| U03 | Cancellation and recovery | Cancel an in-flight controlled operation; no subsequent tools/results overwrite cancellation. Restart with unfinished run; truthful interrupted state and explicit new retry. |
| U04 | Source lifecycle | Atomic reindex changed/deleted docs updates version and removes stale/duplicate chunks; unchanged index is stable. |
| U05 | System Tools and scope | Actual tree/read/search/trusted-suite operations; traversal, escaping symlink, secret read and arbitrary command denied. Real test failure exit is preserved. |
| U06 | Tavily | LIVE real MCP search returns usable attributed public result; failure safely reported. |
| U07 | Telegram self report | LIVE send a labeled saved report to configured owner; receive success/message ID, deduplicate repeated submission; no generated target override and no secret disclosure. |
| U08 | Bounded loop and single writer | Repeated failure stops within bounds; overlapping starts queued/rejected correctly; provider/tool errors remain visible and never become semantic PASS. |
| U09 | Safe reproducibility/privacy | Setup/run/check from documented entrypoints; missing keys leave usable UI with safe error. Public staged files contain no secrets/private paths; ignored data remain untracked. |
| U10 | Learning document | Word explains actual upload/index/retrieval/MCP/provider/tool/result flow with real screenshots and week-7 theory; all rendered pages readable. |

## Evidence rules

Use isolated temporary projects/SQLite for negative tests; never destroy owner data. Saved evidence records scenario, kind (OFFLINE/LIVE/BROWSER), actual provider, source versions, observed result and reviewer judgment. Unit tests do not demonstrate real provider/UI behavior. Quality is distinct from transport/schema. Secret values, raw `.env`, credentials, private infrastructure and internal reasoning are excluded from evidence and public artifacts. Report any FAIL/NOT_ASSESSED criterion explicitly until real verification succeeds.
