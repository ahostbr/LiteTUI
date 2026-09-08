# LiteTUI Worktree Survey — 2026-09-08

| Tree | Branch | Tip | Merged? | Unmerged | Dirty | Junctions | Action |
|------|--------|-----|---------|----------|-------|-----------|--------|
| main checkout | main | 0d6f566 | — | — | T519 commit (clean) | none | **untouched** |
| litetui-baseline | (detached) | 02c8fe1 | — | — | clean | none | **keep** (comparison baseline) |
| verify | verify/jobobject | c4d7738 | no | — | 18 untracked | none | **keep** (only pre-fix tree, irreproducible) |
| seat-mcp | fix/seat-and-mcp | d975896 | no | 1 | clean | none | **keep** (1 unmerged commit) |
| runtime | chore/package-move | 980f6d2 | yes | 0 | clean | none | **removed** |
| t219-secret-redaction | fix/t219-secret-redaction | bfbc3ea | yes | 0 | clean | none | **removed** |
| t420-autoscroll | fix/t420-autoscroll-thinking | b1a1458 | yes | 0 | clean | none | **removed** |

## Summary

- **Before:** 7 (main + 6 worktrees)
- **After:** 4 (main + litetui-baseline + verify + seat-mcp)
- **Removed:** 3 merged trees (runtime, t219, t420)
- **Kept:** seat-mcp (1 unmerged commit), verify (per ruling — only pre-fix tree for JobObject gradient disagreement), litetui-baseline (comparison baseline)
- **Junctions:** 0 escaping (0 total across all trees)
- **Harvest:** 0 stranded patterns
