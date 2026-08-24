# Handoff — SilverBolt, 2026-08-24

**Supersedes** `Docs/handoff-silverbolt-2026-08-23-session-close.md` — verified present:
`git cat-file -e origin/fix/kill-tree-honesty:Docs/handoff-silverbolt-2026-08-23-session-close.md`
→ PRESENT, at `b2e667d`.

⚠️ **That predecessor was unreachable from the server until 30 seconds ago.** It was committed while
"every push is Ryan's trigger" was in force, so it lived on this machine only — invisible to any
sweep, on any ref, by anyone else. The gate is now rescinded (Ryan, 2026-08-24), so
`fix/kill-tree-honesty` is pushed and the chain is whole. **If you inherit an unpushed branch
carrying a handoff, push it before you write yours** — a predecessor that only exists locally makes
the supersession detector blind exactly where it matters.

**The work itself is NOT repeated here.** Read these instead:

| artefact | what it holds |
|---|---|
| `PLAN.md` @ `adbceec` (branch `refactor/app-decomposition`) | **§2b** the 43-site in-scope table · **§2c** O-A's 13 test edits · **§SS8** measure-at-a-ref |
| `PHASE1-SILVERBOLT.md` @ `3ed952c` | the seal-half analysis, capability groups, per-file coupling |
| `PHASE1-OPENBOLT.md` @ `12ab5a1` | the extraction half, the 35-method knot |
| commit `909a7d7` body | T073's full reasoning and its three mutants |
| `Docs/adr/0001..0003` (on `main`) | the T069 narratives |

---

## 1. In flight — NOTHING

Queries, not claims:

```bash
git -C C:/Projects/LiteTUI/.worktrees/decomp status --porcelain | wc -l          # 0
git -C C:/Projects/LiteTUI/.worktrees/tools-disabled status --porcelain | wc -l  # 0
```

| branch | local | server | merged to main? |
|---|---|---|---|
| `fix/tools-disabled` (T073) | `909a7d7` | `909a7d7` | `git merge-base --is-ancestor origin/fix/tools-disabled origin/main` → **NOT MERGED** |
| `refactor/app-decomposition` (T070) | `adbceec` | `adbceec` | not merged; Sentinel sequences |
| `fix/kill-tree-honesty` (T062 + old handoffs) | `b2e667d` | `b2e667d` | not merged |
| `origin/main` | — | `ff4f122` | — |

**T073 is on its own branch off `main`, deliberately.** I never edited `app.py` in the worktree
OpenBolt works in. `git diff --name-only main...fix/tools-disabled` → 5 files, all mine.

## 2. Owed — split by owner

### MINE
**Nothing owed.** S4's three edits are **HELD BY DESIGN, not outstanding**:
`mcp_plugin.py:21`, `scheduler_plugin.py:15`, `scheduler_plugin.py:25`.

> **The query that releases me:** `grep -c "def jobs" src/litetui/app.py` returns **1**.

`app.jobs` raises `AttributeError` until OpenBolt's two properties land, so committing my side first
is a knowingly-red commit on a shared branch. Reach moves 43 → 40 when it lands.

### OPENBOLT
| item | the check |
|---|---|
| S4 arrival — the two properties | sent verbatim, message `e4d84a85`; `grep -c "def jobs\|def mcp_dispatch" src/litetui/app.py` → 2 when done |
| 🔴 **`4e6125e` is RED** — `tests/test_footer.py` 27/28 | bisected: `28/28` at `e6b7ee9`, `27/28` at `4e6125e`; `run_all.py` **exits 1** there. `ContextFooter` is in his widgets lift. **I LOCATED IT, I DID NOT DIAGNOSE IT** |
| O-A is **not** zero-edit | 13 test call sites, 5 files, same commit: `_read_convo` 7 · `_convo_label` 3 · `_list_convos` 2 · `_persist_error` 1. `_fmt_size` and `_write_record` are dead and free |

### RYAN
- **T073 wording review** — `TOOLS_DISABLED_RESULT` and `TOOLS_DISABLED_PROMPT` in `app.py`. He asked
  for *"disabled by litetui's settings and must be turned on manually by the user"*; both strings are
  mine and name **Ctrl+T** and **Settings → Agent loop → Tools enabled** (both verified real:
  `action_toggle_tools`, and `settings_screen.py:308`).
- The two CI failures on `main` still have **no owner**. Not mine, not T070's.

### BOARD
`lst run tasks action=update task_id=T073` → **"Task T073 not found"**. The row does not exist. I did
not invent one; the commit trailer says `T073` as instructed. **Kills this row:** the task appearing
in `lst run tasks action=list`.

## 3. Absent by decision — each with the gate that defends it

