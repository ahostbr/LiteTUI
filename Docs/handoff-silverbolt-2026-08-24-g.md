# Handoff — SilverBolt, 2026-08-24 (seventh; swap button built, clip FIXED, ready to merge)

> 🔄 **UPDATED IN PLACE AFTER THE CLIP WAS FIXED.** The text below §0 describes a HELD branch and
> an unresolved rendering defect; both are resolved. Superseded statements are left VISIBLE rather
> than deleted — the account of how the defect was FOUND is the valuable part, and rewriting it
> away would hand the next seat a conclusion with no method.

## 0. ⚡ CURRENT STATE — READ FIRST. EVERYTHING BELOW THIS SECTION PREDATES IT.

```bash
git -C C:/Projects/LiteTUI rev-parse --short origin/main                    # 805db77
git -C C:/Projects/LiteTUI rev-parse --short origin/feature/swap-button     # dedcb35
git -C C:/Projects/LiteTUI rev-list --count origin/main..origin/feature/swap-button   # 2
```

| what | state |
|---|---|
| **The clip** | ✅ **FIXED** — OpenBolt, `131926d`, on `main` via `805db77`. Root cause: `#picker-box { max-height: 80% }` resolving against a `height: auto` parent — **a base computed from the thing it constrains.** Moving the cap only RELOCATES the clip; one node must be definite. |
| **My branch** | ✅ **MERGED.** `origin/main` = `81635b5` (fast-forward `1e36dfe..81635b5`). Branch ref updated to the same sha. |
| **The picture** | ✅ **LABEL PAINTS IN BOTH HOSTS.** modal `18\|█ Dock to side █` (was BLANK), sidebar `21\|▏█ Open as modal █`. sidebar full height True · modal box centred 11/11 · `FITS=True` both. |
| **My gate** | ✅ **GREEN on `81635b5`** — instrument 1 exit 0 (1418 passed), instrument 2 exit 0 (1413 passed), off a FROZEN script (md5 verified identical after the run). |
| **Merge** | ✅ **DONE.** See §0d for what the gate caught first — it was not a formality. |

✅ **THAT INSTRUCTION WAS FOLLOWED AND IT PAID FOR ITSELF — see §0d.** The original text:

🔴 **NEXT SEAT'S FIRST ACT: re-run BOTH instruments on `feature/swap-button`, merge only if both are
green.** Do NOT merge on OpenBolt's green — that is HIS tree (he reported `run_all` REAL EXIT 0,
1397 passed, 122+13). Do NOT merge on the picture alone: the picture proves it RENDERS, the suite
proves nothing else broke. `pytest -q tests/` is structurally blind to script-style files (§3), so
one instrument is not a gate here.

## 0d. WHAT THE RE-RUN CAUGHT — THE HOLD WAS NOT A FORMALITY

**The gate came back RED on my own branch.** `test_no_child_is_clipped_by_its_own_container[sidebar-picker]`:
`SwapButton rows 28..30 outside Vertical content rows 2..29`. Discriminated, not assumed —
same single test, `main` 4 passed / exit 0, branch 1 failed / exit 1. **Merging on the state this
document described would have shipped a clipped control.**

Cause: `#picker-box` is `height: auto` capped by `max-height: 100%`; nothing in the column shrinks,
so overflow lands on the LAST child. `title 2 + list 20 + hint 4 + button 3 = 29` into 28 rows.
Fix `81635b5`: `#picker-list` cap `20 → 19` — the only slack in the column.

⚠️ **I REJECTED A GREENER ARM ON PURPOSE.** `height: 1fr` PASSED the gate and stopped the box hugging
its content — a 2-item picker rendered an 18-row list instead of 4. Green and visibly wrong, which is
this row's own failure class. ⇒ **Score candidate fixes on the gate AND on a number the gate does not
assert.**

🔴 **§0 SAID `FITS=True` IN BOTH HOSTS. THAT LINE WAS PRODUCED BY A BLIND CHECK.** My probe's `FITS`
had two independent defects: it compared the child against the box's **`region`** (which includes the
border row) instead of **`content_region`** (what actually clips), and it fed the picker **2 rows**
when the gate feeds **12**. At 2 rows nothing overflows; fed 12, the `region` comparison would STILL
have printed `FITS=True`. ⇒ **A containment check needs the rectangle that clips AND an input that
makes the container overflow. Neither alone is a check.**

📌 **STILL OPEN, NOT MINE, NOT FIXED:** on `main` today, at a **24-row terminal**, the picker clips
the list AND the hint with **no swap button present** (children 2/20/4 into content rows 2..21).
Measured, not inferred. The geometry gate only runs at 100x32, so nothing sees it.

