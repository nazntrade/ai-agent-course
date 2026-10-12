# D31 implementation plan

1. Independently review this SPEC/PLAN/ACCEPTANCE; retain the full official and owner contract. Prepare the existing nonempty module without regenerating or overwriting its secrets/data. Respect course bootstrap requirements and isolate any required setup from product edits.
2. After design gate, perform small real preflights for SDK v2 stdio discovery/branch and configured providers. Failures establish a concrete cause; do not weaken criteria or substitute an unrelated service.
3. Build FastAPI/static UI, SQLite state, health, project selection, provider selection, task history, single worker, cancellation and restart recovery. Provide setup/run/test entrypoints and `.env.example` without secrets.
4. Implement root confinement, documentation manifest, FTS5/BM25 indexing/reindexing, bounded retrieval and human-readable citations. Test source mutation/deletion, empty support, contradictory and injected documents.
5. Implement standalone project MCP server and Client v2 integration; branch must be obtained through actual tools discovery/call. Register bounded System Tools with common executor and trusted-suite execution. Record real outcomes.
6. Implement `/help` with project MCP context and RAG, explicit Local/DeepSeek routing and bounded tool execution. Test answer support using golden questions about actual README/docs/API. Preserve errors/cancellation rather than synthesizing success.
7. Connect real Tavily MCP and restricted Telegram owner report delivery. Verify safe failure, fixed recipient and deduplication. No assistant-controlled recipients or arbitrary shell.
8. Architect post-review of actual changes, then independent Tester offline/LIVE/browser acceptance of every criterion. New failures require focused correction and affected regression. Preserve all negative findings until genuinely rerun.
9. Run the real app, capture wide readable video covering D31 plus relevant extensions; produce implementation-specific Word tutorial/screenshots. Inspect final document pages and video playback. Sanitize public files, update module/course README, commit/push only verified authorized changes; preserve local data and secrets.

## Suggested small implementation units

State/UI → RAG → MCP/tools → provider help → integrations → integrated acceptance/artifacts. Each unit has a check and explicit dependencies; unit completion never substitutes full parent acceptance.

## Risks requiring explicit evidence

FTS5 keyword search can miss paraphrases: test representative Russian/English questions, normalize search and report insufficient support instead of invented facts. SDK v2 API differs from old examples: real preflight first. Local endpoint/model runtime configuration is unknown until application probes it without exposing secrets. Stop must interrupt in-flight HTTP/process calls, not only change a UI label. Selected project can contain hostile files/symlinks. Project tests execute code, so only explicitly trusted suites are allowed. Telegram LIVE creates a real owner message authorized by the user's requested reporting integration; use a small labeled test report.
