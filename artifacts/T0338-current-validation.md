# T0338 current contract and validation candidate

**Status: REVIEWING, not Done.** Independent final review, main deployment checks and human look remain gates. This report supersedes the three historical T0338 reports, whose bodies are retained behind explicit SUPERSEDED pointers.

- Runtime implementation: `f5d61b318c1341e1916f011b76272b8537f0d89c`.
- Final tested/captured tree: `337df18aa0754b4692aa95e6294c00d71f57453c` (one test-only successor; `src`, `tools` and `pyproject.toml` byte-equivalent to f5).
- Broad focused execution: `b56c8cf380cfa5e4c813e596cbec15ac18a85842`. Its only subsequent production delta is the raw-command validator in the human `/system` door, not a composer change.
- Branch: `work/T0338-AgentPromptsGammaSol`.
- A subsequent evidence-only commit adds this report, receipts, sanitized capture and three historical headers. It does **not** relabel these executions as having run on that future commit. Exact evidence-HEAD smoke/equivalence is reported separately to the owner.

## Pain Diagnosis

The actual pain was coupled role/method scaffolding, import-once text and conversation-scoped partial overrides: a spawner could not author one seat's system body reliably, live changes could be lost on restart, and legacy/native prefixes required explicit provenance rather than guessing where to cut. The critic also found real Unicode-render corruption, custom store-template resume corruption and stale manual owner-compaction reads.

The final integration exposed another concrete priority bug: a custom fresh launch recorded CUSTOM's digest, but default-only named resume compared DEFAULT against that digest. Origin metadata now distinguishes fallback from explicit authoring. Sentinel subsequently chose **new explicit role authoring on named resume**, superseding the earlier changed-explicit-role refusal policy.

## Composition Analysis

Use the existing AgentSession HOME lease, one app composer, existing per-turn snapshot reader, and existing `prompt_commands.validate` + `apply` audit publisher. RPC, explicit launch and human `/system` retain distinct actor/transport boundaries while sharing validation/publication. No inheritance, new policy framework, alternate launch writer or prompt-derived execution authority was introduced.

Actual tier, identity, ownership, tool approval and deny-floor enforcement remain executable state. Editable prose is not an immutable safety boundary. Displayed OWNER/BUILDER/SCOUT/CRITIC labels correspond to wire leader/worker/thinker/reviewer; the display label alone grants no additional authority.

## Refactoring: current contract

### Launch and named resume

- `--role-prompt-file` supplies the default generic role body. Existing `--system-prompt` / `--system-prompt-file` supplies an explicit custom role body **instead of** that default, not beside it. The host catalog still composes the rest of SYSTEM.
- `--cognitive-file` selects the method. Supplied wrapper sources must exist before canonical pure-counterpart lookup. Actual `workers/gamma.md` and `workers/miyamoto.md` are covered; no arbitrary last-heading splitter is used.
- Optional `--prompts-dir` is an exclusive direct-CLI full-folder seed; it cannot overlap role/system/cognitive inputs and is deliberately not exposed by the producer.
- The complete catalog is seeded into the **leased agent HOME**, before first composition, not the conversation folder or external owner-notes folder.
- Version-2 `.prompt-launch.json` records source digests and role-source origin, not tier authority. Default-only resume preserves the current custom/live-edited home body and method. Hash-only historical manifests cannot prove custom-vs-default origin when their digests differ and remain fail-closed.
- Explicit custom role input on fresh launch **or named resume** is an intentional audited revision. Both raw input and canonical role body are checked before publication. It uses the same low-level on-fly publisher, retained revision, audit barriers and managed metadata, under the already acquired child HOME lease, before composition. Source/method/full-seed changes otherwise remain pinned; explicit role input does not relax actual-role validation.
- Launch audit provenance is versioned: captured `Seat.spawned_by` is labeled a launch-envelope parent assertion, not private-RPC authentication. Unparented direct launch labels the actor as the owned-home identity under an OS-user launch capability. Input-text/body digests and actual role are recorded. Launch prints publication, **not** notification delivery or model consumption.
- The real CLI owner-resume proof uses `--owner NAME --memory DIR --convo OLD_CID`. These must not be combined with `--agent`; ordinary child resume uses `--agent NAME --convo OLD_CID`.

