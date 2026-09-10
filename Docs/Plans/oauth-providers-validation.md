# T560 validation and handoff

Date: 2026-09-10. Branch: `codex/litetui-oauth-providers`.
Base: `9e6d40c17080fe8bb3ef4586aedfd1219872cc68`.
Worktree: `C:/Projects/.worktrees/litetui-oauth-providers`.

## Implemented

Native Python Codex OAuth inference, provider-scoped reasoning persistence,
read-only CLI authentication, CLI-cache model capabilities, backend/model UI,
and shared inference routing for all four callers. Tool execution, background
tasks, prompts, conversation storage, compaction and wake stay in LiteTUI.
Unknown backends fail rather than silently falling back.

Claude's production backend is intentionally omitted. Its direct OAuth probe
returned HTTP 429; no tool round-trip acceptance was established. No claim of
incompatibility or future availability is made. No Claude CLI process was used.

## Live evidence

- `uv run --locked python e2e/oauth_probe.py codex gpt-5.5`: PASS, custom-tool
  request/result on separate model calls, supplied-history continuation, and
  synthetic image input. No provider-owned tools or agent loop.
- `uv run --locked python e2e/oauth_tui_smoke.py`: PASS, actual Textual chat,
  compaction, and post-compaction fact recall. Temporary conversation data.
- Rendered `e2e/artifacts/oauth/codex-tui.svg` through Edge to PNG and inspected
  it. Backend badge, thinking, compaction card and continuation were visible.
- `uv run --locked python tools/wheel_import_gate.py`: PASS, clean install,
  package data (including Pi notice), heavy imports, and executable version.

The live TUI probe has tools disabled to limit side effects. Tool-enabled
compaction is independently exercised through the real TUI/tool door with a
mocked HTTP stream and a temporary durable file. Existing compaction/wake tests
cover failure and abandoned-turn behavior.

## Automated evidence

- Focused acceptance set: 100 passed (transport, UI, subagent, summarization,
  compaction, wake, denial and cancellation).
- Final focused rerun after cleanup: 51 passed. New provider tests: 16 passed.
- Full locked suite, Python 3.11.9 matching the main checkout's interpreter:
  1861 passed, 24 failed, 6 skipped, 6 xfailed. This is NOT a green full suite.
- Clean pinned-base comparisons reproduce the existing settings-read detector,
  log producer inventory, seat identity/rebind, model-picker doubles, and tool
  policy/toggle failures. CLI-model tests pass alone on 3.11 but fail after
  async tests close the default event loop; they also fail alone on 3.14.
- Initial Python 3.14 run found four summarizer-double regressions introduced by
  requiring backend metadata. Those were fixed and their targeted tests pass.
- Advisory Ruff: 723 findings, matching the clean base. Mypy: 121 errors in 18
  files, matching the clean base. Both new runtime modules pass focused checks.
- `git diff --check`: PASS. No changes to the source checkout or shared CLI
  credential files. No merge or push.

Raw local logs (`oauth-*.log`) and synthetic screenshots remain ignored in this
worktree. The full suite started before the last two provider test additions;
the final focused run covers the final provider code. The baseline comparison
worktree is separate and contains no implementation changes.

## Operational limits

Use `codex login`, start Codex once to populate its model cache, then select
`/backend codex`. Expired tokens require the CLI to renew the login; LiteTUI
does not rotate shared refresh tokens. Local token caps/sampling/loading do not
apply to Codex. `/modelcfg` offers supported reasoning effort. Claude remains
unavailable until a new bounded acceptance test succeeds.
