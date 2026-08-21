# Plugin Wave — LiteTUI plugin-ification, split, clean-code, review

**Ryan's brief (2026-08-21, verbatim intent):** split app.py (~5.6k lines — way too
big), study the deepseek harness again, make everything-that-can-be a plugin the
same way LiteSuite's panel system works, then a clean-code pass over everything.
**Ultracode ON. The wave starts AFTER compaction.**

## The roster (Ryan chose, AskUserQuestion 2026-08-21)

| phase | who | how |
|---|---|---|
| Architecture panel | **Johnson · Tesla · Gamma · Shannon** (all four) | Workflow 1, parallel, independent designs |
| The split itself | **Sentinel — my own hands** (Ryan: "you") | main loop, scripted edits, suite green at every step |
| Clean-code sweep | **Uncle Bob · Linus · Dijkstra** (no Knuth) | Workflow 2, disjoint file partitions |
| Review gate | **Holmes · Carmack · Rams · Moriarty** (all four) | Workflow 2, read-only, findings adversarially verified |

Operational law: polymath findings ALL get fixed. Nothing deferred.

## Sequence

1. **Workflow 1** (`workflow-1-recon-architecture.mjs`) — recon the three sources
   (deepseek-harness at `E:/SAS/REPO_CLONES/deepseek-harness`; LiteSuite panel
   system `C:/Projects/docs/architecture/02-Panel-System.md` + source; LiteTUI's
   own map), then the four architects design independently off the same recon.
   Returns recon + 4 designs + an agreement map. **I synthesize into ONE spec.**
2. **The split** — mine. Plan from the spec, execute with anchored scripts,
   `pytest` (688 green) after every move, commit per phase. Path anchors law:
   a moved file's `Path(__file__)` re-homes data silently — prove by resolution.
3. **Workflow 2** (`workflow-2-cleanup-review.mjs`) — pass the post-split module
   partitions as `args.partitions` (three disjoint lists, one per cleaner).
   Cleaners edit; one suite gate; one fix round; then the four reviewers, each
   confirmed finding verified by refuters before it reaches me. I fix all.
4. Version bump (0.21.0 or 1.0.0-worthy — Ryan's call), CHANGELOG, ship.

## Ground truths for the post-compaction seat

- LiteTUI `52a588f` v0.20.0 · suite **688 passed** · runtime in `src/` (16 modules,
  app.py ~5.6k lines) · tests 50 files (pytest-style collected via conftest's
  derived collect_ignore — script-style files are NOT collected; keep it that way).
- Known plugin-adjacent seams already in app.py: tool registry (`_all_tools` /
  `_dispatch_for`), command dispatch (`_handle_command` if/elif chain), palette
  provider table, screens (Calendar/Day/Job/Settings/Picker/Help), themes,
  skills, MCP client, prompts dir. These are the candidate plugin surfaces —
  let the architects rule, don't pre-decide.
- LiteSuite panel-system parity is the model Ryan named: registration,
  manifest, lifecycle. The `.pi/extensions` era and `litesuite-ships-no-pi-extensions`
  memory are relevant history: a plugin system that ships NONE of its plugins
  is a known failure mode here — the split must dogfood itself (core features
  AS plugins from day one).
- deepseek-harness intel in memory: `qwen-in-the-deepseek-harness-is-a-controlled-comparison`
  (localhost:3080). ~1M LOC ts/js, 824 test files — recon finds its
  extension/plugin architecture specifically, not everything.
- Textual/house traps banked in memory: `clickable-regions-on-painted-textual-text`
  (mount races, Select echo, Button debounce), `a-test-whose-expectation-came-from-the-code`,
  `a-piped-gate-reports-the-pipes-exit` (pipefail), `weights-size-is-not-residency`.
- Suite gates that WILL catch you: ttyguard envelope sweep (raw subprocess),
  collector integrity, version/CHANGELOG drift, position/centering, palette drift.

## How to start (first act after compaction)

```
Workflow({ scriptPath: "C:/Projects/LiteTUI/Docs/Plans/plugin-wave/workflow-1-recon-architecture.mjs" })
```
Then read the result, synthesize the spec into `spec.md` here, and begin the split.
