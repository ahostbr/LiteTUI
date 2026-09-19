# LiteTUI branch census — e3957d9 = origin/main tip

Total branches: 160  ·  MERGED: 154  ·  UNMERGED: 6

> **STATUS: census for a merge/scrub session, 2026-09-19. Every per-branch call below
> is UNVERIFIED — read the branch's diff before acting.** "MERGED" = tip is an ancestor
> of `origin/main`, OR every commit has an equivalent patch in main (squash/cherry/rebase),
> so nothing here is lost by deleting it. Big `behind` counts mean the branch is stale.

## The scrub (Ryan-gated, next step)

- **154 merged branches → delete.** ~90 are on the PUBLIC remote (`git branch -r`) and
  carry internal names (`seat/*`, `docs/handoff-openbolt-*`, `wip/ryan-0914`, `keep/*`) —
  those are visible on ahostbr/LiteTUI right now. Remote: `git push origin --delete <b>`;
  local-only: `git branch -d <b>`.
- **6 unmerged → decide each** (recommendations, UNVERIFIED):

| branch | recommend | why |
|---|---|---|
| `feat/subagent-sol-high` | review → merge or drop | 3 commits, codex tier-wire contract; 191 behind |
| `ci/advisory-scope` | **drop** | 662 behind, ancient lint-path pin, long superseded |
| `drive/t863-ninfer-free-column` | review → merge | recent (09-17) NInfer FREE-column work, only 40 behind |
| `probe/t632-real-child` | **drop** | a test probe branch, 280 behind |
| `wip/ryan-0914` | verify absorbed → drop | `feat/t753` says it "landed the 09-14 docs from wip/ryan-0914"; confirm then drop |
| `feat/t806b-ninfer-wiring` | review → merge or drop | LOCAL-ONLY WIP "delta before the ownership split", NInfer wiring, 96 behind |


## UNMERGED — real work not in main

| branch | where | uniq commits | ahead/behind | last commit |
|---|---|---:|---|---|
| `feat/subagent-sol-high` | remote | 3 | 3/191 | 2026-09-12 4b0cfd7 fix(codex): follow standard tier wire contract |
| `ci/advisory-scope` | remote | 1 | 1/662 | 2026-08-24 a8c5be7 ci(lint): pin both advisory tools to one path list and print the comman |
| `drive/t863-ninfer-free-column` | remote | 1 | 1/40 | 2026-09-17 817e921 feat(scripts): set up one NInfer FREE-column state without touching any |
| `probe/t632-real-child` | remote | 1 | 1/280 | 2026-09-11 6149cbe test(rpc): the real-child arm T632 shipped without â€” abort releases a |
| `wip/ryan-0914` | remote | 1 | 1/174 | 2026-09-16 16a278d wip(ryan-0914): bank the main checkout's 71-file dirty tree as found |
| `feat/t806b-ninfer-wiring` | LOCAL-ONLY | 1 | 1/96 | 2026-09-17 029fec5 wip(t806): delta before the ownership split â€” reconciling |

## MERGED — in main (ancestor or all patches present)

