# Handoff — SilverBolt, 2026-08-24 (second, post-T073)

**Supersedes** `Docs/handoff-silverbolt-2026-08-24.md` — verified present:
`git cat-file -e origin/fix/tools-disabled:Docs/handoff-silverbolt-2026-08-24.md` → PRESENT at `ff1487c`.

🔴 **This file is on `fix/tools-disabled`, NOT on the decomposition branch.** A sweep of
`refactor/app-decomposition` will correctly report it ABSENT. It lives here because the shared
`decomp` checkout is dirty with OpenBolt's in-flight O4-c and I was ordered to stay off it.

**Work is not repeated here.** `PLAN.md` @ `refactor/app-decomposition`, the commit bodies of the
shas below, and `HANDOFF-OPENBOLT-T070.md` carry it.

---

## 1. IN FLIGHT — NOTHING

```bash
git -C .worktrees/tools-disabled status --porcelain | wc -l     # 0
git -C .worktrees/decomp        status --porcelain              # OpenBolt's O4-c, NOT mine
```

| ref | sha | mine? |
|---|---|---|
| `refactor/app-decomposition` | `6ec9934` at time of writing | shared; **stay off, O4-c in flight** |
| `fix/tools-disabled` | this file | mine, clean |

**My commits today**, all pushed: `2d33c64` S4 consumer · `4aeb5e7` S5 consumer · `f299f6f` S3
arrival (2 of 3) · `f45fadc` skills docstring · `e9f30bd` + `c61343d` `tools/comment_census.py`.
Memory: `c925273`, `5b3fd25` on `ahostbr/dotclaude`.

## 2. 🔴 ITEM 1 — `on_model_picked` TEST: **OWED AND UNSTARTED. MINE. DO THIS FIRST.**

**It is S5's member, S5 is LANDED, so the hole is live on the branch right now** — not ahead of it.
Sentinel initially bound it to S6 and **WITHDREW** that: parking a shipped hole behind a held step.

```bash
git merge-base --is-ancestor 63fd480 origin/refactor/app-decomposition   # YES — S5 arrival
git merge-base --is-ancestor 4aeb5e7 origin/refactor/app-decomposition   # YES — my consumer half
git show origin/refactor/app-decomposition:src/litetui/app.py | grep -c "def on_model_picked"  # 1
git grep -l "on_model_picked" <ref> -- tests/ | wc -l                    # 0  ← ZERO COVERAGE
```

**IT IS MINE AND I SHOULD SAY SO PLAINLY: I converted `model_switch.py:77` myself in `4aeb5e7` and
reported `run_all REAL EXIT 0`. That green was never watching this member.** A coverage debt filed
without its owner drifts.

**The work:** one test file, ~20 lines, its own commit. Callback behaviour — picking a model
switches it; picking `None` or the same model is a no-op. **No app.py change, no S6 dependency,
nothing OpenBolt holds.** Verify RED against a gutted `on_model_picked`, then GREEN against the
restored one. Do it in a **detached worktree** (shared tree is dirty), junction-scan before removal.

## 3. THE PREDICTOR — THE RULE, BECAUSE NO SCRIPT LANDED

Sentinel: *"a method nobody can re-run is a result, not an instrument."* It exists only as an inline
measurement. **The rule, so it can be rebuilt in ~40 lines:**

> **A member reached ONLY as a callback reference — never called by name — is a blind spot.**
> Nothing calls it, so nothing accidentally covers it.

**Scope and exclusions it REQUIRES, each learned from a defect:**

- **`src`-WIDE + `tests`.** An `app.py`-only scan misses `_on_convo_picked` (only ref
  `plugins/convo.py`) and `on_model_picked` (only ref `plugins/model_switch.py`) — **both of the
  worst cases.** Scope-in-the-query-not-in-the-claim bit four times tonight, all three of us.
- **EXCLUDE `@property` / `@x.setter`.** They are referenced-never-called *by nature*: 10 of 112.
  Naming the class is not filtering it — a list shipped with `_convo_loading` (a property) in it.
- **Collect class-level `_x = x` ALIASES.** `_on_model_picked` at `app.py:2851` is an arrival alias,
  not a member. A prediction about the wrong name is untestable.
- **Distinguish CALL from REFERENCE by AST**, not by text: `x.m(...)` is `ast.Call` whose `func` is
  the Attribute. Count each site once — emitting both double-counts every called site ~2x.

Result at `6ec9934`, scope `src`+`tests`, 171 of 171 files, skipped none, properties excluded:

| member | test files | only reference |
|---|---|---|
| `_on_convo_picked` | **0** | `plugins/convo.py` — **MEASURED blind by mutation** |
| `on_model_picked` | **0** | `plugins/model_switch.py` — **mine, item 1** |
| `_on_stop_answer` | **0** | `app.py` — *neither of us had it; the predictor's real payoff* |
| `_report_persist_error` | **0** | `app.py` |
| `_flush_pending_input` | 1 | — |

**`_on_convo_picked` is measured, not suspected:** gutted to a bare `return` in a detached worktree
at `6ec9934`, **full suite green — 95 pytest files + 13 scripts, EXIT 0.** 1,123 tests do not notice
that picking a conversation does nothing. Proof of edit captured *during* the run (lines asserted
before edit, file re-read and re-parsed, `git status` dirty, marker present at run END).

## 4. S6 — HELD, AND ITS NUMBERS ARE **WITHDRAWN**, NOT PENDING

