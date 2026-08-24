# HANDOFF — OpenBolt, 2026-08-24, post-T071

**Supersedes `Docs/handoff-openbolt-2026-08-24-t070.md`** (verified present:
`git ls-files --error-unmatch Docs/handoff-openbolt-2026-08-24-t070.md`).
Written at Sentinel's order ahead of a compaction.

🔴 **EVERY ROW IS A SYMBOL, A QUERY, OR A SHA. Re-run the query; do not trust the cell.**

---

## 1. IN FLIGHT — NOTHING

`git status --porcelain` → **0**. `git rev-list --count origin/refactor/app-decomposition..HEAD` → **0**.
Local = server. 🔴 **THIS ROW CANNOT NAME ITS OWN SHA — RE-RUN IT, DO NOT READ IT.**
```
git rev-parse --short HEAD
git status --porcelain | wc -l
git rev-list --count origin/refactor/app-decomposition..HEAD
```
Written at `d47af72`; **committing this file advanced HEAD**, so the sha you hold is strictly
later. At the time of writing those three answered `9e6e9de` / `0` / `0`.
⭐ A handoff is the one document that is stale about its own position **by construction** —
which is exactly why the header rule says every row is a symbol, a query, or a sha. I wrote a
bare value and it was wrong one commit later. **The queries are the position; this prose is not.**

⚠️ **ON THE API ERROR Sentinel FLAGGED** ("Connection lost mid-response"): I re-verified every
artifact I touched after it rather than assuming. `ci.yml` parses, its job names are unchanged, and
its advisory block was **run** against the real tree (533 lines / 531 findings). Both memory files I
wrote are committed and pushed. **Nothing of mine is half-written.** The truncated turn cost a
message, not an artifact.

## 2. MY BOARD — ALL FOUR CLOSED, WITH THE MERGED-vs-PUSHED DISTINCTION

| row | sha | state | **merged?** |
|---|---|---|---|
| `T068` Windows CI | — | done before this session | in `main` |
| `T070-A` app.py extraction | `d47af72` chain | done | **branch only** — `refactor/app-decomposition`, NOT in main |
| `T067` brain camera | `5e534d67` | done | **MERGED to `develop`** in **LiteSuite**, `merge-base --is-ancestor` = yes |
| `T071` CI advisory | `b32d6ec` + `e252d84` | done | **branch only** |

🔴 **`T067` WAS ALREADY BUILT — BY ME, IN AN EARLIER SESSION.** Same `Agent-ID`. Closed without
writing code. See §4.

## 3. OWED — SPLIT BY OWNER

### 3a. MINE — **NOTHING.** All four rows closed. No open assignments.

### 3b. RYAN'S / SENTINEL'S
- 🔴 **`liteharness-oss/liteharness/hooks.py` is EDITED AND UNCOMMITTED** — Sentinel's fix to the
  parking guidance, awaiting Ryan's diff review. `M`, 0 staged, 0 ahead.
  ⚠️ **THAT REPO HAS NO STAGING GAP**: the working tree *is* production. `liteharness.__file__`
  resolves there, so the edit changed live agent behaviour before any commit. Verified by me from
  the receiving end — the new wording arrived in my per-turn nudge.
- **`MEMORY.md` line ~978** — see §5, the promotion decision. Not mine to make.
- **main's CI red** from 2 environmental failures. Unowned, unchanged all session.

### 3c. SILVERBOLT'S — `T062`/`T066`/`T072` all await a merge/close decision, not code.

## 4. ⭐ THE FIVE-OF-FIVE FINDING — AND ITS RULE

**Five rows investigated across two seats this session. Five already done or needing nothing built.**

| row | mine? | actual state |
|---|---|---|
| `T067` brain camera | **MINE** | built + merged, `5e534d67`, earlier session |
| `T071` CI advisory | **MINE** | the "pin paths" half had **no defect** — scope was already correct |
| `T062` kill-tree | SilverBolt | already merged to main, `4cc2666` |
| `T066` brain feed | SilverBolt | built + merged to develop, different repo |
| `T072` tools-off | SilverBolt | built, `909a7d7`, unmerged |

⇒ **THE DEFAULT FIRST ACTION ON A CLAIMED ROW IS "PROVE IT IS NOT ALREADY DONE", NOT "START
BUILDING".** Cost: one `git log --grep`, one grep for the feature name, one check of *which repo*.
⚠️ **`reviewing` is the status that hides this** — it reads as "work exists, awaiting a look", which
is true, and conceals that the look is all that remains.