### Full HOME catalog and live publication

The 16 files are:

`card-summary.md`, `claude-tools.md`, `compact-owner.md`, `compact.md`, `conversation-store.md`, `harness-capabilities.md`, `method.md`, `ordinary-instructions.md`, `owner-store.md`, `plan-mode.md`, `role.md`, `seat-instructions.md`, `systemprompt.md`, `tool-denied.md`, `tools.md`, `wake-after-compact.md`.

Example parent-owned operations:

```text
litetui prompt show Child role
litetui prompt set Child role --via Parent --file <UTF8-file> --reason "Revise child role body"
litetui prompt set Child method --via Parent --file <pure-method-file> --reason "Revise selected method"
litetui prompt reset Child tools --via Parent --reason "Use current global default"
```

`--convo` is an optional current-conversation assertion, not storage scope. A real pending/unmaterialized child HOME is supported. Writes require the live registered parent/child relationship and target ownership; the CLI/parent does not borrow the child's lease. Role/method reset refuses because there is no global default. A selected method cannot become blank through launch, RPC or direct-file composition. Truly unselected ordinary interactive seats may have no method.

Per-turn snapshots pin active rounds/retries; changes become visible on the next turn. Manual local/Claude/Codex owner compaction explicitly reads fresh `compact-owner.md` once while preserving any active snapshot. RPC notification/ACK, audit fingerprint, duplicate and uncertain-publication barriers remain intact. `APPLIED` is not evidence that a model read a notification.

### History, snapshots and native limits

- One composer supplies one outgoing SYSTEM with one generated actual-role scaffold and one selected method. BOM is removed for parsing/rendering, not by rewriting supplied legacy proof text.
- Exact supplied/catalog legacy-prefix recognition preserves the opaque frozen suffix and old user history. Both supplied DEFAULT and NEW CUSTOM are known historical candidates during explicit resume authoring; no heading-based transcript scan or archive activation occurs.
- Proved version-2 host prefixes with validated explicit store provenance skip legacy address rewriting. Frozen custom storage templates, lore and notes are not refreshed from current disk on resume.
- Selected-policy legacy native segments missing recorded prompt revision HOLD before prepare/query/queue/reuse, even when current files appear unchanged. Explicit `/claude new` is required; current launch flags never certify historical compatibility.
- Known versioned native edits rotate only after pending deliveries settle. The fresh segment carries **APP-RECORDED history and the stable prior base seed**, not hidden native context/session-fork continuity. History activities are retained; prior expanded carry text is not recursively accumulated. Old segments remain intact and uncertain inputs are not silently rebound/replayed.

## Clause-to-code and regression map

| Requirement/fix | Source | Evidence |
|---|---|---|
| Leased HOME catalog; fallback origin; explicit launch authoring | `seat_prompts.py:103,156,222` | `test_custom_resume_t0338.py`, `test_launch_authoring_t0338.py` |
| Raw bounds and role authority before publish | `seat_prompts.py:49,240,339,352` | stripped oversized-comment/NUL negatives for launch and human helper; valid update retained |
| One actual-role/method composer; independent Unicode | `seat_prompts.py:254,361`; `app.py:3806,3813,3928` | `test_unicode_mode_t0338.py`, composer capture; literal U+2014 checked independently |
| Selected source existence, pure method and BOM | `seat_prompts.py:21,69,84,95` | selected-method/BOM/home/catalog tests |
| Exact old conversation migration and frozen suffix | `seat_prompts.py:276,298`; `app.py:4349,4374,4501` | real CLI startup resume/first-submit assertion; store-template and legacy resume tests |
| RPC ownership + common audited publisher | `prompt_commands.py:143,243,371`; `prompt_overrides.py:147` | command/edge/review/home tests; publication uncertainty and receipt tests |
| Fresh owner maintenance reads | `owner_mode.py:292`; `app.py:10311,10433`; `claude_compact.py:73` | local same-loop twice/retry pin, Claude and Codex freshness tests |
| Native revision admission and bounded rotation | `claude_turn.py:39,103,327,387,434,530`; `claude_persistence.py:175` | native-restart/role-revision/selected-policy/queue tests |