📌 **TWO INSTRUMENT LESSONS FROM THIS SESSION, BOTH SELF-INFLICTED:** a gate wrapper that PRINTS its
children's exits but ends on an `echo` exits 0 and is a **log, not a gate** (`exit $rc`); and
**editing a script while it runs changes what the running shell executes** — bash reads by byte
offset, and my mid-run fix printed a fake `RED` over two exit-0 instruments. Run a frozen copy.

## 0b. UPDATE — WHAT §2 GOT RIGHT, AND THE ONE THING I GOT WRONG

**§2 stands as the account of the defect**, including that 72 tests — one of them a geometry gate
written that hour for exactly this class — passed over an unreadable button. That is still the
answer to *"how is this possible"*.

🔴 **BUT §2's ELIMINATION LIST CONTAINS AN OVER-WIDE NEGATIVE.** It reads *"NOT max-height: 80% of
32 = 25, box is 11"* under a heading saying **"do not redo these"**. **`max-height` WAS the cause.**
I tested its numeric BOUND and reported the whole PROPERTY as eliminated. OpenBolt ran his own
property matrix instead of trusting my list, so it cost nothing — the lesson survives the luck:

> **A negative result is scoped to the property you ACTUALLY tested. An elimination list is not a
> record of what you did — it is an INSTRUCTION TO OTHER PEOPLE TO STOP LOOKING.** A positive claim
> is checked by whoever consumes it; a negative one is checked by nobody, because its whole purpose
> is to remove a branch from someone else's search. The more helpful the framing, the more
> load-bearing an over-wide negative becomes.

⚠️ **MY OWN INSTRUMENT HAD THE SAME SHAPE TWICE** (fixed in `dedcb35`): `centred?` measured
`PickerBody`, which the fix turns into the full-width container the box centres INSIDE, so it
printed `left=0 right=0` — reading exactly like the off-centre symptom Ryan photographed. **I was
one sentence from filing a regression in OpenBolt's fix that was a defect in mine.** And the text
render carries **no horizontal position at all**, invisible for as long as everything happened to be
centred. ⇒ **Print the SUBJECT beside the number:** `box x=11 w=78` cannot be misread the way
`centred? left=0 right=0` can.

## 0c. UPDATE — OWED, REPLACING §4

- **MINE** — ~~run both instruments on `dedcb35`, then merge~~ ✅ DONE (`81635b5`, then the
  handoff at `d295cc1`). ⚠️ **BUT "Nothing else" IS NO LONGER TRUE — T087 OPENED OFF THIS ROW.**
  `ab7add1` parameterised the containment gate over viewport height (24/32/50) and **six 24-row
  clips appeared across three of the four dialogs, in both hosts** — pre-existing, shipped, and
  invisible while the gate ran only at 100x32. They are `xfail(strict=True)`, so they are visible in
  every run and turn into a hard FAILURE if anyone fixes one silently (controlled: `confirm_stop`,
  which passes at 24, gives `[XPASS(strict)]` → exit 1). **The fix is ruled and owed by me:** dialogs
  SCROLL when they do not fit, the action row is PINNED, and the gate keeps STRICT containment on the
  action row so "reachable" does not become a universal excuse. Carried as its own item — it is a
  structural change to three dialog bodies plus a rewrite of the gate's semantics, not a follow-up.
- **OPENBOLT'S** — nothing. Clip fixed; `test_no_child_is_clipped_by_its_own_container` added and it
  bites without my button.
- **RYAN'S** — nothing.
- **UNOWNED** — ✅ RESOLVED. The probe is committed at `28871b1` as `tests/picture_probe.py`, an
  INSTRUMENT: invisible to `run_all`'s `test_*.py` glob and to pytest's default collection, runnable
  by explicit path so `conftest` applies. Its calibration travels inside the file.

---

*Original text follows, unedited.*

# Handoff — SilverBolt, 2026-08-24 (seventh; P1 swap button built and HELD)

**Supersedes** `Docs/handoff-silverbolt-2026-08-24-f.md`
(verified: `git ls-files --error-unmatch Docs/handoff-silverbolt-2026-08-24-f.md`).

The session ends **mid-P1**, with my half done, gated, and deliberately **not merged**. The reason
it is not merged is the most useful thing in this document — see §2.

---

## 1. IN FLIGHT — ONE BRANCH, ONE COMMIT, NOT ON `main`

```bash
git -C C:/Projects/LiteTUI rev-parse --short origin/main                       # 9380225
git -C C:/Projects/LiteTUI rev-parse --short origin/feature/swap-button        # 03a0a3c
git -C C:/Projects/LiteTUI rev-list --count origin/main..origin/feature/swap-button   # 1
git -C C:/Projects/LiteTUI branch -a --contains 03a0a3c   # feature/swap-button ONLY
```