🔴 **Do not resume on my earlier S6 figures.** Sentinel withdrew them; they need re-derivation
`src`-wide with the reads/writes split and the **scope printed on the table**.

- Arrival-first confirmed: all 7 public names 0 at HEAD (`new_convo` … `resume`), and none collides
  with `textual.app.App`.
- **RULING: S6 ships a test for `_on_convo_picked`'s behaviour in the same commit.** A green suite is
  not available as evidence for that member — measured, not suspected.
- Alias direction for S6 is **(a): re-point the four gates** (Sentinel, after OpenBolt's override).
  Four gates split app.py source on `def _x` and go **IndexError** on the arrival:
  `test_seat_rebind.py:271` ×2 · `test_fleet_identity.py:126` · `test_tool_context_wiring.py:154`.
- `_materialise_convo` also has 2 stub-class defs (`test_convo_rename.py:77`,
  `test_message_queue.py:59`) and 2 **product** callers in `goal_loop.py:362,425` — outside the
  plugin tier, so the reach metric cannot see them and folding them in buys no number.

## 5. OWED — SPLIT BY OWNER

**MINE** — `on_model_picked` test (item 2 above). Nothing else.

**OPENBOLT'S** — O4-c in flight (`M app.py · M turnstats.py · M tests/test_footer.py`), chasing a
RED gate he has diagnosed as a source-text proximity assertion, not a behaviour break. O4-c carries
**8 external inert sites** — `test_footer.py` 7, `test_footer_fields.py` 1 — the *entire* silent
surface of O4; a mutation gate is required there and only there.

**RYAN'S** — (a) T073's `TOOLS_DISABLED_RESULT` / `TOOLS_DISABLED_PROMPT` wording; (b) T073's
**UNPROVEN** arm: *"the turn does not die"* is asserted, not observed — discharged by **one live
qwen3.8 turn with tools off that ends normally**, and by nothing else. No mutation reaches it; its
unit gates ARE controlled (I removed the refusal guard: 1 failed naming the right assertion,
restored 7 passed). (c) The **Obsidian vault**: 210 untracked files, newest `Daily/2026-08-23.md`.
Not vestigial — `origin` exists and `refs/heads/master` == local, 3,014 files backed. **A working
backup that stopped being run in March.** Three commands, his call, I touched nothing.

**FILED AS OWED, GATES NOTHING** — `_on_stop_answer`, `_report_persist_error`: app.py members nobody
is moving.

## 6. MY OWN CORRECTIONS THIS STRETCH

1. **The 3× proxy over-report.** "3 of S6's 7 uncovered" from a call-site proxy. Tracing callers:
   `_load_system_prompt` is called by `__init__` (every test constructing the app exercises it),
   `_edit` from three driven methods. **"No direct call" is not "no coverage."**
2. **I read the WORKING TREE, not a ref** — twice, the second time *inside the check of the first*.
   Fix is mechanical: `blob("HEAD", path)`, never `Path(p).read_text()`.
3. **`grep -c` reduced a discriminator to a number.** Got `1`, read it as an echo; it was
   `[2 of 3 members found, MISSING [...]]` on line 2. **I nearly filed a bug against a tool that had
   already reported the problem.** When a match count feeds a judgement, print the lines.
4. **Nearly reported "3 outside"** on `_tps` from scraping the per-site listing — which is
   **truncated**; the per-file summary is complete. True answer 8. Caught by arithmetic refusing to
   reconcile (13+2+1 ≠ 19), not by vigilance.
5. **`str.replace` on an anchor that did not match** — changed nothing, raised nothing, reported
   success. Assert every anchor.
6. **Read `$?` after a pipe** twice, getting `tail`'s / `sed`'s status. Once it would have produced a
   false *alarm* ("your memory dir is gitignored"), which is the other direction.
7. **A control that measured nothing:** my first T073 mutation died on a POSIX path *before* the
   edit; the run reported 7 passed against **unmutated** source. **An unapplied break and a survived
   break print the same green.**
8. **Declined credit I was never offered** — read a cc's "YOU" as pointing at me. Under-claiming
   loses a formulation as thoroughly as over-claiming.
9. **Four state claims went stale between measuring and sending.** Measure in the same breath.

## 7. UNCOMMITTED / UNTRACKED — MY OWN LAW, TURNED ON MYSELF

```
.worktrees/tools-disabled   0 dirty
~/.claude                   209 dirty — ALL skills-archived deletions, NONE mine
  my memory dir             31 topic files, 31 indexed, 0 orphans, all committed (c925273, 5b3fd25)
scratchpad                  29 .py — SESSION-SCOPED, DIES WITH ME. Nothing of value lives only there;
                            the one that mattered is tools/comment_census.py.
```

⭐ Earlier tonight **both of my memory topic files were untracked** and the index uncommitted — found
by pointing the untracked-file check at myself after using it on someone else. **The artefacts
designed to outlive the session were the only ones not backed**, because a memory dir is watched by
no build and no test. Check every directory you wrote to, not just the repo you were assigned.

## 8. FOR THE FAR SIDE — FIRST THREE COMMANDS

```bash
git -C .worktrees/decomp status --porcelain      # is OpenBolt's O4-c still in flight? STAY OFF IF SO
git log --oneline -5 origin/refactor/app-decomposition
git grep -l "on_model_picked" origin/refactor/app-decomposition -- tests/ | wc -l   # 0 = item 1 still owed
```

**Then item 1.** Not S6 — S6 is held and its numbers are withdrawn.
