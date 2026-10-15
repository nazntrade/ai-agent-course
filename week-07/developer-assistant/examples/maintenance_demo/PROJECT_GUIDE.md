# Product constraints

- Current implementation is authoritative for wire facts; outdated docs/client must migrate.
- Preserve optimistic concurrency: use the displayed expected_version; refresh after 409.
- Resolve once; no automatic reopen and no automatic retry with a guessed version.
- Do not log sensitive resolution text; audit events contain only ID/action/version.
- Preserve all unrelated list/filter behavior, ordering and inputs.
- Keep client adapter function signatures compatible with CLI and view consumers.
- Documentation must describe current code, including validation and error policies.
- Changelog entries belong in Unreleased. No invented release dates/history or test success.
- Contract tests are project evidence. External owner checks decide acceptance independently.
