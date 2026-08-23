# OpenBolt — LiteTUI — 2026-08-23

**Seat:** OpenBolt (`431b1349-7135-4ba6-9998-f6a215fea839`), worker.
**Assignment:** BoldChip review finding 3 (reproducible runtime) + the test-organisation section.
**Branch:** `chore/package-move` @ `ba85462` — **0 ahead of main, porcelain 0**
(`git rev-list --count main..HEAD` → 0).

Everything I did is merged: `main` = `b82668a`.
Verify: `git merge-base --is-ancestor <sha> main` for `2215e0b`, `7bed9e1`, `ba85462`, `b82668a`.

---

## 1. IN FLIGHT

**One thing, and it is un-committed analysis rather than un-committed code.**

I am mid-diagnosis on the autoscroll flake. **No source file is modified anywhere** — confirm with
`git -C C:/Projects/LiteTUI status --porcelain` (the 6 dirty entries there predate all of today's
work and belong to no current seat) and `git -C .worktrees/runtime status --porcelain` → 0.

What exists only in my head and in a scratch probe:

- **Measured rate, n=20, isolation:** `5 failed / 20 runs` = **25%**, on
  `tests/test_thinking_autoscroll.py` at `main`. Run 19 failed BOTH tests, not one.
- **Hypothesis, NOT yet confirmed:** the 40-append burst has not been *measured* by Textual when
  the assertion runs, so `max_scroll_y` never grows, and both "not following" assertions compare
  `scroll_y` against an un-grown `max_scroll_y`. The observed failure
  `assert 38 < (38 - 2)` shows `scroll_y == max_scroll_y == 38` — the log neither scrolled **nor
  grew**, which is the signature of un-settled layout rather than of a wrong policy.
- **The probe that would confirm or kill it:**
  `C:\Users\Ryan\AppData\Local\Temp\claude\C--Projects\431b1349-7135-4ba6-9998-f6a215fea839\scratchpad\probe_flake.py`
  It records `max_scroll_y` before and after the burst per run and prints `grew`. **Discriminating
  prediction: every FAIL shows `grew ≈ 0`. If any FAIL shows a grown log, the hypothesis is WRONG
  and the bug is real policy, not a race.** Written, **never executed** — do not report its
  conclusion as mine.

---

## 2. OWED, SPLIT BY OWNER

### 2a. MINE

**The autoscroll flake, as its own commit.** Sentinel's constraints: no sleep, no retry, no
widened tolerance; report BEFORE and AFTER at the same n.

- Baseline is measured: **5/20 = 25%** (above).
- **The repo already contains the sanctioned fix pattern**, and I did not invent it:
  `git show 9d8abfe` — `_settle(pilot, cond, ceiling=3.0)` in
  `tests/test_slash_autocomplete_commands.py`, plus a sibling `_wait` in `tests/test_tool_cancel.py`.
  Its own docstring rules out the tempting alternative: *"a bigger fixed sleep is the same defect,
  slower, and it is paid on EVERY run instead of only on a slow one."*
- ⚠️ **Poll the PRECONDITION, never the assertion.** The condition to settle on is *"the burst
  actually grew the log"*. Polling until the **assertion** holds would make the not-following tests
  pass by construction and delete the only coverage that can catch an unconditional scroll.

### 2b. THEIRS

**Nothing. I am blocked on no seat.** Stated explicitly so the empty list is not read as an
omission.

### 2c. RYAN'S

- **Every push. Nothing is pushed anywhere and nothing gets pushed without him.**
- The `.worktrees/runtime` worktree and `chore/package-move` branch still exist and are now
  redundant (0 ahead of main). Removing either is his call, not mine.

---

## 3. ABSENT BY DECISION

| Not done | The gate / ruling / evidence that defends it |
|---|---|
| **`test_thinking_autoscroll.py` NOT deleted** | Sentinel raised deletion-over-repair. **Answered with evidence and refused:** it mounts 25 fillers, a real `_assistant_bubble()` and a real `ThinkingBlock`, appends 40 lines to trigger the actual `.thinking-body { max-height: 10 }` burst, and asserts on real `log.scroll_y`/`max_scroll_y`. My converted `tests/test_autoscroll.py` uses `FakeScroll(40.0, 100.0)` — hand-set numbers that **cannot reproduce burst geometry**, which is the exact mechanism of the original bug. 2 of 3 overlap in INTENT; 0 overlap in INSTRUMENT. Deleting it removes the only integration-level proof. |
| **The six `ROOT` anchors NOT consolidated** | `src/litetui/paths.py` docstring records it: app.py, chrome_tool, pccontrol_tool, seat_guard, settings each recompute `__file__.parent…` instead of importing `paths.ROOT`. Consolidating deletes the whole class but is a design change; it does not belong in a rename commit. |
| **The flake NOT fixed with a sleep, retry or wider tolerance** | Sentinel's explicit constraint, and `9d8abfe`'s docstring independently rejects the same shortcut. |
| **`tests/test_ttyguard.py`'s `relative_to(REPO / "src")` NOT changed** | It is an `rglob` SWEEP, not a fixed path: it still finds every runtime file one level deeper, and `relative_to` only shapes the violation message. Mentioning `"src"` is not sufficient reason to touch it. |
| **`src/litetui/` layout NOT further restructured** | The move was authorised as a rename. `plugins/` became `litetui/plugins/`; nothing else was re-homed. |

