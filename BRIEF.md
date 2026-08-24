# BRIEF — T070: Full decomposition of `app.py`

**TWO SEATS, ONE FILE. Ryan's call: "let openbolt and silverbolt finish then split the decomp
between them."** It is the largest single piece of work in the LiteTUI review, and he ruled it
explicitly tonight: `refactor_depth = full`, note "go ahead and dispatch the decomp".

## 🔴 THE SPLIT, AND THE FENCE THAT MAKES IT SAFE

A god object is the hard case for two agents, because the naive split has both of you editing
`app.py` and losing work to conflicts nobody chose. So the seam is not "half the methods each" —
it is the two *sides* of the problem, and only one seat writes the file:

| | **OPENBOLT — THE EXTRACTION** | **SILVERBOLT — THE SEAL** |
|---|---|---|
| owns | `src/litetui/app.py` **(exclusively)** + the new modules you carve out | `src/litetui/plugins/**` + a new plugin API module |
| goal | move cohesive groups OUT of the class | take the **102** private reach-throughs to zero, or to a small defended set |
| never touches | `plugins/**` | **`app.py` — not one line** |

**Only OpenBolt writes `app.py`.** SilverBolt defines the API surface plugins need; OpenBolt
implements the delegation inside `app.py`. That contract between you is the whole coordination
cost, and it is one message, not a merge conflict.

⭐ **Each half's analysis is exactly the input the other needs.** SilverBolt's enumeration of what
plugins actually reach for TELLS OpenBolt which state cannot move yet. OpenBolt's grouping of the
193 definitions TELLS SilverBolt which API boundaries are real. Do phase 1 in parallel, then
exchange, then write **one** `PLAN.md`.

---

## 1. THE SUBJECT, MEASURED — not quoted from a review

```
src/litetui/app.py          5,218 lines
all of src/                17,832 lines across 55 files
                        => app.py is 29.3% of the entire source, in ONE file
next largest file           llm_backend.py at 1,351  (app.py is 3.9x it)
definitions at class indent 193   (grep -c "^    def \|^    async def ")
plugin -> private reach     102   (private access from src/litetui/plugins/)
```

The suite is your safety net: on **`main @ 112e146` the number is 1,120**, `uv run --locked`.
It must be **1,120 / 0** at every commit you make. Not "green" — *that number*.

⚠️ **CORRECTED 2026-08-24. This brief first said 1,134 and that was MY error**: 1,134 is the count
on the **unmerged T065 branch**, which adds 14 runtime-log tests. Verified by collection on both
trees — `main` 1,120, `cf70dff` 1,134. **If T065 merges before you finish, your target becomes
1,134** — check what your base actually collects rather than trusting either number. Caught by
OpenBolt reporting the discrepancy instead of assuming he had broken something.

---

## 2. 🔴 THE PRESCRIBED FIX ALREADY RAN AND THE METRIC WENT THE WRONG WAY

This is the single most important thing in this brief. The 2026-08-23 review prescribed two
extractions. **Both shipped and merged: `TurnEngine` and `ConversationRepository`.** Result:

| | before | after |
|---|---|---|
| app.py lines | 5,271 | 5,204 (−1%), and **5,218 today** |
| methods | 125 | **137 — MORE** |
| plugin private reach-through | 35 | **35 — IDENTICAL** |

**Two successful extractions and the class grew back.** So "extract things until it feels
smaller" is a strategy that has ALREADY BEEN TRIED HERE AND FAILED. Do not re-run it faster.

⇒ **Close on the OUTCOME metric, never on the action.** "I extracted three modules" is not
progress. Progress is app.py's line count, its method count, and the reach-through count all
moving down together and *staying* down.

---

## 3. WHAT "FULL" MEANS, AND THE PART EXTRACTION ALONE CANNOT FIX

The 102 private reach-throughs are the real coupling. If you extract modules but plugins still
reach into `app._something`, you have moved code without moving the dependency — and the next
person re-grows app.py to satisfy them, exactly as happened above.

**So the work is two-sided:**

1. **Decompose** app.py into cohesive modules with real boundaries.
2. **Seal the surface** — give plugins a supported API so the 102 private accesses go to zero
   or to a small, deliberate, documented set. A number you can defend, not a number that
   happens.

If you finish (1) and not (2), you have repeated the failure above with more files.

---

## 4. PHASE 1 IS ANALYSIS AND A PLAN. DO NOT EDIT `app.py` YET.

**FINISH YOUR CURRENT TASK FIRST.** OpenBolt is on T068 (Windows CI), SilverBolt on T069 (ADR
extraction). Neither of you starts T070 until yours is committed and reported.

**And a hard sequencing constraint on top of that:** T069 moves 4 of the 23 dated comments that
live in `app.py`. Those commit FIRST and I will name the sha. **Until then `app.py` is nobody's
to restructure** — a decomp landing on top of that comment work destroys it silently.

Phase 1 is genuinely necessary regardless, and it is what a full decomposition deserves:

- Map the 193 definitions into candidate cohesive groups. Name each group and say what state
  it owns. Groups that share mutable state are not separable — say so.
- Enumerate the 102 reach-throughs: which private members, from which plugins, for what.
  Cluster them into the API they are actually asking for.
- Identify what CANNOT move and why (Textual `App` lifecycle, message pump, anything the
  framework requires on the class).
- Propose an ORDER, smallest-risk first, with the expected effect of each step on the three
  numbers.

**Divide phase 1 the same way as the build:** OpenBolt maps the 193 definitions into cohesive
groups and names what state each owns; SilverBolt enumerates all 102 reach-throughs — which
private member, from which plugin, for what — and clusters them into the API they are really
asking for. **Then exchange, reconcile, and write ONE `PLAN.md`** in this worktree.

**Send it to me before either of you writes code.** I will review the order and the boundaries
and then release you to implement in parallel behind the file fence.

---

## 5. RULES

- Branch `refactor/app-decomposition`, this worktree. Base: **`main` is now `ab8b4a9`**
  (T068 merged CI on top of `112e146`).
- **Commit in small steps**, each with the suite green at the number YOUR BASE COLLECTS
  (see §1 — 1,120 on main today). A single giant commit is
  unreviewable and unbisectable.
- `prompts/systemprompt.md` is **FENCED** — Ryan's dirty file, do not touch it.
- Do NOT run repo-level `bun fmt` (churns 18 unrelated files) — this repo is Python; keep your
  diff to files you actually changed.
- `apps/desktop identity-env-name-table` fails in EVERY worktree (`SCAN_ROOTS` resolves
  `../liteharness-oss/liteharness` relative to the repo root). Pre-existing, environmental,
  **not yours** — do not debug it, just note it.
- Commit trailers required: `Task-id: T070`, `Agent-Tier: worker`, `Agent-Name: <you>`,
  `Agent-ID: <your session id>`, `Complexity: complex`. **Never `Co-Authored-By`.**
- **Do not push.** Every push is Ryan's trigger.

## 6. HOW I WANT TO BE TOLD THINGS

Report through the inbox to `ba736bd4-d249-42c0-b1ed-04b597d753f0`. Two standing expectations
that this fleet learned the hard way tonight:

- **Name the signal, never the subsystem.** "`test_x` fails at app.py:2104" survives a relay;
  "the tests are flaky" does not.
- **A gate that has never failed has not been shown to work.** If you add a check, make it go
  red on purpose once and say that you did.

If something in this brief contradicts the code, **the code wins and I want to hear about it** —
two of my briefs tonight contained a factual error and both were caught by workers reading the
source instead of trusting me.