⚠️ **`03a0a3c` IS BRANCH-ONLY. A sweep of `main` will not find it**, and that is intentional, not an
oversight. It sits exactly one commit ahead of `main`, off OpenBolt's `9380225`, so the rebase is
already clean — there is nothing to replay.

| sha | ref | row |
|---|---|---|
| `03a0a3c` | `feature/swap-button` | **T086** the swap control in all four real dialogs + its gate — **HELD** |
| `50e1d6d` | `main` | the control carries its own CSS, so adding it edits no stylesheet |
| `50f615a` | `main` | `SwapButton`: ONE control, relabel logic moved out of `dialog_demo.py` |
| `9380225` | `main` | *(OpenBolt)* the DEFAULT_CSS sweep + `tests/test_dialog_geometry.py` |

My own worktree: `C:/Projects/LiteTUI/.worktrees/swapbtn` (branch `feature/swap-button`).
🔴 **Do not `git worktree remove --force` anything here without a junction scan first** — that
command follows Windows junctions and deletes the TARGET's contents.

## 2. 🔴 WHY IT IS HELD: 72 GREEN TESTS AND AN UNREADABLE BUTTON

In the **modal** host — the default, `dialog_style: "modal"` — the swap button's label and bottom
border **do not paint**. The box ends one row early:

```
sidebar   13|▔▔▔   14|█ Open as modal █   15|▁▁▁       correct
modal     18|▔▔▔   19|██  <- LABEL ROW BLANK   20|▄▄▄  <- the BOX bottom (78 wide, button is 72)
```

**Reproduce:** `C:/Users/Ryan/AppData/Local/Temp/.../scratchpad/picture_probe.py` — a pytest module
(NOT standalone; see §3) that opens `/model` in both styles, prints `region` for panel/box/button
and reconstructs the screen as text from `export_screenshot()`.

**Ruled out already — do not redo these:**
- NOT the box refusing to grow: **h=8 without the button, h=11 with it.** It grew by exactly 3.
- NOT `max-height: 80%` — that is 25 rows of 32; the box is 11.
- NOT the button's own height: forcing `SwapButton { height: 3 }` changed nothing (region already 3).
- NOT a bad extraction: the **sidebar** label comes out of the same SVG pipeline, same widget class.

⇒ It is a container/padding interaction in `#picker-box` (`app.py`, `grep -n '#picker-box {'`), which
is the CSS sweep's territory. Reported to OpenBolt with all of the above at message `5ed3de43`.

⭐ **THIS IS THE WHOLE ANSWER TO RYAN'S "HOW IS THIS POSSIBLE".** 72 tests passed over this —
including OpenBolt's brand-new `test_dialog_geometry.py`, written that hour for exactly this class.
The gate asserts the BOX and the PANEL; **nothing asserts that a child at the BOTTOM of the box is
fully inside it.** A green suite is compatible with a control you cannot read.

## 3. CAVEATS RIDING THE PASS LINES

- **`72 passed` DOES NOT MEAN IT RENDERS.** §2. Treat any dialog change as ungated until a picture
  is taken.
- **A STANDALONE PROBE IS NOT THE GATE, AND IT LIES DIFFERENTLY.** My first two probes HUNG as plain
  scripts and passed in ~1.5s as pytest modules, because `tests/conftest.py` never applied. OpenBolt
  hit the same thing one layer up and photographed the app's **first-boot engine picker** over the
  sidebar, then reported a CSS bug that did not exist. **Run probes under pytest**; if you must go
  standalone, set `LITETUI_NO_HARNESS=1` and `LITETUI_BACKEND=lmstudio`.
- **MY PICTURE'S LINE INDEX IS OFFSET +1 FROM `region.y`.** Calibrated against the sidebar case,
  which is known good. **UNPROVEN for any other widget** — recalibrate against a known-good render
  before trusting row numbers. Uncalibrated I would have named the wrong rows.
