# T560: LiteTUI OAuth provider backends

Approved scope: native Python Codex transport (required), Claude transport only
after live feasibility. Base 9e6d40c17080fe8bb3ef4586aedfd1219872cc68; branch
codex/litetui-oauth-providers. LiteSuite and Pi are source references, never runtime
dependencies. Pi reference: E:/SAS/REPO_CLONES/pi-mono at 421c03efb.

LiteTUI owns history, prompt composition, tools/approvals/background execution,
compaction and wake. Add an inference boundary for chat, compaction, tool-result
summarization and subagents. Normalize streams without adding an agent loop.
Preserve provider reasoning metadata for same-provider/model continuation and
strip foreign metadata at conversion time. Preserve canonical transcripts across
backend switches. Cloud model controls must reflect supported capabilities.

Read existing CLI subscription credentials only. No API-key fallback, automatic
login, store writes or independent refresh-token rotation. Re-read credentials
after authentication failure; report official login guidance if unusable. Never
log credentials, HTTP bodies, or headers. Claude --bare is excluded because it
disables stored OAuth authentication.

Execution: isolated worktree -> focused failing tests -> implementation -> debug
-> locked pytest and wheel gate -> advisory Ruff/mypy -> rendered TUI check ->
review -> commit with Task-id/Agent-Tier trailers. Do not merge.

Acceptance: live streamed text + custom tool request + synthetic tool result on
a separate request + continued supplied history. Test missing/expired/malformed/
API-key credentials, cancellation, error redaction, metadata and image conversion,
backend switching, local regressions, denied tools, background tasks, compaction
success/failure and wake guards. Claude failure does not delay Codex. Record live
results separately from mocked coverage.