| branch | where | last commit |
|---|---|---|
| `` | remote | 2026-09-19 e3957d9 feat(chrome): shot attaches its screenshot to the next message automati |
| `chore/package-move` | LOCAL-ONLY | 2026-08-23 980f6d2 docs(handoff): the memory stand-down, a deliberately-killed monitor, an |
| `chore/t592-derive-source-reading-tests` | LOCAL-ONLY | 2026-09-10 51e2a89 feat(scripts): derive the tests that read source they do not own |
| `codex/litegui-workspace-runtime` | LOCAL-ONLY | 2026-09-12 f51f0be docs(runtime): record green T719 integration gate |
| `codex/litetui-lifecycle-hooks` | remote | 2026-09-12 d7e7568 test(hooks): isolate UI workers on the project runtime |
| `codex/litetui-oauth-providers` | LOCAL-ONLY | 2026-09-10 7a2df83 feat(oauth): add Codex subscription backend |
| `develop` | remote | 2026-09-15 2c1c2b7 chore(release): 0.23.0 â€” the native Codex path |
| `docs/adr-extraction` | remote | 2026-08-23 b45863e docs(adr): lift the harness narratives, keep the stop-claims at the cal |
| `docs/handoff-openbolt-guard-scope` | LOCAL-ONLY | 2026-09-10 9dcf6f6 docs(handoff): "3/6" was the guard's sensitivity â€” the blast radius i |
| `docs/handoff-openbolt-merge-guard` | LOCAL-ONLY | 2026-09-10 39a7a57 docs(handoff): the merge guard's five files are instruments â€” six mut |
| `docs/handoff-openbolt-t588` | LOCAL-ONLY | 2026-09-10 ba9ee74 docs(handoff): T584 piece 2, T559 and T588 â€” and two claims in this f |
| `docs/handoff-openbolt-t594-t584` | LOCAL-ONLY | 2026-09-10 5e6863b docs(handoff): T594, T584 piece 1, and the VRAM rule |
| `docs/handoff-refresh` | remote | 2026-09-12 c846423 docs(handoff): retract the T689 row, and record T689-T703 |
| `docs/handoff-s7` | remote | 2026-09-12 b024bca docs(handoff): section 7 â€” T694-T706 landed, T704 open mid-card |
| `docs/t583-settings-red-closed` | LOCAL-ONLY | 2026-09-10 495c4ce merge(docs): SilverBolt's handoff Â§1 â€” the settings red is closed, t |
| `docs/t619-current-contracts` | LOCAL-ONLY | 2026-09-11 e7c5ab0 docs: state hosted backend and current process seat identity |
| `docs/t703-cancel-then-jit` | remote | 2026-09-12 899d33d docs(plan): cancel-then-JIT â€” what a refused load does not prevent |
| `docs/t708-creator-magic` | remote | 2026-09-12 8af6129 docs(resolve): compare Creator Magic workflow with local CLI |
| `dual-backend` | LOCAL-ONLY | 2026-08-21 427c3c3 chore(deps): uv.lock refresh for the lmstudio SDK dependency |
| `feat/glassbox-wiring` | LOCAL-ONLY | 2026-08-22 ea0123e feat(glassbox): six channels fire from the real turn, and a plugin that |
| `feat/goal-loop` | LOCAL-ONLY | 2026-08-23 1ec2402 feat(automation): add evidence goals and fixed loops |
| `feat/openbolt-next` | LOCAL-ONLY | 2026-09-10 5d22efc merge(ux): integrate T569 â€” /think opens the level picker, /load open |
| `feat/prompt-compiler` | LOCAL-ONLY | 2026-08-23 112e146 fix(prompt): compile local path placeholders |
| `feat/runtime-log` | remote | 2026-08-24 b701c7d docs(observability): remap T065 producers by symbol |
| `feat/t558-plan-mode` | remote | 2026-09-10 9660da1 feat(app): plan mode â€” quizmaster planning through the ask tool |
| `feat/t558a-ask-over-rpc` | remote | 2026-09-10 16c1d65 feat(ask): answer ask_user_question over the wire instead of deadlockin |
| `feat/t558a-manual-verify` | remote | 2026-09-10 e8d3b65 test(ask): manual end-to-end verification of ask_user_question over --r |
| `feat/t558b-set-command` | remote | 2026-09-10 92ac417 feat(rpc): authority and plan mode change mid-session, through one path |
| `feat/t558c-pytest-hygiene` | remote | 2026-09-10 972d358 fix(tests): run_all gives the script half src/ on the path, as conftest |
| `feat/t570-footer-nav` | LOCAL-ONLY | 2026-09-10 3c8c0a0 feat(footer): the two panels behind the bg and agents chips |
| `feat/t584-consult-smoke` | LOCAL-ONLY | 2026-09-10 4567f89 test(rpc): select rpc events by type, not position â€” `ready` is not r |
| `feat/t584-consult-via-litetui` | LOCAL-ONLY | 2026-09-10 14a173f merge(tests): integrate T579 part 2 â€” three more pre-existing LiteTUI |
| `feat/t645-usage-on-the-wire` | remote | 2026-09-11 9448131 feat(rpc): report context usage, so the host's meter has a number to dr |
| `feat/t647-auditable-spawn` | remote | 2026-09-11 40c7dfe feat(rpc): ready says what profile was ASKED for, not only what is acti |
| `feat/t676-codex-reasoning-parts` | remote | 2026-09-11 c73b759 fix(codex): keep the reasoning summary parts apart, and render the head |
| `feat/t677-footer-fit` | LOCAL-ONLY | 2026-09-11 28c39a2 fix(footer): preserve identity and context percent when narrow |
| `feat/t679-footer-colour` | LOCAL-ONLY | 2026-09-11 c82bf4e feat(themes): add independent context footer colours |
| `feat/t684-runtime-models` | LOCAL-ONLY | 2026-09-12 d88f478 feat(rpc): report and switch backend models |
| `feat/t688-coexist` | remote | 2026-09-12 d66cc81 fix(settings): a save writes only what this instance changed, atomicall |
| `feat/t688-router` | remote | 2026-09-12 3a3be86 fix(router): take over when the owner leaves, and say what a load will  |
| `feat/t689-store-lost-update` | remote | 2026-09-12 8f722fc fix(tasks): our own pid on a disk row means the pid was reused, not tha |
| `feat/t690-second-instance` | remote | 2026-09-12 fd52e9c fix(vram): the gate moves into the backend, where no caller can go roun |
| `feat/t691-per-convo` | remote | 2026-09-12 1b5a263 fix(convo): the four fields the first cut declared and never wired |
| `feat/t692-docs` | remote | 2026-09-12 306370b docs(handoff): SilverBolt, the two-instances run T688-T692 |
| `feat/t694-switch-site-count` | remote | 2026-09-12 8d4bb3f test(context-length): the switch-site count was pinned to a layout, not |
| `feat/t695-tool-profile-per-convo` | remote | 2026-09-12 66f7b56 feat(convo): the authority a conversation was SET to, apart from every  |
| `feat/t698-fold-picker-onto-switch` | remote | 2026-09-12 48b270d refactor(model): the picker goes through the one switch path |
| `feat/t699-script-files-abort-collection` | remote | 2026-09-12 c2196a1 fix(tests): two files named test_* were scripts that killed the whole r |
| `feat/t700-module-level-exit-scan` | remote | 2026-09-12 099346f fix(tests): every test_* that exits at import, and the scan that stops  |
| `feat/t702-one-exit-rule` | remote | 2026-09-12 3964348 fix(tests): one module-level-exit rule, and the thirteen files it was h |
| `feat/t705-stop-line` | remote | 2026-09-12 c933451 feat(t705): add terminal turn stop line |
| `feat/t706-autoscroll-lock` | remote | 2026-09-12 4f6b334 fix(autoscroll): follow is a lock the reader owns, not a default the ap |
| `feat/t707-tool-cards` | remote | 2026-09-12 bd95099 feat(t707): collapse completed tool cards |
| `feat/t719-litegui-runtime` | remote | 2026-09-12 c01fbe6 test(hooks): persist scheduled admission fixture |
| `feat/t751-cache-without-native-loop` | remote | 2026-09-16 fcf2f89 feat(codex): 0.23.1 â€” LiteTUI's own loop is the default Codex path; t |
| `feat/t753-card-summary-on-0231` | remote | 2026-09-16 5d65a31 docs(litetui): land the 09-14 docs, skills and tools from wip/ryan-0914 |
| `feat/t806-ninfer-backend` | remote | 2026-09-17 64a22ee feat(backend): NInfer as a LiteTUI backend â€” attached, never spawned |
| `feat/t806c-ninfer-features` | remote | 2026-09-17 0c0590a feat(backend): connect NInfer to the features LiteTUI already has |
| `feat/t806d-measure` | remote | 2026-09-17 77faa4d feat(rpc): turn_end must name the clock that produced the tok/s |
| `feat/t825-compaction-event` | remote | 2026-09-17 eda67e5 feat(rpc): the compaction says what it did, at all five of its exits |
| `feat/tool-policy` | LOCAL-ONLY | 2026-08-23 e9cb0b6 feat(policy): enforce tool authority profiles |
| `feat/windows-ci` | LOCAL-ONLY | 2026-08-23 949b076 ci(windows): first CI for this repo â€” pytest blocks, ruff and mypy ad |
| `feature/local-ws4` | remote | 2026-08-26 8274452 feat(router): honor the ownership record â€” two apps, one router, nobo |
| `feature/swap-button` | remote | 2026-08-25 98f48ac docs(handoff): carry the fg/bg exit-code polarity at the top, verbatim |
| `feature/t507-rpc` | remote | 2026-09-08 f64442b fix(harness): T5 â€” one seat id per process, no ghost identities |
| `fix/app-defects` | LOCAL-ONLY | 2026-08-22 e0196dd feat(autocomplete): the slash picker lists commands, not only skills |
| `fix/astra-review` | LOCAL-ONLY | 2026-09-11 8c9bcd6 fix(turn): stop remaining tool calls after batch cancellation |
| `fix/autoscroll-collapse` | remote | 2026-09-12 bcf4c17 test: isolate settings and tool timing guards |
| `fix/convos-ui` | LOCAL-ONLY | 2026-08-22 0d7a773 fix(convo): re-surface a broken save on the picker title |
| `fix/kill-tree-honesty` | remote | 2026-08-23 b2e667d docs(handoff): refresh to the closing state, and record the memory stan |
| `fix/llama-backend` | LOCAL-ONLY | 2026-08-22 21ec91c test(llama): make the captured 400/503 bodies load-bearing |
| `fix/runtime-and-tests` | LOCAL-ONLY | 2026-08-23 7d80688 test(org): one authoritative command, green in the locked env, with the |
| `fix/seat-and-mcp` | remote | 2026-08-23 d975896 docs(handoff): SilverBolt seat+mcp handoff, with paths repointed after  |
| `fix/t219-secret-redaction` | remote | 2026-09-06 bfbc3ea fix(sanitize): the one path every tool result crosses now looks at the  |
| `fix/t420-autoscroll-thinking` | remote | 2026-09-06 b1a1458 fix(test): the ThinkingBlock double was a snapshot of the class, and th |
| `fix/t558c-one-runner` | LOCAL-ONLY | 2026-09-10 dc4765e fix(tests): the one runner was silently skipping 83 tests |
| `fix/t571-rpc-tasks` | LOCAL-ONLY | 2026-09-10 473e473 fix(rpc): tasks.list and tasks.kill against real Task rows, not dicts |
| `fix/t572-loop-rpc-guard` | LOCAL-ONLY | 2026-09-10 11320d3 fix(rpc): no keyboard dialog is opened headless, by any route |
| `fix/t573-command-palette-arm` | LOCAL-ONLY | 2026-09-10 cc8aeb5 feat(commands): /plan, and the palette row that comes with it |
| `fix/t576-model-picked-double` | LOCAL-ONLY | 2026-09-10 11203f1 fix(tests): PickDouble carries the backend the callback reads, and both |
| `fix/t577-approval-over-rpc` | LOCAL-ONLY | 2026-09-10 577620c docs(handoff): T577 half 1, and what half 2 still owes |
| `fix/t577-ask-first-shell` | LOCAL-ONLY | 2026-09-11 3fde4c8 merge(ask): integrate T644 â€” the ask tool sends multiSelect on the wi |
| `fix/t577-ruff-new-files` | LOCAL-ONLY | 2026-09-10 98d8971 chore(lint): gate what T577 half 1 added, and say which findings pre-da |
| `fix/t578-footer-no-overlap` | LOCAL-ONLY | 2026-09-10 5676858 test(footer): pin that a clickable footer widget owns its own cells |
| `fix/t579-litetui-reds` | LOCAL-ONLY | 2026-09-10 83074a3 test(seat): the convo-identity arms follow T507-T5, which removed what  |
| `fix/t579-litetui-reds-2` | LOCAL-ONLY | 2026-09-10 ea8eb7c test(tools): two doubles learn the tool_call event the seam now emits |
| `fix/t579-litetui-reds-3` | LOCAL-ONLY | 2026-09-10 29a8227 docs(handoff): T590 â€” fixed outside git, so the .bak stamps are the r |
| `fix/t580-script-guard-coverage` | LOCAL-ONLY | 2026-09-10 f8a559b docs(handoff): SilverBolt's LiteTUI evening â€” and a defect I shipped |
| `fix/t581-bg-chip-panel` | LOCAL-ONLY | 2026-09-10 03946c1 docs(handoff): T581 fixed â€” and two arms that are not pinned by it |
| `fix/t582-ls-mark-skill-path` | LOCAL-ONLY | 2026-09-10 1bdccef docs(handoff): T582, and the three card items that were already true |
| `fix/t583-settings-cannot-save` | LOCAL-ONLY | 2026-09-10 e00e75d fix(settings): the settings screen could not save AT ALL â€” two fields |
| `fix/t585-delete-rebind` | LOCAL-ONLY | 2026-09-10 2550047 refactor(harness): delete Seat.rebind â€” dead since T507-T5, alive onl |
| `fix/t592-bg-store-anchor` | LOCAL-ONLY | 2026-09-10 4f2517b fix(tests): T581's arms were merged flaky â€” they raced the footer's o |
| `fix/t592-conftest-taskstore-pollution` | LOCAL-ONLY | 2026-09-10 7b929b5 merge(tests): integrate T592 follow-up â€” the task-store guard stops s |
| `fix/t594-no-jit-load` | LOCAL-ONLY | 2026-09-10 03e572d fix(rpc): a headless child never conjures VRAM nobody is watching |
| `fix/t595-cron-autonomous` | remote | 2026-09-11 68ea95f test(cron): the test title asserted an overturned ruling â€” production |
| `fix/t611-transport-residency` | remote | 2026-09-11 5036eab fix(model): the two wire paths that named a cold model with nobody pres |
| `fix/t615-torn-tail` | LOCAL-ONLY | 2026-09-11 0cb8634 fix(store): preserve append boundaries after torn transcript tails |
| `fix/t616-goal-transport` | LOCAL-ONLY | 2026-09-11 37fedfa fix(goal): evaluate through the active inference transport |
| `fix/t617-test-isolation` | LOCAL-ONLY | 2026-09-11 5eee78d test(rpc): gate live children and bound reads; run CI script partition |
| `fix/t618-data-root` | LOCAL-ONLY | 2026-09-11 8e3bd86 feat(paths): add opt-in durable data root without moving defaults |
| `fix/t621-compaction-stop` | LOCAL-ONLY | 2026-09-11 e7625f2 fix(compact): scope cancellation latch to the new operation |
| `fix/t622-rpc-job-save` | LOCAL-ONLY | 2026-09-11 ab8b270 fix(rpc): pass scheduler jobs before storage root |
| `fix/t624-cache-usage` | LOCAL-ONLY | 2026-09-11 741ddbd fix(usage): retain optional cache details through transport and app |
| `fix/t627-backend-switch-isolation` | LOCAL-ONLY | 2026-09-11 4a632eb test(backend): isolate public reconnect and retain startup guard |
| `fix/t631-ready-backend` | remote | 2026-09-11 ce1a33d feat(rpc): ready names the backend, because the host cannot derive it |
| `fix/t632-abort-parked-ask` | remote | 2026-09-11 4ec6954 fix(rpc): abort releases the parked ask and stops without asking a keyb |
| `fix/t640-loop-model-pickers` | remote | 2026-09-11 1a1d5b3 feat(settings): the agent loop's two side calls get a model each, and o |
| `fix/t642-substitute-resident` | remote | 2026-09-11 2c38b71 fix(residency): a headless child answers on a LOADED model instead of r |
| `fix/t643-absolute-task-log` | remote | 2026-09-11 19fac31 fix(tasks): advertise the background log by ABSOLUTE path, not one the  |
| `fix/t644-multiselect-flag` | remote | 2026-09-11 673a6a3 fix(ask): the ask says on the wire that its options are multi-select, w |
| `fix/t693-headless-chrome-relay` | remote | 2026-09-12 81bbfda fix(chrome): launch relay without a console |
| `fix/t697-kill-tree-census` | remote | 2026-09-12 05b1f65 test(hooks): include bounded hooks in cancellation provider census |
| `fix/t704-sidebar-mount-race` | LOCAL-ONLY | 2026-09-12 7f1d4b2 merge(docs): integrate SilverBolt's handoff refresh â€” the retracted b |
| `fix/t704-sidebar-mount-race-2` | remote | 2026-09-12 8b37520 fix(side_panel): a teardown waits for the body to compose before prunin |
| `fix/t717-click-helper` | remote | 2026-09-12 4b4c05c test(hooks-ui): the click helper waits for the scroll instead of guessi |
| `fix/t719-pending-spawn-stop` | remote | 2026-09-12 7a8c3a3 fix(tasks): preserve process ownership through shell promotion |
| `fix/t723-shutdown-prune` | remote | 2026-09-12 e31ee72 fix(app): settle the tree before Textual prunes it at shutdown |
| `fix/t733-ttyguard-census` | remote | 2026-09-12 7b47136 test(run-all): the negative control named a file my own fix moved |
| `fix/t816-red-main` | LOCAL-ONLY | 2026-09-17 2691c29 fix(ninfer): start() reads only this spawn's log bytes, and names a hea |
| `fix/t816-red-tests` | remote | 2026-09-17 31a5c4e fix(tests): the seven reds whose assertions had stopped describing the  |
| `fix/t816-settings-crash` | remote | 2026-09-17 06c5067 fix(residency): /settings must open on a backend with no loaded_models |
| `fix/t821-count-completions` | LOCAL-ONLY | 2026-09-17 b0516c9 test(hooks): count completions, not calls through the client |
| `fix/t822-image-path-grep` | remote | 2026-09-17 3a37476 fix(submit): a message that MENTIONS an image is not a message that IS  |
| `fix/t823-image-path-silent-drop` | LOCAL-ONLY | 2026-09-17 32beb00 fix(submit): a URL is not a filesystem path |
| `fix/t824-engine-error-reason` | LOCAL-ONLY | 2026-09-17 a13d7fd merge(errors): T824 â€” the refused-media stub survives a reload, not j |
| `fix/t824-vision-400` | remote | 2026-09-17 ca96539 fix(errors): the engine's reason survives the client, and a refused ima |
| `fix/t827-oversize-preflight` | remote | 2026-09-17 b1c5324 fix(submit): refuse a message bigger than the window BEFORE sending it |
| `fix/t832-t833-compaction-meter` | LOCAL-ONLY | 2026-09-17 334cce5 fix(widgets): one guarded door for every ThinkingBlock header write |
| `fix/t838-pre-existing-reds` | LOCAL-ONLY | 2026-09-17 678c3f7 fix(policy): git restore discards the worktree too, but only in some mo |
| `fix/t839-ninfer-reasoning-vocabulary` | LOCAL-ONLY | 2026-09-17 8d1e5af fix(thinking): one reasoning vocabulary, and it is the engine's |
| `fix/t858-error-path-host` | LOCAL-ONLY | 2026-09-17 0b8272e fix(connect): the error path must not need the thing whose absence caus |
| `fix/t860-actionable-remedy` | LOCAL-ONLY | 2026-09-17 2e15bb2 feat(model): on NInfer the model is the artifact, so /model picks one |
| `fix/t861-backend-label` | LOCAL-ONLY | 2026-09-17 7da7769 fix(header): every backend names itself, instead of one naming the rest |
| `fix/t862-connection-remedy` | LOCAL-ONLY | 2026-09-17 4243c0f fix(errors): a closed server must name a remedy that state allows |
| `fix/t865-promise-before-refusal` | remote | 2026-09-17 86c2441 fix(ninfer): the start promise moves to the launcher, which knows it is |
| `fix/t869-ready-wait-exit` | remote | 2026-09-17 99505d2 fix(rpc): end the pre-ready wait when connect has already answered |
| `fix/t873-promise-before-refuse-if-attached` | remote | 2026-09-18 da17a17 test(models): the widened load signature drags five doubles, and two ar |
| `fix/t877-engine-start-leaves-a-record` | LOCAL-ONLY | 2026-09-18 cd52021 fix(engine): a refused /engine start leaves a record â€” T877 |
| `fix/tool-cancel-instrument` | LOCAL-ONLY | 2026-08-23 c479554 fix(tests): the cancel arm must pass proc, not just the pid |
| `fix/tools-disabled` | remote | 2026-08-24 e1597a5 docs(handoff): my own correction header claimed section 3 and never rea |
| `integration/rel-20260914-codex-parity` | LOCAL-ONLY | 2026-09-14 40d7774 test(codex): prove the native tok/s wiring and the per-turn delta live |
| `keep/litetui-baseline-2026-09-08` | LOCAL-ONLY | 2026-09-03 02c8fe1 fix(mcp): a constructor must not block on a socket â€” connect after th |
| `refactor/app-decomposition` | remote | 2026-08-24 d8b5f4d docs(picker): attach the viewport to a verification claim that read as  |
| `refactor/turn-engine` | LOCAL-ONLY | 2026-08-23 04ac72d docs(handoff): SilverBolt finding 4 â€” TurnEngine + ConversationReposi |
| `release/0.23.0` | remote | 2026-09-15 2c1c2b7 chore(release): 0.23.0 â€” the native Codex path |
| `seat/openbolt-litetui` | LOCAL-ONLY | 2026-09-10 7b929b5 merge(tests): integrate T592 follow-up â€” the task-store guard stops s |
| `seat/silverbolt-t596` | remote | 2026-09-11 02ed4dd test(guards): a test that touches nothing must find its tmp_path empty |
| `seat/silverbolt-t625` | remote | 2026-09-11 6335536 test(scripts): the cache audit reports again, and a cost probe that ref |
| `seat/silverbolt-t633` | remote | 2026-09-11 78855c9 fix(rpc): the correlation id and the target id stop sharing one key |
| `t558-parked` | LOCAL-ONLY | 2026-09-10 a7f4ce7 merge(plan-mode): integrate T558 part 1 â€” --mode plan, Ctrl+P, the pl |
| `verify/astra-full` | LOCAL-ONLY | 2026-09-11 b270b14 fix(compact): scope cancellation latch to the new operation |
| `verify/full-run` | LOCAL-ONLY | 2026-09-11 ff932ae merge(compact): integrate T621 â€” a stop from a previous turn no longe |
| `verify/jobobject` | remote | 2026-08-23 c4d7738 Merge branch 'fix/tool-cancel-instrument' into verify/jobobject |
