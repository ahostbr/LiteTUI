# Oxide handoff, paused 2026-11-12 (night pause by Ryan)

Seat: Oxide (owner, Opus 5.5, agent 196c7cd3-0b5e-454b-8dc2-6ee284db3288), card T0565, worktree
`C:/Projects/.worktrees/OpenBolt-rust-tui`, branch `work/rust-tui-OpenBolt` (pushed, never forced;
the LiteTUI repo is public and Ryan allowed pushing this branch). Nothing below is verified by Ryan
unless it says so.

## Commits this session (oldest first)

| sha | what |
|---|---|
| ac617962 | merge of LiteTUI develop e551f2eb (186 commits) into the port; every conflict listed in its body |
| a834b8d3 | merge seams: UNRECORDED import, `_stream_body` (restart_maintenance only when set), seat_name defaults scope, xolotl/anthropic in the Rust launcher |
| ce4a0ca8 | Ryan's bug 1 (Python model flow for /backend, /model, context dialog) and bug 2 (slash menu prefetch) |
| 55fd754b | picking an unloaded model selects it and runs Python's apply_context_length |
| (this WIP commit) | controlled-matrix fixes below, plus `scripts/capture_parity_screens.py` |

Ryan's word: on Theater page `rust-litetui-try-2` he answered **approve** to "Do /backend, /model and
/ behave like Python now?" (session.json rev 1, 2026-11-12T04:08:35Z), about a minute after it
opened. Recorded as his word on bugs 1 and 2 for build 55fd754b only.

## Where it stands

- Rust ordinary suite at ce4a0ca8: 1,273 passed, 0 failed, 242 ignored, 110 groups (fmt, clippy clean).
- Controlled-child matrix on 55fd754b: **193/238** (`target/oxide-repro-20261111/controlled2/summary.json`).
  The develop merge broke real paths. Ryan was warned via Sentinel (dcf8ea64, ebc58ca6) to avoid
  Ctrl+T/Shift+Tab/Ctrl+P and New in the round-2 build.
- Fixed in this WIP commit (rerun of only these targets, `target/oxide-repro-20261111/rerun-a.out`):
  agent_preferences 1/1, backend_switch 7/7, scheduled_transition 8/8, conversation 8/8,
  work_execution_controls 10/10, work_controls 2/2.
  - New conversation refused: develop's message 0 carries `litetui_prompt_*` keys; both
    `history_projection.FRESH_SYSTEM_KEYS` and `management/scheduled.rs` now accept them.
  - Work Run refused: develop's Job has `owner_agent_id`, `deferred_slot`; added to Rust JOB schema,
    validator and canonical defaults.
  - backend_switch fixture: display-only readiness marks probe engines; fixture neutralizes `_marks`.
  - plugin_reload fixture: private schema dir now seeded with every real schema (develop's
    scheduler loads `schedule_self` at registration).
  - agent_preferences test: compares execution blocks with explicit nulls treated as absent.
- Still failing (rerun stopped for the pause at context_process): context_process 30/35
  (connection_preferences, discovery_preferences, mcp_runtime, model_defaults..., see rerun-a log),
  and NOT yet rerun: history_inspection, compaction_settings, conversation_rewrite, claude_reset,
  work_reconciliation, engine, runtime_controls.

## Next exact step

1. Fix runtime controls (Ctrl+T/Shift+Tab/Ctrl+P close the runtime). Root cause found, not yet
   edited: `native_runtime_controls.review` predicts message 0 as `deepcopy(first)` with new content,
   but develop's toggles rebuild it with `app._host_prompt_message()` (new `litetui_prompt_base`), so
   `first_system_after.row_sha256` never matches. Fix: give `_host_prompt_message(self, base=None)`
   a `base` parameter and predict with
   `app._host_prompt_message(base=app._runtime_control_prompt(**{key: selected}))`.
2. Rebuild tests (`cargo build --tests`, write `target/oxide-repro-20261111/exes.json`), then
   `python target/oxide-repro-20261111/run_subset.py rerun-b context_process history_inspection_process compaction_settings_process conversation_rewrite_process claude_reset_process work_reconciliation_process engine_process runtime_controls_process`
   and fix what remains (first errors are summarized in this session's controlled2 logs).
3. Full 238 matrix (`run_controlled2.py` with a fresh artifacts manifest), full Rust ordinary
   suite, the merged-only Python list (`target/oxide-repro-20261111/remaining-files.txt`, 51 left,
   several are develop-only tests that need a develop baseline in
   `scratchpad/base-e551f2eb`).
4. Restage the package (`stage-package.ps1`, release build) and give Ryan round 3 on a NEW Theater
   page; never edit `rust-litetui-try-1111` or `rust-litetui-try-2`.
5. Parity sweep: run `python -X utf8 scripts/capture_parity_screens.py --binary <release exe> --out target/oxide-parity/run1`
   (global Python311 has psutil and pywinpty), then a NEW Theater page listing every difference,
   ranked. Known big one already: Python binds NO F-keys (screens open by slash command); the
   Rust footer advertises F1-F12. Ask Ryan whether F-keys stay as extras.

## Other work in this session

- LiteSound port plan (plan only, Ryan decides): private repo `ahostbr/LiteSound`, branch
  `plan/rust-port`, commit 08868eea, `docs/plans/rust-port/plan.md`. Worktree
  `C:/Projects/LiteSound/.worktrees/rust-port-plan`.
- Cargo cleanup: deleted `target/cargo-shared` and `rust/litetui-terminal/target/{debug,*-tests}`,
  9,589 MiB freed at the time; builds since recreated `target/cargo-shared`.
- Packages: `target/oxide-pkg-55fd754b` (round 2, has the merge regressions above).
- Pattern recorded: never mutate src/tests while a suite that imports them runs.

## Open questions

- F-keys: keep them as Rust-only extras or remove them for 1:1 with Python?
- Develop baseline for develop-only failing tests was never completed (stopped for the matrix).
