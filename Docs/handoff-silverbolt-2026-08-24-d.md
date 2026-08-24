# Handoff — SilverBolt, 2026-08-24 (fourth; board cleared, one straggler live)

**Supersedes** `Docs/handoff-silverbolt-2026-08-24-c.md`.
Verify: `git cat-file -e origin/main:Docs/handoff-silverbolt-2026-08-24-c.md`

On `main`. Every row I owned is done and pushed. **The live thread is a validation result, not a
build.**

---

## 1. IN FLIGHT — NOTHING OF MINE. ONE RESULT IS OWED TO OPENBOLT.

```bash
git -C C:/Projects/LiteTUI status --porcelain | grep -v '^??'   # nothing of mine, nothing of Ryan's
git rev-parse --short HEAD; git ls-remote origin main | cut -c1-7   # equal
# prompts/systemprompt.md is COMMITTED (816004c) — Ryan lifted his own fence.
```

| sha | row |
|---|---|
| `c55d9d2` `16bc4e7` `43f1ce6` `510bd47` | **T073** tri-state modal · deny-stops-turn · refusals to a prompt file · derived profile list · `AUTONOMOUS` |
| `94e8335` `4585b3c` `c9f965d` | **T076** per-tool disable (schema withheld AND refused at the door) · Ctrl+T persists · the `/tools` panel |
| `b203db0` | **T083 (P1)** the agent may always write its own store |
| `91a66ac` | **T079** token counts beside tok/s |
| `3705790` | **T074** the `/loop` panel |

## 2. 🔴 THE LIVE THREAD — A RED I REPRODUCED, AND A DIAGNOSIS I GOT WRONG ONCE

OpenBolt fixed a vacuous test wait (`screen.focused is not None`, which in a **sidebar** is already
true of the chat input before the dialog has laid out). I am the only box that has ever produced the
failure, so I am the validation instrument. **Both runs, same sha, same box:**

```
RUN 1  pytest -q tests/          @8484923 : 1322 passed, 0 failed          GREEN
RUN 2  python tests/run_all.py   @8484923 : REAL EXIT 1 — 1 failed         RED
       test_confirm_stop_sidebar.py::test_a_swap_carries_the_focused_button_AND_leaves_the_future_pending
```

🔴 **I FIRST REPORTED THIS AS `test_confirm_stop_sidebar.py:155` AND THAT WAS WRONG. RETRACTED.**
I grepped `focused is not None`, found it bare at :155, and named it the unpatched third instance.
Sentinel challenged it and he was right — **I read the LINE and not the BLOCK**:

```
153  for _ in range(10):
154      await pilot.pause()
155      if a.screen.focused is not None:      <- I called this the bug
156          break
158  ctrl = DialogController(a, ConfirmStopBody, "sidebar", "right")   <- the dialog opens HERE
```

`:155` runs **before any dialog exists**. It waits for the APP to finish mounting and place its
startup focus, for which `focused is not None` is the CORRECT condition — there is no body to walk.
Applying the `walk_children` form there would loop ten times against a body that does not exist and
fall through: **a working wait turned into a silent no-op.** `:172` already carries the strong form
on `:173-174`. That file's focus waits are both fine, and "two-of-three" was wrong.

**THE ACTUAL FRAGILITY IS IN THE TEST THAT FAILED**, which is a different one — `:104`
`test_a_swap_carries_the_focused_button_AND_leaves_the_future_pending`, not the focus test at `:144`:

```
113      await pilot.pause()                       # ONE tick
115      a.screen.query_one("#no").focus()         # assumes the body has composed
117      request_swap(...)
118      await pilot.pause()
119      await pilot.pause()                       # TWO ticks for the whole swap
```

Three bare `pause()` counts, no condition anywhere — "a tick count dressed up as a condition", which
is OpenBolt's own phrase for the class, in the one test in that file he did not rewrite.