- **Assert geometry on `region`, never `outer_size`** — they disagree and only `region` renders
  (OpenBolt's catch, fixed in `9380225`).
- **In a sidebar, query from a SCOPE, never globally.** The chat and the dialog share one screen, so
  `screen.query_one("#picker-box")` can match a different dialog's box.
- Instrument-1-is-blind-to-script-style-files, and both-instruments-or-nothing: unchanged from
  handoff-f §2. `tests/conftest.py` `collect_ignore` is the mechanism.

## 4. OWED — SPLIT BY OWNER

**OPENBOLT'S** — the `#picker-box` clip in §2. He owns the stylesheet and has just been through it.
His `9380225` is on `main` now; the clip survived it because the gate does not look at the box's
last child.

**MINE, and ONLY after his fix lands** — re-run `picture_probe.py` in BOTH styles, confirm the modal
label paints, then merge `feature/swap-button`. **Do not merge on a green suite alone.** If the fix
changes box geometry, re-run the four sidebar suites + `test_modals` + `test_dialog_geometry` too.

**RYAN'S** — nothing owed. ✅ His shift+tab keypress ("works switches") discharged the one item I
could not verify. ⚠️ Standing, relayed through OpenBolt: **do not generate load on this box.**

**UNOWNED** — `picture_probe.py` lives only in my scratchpad. It is an INSTRUMENT, not a gate, and I
did not commit it because it duplicates the geometry file's territory. **Offered to OpenBolt; no
answer yet.** If nobody adopts it, it dies with the scratchpad — which is a real loss, because it is
the only thing in this repo that has caught a rendering defect.

## 5. ABSENT BY DECISION — AND THE GATE THAT DEFENDS EACH

- **`03a0a3c` NOT merged.** Gate: §2. Merging would put the control Ryan asked for into the dialog
  he complained about, unreadable.
- **No CSS in my four files.** `SwapButton` carries its own `DEFAULT_CSS`, so adding the button
  needed one line per body and zero stylesheet edits — which is what kept my row off the sweep's
  files in a shared checkout. Gate:
  `test_the_control_carries_its_own_style_so_no_body_needs_a_rule`.
- **`swappable()` reports FALSE on the modal path rather than the button being hidden there.**
  `present_dialog` pushes the original `ModalScreen`, which has no controller
  (`grep -rn 'self\.controller\s*=' src/litetui/*.py` → exactly two lines). Endorsed by Sentinel as
  "the control telling the truth about its own case". Gate:
  `test_on_the_modal_path_the_control_says_it_CANNOT_swap`.
- **I did not switch the shared checkout onto OpenBolt's branch.** It is already checked out at
  `.worktrees/decomp` (his), and switching `C:/Projects/LiteTUI` would move the tree under everyone.

## 6. MY OWN CORRECTIONS AND RETRACTIONS

1. **I TOLD THE FLEET MY ROW NEEDED NO CSS. IT DID.** A bare `Button` has no width rule, and the
   rules would have landed in the four files OpenBolt was rewriting — nominally disjoint, actually
   not. Fixed at `50e1d6d` by giving the control its own stylesheet, which uses the P1's own
   mechanism (DEFAULT_CSS is scoped to the DECLARING class) the right way round.
2. **I NEARLY REPORTED THE WRONG ROWS**, having not calibrated my picture's line index. §3.
3. **I USED THE WRONG INSTRUMENT TWICE IN ONE HOUR.** A class-level check for `.controller` returned
   False for `SidePanel` too — it is set as an INSTANCE attribute — so the probe could not
   discriminate; the grep could. And `grep "Open as modal"` on the SVG returns 0 even where the
   label RENDERS, because textual emits one `<text>` per character.
4. **I CHANGED TWO OF ANOTHER SEAT'S TESTS AND MADE THEM STRONGER, NOT LOOSER.** The approval focus
   trap asserted `fid in BUTTONS` — a hardcoded three-id inventory. A fourth control **inside** the
   dialog turned it red while its own message read *"Tab escaped the approval"*, which was false:
   focus never left. It now asserts the invariant it stood for — the focused widget is a DESCENDANT
   of the approval body — so a fifth control needs no edit. `test_modals`' "has two buttons" is
   three, with the reason named.
5. Earlier in the session, carried forward because they are still live habits worth the next seat's
   attention: `$?` after a pipe measures `tail`; a mutation harness killed by a timeout leaves the
   code MUTATED (verify the restore on the ABORT path); and both times I created a drift pair this
   session, **the rule against it was already written down in a file I had read.**

## 7. FOR THE FAR SIDE — FIRST THREE COMMANDS

```bash
git -C C:/Projects/LiteTUI log --oneline -3 origin/main
git -C C:/Projects/LiteTUI log --oneline -1 origin/feature/swap-button    # 03a0a3c, HELD
python -m pytest -q -s <scratchpad>/picture_probe.py                     # LOOK at the modal rows
```

**Suggested skills:** none required. `/handoff` when you finish.
⚠️ `/sentinel` is the ORCHESTRATOR protocol and is **not for this seat** — Ryan said so explicitly
after a mistyped invocation (*"DO NOT sentinel save that is a type not meant for you"*). This seat is
tier **worker**: it reports through the inbox and does not drive fleet lifecycle.
