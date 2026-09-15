# Native workspace alignment

Date: 2026-09-14. Task: REL-20260914-CODEX-PARITY.

The adapter started native threads with `paths.ROOT`, the package anchor. This
differs from the selected launch workspace, especially for an installed wheel or
`--cwd`. Native policy also defaulted to that package anchor through the shared
authorization method.

Native thread/start and every turn/start now use the host workspace captured by
hook initialization. App-less calls use the actual working directory. The native
policy adapter supplies that same workspace to the existing authorization door.
Resumed threads retain their identity and receive the selected workspace as the
documented per-turn override; prior messages are not retransmitted. No change to
global package/data paths, credentials, caching, or the native execution policy.

Protocol evidence: generated installed-CLI schema at
`artifacts/history-schema/codex_app_server_protocol.v2.schemas.json`,
`TurnStartParams.cwd`: override for this turn and subsequent turns.

Validation:
- Fake native transport tests cover app-less and hosted execution, a distinct
  package directory, captured workspace despite later process cwd change, resume
  retaining native identity, and only the new user input being sent.
- Native policy test checks the captured workspace passed to authorization.
- Existing real Textual approval bridge test accepts/asserts that workspace.
- Focused transport/policy: 20 passed. Bridge/resize: 6 passed.
- Full Codex suite: 206 passed in 13.78s. Ruff and diff checks passed.

An initial full run exposed the approval fixture's outdated method signature
(corrected) and a resize test scroll-position failure (not reproduced in focused
or subsequent full run). The resize failure remains an intermittent validation
observation, not a claimed production fix. No native inference/command was run.
Full C1-C10 acceptance and the existing release/integration holds remain open.