⇒ **A grep matches SYNTAX; whether it is a defect depends on POSITION.** "Vacuous in a sidebar" is
true of a wait that runs AFTER the dialog opens and false of one that runs BEFORE it. I had the
mechanism right and applied it to a line that does not participate in it.

**What still stands:** the `run_all.py` red at `8484923` on this box is real, and a green here does
not close it. **What is withdrawn:** the line number, the file's "unpatched" status, and the
two-of-three count.

✅ **RESOLVED SINCE — OPENBOLT MEASURED IT, so this is no longer my inference.** The `:155` lead is
**dead, not doubtful**: real code, but not in the test that failed. He reproduced the real one 2/12
on his own box with the file alone — a genuine assertion failure, *"focus landed on 'yes' after the
swap, not the 'No, keep going' button it was on"* — then 0/20 on the follow-up, and diagnosed it:

> **LATE, never LOST.** Focus always converges on `no`, worst case frame 3. The product carries the
> state correctly; **the TEST reads it too early.**

So the defect is a **fixed-tick assertion**, exactly the bare `pause()` counts at `:113` / `:118-119`
— and he has censused **nine more sites** of the same mechanism. **That is his row, not mine**, and
it is the third strike on that mechanism today.

⭐ **THIS IS WHY THE RUN WAS DONE TWICE.** Run 1 was green. Stopping there would have reported the
fix VERIFIED on the only box that can verify it, and the third instance would have shipped behind my
green — the exact single-green evidence I had told OpenBolt I would refuse from *his* box.

## 3. 🔴 CAVEATS RIDING GREENS — READ BEFORE TRUSTING ANY PASS LINE HERE

- **A green on this box does not close a sidebar-click or focus test.** These failures are
  order/timing sensitive: they pass ALONE, pass under one collection order, fail under another.
  Green means the ordering landed right. Run **both** instruments, and run them **twice**.
- **`run_all.py` and `pytest -q tests/` ARE DIFFERENT INSTRUMENTS.** run_all executes 13
  script-style files separately; plain pytest collects a different set. **Their totals were never
  comparable** — 1322 vs 1316 differ by exactly that. Never compare the two numbers.
- **`tests/test_command_palette.py` CANNOT RUN STANDALONE** — 13 of 15 fail alone at HEAD, unrelated
  to any change. It needs suite order. Judging a change to it by running the file alone is the wrong
  instrument.
- **T079's wiring test runs `show_thinking=False`**, stated in the test. With it on, that harness
  stops consuming the stream after the first reasoning delta — no exception, no worker error. NOT a
  product fault; nothing in the suite drives `_stream` with reasoning AND show_thinking on.
  **Discovered work, unowned.**

## 4. OWED — SPLIT BY OWNER

**MINE** — nothing. Every row is closed on the board and pushed.

**OPENBOLT'S** — `test_confirm_stop_sidebar.py` **:113 / :118-119** (the swap test's bare tick
counts). **NOT :155** — see the retraction in §2 before touching it. When he pushes, re-run **both**
instruments here and report both; if only one is green, say so rather than the better number.

