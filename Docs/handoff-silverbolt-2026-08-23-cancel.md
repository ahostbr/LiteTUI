# Handoff — SilverBolt, LiteTUI, T062 cancel reliability, 2026-08-23

**Supersedes** nothing; it is a sibling of `handoff-silverbolt-2026-08-23-finding4.md`
(verify: `git cat-file -e main:Docs/handoff-silverbolt-2026-08-23-finding4.md`). That one
covers finding 4; this covers the cancel work. My earliest, `handoff-silverbolt-2026-08-23.md`,
covers the seat/mcp round. **Three files, three rounds, all on `main`.**

🔴 **WHICH REF.** Committed on `fix/kill-tree-honesty`. The five commits it describes are
already merged into `main` (`4cc2666`); **this file is not** — it was written after the merge.
A sweep of `main` will report it absent, correctly.

---

## 1. In flight

**NOTHING.** Checked, not assumed:

```bash
for w in kill-tree turn-engine seat-mcp; do git -C .worktrees/$w status --porcelain | wc -l; done   # 0 0 0
for s in 46b60a0 8fa780f 04ac72d 23cbf78 5f7ca9b 87510f5 98e2481; do
  git merge-base --is-ancestor $s main && echo "$s ON MAIN"; done   # all seven
```

## 2. Owed — split by owner

### MINE
Nothing. T060, T061, T062 all accepted and closed.

### THEIRS
| seat | what |
|---|---|
| **Sentinel** | compacting; owes a ping lifting radio silence. **If it does not come, escalate to RYAN, not to him** — a seat waiting on a message that was never sent is a mutual-wait deadlock, which is the exact shape of a silence order |
| **BoldChip** | T063 `1ec2402` on `feat/goal-loop` — verified, merges clean, HELD |
| **unowned** | the gradient question. `.worktrees/verify` is **KEPT** for it: it is the only pre-fix tree and is **irreproducible once dropped** |

### RYAN'S
- 🔴 **Every push.** `main` = `4cc2666`, remote still `b604bc2`, **20 unpushed**. LiteSuite
  develop has 8 unpushed including the 3D brain. LiteImage `74bf7f6` and liteharness
  `36c14aa` **are** pushed.

## 3. Absent by decision — each with the gate that defends it

| Deliberately not done | The gate |
|---|---|
| **No retry in `kill_tree`** | Sentinel's ruling, and I agree: `taskkill /T /F` is already the hard kill, so a retry only draws another sample from a 6.38s-median/43.06s-max distribution. The number that would decide it — *given a first taskkill that blew 15s, how often does a second succeed?* — **has never been measured** |
| **The taskkill fallback is CONDITIONAL, not unconditional** | Measured: taskkill on an **already-dead pid** costs 3,678/6,568/10,393 ms, statistically the same as a live tree, because the cost is enumerating the process table. Unconditional would add ~6.5s to **every** cancel |
| **The fallback is KEPT despite a clean race measurement** | n=12, 0 escaped, asked directly with `IsProcessInJob` on the grandchild. **12 is not a proof of never** |
| **`kill_on_close` is opt-in, default off** | `ttyguard.popen` has **four** consumers; three are long-lived (MCP server, LM Studio router, mark overlay). A `KILL_ON_JOB_CLOSE` job kills its members when the last handle closes, *including on GC*. Two tests defend it, one of them an AST walk asserting `core_tools.py` is the **only** file passing the keyword |
| **`tests/test_tool_cancel.py` never edited by me** | Anvil's file. I read it, reported a stale comment in it, and gave my cherry-picked copy back (`98e2481`) rather than carrying a divergent version into a merge |
| **The probes are scratchpad-only** | Throwaway instruments, and one of them was wrong. Sentinel asked for them with the `argtypes` bug fixed; they are not product code |
| **`.worktrees/kill-tree` NOT removed** | `git worktree remove --force` FOLLOWS Windows junctions and deleted 264 GB on 2026-08-01. Scan first. It is also the only home of this file |

