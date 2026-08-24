# HANDOFF — OpenBolt, 2026-08-24, post-T082

**Supersedes `Docs/handoff-openbolt-2026-08-24-t071.md`** (verified present:
`git ls-files --error-unmatch Docs/handoff-openbolt-2026-08-24-t071.md`).

🔴 **EVERY ROW IS A SYMBOL, A QUERY, OR A SHA. Re-run the query; do not trust the cell.**

---

## 1. POSITION — AND THIS ROW CANNOT NAME ITS OWN SHA

Committing this file advances HEAD, so any sha written here is stale by one the instant it exists.
That is structural, not sloppiness. **Run these instead:**

```bash
git rev-parse --short HEAD
git status --porcelain | wc -l
git rev-list --count HEAD..origin/main          # behind
git rev-list --count origin/main..HEAD          # ahead
git ls-remote origin refs/heads/main | cut -c1-7
```

At write time those answered: **`8484923` · 0 · 0 · 0 · `8484923`** — my branch
`refactor/app-decomposition` IS main, nothing unlanded.

## 2. IN FLIGHT — NOTHING

No half-written file, no uncommitted edit, no unpushed commit. Everything below is landed.

## 3. WHAT LANDED (all on `main`, all gated on the MERGED tree)

| row | sha | what |
|---|---|---|
| T075 | `4abfefa` | the sidebar host spike — `side_panel.py`, `dialog_style`, `/test-sidebar` |
| T077 | `49e49f9` | `dialog_side` left/right |
| T078 | `b04d031` | ConfirmStop converted |
| T080 | `7b73e63` | PickerScreen converted — **one class, five call sites** |
| T081 | `cce5ee4` | ToolApproval converted + the **focus trap** |
| T082 | `f7cd4bf` | AskUserQuestion converted — the worker-thread one |
| fix  | `8484923` | the vacuous-wait fix, **two files** |

**All six dialogs Ryan named are converted.** `askuserquestion · tool approval · confirm stop ·
models picker · convo selection` (one `PickerScreen` serves the last two) `· tools listings`
(SilverBolt's T076).

## 4. 🔴 OWED — SPLIT BY OWNER

### 4a. MINE — **NOTHING.**

### 4b. SILVERBOLT'S — **THE ONLY OPEN ITEM IN THIS THREAD**
**Re-run the full suite at `8484923` on HIS box and report the result WITH THE COMMAND NAMED.**
⚠️ **I cannot do this and neither can a fresh seat on my machine.** My box passed **1322/1322
(`pytest -q tests/`) WITH THE BUG PRESENT** — so a green here after the fix is the same green I
already had. Only a machine that has PRODUCED the failure can show the fix removed it.

### 4c. RYAN'S — the board. Sentinel says it is empty and he dispatches when Ryan calls it.

## 5. ABSENT BY DECISION, WITH THE GATE THAT DEFENDS IT

- **`T074` is NOT reverted.** SilverBolt measured it: `--ignore=tests/test_loop_list.py` → his box
  1310 passed / 0 failed; with it → 2 failed. **Trigger, not fault.** Reverting would make main
  green and hide a fragility that bites anyone under load. The gate is his measurement, in his
  report and in `8484923`'s commit body.
- **`ColorPickerScreen`, `SettingsScreen`, `CalendarScreen`, `DayScreen`, `JobScreen`,
  `ModelConfigScreen`, `HelpScreen` stay MODAL.** Dense tabbed surfaces lose in a 60-column strip;
  ruled at T075, Ryan has not reopened it.
- **The modal path of every converted dialog is the ORIGINAL SCREEN, not `_ModalHost`.** The gate is
  `test_modal_centering` + `test_modals`' `isinstance` assertions — mutating the modal branch onto
  `_ModalHost` turns both RED. That is deliberate untidiness; do not "simplify" it.

## 6. ⚠️ CAVEATS RIDING THE PASS LINES

- 🔴 **`8484923` IS UNVALIDATED WHERE IT MATTERS.** Mechanism identified, fix landed, gate green on
  my box — and **never reproduced locally**. See §4b. Treat "green" here as "unchanged", not "fixed".
- ⚠️ **QUOTE THE COMMAND WITH EVERY COUNT.** I reported "1322 passed" without saying it was
  `pytest -q tests/` and not `run_all.py`. The denominators differ by run_all's 13 script-style
  files, and Sentinel built a nondeterminism conclusion on comparing my 1322 to a 1316 from the
  other command. A count without its instrument is not comparable to anything.
- ⚠️ **`run_all` EXIT CODE is the gate, never the pytest count.** Last green: **REAL EXIT 0, 117
  pytest files + 13 scripts**, on the merged tree at `8484923`.
- ⚠️ **NOBODY GATES THE MERGE.** Each seat gates its own branch against main-as-it-was; the merged
  tree is a NEW tree nobody has tested. That is how `f1afa42` went red. Sentinel is adding the gate
  to the flow — until it exists, gate after merging, not before.
- ⚠️ **In a sidebar, `screen.query_one(SomeType)` is AMBIGUOUS** — the dialog shares a screen with
  the chat, and mine matched the skill autocomplete's `OptionList`. Select by **id** from outside a
  body. Bodies are fine; their queries are self-scoped.

## 7. 🔴 MY CORRECTIONS AND RETRACTIONS

- 🔴 **"It is the DEPTH, not the type" — WITHDRAWN as a general law.** I wrote it in `cce5ee4`'s
  body about the swallowed-click defect. T082 wrapped `AskUserQuestionBody`'s compose in an extra
  level as a deliberate mutation and **that dialog's click test stayed GREEN**, so an extra DOM
  level is **not sufficient** to swallow clicks. Something more specific to the approval dialog is
  involved (auto-sized body under `align: center middle` is the suspect) and **I never isolated it.**
  **Established:** removing the level fixed THAT case and the base class did not.
  **Not established:** anything general about depth.
  📌 **The wide version reached three places.** Corrected in `tool_approval.py` and in the memory
  entry (`8484923`); Sentinel is fixing the third copy, in his report to Ryan. *A retraction has to
  reach every copy, and the one nobody thinks of is the copy in someone else's report.*
- 🔴 **"red main is mine" — offered before measurement.** So was SilverBolt's "my file shifted
  collection order". Both were attributions ahead of evidence. His measurement settled it.
- 🔴 **I fixed ONE instance of a mechanism I had correctly diagnosed.** SilverBolt found the same
  vacuous wait in TWO files. A one-file fix would have taken his box from 2 reds to 1 — which on an
  intermittent symptom is indistinguishable from a working fix with a remaining flake, and reads as
  progress. **Count a mechanism's instances, not its failures.**
- 🔴 **I could not answer "what is your context %"** and refused to estimate. Last hard reading was
  26%; the real value was 70% — stale by 44 points. The refusal was right and the number came from
  Sentinel's screenshot of my own status line, which I cannot read.

## 8. SUGGESTED FOR THE NEXT SESSION

`/arch` · `/library` · `/liteharness`. Then read `8484923`'s commit body before touching any
sidebar test — it names the vacuous-wait mechanism, and that mechanism is the likeliest thing to be
re-introduced by anyone writing the next dialog test.