---

## 4. THE CAVEAT RIDING EVERY PASS LINE

🔴 **`1038 passed, exit 0` IS A REAL MEASUREMENT AND STILL NOT PROOF THE FLAKE IS GONE.** That run
included `test_thinking_autoscroll.py`, which passes ~75–80% of the time. **A green from it is not
evidence.** Command: `uv run --locked python tests/run_all.py`, locked env, 89 pytest-style + 13
script-style.

⚠️ **n=5 IS THE WRONG SAMPLE SIZE AND I DID NOT USE IT.** Sentinel's dispatch says n=5 twice. At a
1-in-5 rate, `P(5 consecutive passes) = 0.8⁵ = 33%` — so an n=5 BEFORE has a one-in-three chance of
showing 5/5 green on an *unfixed* test, and an n=5 AFTER has the same chance of "proving" a fix that
changed nothing. **I measured at n=20**, where `P(0 failures | 1-in-5) = 0.8²⁰ = 1.2%`. My own first
attempt at n=5 did return 5/5 green — a result that discriminates nothing. **Any AFTER must also be
n=20; matching his n would produce a symmetrical pair of numbers that both mean nothing.**

⚠️ **Every number in my commits is from the LOCKED env**, `uv run --locked …`, never system Python.
This matters because it was the whole finding: `pytest-asyncio` was undeclared, so the historical
**976 was a property of one interpreter**, not of the project.

⚠️ **`b82668a` merged clean and its green inherits the flake caveat above**, plus: the AFTER was run
after the *rewrite* commit, never after the `git mv`. The tree between `7bed9e1` and `ba85462` is
legitimately broken; a red there means nothing.

📌 **`e2e/` is gated twice and both gates were proven with a control that they can OPEN**, not just
close: default run collects **0** from `e2e/`; `pytest e2e/` without the env var → 1 skipped;
`LITETUI_E2E=1` → 1 collected. A gate only shown to close is indistinguishable from a broken one.

---

## 5. MY OWN CORRECTIONS AND RETRACTIONS

**I nearly complied with n=5 without doing the arithmetic.** It was an explicit instruction from a
seat whose word carries Ryan's, and it was underpowered by a factor that makes the answer
meaningless. Following it precisely would have produced a confident BEFORE/AFTER pair proving
nothing. *An instruction can be authoritative and still be the wrong instrument.*

**My miss-detector shared the transform's blind spot.** I built rewrite rules AND a separate
detector to catch what the rules missed — and derived **both** alternations from
`src/litetui/*.py`. `plugins` is a directory, so neither could see it, and `import plugins as
plugins_mod` at `app.py:37` survived. **A detector built from the same source as the transform can
only confirm what the transform already believed.** It was loud only by luck: that line meant the
package would not import at all. In a lazily-imported module it would have shipped green.

**I corrupted `pyproject.toml`** by locating a block with `str.index('[tool.setuptools.dynamic]')`
— which matched that string inside a **comment** at line 15 and spliced the replacement into the
middle of `[project]`. Restored from HEAD, redone by locating the block by content **with
assertions**, then verified it parses.

**I claimed a packaging defect before I could see it.** I said an installed `litetui` would be
missing `tool_policy`/`tool_approval`. All three imported fine — because `uv sync` installs
**editable**, which puts `src/` on the path regardless of `py-modules`. Only building a wheel
showed it: **26 `.py` shipped, 28 on disk.** The claim was right and my evidence did not support it
until I built the artifact.

**I wrote a `\t` into my own docstring.** `src\tools\harness.json` in a non-raw string — caught by
`python -W error::SyntaxWarning`, not by reading it.

**I stated "the rebind will be overwritten on quit" as certain** (in the earlier Minecraft work
this session). It was not: the client only rewrites `options.txt` when settings are dirty. Real
hazard, wrongly stated as inevitable.

**Corrections to Sentinel's state, offered because he asked to be corrected:**
1. **The duplication question is ANSWERED, not open** — evidence in §3; deletion is refused.
2. **n=5 is superseded by n=20**, with the arithmetic in §4.
3. His summary calls the package move "clean and mechanical". The `git mv` was — 48 files, all
   R100, 0 insertions, 0 deletions. **The rewrite was not:** it dragged **seven** distinct classes
   of reference, and **three of them (dynamic module-name strings, `__file__` depth arithmetic,
   variable-mediated path anchors) are invisible to every static sweep and were found only by
   running the suite.**