## 4. Caveats riding every green line

- ✅ **`1099 passed, 0 failed` on `main` (`4cc2666`) — I re-ran it myself**, 173.80s, rather
  than taking Sentinel's number. It matches his exactly.
- 📉 **The runtime is itself evidence:** 174s against 225s (`87510f5`) and 369s (`23cbf78`).
  The suite got faster because the tree-kill stopped burning 15s budgets.
- ⚠️ **A single green suite still does not establish a clean failure set** — that is the
  mistake corrected three times today. Two tests in it were load-dependent flakes for most
  of the day and are green now because the underlying product defects were fixed, not
  because the flakiness was tuned away.
- ⚠️ **`test_cancel_kills_the_whole_tree_and_the_turn_survives` passes now only because
  Anvil updated it.** My commit `87510f5` left it red *on purpose* — it hand-rolled
  `kill_tree(proc.pid)` under a comment claiming to be "the exact core of
  action_cancel_tool", which stopped being true when the cancel moved to a worker.
- ⚠️ **The Job Object is proven on THIS box only** (Windows build 26200). Nested jobs need
  Win8+; the fallback covers a platform that cannot.
- ⚠️ **No mounted-Textual cancel was ever driven.** `action_cancel_tool` → `_cancel_tool_tree`
  is asserted wired by AST and exercised through `kill_tree`, not by pressing the key.
- ⚠️ **The spawn race is mitigated, not eliminated.** Between `Popen` returning and the
  assign, a grandchild could escape. 0/12 observed; the fallback is what makes it non-fatal.

## 5. My own corrections and retractions

1. **I predicted the app.py-scoped door gate would break and it did not.** I said that while
   planning to relocate `_stream`/`_compact`; I then changed the design, they stayed, and the
   gate still reads 3. **Fragile, not wrong** — and Sentinel was retiring it on my claim.
2. **I reported the package grep at 2; it became 3 and I added the third.** My own docstring
   explaining that the grep is unreliable is a false positive for it. **Documenting the flaw
   performed the flaw.** Final number: 25 by grep, 1 by AST — a **96%** false-positive rate.
3. **My first UI-coupling measurement used a regex and was wrong.** `\bpush_screen\b` cannot
   match `push_screen_wait`, so `_execute_tool` showed 0 UI hits when it has one. Re-measured
   by AST; caught before reporting.
4. **My `IsProcessInJob` probe had no `argtypes` and lied.** Undeclared `ctypes` truncates a
   HANDLE to 32 bits on Win64. It reported this process as *not* in a job when it was — so my
   **first** probe was already the nested case and I did not know it. The conclusion was
   **stronger than I claimed, reached for a reason I had wrong.** Caught only because
   `AssignProcessToJobObject` disagreed with the readback.
5. **I inlined a message body containing backticks and the shell ate the evidence**, while
   `send` reported success. `send --help` warns about this in those words and I had read it an
   hour earlier — and had used `--body-file` for *Sentinel's* payload but not my own.
6. **My `list_all` migration made an instance method and called it as a static.** Caught on
   first inspection.
7. **My Phase-B properties broke 8 tests** by requiring `self.store` on instances built with
   `LiteTUI.__new__`. Fixed with a lazy accessor; the tests found it, not me.

⭐ Four of these seven are one root — **text matched where a statement was meant** — and it
has now fooled a guard, a classifier, an audit, a search, a gate written to replace a failed
gate, a shell, and a ctypes call. **Finding it repeatedly has not stopped it recurring.**

## Verification block

```bash
cd C:/Projects/LiteTUI
git rev-parse --short main                        # 4cc2666
git rev-list --count @{u}..main                   # 20 unpushed
python tools/tool_door_gate.py                    # 1 door, 2 callers, exit 0
uv run pytest -q                                  # 1099 passed — but see §4
uv run pytest tests/test_kill_tree_honesty.py -q  # 17 passed, ~0.5s
# the runtime IS the evidence: taskkill's MINIMUM is 3,420ms
```
