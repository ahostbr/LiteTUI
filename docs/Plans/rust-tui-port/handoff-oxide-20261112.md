# Oxide handoff, 2026-11-12 (resumed after Ryan's night pause)

Seat: Oxide (owner, Opus 5.5, agent 196c7cd3-0b5e-454b-8dc2-6ee284db3288), card T0565, worktree
`<worktree>`, branch `work/rust-tui-OpenBolt`. Nothing below is verified by Ryan unless it says so.

**Public repo rule (Sentinel bb8c4f98, 2026-11-12):** ahostbr/LiteTUI is PUBLIC. Commit only; never
push any branch without Ryan's OK through Sentinel. Sentinel does the pushes. No real machine paths,
keys, emails or build logs in any committed file: use `<worktree>`-style placeholders in docs,
synthetic `C:/fixture/...` roots in fixtures, env vars in scripts. Ryan: "fix on top, no rewrite".
**Testing rule (Ryan via Sentinel a2772c62):** anything Ryan tests must be merged to develop first;
never ask him to test a branch build.

## Commits today (oldest first)

| sha | what |
|---|---|
| 2d0c9a20 | Ctrl+T/Shift+Tab/Ctrl+P fix (`_host_prompt_message(base=)`), Codex row identity in `_request_messages`, engine settings device-wide per develop 5f5ef63f, lazy slash-menu read (prefetch at Ready raced the first prompt), `anthropic_api_key` in Rust KEY_SETTINGS, develop job fields optional-but-paired |
| 1d523051, 2a1eb364, 7653dc71, 7c5568a0 | public-path scrub of 18 files; fixture digests recomputed (`scripts/rehash_fixture.py`) |
| e3480267 | merge of develop b224f2f8 (develop got 2c7299d8 Codex row identity and b224f2f8 path scrub from this seat) |
| ad898b1c | the 45 port-only Python failures: `_stream.__wrapped__` seam, class-level `_stream_body`, `refuse_below_floor` call shape, effort projection without settings, test fakes |

Pushed by Sentinel: e3480267. ad898b1c and later are local until Sentinel pushes.

## Where it stands (measured on this tree; controlled providers, no model loads)

- Rust ordinary: 1,273 passed, 0 failed, 242 ignored (fmt, clippy -D warnings clean) at e3480267.
- Controlled matrix on 2d0c9a20: 236/238 (`target/oxide-repro-20261111/controlled3.out`). The 2 misses
  (monitor_commands_review_sweep..., dictation_voice_route...) pass alone, 1/1 each: load-sensitive
  under 3 parallel targets, not fixed. Matrix on ad898b1c: `controlled4.out` (running at writing).
- Python: merged-only list + related suites at ad898b1c: 1185 passed, 12 skipped, 9 failed, all 9
  also failing on develop b224f2f8 (not this card): test_ninfer_artifact_choice (1),
  test_ninfer_backend (1), test_role_revision_t0338 (1), test_compaction_ui
  store_writes_render_as_tool_cards (1), test_fleet_floor_t1043 (5). Also on develop and not this
  card: test_seat_prompts_t0338 (6 with LITESUITE_ROOT set), test_codex_steering
  uncertain_delivery_survives_actual_conversation_store_reload (1).
- Public scan: `target/oxide-repro-20261111/tip_scan.py <repo> HEAD --all` -> 0 real paths at
  e3480267 (only synthetic ExampleProjects/TestUser/<user> remain).

## Next exact step

1. Read `controlled4.out`; rerun any miss alone before calling it real.
2. Parity sweep: `python -X utf8 scripts/capture_parity_screens.py --binary <release exe> --out target/oxide-parity/run1`
   (needs psutil + pywinpty in the parent interpreter; `--python` defaults to $LITETUI_PYTHON, else
   python on PATH), then a NEW Theater page ranking every difference. Never edit
   `rust-litetui-try-1111` or `rust-litetui-try-2`.
3. Round 3 for Ryan only through develop (Sentinel's intent gate), not a branch package.

## Open questions for Ryan

- F-keys: Python binds none (every screen opens by slash command); the Rust footer advertises F1-F12.
  Keep them as Rust-only extras or remove them for 1:1?
- LiteSound port plan (private repo ahostbr/LiteSound, branch `plan/rust-port`, 08868eea) ends with
  four questions; no build starts until he answers.

## Traps learned today

- Bash-tool heredocs collapse `\\` to `\`: regex or path edits go in a file written by the Write
  tool (pattern recorded 2026-11-12).
- Recorded fixtures carry SHA-256 chains over their own objects; any fixture edit needs
  `scripts/rehash_fixture.py ORIGINAL EDITED`.
- `run_controlled2.py` checks binary hashes against its manifest; rebuild the manifest after any
  test build (`cargo test` rebuilds include_str fixtures).