The named OWNER proof restores the requested old CID through real startup `_resume_cli_conversation -> _resume`, then actual `_apply_cli_args`/rehydration and first-submit request capture. Same agent ID/name/HOME, active old CID/path, old user history, exact frozen lore/owner-notes suffix and pre-request audit/retained revision are asserted. Backend readiness/registration and terminal view are adapted; no provider/model call is made. Parent-envelope and unparented provenance are both covered. This is **not** a physical running-seat restart.

## Validation and provenance

Exact executable, module path, argv and file manifests are committed in [T0338-validation-receipts.json](T0338-validation-receipts.json).

| Execution | Exact tree | Result |
|---|---|---|
| Broad focused, 40 files | `b56c8cf3` | **776 passed, 1 skipped, 327.10s** |
| First final affected subset, retained RED | `f5d61b31` | **252 passed, 4 failed, 124.12s** |
| Final affected subset, 26 files | `337df18a` | **256 passed, 115.15s; no failures/skips** |
| Current type check | `337df18a` | 12 production modules clean, `mypy --follow-imports=skip` |
| Current owned Ruff selection | `337df18a` | clean |
| Full touched-production Ruff | `337df18a` | **144 findings**, versus **145** on exact `be14684`; NOT globally lint-clean |

All these runtime runs completed before captures or subsequent evidence edits. No root-write guard was weakened; these final gates have zero guard errors. They are focused selections, **not the entire repository suite**. Type checking does not claim full-app/import-graph cleanliness.

The one broad-run skip is exactly `tests/test_agent_ownership.py::test_stdlib_contract_and_canonical_mirror_parity`, line 517: **"paired checkout not provided; package contract tested above"**. `T0308_COUNTERPART_AGENT_OWNERSHIP` was not supplied. No paired links or fake checkout were manufactured.

The f5 red cases were four `test_store_writer_paths.py::test_gui_retry_preserves_custom_system_facts_on_reopen` home/owner and pending/loaded variants. Their old expectation required CUSTOM to be the first bytes, contradicting the required actual-role header. Commit `337df18a` changes only that expectation to literal BUILDER/U+2014 + custom body and adds counts of one role/body. Store identity, injection receipt, exact readback/messages, real startup resume and stored-body assertions are unchanged. Critic independently agreed this classification. No header was removed to appease tests, and the red receipt remains retained.

Ruff comparison is a multiset of path/message excluding line numbers: no added diagnostic entries, one removed existing app SIM102. The full-file check remains red; this is baseline accounting, not a lint waiver. Logs are under the owned worktree `temp-working-dir/`; durable task paths are in receipts. Baseline was a read-only `git archive` export, not another worktree/link.

### Sanitized finished composer capture

[T0338-composer-capture.json](T0338-composer-capture.json), tree `337df18a`, came from shipped `tools/capture_seat_prompts.py` through real CLI -> lease -> constructor -> request composition. Only terminal run/image setup is substituted. Synthetic isolated HOME/data/workspace/lore; **no full SYSTEM bodies, real home contents or real lore are retained**.

| Role | SYSTEM rows | Characters | Active headers |
|---|---:|---:|---|
| OWNER | 1 | 61,867 | OWNER/U+2014 + POLYMATHIC GAMMA |
| BUILDER | 1 | 49,751 | BUILDER/U+2014 + POLYMATHIC GAMMA |
| CRITIC | 1 | 45,304 | CRITIC/U+2014 + POLYMATHIC GAMMA |

Each has 16 prompt files. SHA256 values and exact Unicode headers are in JSON; UTF-8 readback independently asserted actual U+2014 and absence of the three-code-point mojibake sequence. Character counts are not token counts. No human-look or running-seat/model-consumption proof is implied; result display is unavailable in this worker session.

### Corrections retained, not erased

