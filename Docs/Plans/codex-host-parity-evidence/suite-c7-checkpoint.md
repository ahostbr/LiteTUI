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
Legacy Stop closes only local UI before waiting for an abort response, even for a wedged child. Native requests require evidence and remain recoverable after failed Stop. Process death settles remaining requests. Runtime schema retains required answers
and adds optional cancelled and cancellationSource (host/native/process); cancellation uses answers:{} and cancelled:true, with
truthful cancellation activity text. Empty answers alone do not mean cancellation.

## Scope and SHA-256 snapshot

| Suite-relative file | SHA-256 |
| --- | --- |
| apps/server/src/provider/Layers/LiteTuiAdapter.ts | 3142057BA13C8768FA4A783EAD7CF192C54DC807085ED09E426C7AAE7AC7937F |
| apps/server/src/provider/Layers/LiteTuiQuestionEvents.ts | BCE761514337F6CF03D7F6C9B6412C2297D9D72129C2139CF5BF262BC70E8831 |
| apps/server/src/provider/Layers/LiteTuiQuestionEvents.test.ts | 53217C5E22BBC6CAE45DB5C6D0E4F0C6AC9266AB11349D12FCD5C926F678A28D |
| apps/server/src/orchestration/Layers/ProviderRuntimeIngestion.ts | 23E723A04F036E8E8C1540D40BDDC6DFF8D94F940F2F757D4956DB1284A476BA |
| packages/contracts/src/providerRuntime.ts | 82488E3B2B4C886ED1FCB33B1973E25AFA18D5BD58B4109F257D3554606C7C97 |
| apps/web/src/session-logic.test.ts | FA0EAF70EEDCB9D1A0D88C897A3464DC8D970568D93A7892FA842FD627B54D9C |

## Validation

- apps/server: `bun run test -- src/provider/Layers/LiteTuiQuestionEvents.test.ts src/provider/Layers/LiteTuiToolEvents.test.ts`: 21 passed (9 question, 12 tool) at initial checkpoint. Follow-up question-only run: 15 passed, covering the extracted production Stop path.
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


## Stop compatibility follow-up

Sentinel found that the initial evidence-only Stop implementation regressed older
children that never acknowledge abort. The adapter now calls the tested production
`LiteTuiQuestionEvents.interrupt` path. It closes legacy question UI locally before
sending, captures the request set, and preserves native questions on generic ACK or
timeout. Delayed answer ACK cannot replace local cancellation with consent. New
questions are not included in an older Stop. Failed Stop still rejects through the
adapter's ProviderAdapterProcessError; the existing ProviderCommandReactor records
provider.turn.interrupt.failed (source inspection, not a live failure probe).

Cancellation origin is explicit through schema and ingestion: host -> User input
cancelled locally; native -> User input cancelled; process -> User input closed
after process termination. Successful answers retain their prior payload shape.
Old cancellation payloads without source still decode. Malformed versioned requests
are rejected rather than downgraded to legacy. The compatibility boundary is per
request wire version, not a guessed installed runtime version.

Follow-up: 15 question helper/control-path tests, 49 web session-logic tests, and 8
runtime contract tests pass. Server, contracts and web typechecks pass. These are
synthetic tests, with no child/provider launches. Suite edits remain uncommitted.