**RYAN'S** — nothing outstanding. ✅ ANSWERED: "keep the footer tps" — the token count stays on `footer_show_tps`, no separate
toggle. ✅ And his `prompts/systemprompt.md` is COMMITTED at his instruction ("ya commit my
systemprompt"), so the PLAN.md:1024 fence is lifted by its owner. Nothing of his is uncommitted.

## 5. ABSENT BY DECISION — AND THE GATE THAT DEFENDS EACH

- **T074 is NOT reverted although my file triggers the red.** Measured: without
  `tests/test_loop_list.py` this box is green, with it 2 failed, on OpenBolt's box green either way,
  and the two files together pass 18/18 in both orders. **It is a trigger, not a fault** — removing
  it would hide a fragility that bites anyone under load.
  Gate: `pytest -q tests/ --ignore=tests/test_loop_list.py` is GREEN on this box; with it, RED.
- **Loops stay off the calendar.** `calendar_view.py:125` — a fixed-cadence loop has no date to sit
  on. The `/loop` panel is the surface that decision implies. Gate:
  `test_only_loops_appear_not_cron_jobs`.
- **No per-MCP-tool checkboxes.** Governed per SERVER by `mcp_disabled_servers`; a third overlapping
  control is worse than a pointer to the second. The engine's denylist IS name-based and DOES honour
  dynamic specs — gate: `test_dynamic_specs_honour_the_denylist_too`.
- **Ctrl+T stayed a toggle when `/tools` became a list.** Every tools-off refusal names that key to
  the model and the user. Gate: `test_running_the_tools_row_opens_the_list_and_does_NOT_toggle`.
- **`TOOLS_DISABLED_PROMPT` did not move** to `prompts/tool-denied.md`. It is a system-prompt section
  composed at turn start, not a refusal returned in place of a tool result.

## 6. MY OWN CORRECTIONS AND RETRACTIONS

1. **I nearly filed nondeterminism from two different commands.** OpenBolt reported 1322 passed; I
   had 1316 passed + 1 failed; we were one message from calling it nondeterministic. **Different
   instruments, different denominators.** The datum that mattered came from running HIS command on
   MY box: **2 failures, not 1**, in two different files — which converted the diagnosis from
   "nondeterminism" to "reproducible and box-specific", a different bug with a different fix
   criterion. Sentinel had already written the nondeterminism conclusion to Ryan and retracted it.
2. **My collected-count prediction missed: I said 115, it was 116.** The arithmetic was right on a
   STALE input — my baseline predated a merge that added a file. **Re-baseline after a merge.**
3. **I shipped a green-that-cannot-run for ten minutes in T079.** Thirteen green tests over a
   feature wired to nothing: every one CONSTRUCTED the input it tested for (`tick(reasoning=True)`)
   while the app called plain `tick()`. A test that builds its own input proves the function works
   and says nothing about whether anything calls it that way.
4. **I hit "two structures that must agree" FOUR times in one day**, once within an hour of filing
   the law for it, in tables I had written that session. Filing a class does not immunise you: a law
   only fires if it is attached to the ACTION (adding a dict key) rather than to the category of bug.
5. **I built the drift pair I was avoiding in T083** — derived the store as `workspace / ".convos"`,
   a second spelling agreeing with `paths.CONVO_DIR` only by coincidence. Passed 26/26 alone, failed
   15 in the suite. Fixed by reading the one anchor, and importing THE MODULE not the name.
6. **I reported the wrong line to another seat, and he could have acted on it.** I grepped
   `focused is not None`, found it bare at `test_confirm_stop_sidebar.py:155`, and sent it as the
   unpatched defect. It runs BEFORE the dialog exists — the correct condition there — and the
   "fix" would have replaced a working wait with a silent no-op. **A grep matches SYNTAX; whether
   it is a defect depends on POSITION.** Caught by Sentinel reading the block I had only grepped.
7. **A guard I wrote aborted before writing** because `SELF_STORE` contains `STORE` and my
   substring check flagged its own docstring. A guard that fails closed on a false positive still
   costs you the write.

## 7. FOR THE FAR SIDE — FIRST THREE COMMANDS

```bash
git -C C:/Projects/LiteTUI log --oneline -3 origin/main
sed -n '104,130p' tests/test_confirm_stop_sidebar.py   # bare pause() counts = the real one
python tests/run_all.py                                            # BOTH instruments, TWICE
```

Nothing to build. The only open item is OpenBolt's swap test, and my job on it is to re-measure —
on this box, with both commands, and to report the worse number rather than the better one.
⚠️ Read §2's retraction first: the obvious grep hit in that file is NOT the defect, and patching it
would replace a working wait with a silent no-op.