- First catalog check falsely passed a nonexistent singular `worker/gamma.md` through pure fallback. Corrected to existence-first and actual plural `workers/` paths, with positive source/pure controls.
- A default-encoding read under Windows cp1252 corrupted U+2014 in renderer/regex. Sixteen independent tests caught what coupled count tests missed. R1 uses ASCII-safe `\u2014`, independent literal oracles and composer policy version 2. Subsequent edits use explicit UTF-8/surgical bytes; no wholesale existing mixed-EOL file rewrite.
- Original `493ad243` focused red receipt remains `temp-working-dir/final-integrated-gates.log`: 22 failed/729 passed/1 skipped. Causes: 16 Unicode production failures, one obsolete post-constructor Claude fixture mutation, four persisted-bookkeeping versus public-wire dictionary expectations, and one FakeSeat missing `current_spawner`. The latter reproduced on exact `be14684` (one failure, 4.55s); the approved fixture repair lets ownership assertions execute and changes no policy source.
- Earlier OWNER size 59,007 was one of TWO SYSTEM messages; complete measured total was 79,406 (59,007 + 20,399). These synthetic capture counts are not a precise real-seat shrink comparison.
- New authoring test-driver failures (wire/private metadata lookup, loop-variable seat-name shadowing, missing actual startup restoration before CLI args) were corrected in tests; they did not justify product changes. New managed role bodies are revised through the audited publisher, not by invalidating their metadata with direct writes.

## Cross-repository producer evidence (owner-supplied, not rerun here)

- Harness `dc186bb311574f08eda9eec0e85e2ee0a796b236`: **387 passed, 17.01s**, 13 files including T0586, no failures/skips/warnings. Exact receipt: `C:/Projects/LiteTUI/output/tasks/t-7849b3cf541646d8bb42fb75036e6226.log`.
- Harness map: `cli.py:3472,3986,4332,4462`; `resume_seat.py:233,252-258`; `owned_resume.py:17,73-91,97,105`; `owned_launch.py:85-97`. NEW explicit file routing supports dead owned named resume, validates exact bridge echo, and loudly refuses unsupported/live custom routes. Producers do not publish consumer HOME files.
- Suite `66f2231cd4f41dcba88d241cbaef870279dbeeb2`: owner-supplied prior **104 passed, 2 skipped**, receipt `C:/Projects/.scratch/T0338/vitest_66f2231_sentinel.log` (skip names not embedded in supplied receipt). Four-file oxlint 0/0. Auth bridge route suite collected **zero tests** because the effect loader was missing; this is NOT route-test coverage. Main typecheck has TS2688 electron dependency failure, receipt task `t-dd7fb86768274841bf2ccf5710a158b9`.
- Suite map: `spawn-service.ts:71,135,231-260`; `cli-adapters.ts:186-198`; `agent-bridge.ts:564-575`. Existing typed `systemPromptFile`/`spawnedBy` contract is unchanged by resume authoring.
- **MAIN POSTMERGE hard gate, before ANY launch/restart:** run `agent-bridge-litetui-spawn.test.ts` and `tsc`; RED means Sentinel reverts. Old 190-test producer evidence is not represented as current new-route coverage.

## Evolution Assessment and remaining gates

Measured ancestry: `be14684f2112037517ca3957c55d7f78a0978fa4`, native/BOM bundle, conditional reader, R1/R2/R3 fixes, fixture closure, origin fix, named authoring and f5 validator are all ancestors of tested tree 337 (all exit 0, full IDs in receipts). `f5..337 -- src tools pyproject.toml` is empty. The subsequent evidence-only commit must also have empty `337..HEAD -- src tests tools pyproject.toml`; its file list and exact-head smoke are delivered separately.

No merge was initiated here; no main-tree mutation, push, dependency install, seat spawn/restart, model call or rollout was performed. Independent Dijkstra re-review of this fixed source/evidence remains required. Actual main restart, paired contract if required, Suite postmerge tests/typecheck and Ryan's look are not replaced by green worker tests. Keep the task REVIEWING until those gates are satisfied.

| Self-check | Score / 5 |
|---|---:|
| Pain-driven | 5 |
| Composition over inheritance | 5 |
| Minimalism | 5 |
| Requirement-driven flexibility | 5 |
| Shippable within explicit review/deployment gates | 4 |
