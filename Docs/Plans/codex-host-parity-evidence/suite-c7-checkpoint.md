# Suite C7 request identity and cancellation checkpoint

Task REL-20260914-CODEX-PARITY. Suite candidate only:
C:/Projects/.worktrees/release-20260914-suite, base
6885d57c3c73bba1d629d7e65425255cc805feff. Source remains uncommitted.
Sentinel authorized the six paths below; unrelated candidate work and C5 edits are
preserved. No real provider/session launch, deployed restart, root edit, or release.

## Behavior

An explicit native_turn_started producer event binds the native thread/turn to its
host turn. Native questions arriving after completion or during a later host turn
retain that binding. Unknown native origins are unbound rather than guessed. Legacy
questions retain the host turn at request receipt. Request IDs remain authoritative
for replies/cancellation; replay and terminal tombstones cannot reopen a request.

Answer ACK settles once. Failed send retains the pending request. Concurrent answer
sends are refused. Native cancellation wins an in-flight ACK race and cannot become
a successful answer. Abort ACK closes only explicitly named cancelled_asks captured
before sending the abort, not newer questions or all requests on a generic ACK.
Process death settles remaining requests. Runtime schema retains required answers
and adds optional cancelled; cancellation uses answers:{} and cancelled:true, with
truthful cancellation activity text. Empty answers alone do not mean cancellation.

## Scope and SHA-256 snapshot

| Suite-relative file | SHA-256 |
| --- | --- |
| apps/server/src/provider/Layers/LiteTuiAdapter.ts | 52A76A073510BEF679959A7361B5933F4779842D42D754B250A5BB3723047746 |
| apps/server/src/provider/Layers/LiteTuiQuestionEvents.ts | E7D840516ABA8489E0EBDA3A6894A51D9489E94390D9DBEB9B515C09B63D7546 |
| apps/server/src/provider/Layers/LiteTuiQuestionEvents.test.ts | F17AE03BDC34AFE0CC9C74AB048C9FA63CCEBAAB3934D719447F2B1543C2C98E |
| apps/server/src/orchestration/Layers/ProviderRuntimeIngestion.ts | 59EE73389FAAD38E7C07DB2413505CB3848C14C8AEC8556F60A33182E57AF804 |
| packages/contracts/src/providerRuntime.ts | 6A2198CB454F23BDADFAD31BF5AC7B1022593BBC32274675893A9F9B33003F59 |
| apps/web/src/session-logic.test.ts | 5D598A44741504B71A65A9AA4964F86B77AC6D4DFDF75621CF4613812662EC06 |

## Validation

- apps/server: `bun run test -- src/provider/Layers/LiteTuiQuestionEvents.test.ts src/provider/Layers/LiteTuiToolEvents.test.ts`: 21 passed (9 question, 12 tool).
- packages/contracts: `bun run test -- src/providerRuntime.test.ts src/providerRuntime.turnCompleted.test.ts`: 8 passed.
- apps/web: `bun run test -- src/session-logic.test.ts`: 49 passed.
- `bun run typecheck` in apps/server, packages/contracts, and apps/web passed. Server retains
  three existing advisory Effect messages, exit zero.
- Six owned Suite files pass scoped oxfmt; git diff --check passed.
- Producer: PYTHONPATH=src `python -m pytest tests/test_codex_async_questions.py tests/test_codex_app_server.py tests/test_codex_question_lifecycle.py tests/test_codex_questions.py -q`: 40 passed in 3.86s. Changed producer/test Ruff passes.

Server tests decode the actual runtime schema and inspect actual ingestion. The web
test uses the identical asserted cancellation payload and confirms that matching
request identity clears only that question across turns; replay and unrelated
resolution leave the other request pending. An initial direct web import in the
server test passed at runtime but violated TS6307 project boundaries. It was removed
and the consumer assertion relocated to the explicitly authorized web test file;
no tsconfig weakening or test-only production shim was introduced.

Full adapter runtime, packaged transport, GUI restore/reply flows and remaining
C1-C10 gates are not established by these isolated tests. No release clearance.