## 5. 🔴 THE MEMORY FINDINGS — TWO ENTRIES FROM ONE INDEX SETTLE THE CAP ARGUMENT

**Comment-parked index prose is NOT loaded into context.** Proven twice: the loader's reported size
reconciles to comment-stripped non-blank lines (621 reported vs 618 measured, against 1,295 raw),
and a control pair inside one loaded context shows a heading whose parked bullets are absent beside
a sibling keeping exactly its unparked ones. **"Zero cost" is true of the cap and false of recall.**

```
line  81  lite-apps-assume-it-is-already-built   INSIDE window, unparked
          -> FIRED UNASKED -> stopped me rebuilding T067
line 978  possessing-a-fact-is-not-applying-it   OUTSIDE window AND PARKED IN A COMMENT
          -> never fired -> three agents spent an hour re-deriving a piece of it
```

⇒ **DARK TWICE OVER. MOVING IT ALONE WOULD NOT FIX IT** — the remediation is unpark **and** move.
Verified: raw file contains the phrase once, comment-stripped file contains it **zero** times.
📌 **I am not touching it.** Ryan's file, Ryan's placement rule, and I am stood down from that index.

## 6. MY CORRECTIONS AND RETRACTIONS THIS SESSION

- 🔴 **"the announce protocol is 0-for-3"** — wrong **twice**: a preventive control cannot be scored
  by the failures that got through, *and* I counted failures outside the class it guards.
- 🔴 **"seven of my commits were Task-id deviations"** — overstated by six. `T070-A` was created
  **12:56:49Z**; six commits predate it. **You cannot use an id that does not exist yet.** One real
  miss (`837f408`, 553s after).
- 🔴 **"nobody announced the split"** — false; Sentinel announced 44s after creating the rows. And
  **I could never have established that negative**: my inbox retains ~7 minutes, and I have a filed
  memory saying maildir absence proves nothing.
- 🔴 **"the entry that exists argues the other way"** — asserted about a file I had not opened.
- 🔴 **"both phrases sit in the same block"** — imported from a peer's message, never verified.
  `grep -F` → 0. **A correction can import a fresh error from the material it summarises.**
- 🔴 **"index lines have near-zero recall value"** — falsified by my own `T067` case an hour later.
- 🔴 **"the ranker establishes neither presence nor absence"** — over-retraction. Presence *is*
  establishable if you **open the file**.

⭐ **THE PATTERN, TESTED ON BOTH SEATS: every self-caught error was MECHANICAL; every peer-caught
one was an INFERENCE.** My controls check instruments and nothing checks the sentence built on a
correct number — **except where I had pre-registered a criterion**, which is the one case I caught
an inference myself. **A pre-registered criterion is a positive control for an inference**, and it
only covers claims you knew you were about to make.

## 7. CAVEATS RIDING THE PASS LINES

- ⚠️ **`run_all` EXIT CODE is the gate, never the pytest count.** Last full green: **REAL EXIT 0**,
  97 pytest files + 13 scripts, re-run *after* the CI commit.
  ✅ **DISCHARGED — re-run at `9e6e9de`, after `e252d84` AND the `0065961` merge: REAL EXIT 0**,
  **98** pytest files + 13 scripts.
  ⚠️ **The file count moved 97 → 98** — the merge added a collected test file. So the reasoning
  that would have let me skip this run (*"a `.yml` edit and a new test file cannot change existing
  behaviour"*) was **not** what made the tree green; running the gate was. **A new file changes what
  the suite COLLECTS even when it changes nothing the suite ASSERTS** — and collection is where
  an import error lands, which is a red that no amount of reasoning about the diff would predict.
- ⚠️ **`find_conversation.py` establishes PRESENCE but never ABSENCE.** A ranked hit is a
  nearest-neighbour: it counts only if it **names your target AND quotes it AND the quote is still
  on disk**. Ranking is term-rarity, not membership. Use `find` for absence, unscoped across all
  **49** memory trees.
- ⚠️ **A memory you just wrote is not searchable for ~5 minutes** (staleness-triggered refresh).
- ⚠️ **The search corpus is keyed to the plugin copy you run** — four versions, four databases, one
  six days stale. `ls | tail -1` picks **1.0.9**; use `sort -V`.

## 8. SUGGESTED FOR THE NEXT SESSION

`/arch` · `/library` · `/liteharness`. Then **PLAN §3l** (O4's completion and the two rules it
corrected). Before claiming any row needs building, apply §4.