| Deliberately not done | The gate |
|---|---|
| The disabled tool path is **not executable**, and the text form is **not parsed** | `test_the_refusal_is_not_reachable_when_tools_are_on`, plus the `_Refused` stub in `tests/test_tools_disabled.py` whose `__getattr__` **raises** — if the refusal ever starts resolving a tool, that stub fails instead of quietly permitting |
| `_execute_tool` does **not** use `getattr(self, "tools_enabled", True)` | three stubs declare it explicitly (`tests/test_tool_policy_wiring.py`, the `_host` factory). **A guard whose missing input means PERMIT is not a guard** |
| The tools prompt ships **neither** all of `tools.md` **nor** silence when off | `test_prompt_compose`'s reference fold, 4 parametrizations — proven to fail if the toggle is ignored |
| `ctx.notify` / `ctx.model` / `ctx.conversation` are **dropped, not deferred** | `PLAN.md` §2b's in-scope table: **40 of 43** sites are `app`-only, 3 ctx-reachable |
| I never edited `app.py` for T070 | `git diff --name-only main...refactor/app-decomposition -- src/litetui/app.py` shows only OpenBolt's commits |
| `.worktrees/verify` and `.worktrees/s2verify` **kept** | detached verify trees; `s2verify` is the SS8 instrument. Removing a worktree here needs the junction scan first |

## 4. Caveats riding the green lines

- ✅ **T073: `run_all.py` EXIT 0**, 1,122 passed, 94 pytest files + 13 scripts — in a worktree off
  `ff4f122` with my 5 files as the only diff. **Not measured on the T070 branch**; if T073 merges
  after T070, re-run there.
- 🔴 **UNPROVEN: the T073 fix has never faced a live model.** Everything is unit-level. Nobody has
  run qwen3.8 with tools off and watched a real turn survive. The mechanism is right and the markup
  is no longer the model's only option, but *"the turn does not die"* is **asserted, not observed.**
  **Discharged by:** one live turn with tools off that ends normally.
- ⚠️ **Ruff baselines are PER-REF.** `ruff check src` = **220** on `main` and 220 with T073 (+0 from
  me); the same command on the T070 branch reads **241** because of OpenBolt's `textfmt`/`widgets`.
  Neither number is wrong and neither is a regression.
- ⚠️ **`run_all.py`'s EXIT CODE is the gate, never the pytest total.** A script-style failure is
  invisible in that number — which is exactly how `4e6125e` reads green at "1,120/0".
- ⚠️ Two stubs still model only the private name: `tests/test_goal_loop.py:102` and
  `tests/test_no_fleet_registration.py:232` define `def _system(self, text: str)` and no
  `system_message`. They **pass** — their paths never reach a renamed call — so my change did not
  break them and I did not touch them. They will bite whoever routes those tests through a speaking
  plugin.

## 5. My own corrections and retractions

1. I wrote **"pushed" under a column headed "landed"** in PLAN §4 — having written the warning
   against exactly that in my own handoff eight hours earlier. Knowing the distinction did not stop
   me, because **the row format accepted either word.** Predecessor rows now carry the query.
2. My **"compatibility window"** phrasing became a scheduling rule in PLAN 2b that licensed a red
   commit. The real discriminator: **does the target name already exist at HEAD?**
3. My phase-1 doc said `model_switch` had **20** hits; it is **18**. Two were `_system`, since
   converted. True when written.
4. My `convo.py` docstring named `_list_convos` after S2 removed the call, and a text gate counted
   the prose as a live caller — **the phantom-member defect I had found four hours earlier, from the
   other direction.** Fixed at the source, `e364fd3`.
5. I classified `_jobs` as safe to expose **read-only**. **A read of a mutable object is a write
   channel** — `_apply_job_edit` does `jobs.remove()` / `append()`.
6. **Three substring-vs-statement matches in one stretch**: `read(thread)` inside `markThread`, a
   16-space indent inside a 20-space one, and `git check-ignore temp` vs `temp/`.
7. I nearly filed a **data-loss bug** in `_apply_job_edit` from a partial read. It saves twelve lines
   below where I stopped.

## 6. For the next session — S4 onward

**Order** (PLAN §2, as ruled): S4 → S5 → S6 → S7, interleaved with OpenBolt's O-A/O2/O3/O4.
**S5 and S6 were reshaped** to public methods after I measured that `ctx` is unreachable at every
target call site — see §2b. Same members, same counts, same arrival-first law.

```bash
# 1. Am I released for S4?
grep -c "def jobs" src/litetui/app.py                  # 1 = go

# 2. SS8, before ANY number you intend to quote — revalidate, do not assume
git -C .worktrees/s2verify rev-parse HEAD:src
git -C .worktrees/decomp   rev-parse HEAD:src          # must be EQUAL

# 3. The gate
python tests/run_all.py ; echo "EXIT: $?"              # 0, never the pytest total
```

**Suggested skills:** `/liteharness` (register + inbox watcher — count live watchers before arming a
second). `/ls-conversation-lookup` before any claim that something is absent or unbuilt. `/arch` only
if the work reaches LiteSuite. **Not** `/handoff` again until there is something new to hand off.

🔴 **The one thing that saved the most time tonight, and it is cheap:** before scheduling any
"seal it behind X" step, ask **"is X reachable from every call site?"** — one AST pass. It killed
three approved steps that would each have merged clean and moved nothing.
