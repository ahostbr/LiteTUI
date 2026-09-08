# LiteTUI Worktree Harvest — 2026-09-08

Surveyed by SilverBolt (T494). Commands: `git -C C:/Projects/LiteTUI worktree list`, per-tree `git log main..HEAD --oneline`, `git status --porcelain`, `git merge-base --is-ancestor <sha> main`.

## Summary

6 worktrees + root. 1 detached HEAD banked. 1 unmerged commit (seat-mcp handoff doc). 1 forensic tree (verify) with 18 untracked investigation files — untouched per memory entry `the-verify-jobobject-worktree-and-why-it-exists.md`.

## Per-tree survey

| Tree | Path | HEAD | Branch | On main? | Unmerged | Uncommitted | Action |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **root** | `C:/Projects/LiteTUI` | fd322e1 | main | — | — | 9 untracked dirs (`.liteharness/`, `.playwright-cli/`, `logs/`, `pccontrol/`, `skills/find-claude-skills/`, `skills/ls-bridge/`, `skills/minecraft-agent/`, `temp/`, `tools/pccontrol/screenshot.py`) | None — untracked dirs are runtime/plugin artefacts |
| **litetui-baseline** | `C:/Projects/.worktrees/litetui-baseline` | 02c8fe1 | **DETACHED** → banked as `keep/litetui-baseline-2026-09-08` | YES | 0 | 0 | Banked detached HEAD to named branch |
| **runtime** | `.worktrees/runtime` | 980f6d2 | chore/package-move | YES | 0 | 0 | Clean, already merged |
| **seat-mcp** | `.worktrees/seat-mcp` | d975896 | fix/seat-and-mcp | NO | 1 | 0 | `d975896 docs(handoff): SilverBolt seat+mcp handoff, with paths repointed after the package move` — a handoff doc, not code; branch holds it |
| **t219-secret-redaction** | `.worktrees/t219-secret-redaction` | bfbc3ea | fix/t219-secret-redaction | YES | 0 | 0 | Clean, already merged |
| **t420-autoscroll** | `.worktrees/t420-autoscroll` | b1a1458 | fix/t420-autoscroll-thinking | YES | 0 | 0 | Clean, already merged |
| **verify** | `.worktrees/verify` | c4d7738 | verify/jobobject | NO | 2 (merge commits into verify/jobobject) | 18 untracked .txt files | **Forensic tree — DO NOT TOUCH.** The only pre-fix tree for the jobobject gradient disagreement (`c4d7738`). 18 investigation files: `b3_interim.txt`, `backtick_nearmiss.txt`, `chase_report.txt`, `comment_claim_unsourced.txt`, `disclosure.txt`, `eviction_correction.txt`, `filed_it.txt`, `final_report.txt`, `index_overflow.txt`, `maildir_absence.txt`, `maildir_mechanism.txt`, `nothing_lost.txt`, `ordering_undecidable.txt`, `prune_correction.txt`, `shutdown_report.txt`, `stood_down.txt`, `third_case.txt`, `who_is_writing.txt` |

## Actions taken

1. **Banked** `litetui-baseline` detached HEAD (`02c8fe1`) to `keep/litetui-baseline-2026-09-08` at the root repo.
2. **No deletions**, no `git worktree remove`, no `--force` operations.
3. **verify** tree left completely untouched per standing memory entry.

## Verify command

```bash
python C:/Projects/scripts/harvest_worktrees.py --repo C:/Projects/LiteTUI
```
